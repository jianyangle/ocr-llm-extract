from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Callable, Literal

from src.core.engine_events import (
    EngineEvent,
    TaskAutoDispatchTriggered,
    TaskFailed,
    TaskOcrCompleted,
    TaskOcrProgressed,
    TaskPageResultStreamed,
    TaskProgressed,
    TaskStarted,
    TaskSucceeded,
)
from src.core.extract_stage import ExtractStage
from src.core.ocr_stage import (
    DocOcrCompleted,
    ImageRecognized,
    OCRFailure,
    OCRPassthrough,
    PageCommitted,
    PdfDocStarted,
)
from src.core.admission_policy import decide_admission
from src.core.pdf_ocr_aggregator import PDFOCRAggregator, PDFRetryBudgetSettings
from src.domain.schemas import (
    AppConfig,
    ExtractRow,
    ExtractionOptions,
    ItemResult,
    PDFLimits,
    PDFPageExtractResult,
    PDFPageOCRSnapshot,
    TaskItem,
)
from src.extract import Extractor
from src.extract.template_catalog import TemplateCatalog
from src.ocr.errors import PDFAdapterError
from src.ocr.models import OCRTextBlock
from src.ocr.paddle_service import PaddleOCRService


class TaskPipelineError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class EventPublisher:
    """Forward typed EngineEvents to the sink while isolating queue behavior."""

    def __init__(self, event_sink: Callable[[EngineEvent], None] | None = None) -> None:
        self._event_sink = event_sink

    def publish(self, event: EngineEvent) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(event)
        except Exception:
            return


class ResultStore:
    @staticmethod
    def upsert_task_rows(existing_rows: list[ExtractRow], task_id: str, rows: list[ExtractRow]) -> list[ExtractRow]:
        replaced_rows: list[ExtractRow] = []
        inserted = False
        for existing in existing_rows:
            if existing.task_id == task_id:
                if not inserted:
                    replaced_rows.extend(rows)
                    inserted = True
                continue
            replaced_rows.append(existing)
        if not inserted:
            replaced_rows.extend(rows)
        return replaced_rows

    @staticmethod
    def upsert_item_result(existing_results: list[ItemResult], item_result: ItemResult) -> list[ItemResult]:
        replaced_items: list[ItemResult] = []
        inserted = False
        for existing in existing_results:
            if existing.task_id == item_result.task_id:
                if not inserted:
                    replaced_items.append(item_result)
                    inserted = True
                continue
            replaced_items.append(existing)
        if not inserted:
            replaced_items.append(item_result)
        return replaced_items

    @staticmethod
    def build_success_rows(
        *,
        task_id: str,
        normalized_rows: list[list[str]],
        grounded_rows: Any,
        ocr_confidence: float | None,
        typed_rows: list[list[object]] | None = None,
        page_index: int | None = None,
    ) -> list[ExtractRow]:
        item_rows: list[ExtractRow] = []
        for row_index, row_values in enumerate(normalized_rows):
            grounded_cells = []
            extraction_classification = "UNCERTAIN"
            if isinstance(grounded_rows, list) and row_index < len(grounded_rows):
                grounded_row = grounded_rows[row_index]
                cells = getattr(grounded_row, "cells", None)
                if isinstance(cells, list):
                    grounded_cells = cells
                classification = getattr(grounded_row, "classification", None)
                if classification in {"ASSIGNED", "ASSIGNED_PARTIAL", "INFERRED", "INFERRED_FUZZY", "UNCERTAIN"}:
                    extraction_classification = classification

            item_rows.append(
                ExtractRow(
                    task_id=task_id,
                    values=row_values,
                    typed_values=typed_rows[row_index] if typed_rows is not None and row_index < len(typed_rows) else None,
                    action="write",
                    page_index=page_index,
                    ocr_confidence=ocr_confidence,
                    grounded_cells=grounded_cells,
                    extraction_classification=extraction_classification,
                )
            )
        return item_rows

    @staticmethod
    def build_failed_rows(*, task_id: str, expected_columns: int) -> list[ExtractRow]:
        return [
            ExtractRow(
                task_id=task_id,
                values=["error"] * max(0, expected_columns),
                action="skip",
                ocr_confidence=None,
                is_error_row=True,
            )
        ]

    @staticmethod
    def serialize_row(row: ExtractRow) -> dict[str, Any]:
        return {
            "row_id": row.row_id,
            "task_id": row.task_id,
            "values": list(row.values),
            "action": row.action,
            "page_index": row.page_index,
            "ocr_confidence": row.ocr_confidence,
            "is_error_row": row.is_error_row,
        }

    @staticmethod
    def serialize_pdf_page_snapshot(snapshot: PDFPageOCRSnapshot) -> dict[str, Any]:
        return {
            "page_index": snapshot.page_index,
            "normalized_text": snapshot.normalized_text,
            "image_path": snapshot.image_path,
            "ocr_confidence": snapshot.ocr_confidence,
            "confidence_min": snapshot.confidence_min,
            "block_count": snapshot.block_count,
            "adaptive_retry_triggered": snapshot.adaptive_retry_triggered,
            "adaptive_retry_applied": snapshot.adaptive_retry_applied,
            "retry_profile_from": snapshot.retry_profile_from,
            "retry_profile_to": snapshot.retry_profile_to,
            "first_pass_confidence_min": snapshot.first_pass_confidence_min,
            "second_pass_confidence_min": snapshot.second_pass_confidence_min,
            "source_path": snapshot.source_path,
        }

    @staticmethod
    def serialize_pdf_page_result(page_result: PDFPageExtractResult) -> dict[str, Any]:
        return {
            "page_index": page_result.page_index,
            "normalized_text": page_result.normalized_text,
            "status": page_result.status,
            "row_count": page_result.row_count,
            "ocr_confidence": page_result.ocr_confidence,
            "error_code": page_result.error_code,
            "error_message": page_result.error_message,
        }


class SourceProcessor:
    def __init__(
        self,
        *,
        config_getter: Callable[[], AppConfig],
        ocr_service_getter: Callable[[], Any],
        pdf_adapter_getter: Callable[[], Any],
        pdf_ocr_aggregator: PDFOCRAggregator,
    ) -> None:
        self._config_getter = config_getter
        self._ocr_service_getter = ocr_service_getter
        self._pdf_adapter_getter = pdf_adapter_getter
        self._pdf_ocr_aggregator = pdf_ocr_aggregator

    @property
    def ocr_service(self) -> Any:
        return self._ocr_service_getter()

    @property
    def pdf_adapter(self) -> Any:
        return self._pdf_adapter_getter()

    def process(self, task: TaskItem) -> tuple[str, float | None, TaskOcrCompleted | None]:
        if task.source_type == "image":
            task.status = "running_ocr"
            text, confidence, event, _blocks = self._recognize_image(task)
            return text, confidence, event
        if task.source_type == "pdf":
            task.status = "running_ocr"
            page_snapshots, event = self.recognize_pdf_pages(task)
            _ = page_snapshots
            return event.normalized_text, event.ocr_confidence, event
        return task.source_value, None, None

    def _recognize_image(self, task: TaskItem) -> tuple[str, float | None, TaskOcrCompleted, list[OCRTextBlock]]:
        ocr_result = self.ocr_service.recognize(task.source_value)
        event = TaskOcrCompleted(
            task_id=task.task_id,
            normalized_text=ocr_result.text,
            ocr_confidence=ocr_result.confidence_avg,
            page_snapshots=[],
            active_template_name=None,
            region_rescue=[],
            block_count=ocr_result.block_count,
            confidence_min=getattr(ocr_result, "confidence_min", None),
            pdf_page_count=None,
            adaptive_retry_triggered=getattr(ocr_result, "retry_triggered", False),
            adaptive_retry_applied=getattr(ocr_result, "retry_applied", False),
            retry_profile_from=getattr(ocr_result, "retry_profile_from", None),
            retry_profile_to=getattr(ocr_result, "retry_profile_to", None),
            first_pass_confidence_min=getattr(ocr_result, "first_pass_confidence_min", None),
            second_pass_confidence_min=getattr(ocr_result, "second_pass_confidence_min", None),
            markdown=getattr(ocr_result, "markdown", None),
        )
        return ocr_result.text, ocr_result.confidence_avg, event, list(ocr_result.blocks)

    def recognize_pdf_pages(self, task: TaskItem) -> tuple[list[PDFPageOCRSnapshot], TaskOcrCompleted]:
        config = self._config_getter()
        task.pdf_processed_pages = 0
        task.pdf_page_ocr_snapshots = []
        run = self._pdf_ocr_aggregator.begin(
            pdf_path=task.source_value,
            ocr_options=PaddleOCRService.runtime_options_from_app_config(config),
            extract_options=self._build_extraction_options(config),
            limits=self._build_pdf_limits(config),
            retry_budget=self._build_retry_budget_settings(config),
            pdf_adapter=self.pdf_adapter,
            ocr_service=self.ocr_service,
        )
        task.pdf_total_pages = run.expected_page_count
        for page in run.iter_pages():
            task.pdf_processed_pages = page.page_index
            task.progress = min(90, 5 + int((task.pdf_processed_pages / max(task.pdf_processed_pages, 1)) * 50))
            if page.snapshot is not None:
                task.pdf_page_ocr_snapshots = list(run.page_snapshots)
                task.ocr_text = page.aggregated_text_after_page
        if not run.page_snapshots:
            page_errors = run.page_errors
            if page_errors:
                first_error = page_errors[0]
                raise PDFAdapterError(first_error.code, first_error.message)
            raise PDFAdapterError("E_PDF_002", "PDF rendering produced no pages")
        event = run.build_base_ocr_event(task_id=task.task_id)
        task.pdf_page_ocr_snapshots = list(run.page_snapshots)
        task.ocr_text = event.normalized_text
        return list(run.page_snapshots), event

    @staticmethod
    def _build_pdf_limits(config: AppConfig) -> PDFLimits:
        return PDFLimits(
            max_pages=max(int(getattr(config, "pdf_max_pages", 30)), 1),
            max_file_size=max(int(getattr(config, "pdf_max_file_size", 20 * 1024 * 1024)), 1),
            render_dpi=max(int(getattr(config, "pdf_render_dpi", 200)), 72),
            page_render_parallelism=max(int(getattr(config, "pdf_page_render_parallelism", 2)), 1),
            prefer_text_layer=bool(getattr(config, "pdf_prefer_text_layer", True)),
            text_layer_min_chars=max(int(getattr(config, "pdf_text_layer_min_chars", 40)), 1),
            text_layer_completeness_ocr=bool(getattr(config, "pdf_text_layer_completeness_ocr", True)),
        )

    @staticmethod
    def _build_extraction_options(config: AppConfig) -> ExtractionOptions:
        return ExtractionOptions(
            prompts=config.prompts,
            examples_normalized=list(config.examples_normalized),
            provider=config.provider,
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            templates=list(config.templates),
            extraction_passes=int(getattr(config, "extraction_passes", 2)),
            extraction_max_char_buffer=int(getattr(config, "extraction_max_char_buffer", 2200)),
            extraction_passes_increment=int(getattr(config, "extraction_passes_increment", 800)),
            extraction_parse_mode=getattr(config, "extraction_parse_mode", "balanced"),
            use_structured_output=bool(getattr(config, "use_structured_output", True)),
            llm_seed=getattr(config, "llm_seed", None),
            llm_prompt_cache_enabled=bool(getattr(config, "llm_prompt_cache_enabled", False)),
            ollama_num_ctx=int(getattr(config, "ollama_num_ctx", 8192)),
            grounding_fuzzy_threshold=float(getattr(config, "grounding_fuzzy_threshold", 0.75)),
            grounding_mode=getattr(config, "grounding_mode", "off"),
        )

    @staticmethod
    def _build_retry_budget_settings(config: AppConfig) -> PDFRetryBudgetSettings:
        return PDFRetryBudgetSettings(
            budget=max(int(getattr(config, "pdf_retry_budget", 8)), 1),
            unimproved_stop=max(int(getattr(config, "pdf_retry_unimproved_stop", 2)), 1),
        )


_STAGE_BUFFER_MAXSIZE = 2
_QUEUE_PUT_TIMEOUT_SECONDS = 0.2
_FEEDER_POLL_SECONDS = 0.01


@dataclass(frozen=True)
class _StopSignal:
    pass


@dataclass(frozen=True)
class _PipelineTaskFailure:
    task: TaskItem
    code: str
    message: str
    stage: str


@dataclass(frozen=True)
class _PipelineWorkItem:
    task_id: str
    source_type: Literal["text", "image", "pdf"]
    sequence_index: int
    page_index: int | None
    source_value: str
    source_path: str | None
    is_last_ocr_item: bool
    is_last_extract_item: bool
    task: TaskItem
    snapshot: PDFPageOCRSnapshot | None = None
    normalized_text: str | None = None
    ocr_confidence: float | None = None
    markdown: str | None = None
    crop: Callable[[Any], Any] | None = None
    error: Any | None = None
    ocr_event: TaskOcrCompleted | None = None
    blocks: list[OCRTextBlock] = field(default_factory=list)


@dataclass
class ExtractCompletionState:
    task: TaskItem
    expected_items: int | None
    started_perf: float
    expected_columns: int
    ocr_completed_items: int = 0
    extract_completed_items: int = 0
    page_snapshots: list[PDFPageOCRSnapshot] = field(default_factory=list)
    page_results: list[PDFPageExtractResult] = field(default_factory=list)
    success_rows: list[ExtractRow] = field(default_factory=list)
    normalized_text: str = ""
    ocr_confidence: float | None = None


_TaskPipelineState = ExtractCompletionState


@dataclass(frozen=True)
class ExtractCompletionReduction:
    result_rows: list[ExtractRow]
    item_results: list[ItemResult]
    events: list[EngineEvent]


class ExtractCompletionReducer:
    def __init__(self, *, result_store: ResultStore) -> None:
        self._result_store = result_store

    def reduce_single_item_completion(
        self,
        *,
        state: ExtractCompletionState,
        stored_rows: list[ExtractRow],
        item_results: list[ItemResult],
        normalized_text: str,
        success_rows: list[ExtractRow],
        error_code: str | None,
        error_message: str | None,
        stage: str,
        ocr_event: TaskOcrCompleted | None,
        rescue_events: list[dict[str, Any]],
    ) -> ExtractCompletionReduction:
        state.extract_completed_items += 1
        state.normalized_text = normalized_text

        events: list[EngineEvent] = []
        next_rows = stored_rows
        next_items = item_results
        if error_code is not None:
            next_rows, next_items, terminal_event = self._finish_task_failure(
                state=state,
                stored_rows=stored_rows,
                item_results=item_results,
                code=error_code,
                message=error_message or "Extract failed",
                stage=stage,
                normalized_text=normalized_text,
                page_results=None,
            )
        else:
            state.success_rows.extend(success_rows)
            next_rows, next_items, terminal_event = self._finish_success_if_ready(
                state=state,
                stored_rows=stored_rows,
                item_results=item_results,
            )

        if state.task.source_type == "image" and error_code is None and rescue_events and ocr_event is not None:
            events.append(replace(ocr_event, region_rescue=list(rescue_events)))
        if terminal_event is not None:
            events.append(terminal_event)
        return ExtractCompletionReduction(
            result_rows=next_rows,
            item_results=next_items,
            events=events,
        )

    def reduce_pdf_page_completion(
        self,
        *,
        state: ExtractCompletionState,
        stored_rows: list[ExtractRow],
        item_results: list[ItemResult],
        page_index: int,
        page_text: str,
        page_confidence: float | None,
        success_rows: list[ExtractRow],
        error_code: str | None,
        error_message: str | None,
        rescue_events: list[dict[str, Any]],
    ) -> ExtractCompletionReduction:
        if error_code is not None:
            page_result = PDFPageExtractResult(
                page_index=page_index,
                normalized_text=page_text,
                status="failed",
                row_count=0,
                ocr_confidence=page_confidence,
                error_code=error_code,
                error_message=error_message,
            )
        elif success_rows:
            page_result = PDFPageExtractResult(
                page_index=page_index,
                normalized_text=page_text,
                status="success",
                row_count=len(success_rows),
                ocr_confidence=page_confidence,
            )
            state.success_rows.extend(success_rows)
        else:
            page_result = PDFPageExtractResult(
                page_index=page_index,
                normalized_text=page_text,
                status="empty",
                row_count=0,
                ocr_confidence=page_confidence,
            )

        state.page_results.append(page_result)
        state.page_results.sort(key=lambda result: result.page_index)
        state.extract_completed_items += 1
        state.task.pdf_page_results = list(state.page_results)
        return ExtractCompletionReduction(
            result_rows=stored_rows,
            item_results=item_results,
            events=[
                TaskPageResultStreamed(
                    task_id=state.task.task_id,
                    page_index=page_result.page_index,
                    page_text=page_text,
                    aggregated_text=state.normalized_text,
                    status=page_result.status,
                    row_count=len(success_rows),
                    error_code=page_result.error_code,
                    error_message=page_result.error_message,
                    rows=success_rows,
                    page_result=page_result,
                    region_rescue=list(rescue_events),
                )
            ],
        )

    def reduce_pdf_task_completion_if_ready(
        self,
        *,
        state: ExtractCompletionState,
        stored_rows: list[ExtractRow],
        item_results: list[ItemResult],
        stage: str,
    ) -> ExtractCompletionReduction | None:
        next_rows, next_items, terminal_event = self._finish_success_if_ready(
            state=state,
            stored_rows=stored_rows,
            item_results=item_results,
        )
        if terminal_event is not None:
            return ExtractCompletionReduction(
                result_rows=next_rows,
                item_results=next_items,
                events=[terminal_event],
            )
        if state.expected_items is None or state.extract_completed_items < state.expected_items:
            return None

        page_results = list(state.page_results)
        if page_results and all(result.status == "failed" for result in page_results):
            first_failure = page_results[0]
            code = first_failure.error_code or "E_QUEUE_001"
            message = first_failure.error_message or self._build_empty_pdf_result_message(page_results)
        else:
            code = "E_PDF_005"
            message = self._build_empty_pdf_result_message(page_results)
        next_rows, next_items, failure_event = self._finish_task_failure(
            state=state,
            stored_rows=stored_rows,
            item_results=item_results,
            code=code,
            message=message,
            stage=stage,
            normalized_text=state.normalized_text,
            page_results=page_results,
        )
        return ExtractCompletionReduction(
            result_rows=next_rows,
            item_results=next_items,
            events=[failure_event],
        )

    def reduce_task_failure(
        self,
        *,
        state: ExtractCompletionState,
        stored_rows: list[ExtractRow],
        item_results: list[ItemResult],
        code: str,
        message: str,
        stage: str,
        normalized_text: str,
        page_results: list[PDFPageExtractResult] | None = None,
    ) -> ExtractCompletionReduction:
        next_rows, next_items, failure_event = self._finish_task_failure(
            state=state,
            stored_rows=stored_rows,
            item_results=item_results,
            code=code,
            message=message,
            stage=stage,
            normalized_text=normalized_text,
            page_results=page_results,
        )
        return ExtractCompletionReduction(
            result_rows=next_rows,
            item_results=next_items,
            events=[failure_event],
        )

    def _finish_task_failure(
        self,
        *,
        state: ExtractCompletionState,
        stored_rows: list[ExtractRow],
        item_results: list[ItemResult],
        code: str,
        message: str,
        stage: str,
        normalized_text: str,
        page_results: list[PDFPageExtractResult] | None,
    ) -> tuple[list[ExtractRow], list[ItemResult], TaskFailed]:
        task = state.task
        task.status = "failed"
        task.error_code = code
        task.error_message = message
        task.progress = 100
        task.finished_at = datetime.now(UTC)
        failed_rows = self._result_store.build_failed_rows(task_id=task.task_id, expected_columns=state.expected_columns)
        next_rows = self._result_store.upsert_task_rows(stored_rows, task.task_id, failed_rows)
        next_items = self._result_store.upsert_item_result(
            item_results,
            ItemResult(
                task_id=task.task_id,
                rows=failed_rows,
                normalized_text=normalized_text,
                error_code=code,
                error_message=message,
                page_results=list(page_results or []),
            ),
        )
        return (
            next_rows,
            next_items,
            TaskFailed(
                task_id=task.task_id,
                status=task.status,
                error_code=task.error_code,
                error_message=task.error_message,
                stage=stage,
                row_count=len(failed_rows),
                normalized_text=normalized_text,
                rows=failed_rows,
                page_results=list(page_results or []) if page_results else None,
            ),
        )

    def _finish_success_if_ready(
        self,
        *,
        state: ExtractCompletionState,
        stored_rows: list[ExtractRow],
        item_results: list[ItemResult],
    ) -> tuple[list[ExtractRow], list[ItemResult], TaskSucceeded | TaskFailed | None]:
        if state.expected_items is None or state.extract_completed_items < state.expected_items:
            return stored_rows, item_results, None

        task = state.task
        page_results = list(state.page_results)
        normalized_text = state.normalized_text
        if state.success_rows or task.source_type != "pdf":
            task.status = "done"
            task.error_code = None
            task.error_message = None
            task.progress = 100
            task.finished_at = datetime.now(UTC)
            success_rows = list(state.success_rows)
            next_rows = self._result_store.upsert_task_rows(stored_rows, task.task_id, success_rows)
            next_items = self._result_store.upsert_item_result(
                item_results,
                ItemResult(
                    task_id=task.task_id,
                    rows=success_rows,
                    normalized_text=normalized_text,
                    page_results=page_results,
                ),
            )
            return (
                next_rows,
                next_items,
                TaskSucceeded(
                    task_id=task.task_id,
                    status=task.status,
                    row_count=len(success_rows),
                    latency_ms=int((perf_counter() - state.started_perf) * 1000),
                    normalized_text=normalized_text,
                    rows=success_rows,
                    page_results=page_results if task.source_type == "pdf" else None,
                ),
            )
        return stored_rows, item_results, None

    @staticmethod
    def _build_empty_pdf_result_message(page_results: list[PDFPageExtractResult]) -> str:
        empty_pages = sum(1 for result in page_results if result.status == "empty")
        failed_pages = [result for result in page_results if result.status == "failed"]
        summary = f"No valid rows extracted from any PDF page (empty_pages={empty_pages}, failed_pages={len(failed_pages)})."
        if not failed_pages:
            return summary
        first_failure = failed_pages[0]
        failure_code = first_failure.error_code or "E_QUEUE_001"
        failure_message = first_failure.error_message or "unknown error"
        return f"{summary} First failure: page {first_failure.page_index} {failure_code}: {failure_message}"


class TaskOrchestrator:
    def __init__(
        self,
        *,
        config_getter: Callable[[], AppConfig],
        extractor_getter: Callable[[], Extractor],
        source_processor: SourceProcessor,
        pdf_ocr_aggregator: PDFOCRAggregator,
        ocr_stage: Any,
        result_store: ResultStore,
        event_publisher: EventPublisher,
        online_pdf_processor: Any | None = None,
    ) -> None:
        self._config_getter = config_getter
        self._extractor_getter = extractor_getter
        self._source_processor = source_processor
        self._pdf_ocr_aggregator = pdf_ocr_aggregator
        self._ocr_stage = ocr_stage
        self._online_pdf_processor = online_pdf_processor
        self._result_store = result_store
        self._extract_stage = ExtractStage(source_processor=source_processor, result_store=result_store)
        self._event_publisher = event_publisher
        self._completion_reducer = ExtractCompletionReducer(result_store=result_store)
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        """请求中止运行中的 run()（UI 关闭/取消时调用）。

        设置 stop_event 后，各阶段循环 ``while not stop_event.is_set()`` 会退出，
        在线 PDF 轮询的 ``cancel_check=stop_event.is_set`` 也会中止，run() 随之返回。
        """
        self._stop_event.set()

    def run(
        self,
        *,
        tasks: list[TaskItem],
        stored_rows: list[ExtractRow],
        item_results: list[ItemResult],
    ) -> tuple[list[ExtractRow], list[ItemResult]]:
        next_rows = stored_rows
        next_items = item_results
        config = self._config_getter()
        extractor = self._extractor_getter()
        ocr_input_queue: queue.Queue[_PipelineWorkItem | _StopSignal] = queue.Queue()
        stage_buffer: queue.Queue[_PipelineWorkItem | _PipelineTaskFailure | _StopSignal] = queue.Queue(
            maxsize=_STAGE_BUFFER_MAXSIZE
        )
        stop_signal = _StopSignal()
        # 复用实例级 stop_event，使外部 request_stop()（如 UI 关闭）能中止运行中的 run()——
        # 在线轮询的 cancel_check=stop_event.is_set 会随之中止。每次 run 起始清零。
        self._stop_event.clear()
        stop_event = self._stop_event
        state_lock = threading.Lock()
        worker_errors_lock = threading.Lock()
        worker_errors: list[BaseException] = []
        states: dict[str, _TaskPipelineState] = {}

        def _record_worker_error(exc: BaseException) -> None:
            with worker_errors_lock:
                worker_errors.append(exc)
            stop_event.set()

        def _first_worker_error() -> BaseException | None:
            with worker_errors_lock:
                return worker_errors[0] if worker_errors else None

        def _put_stage_buffer(payload: _PipelineWorkItem | _PipelineTaskFailure | _StopSignal) -> None:
            while not stop_event.is_set():
                try:
                    stage_buffer.put(payload, timeout=_QUEUE_PUT_TIMEOUT_SECONDS)
                    return
                except queue.Full:
                    error = _first_worker_error()
                    if error is not None:
                        raise error
            error = _first_worker_error()
            if error is not None:
                raise error
            raise TaskPipelineError("E_QUEUE_002", "Pipeline stopped before drain")

        def _signal_extract_stage_to_stop() -> None:
            while extract_thread.is_alive():
                try:
                    stage_buffer.put(stop_signal, timeout=_QUEUE_PUT_TIMEOUT_SECONDS)
                    return
                except queue.Full:
                    continue

        active_template_name = self._resolve_active_template_name(config)

        def _start_pending_task(task: TaskItem) -> None:
            expected_columns = len(config.examples_normalized[0]) if config.examples_normalized else 0
            if task.auto_dispatch_while_running:
                self._event_publisher.publish(
                    TaskAutoDispatchTriggered(task_id=task.task_id, source_type=task.source_type)
                )
                task.auto_dispatch_while_running = False
            task.started_at = datetime.now(UTC)
            task.progress = 1
            task.error_code = None
            task.error_message = None
            task.pdf_processed_pages = 0
            task.pdf_page_ocr_snapshots = []
            task.pdf_page_results = []
            if task.source_type == "text":
                task.status = "running_extract"
                expected_items: int | None = 1
            else:
                task.status = "running_ocr"
                expected_items = 1 if task.source_type == "image" else None
            states[task.task_id] = _TaskPipelineState(
                task=task,
                expected_items=expected_items,
                started_perf=perf_counter(),
                expected_columns=expected_columns,
            )
            self._event_publisher.publish(TaskStarted(task_id=task.task_id, source_type=task.source_type))
            ocr_input_queue.put(
                _PipelineWorkItem(
                    task_id=task.task_id,
                    source_type=task.source_type,
                    sequence_index=0,
                    page_index=None,
                    source_value=task.source_value,
                    source_path=task.source_path,
                    is_last_ocr_item=True,
                    is_last_extract_item=True,
                    task=task,
                    normalized_text=task.source_value if task.source_type == "text" else None,
                    ocr_confidence=None,
                )
            )

        def _feed_pending_tasks() -> None:
            seen: set[str] = set()
            while not stop_event.is_set():
                with state_lock:
                    decision = decide_admission(tasks, seen)
                    if decision.kind == "admit":
                        assert decision.task is not None
                        _start_pending_task(decision.task)
                        seen.add(decision.task.task_id)
                # 循环控制在锁外；上面的 state_lock 只覆盖准入突变本身。
                if decision.kind == "admit":
                    continue
                if decision.kind == "done":
                    break
                time.sleep(_FEEDER_POLL_SECONDS)
            ocr_input_queue.put(stop_signal)

        def _run_ocr_stage() -> None:
            try:
                while True:
                    item = ocr_input_queue.get()
                    if isinstance(item, _StopSignal):
                        _put_stage_buffer(stop_signal)
                        return
                    page_items: list[_PipelineWorkItem] = []
                    held_item: _PipelineWorkItem | None = None

                    def _progress_sink(
                        extracted: int, total: int, _item: _PipelineWorkItem = item
                    ) -> None:
                        # 在线 PDF 整份 job 在 done 前无结果；用进度事件驱动 UI，避免长任务被误判为卡死。
                        with state_lock:
                            progress_state = states.get(_item.task.task_id)
                            if progress_state is not None:
                                if total > 0:
                                    progress_state.expected_items = total
                                    _item.task.pdf_total_pages = total
                                _item.task.pdf_processed_pages = extracted
                                _item.task.progress = min(95, 5 + extracted)
                        self._event_publisher.publish(
                            TaskOcrProgressed(
                                task_id=_item.task.task_id,
                                processed_pages=extracted,
                                total_pages=total,
                            )
                        )

                    for output in self._ocr_stage.recognize_one(
                        item=item,
                        config=config,
                        progress_sink=_progress_sink,
                        cancel_check=stop_event.is_set,
                    ):
                        if isinstance(output, OCRPassthrough):
                            _put_stage_buffer(item)
                            continue

                        if isinstance(output, ImageRecognized):
                            with state_lock:
                                state = states[item.task_id]
                                state.normalized_text = output.ocr_text
                                state.ocr_confidence = output.ocr_confidence
                                state.ocr_completed_items = 1
                                item.task.ocr_text = output.ocr_text
                                item.task.status = "running_extract"
                            published_ocr_event = replace(
                                output.ocr_event,
                                active_template_name=active_template_name,
                                region_rescue=[],
                            )
                            self._event_publisher.publish(published_ocr_event)
                            _put_stage_buffer(
                                replace(
                                    item,
                                    normalized_text=output.ocr_text,
                                    ocr_confidence=output.ocr_confidence,
                                    markdown=published_ocr_event.markdown,
                                    ocr_event=published_ocr_event,
                                    blocks=output.blocks,
                                )
                            )
                            continue

                        if isinstance(output, PdfDocStarted):
                            with state_lock:
                                state = states[item.task_id]
                                state.expected_items = output.expected_page_count
                                item.task.pdf_total_pages = output.expected_page_count
                            continue

                        if isinstance(output, PageCommitted):
                            aggregate = output.aggregate
                            snapshot = aggregate.snapshot
                            if snapshot is not None:
                                with state_lock:
                                    state = states[item.task_id]
                                    state.page_snapshots = list(output.page_snapshots_so_far)
                                    state.ocr_completed_items += 1
                                    state.normalized_text = aggregate.aggregated_text_after_page
                                    item.task.pdf_processed_pages = aggregate.page_index
                                    item.task.pdf_page_ocr_snapshots = list(state.page_snapshots)
                                    item.task.ocr_text = state.normalized_text
                                    item.task.progress = min(95, 5 + state.ocr_completed_items)
                            page_item = replace(
                                item,
                                sequence_index=len(page_items),
                                page_index=aggregate.zero_based_page_index,
                                snapshot=snapshot,
                                normalized_text=aggregate.page_text,
                                ocr_confidence=aggregate.ocr_confidence,
                                crop=aggregate.crop,
                                error=aggregate.error,
                                is_last_ocr_item=False,
                                is_last_extract_item=False,
                            )
                            page_items.append(page_item)
                            if output.is_last:
                                held_item = page_item
                            else:
                                _put_stage_buffer(page_item)
                            continue

                        if isinstance(output, DocOcrCompleted):
                            base_event = output.base_event
                            with state_lock:
                                state = states[item.task_id]
                                state.normalized_text = base_event.normalized_text
                                item.task.status = "running_extract"
                                item.task.ocr_text = base_event.normalized_text
                                item.task.pdf_page_ocr_snapshots = list(output.page_snapshots)
                            self._event_publisher.publish(
                                replace(
                                    base_event,
                                    active_template_name=active_template_name,
                                    region_rescue=[],
                                )
                            )
                            if held_item is not None:
                                _put_stage_buffer(
                                    replace(held_item, is_last_ocr_item=True, is_last_extract_item=True)
                                )
                            continue

                        if isinstance(output, OCRFailure):
                            _put_stage_buffer(
                                _PipelineTaskFailure(
                                    task=item.task,
                                    code=output.code,
                                    message=output.message,
                                    stage=output.stage,
                                )
                            )
            except BaseException as exc:
                _record_worker_error(exc)
                _signal_extract_stage_to_stop()

        def _run_extract_stage() -> None:
            nonlocal next_rows, next_items
            try:
                while True:
                    payload = stage_buffer.get()
                    if isinstance(payload, _StopSignal):
                        return
                    if isinstance(payload, _PipelineTaskFailure):
                        with state_lock:
                            state = states.get(payload.task.task_id)
                            normalized_text = state.normalized_text if state is not None else payload.task.ocr_text
                            failure_state = state or ExtractCompletionState(
                                task=payload.task,
                                expected_items=None,
                                started_perf=perf_counter(),
                                expected_columns=len(config.examples_normalized[0]) if config.examples_normalized else 0,
                            )
                            reduction = self._completion_reducer.reduce_task_failure(
                                state=failure_state,
                                stored_rows=next_rows,
                                item_results=next_items,
                                code=payload.code,
                                message=payload.message,
                                stage=payload.stage,
                                normalized_text=normalized_text or payload.task.source_value,
                                page_results=list(payload.task.pdf_page_results),
                            )
                            next_rows = reduction.result_rows
                            next_items = reduction.item_results
                            event = reduction.events[0]
                        self._event_publisher.publish(event)
                        continue

                    item = payload
                    task = item.task
                    if item.source_type == "image":
                        with state_lock:
                            state = states[item.task_id]
                            progress_event = TaskProgressed(
                                task_id=task.task_id,
                                status=task.status,
                                row_count=0,
                                normalized_text=state.normalized_text,
                                rows=[],
                                page_results=[],
                            )
                        self._event_publisher.publish(progress_event)

                    if item.error is not None:
                        snapshot = item.snapshot
                        terminal_event = None
                        with state_lock:
                            reduction = self._completion_reducer.reduce_pdf_page_completion(
                                state=states[item.task_id],
                                stored_rows=next_rows,
                                item_results=next_items,
                                page_index=(snapshot.page_index if snapshot is not None else (item.page_index or 0) + 1),
                                page_text=(snapshot.normalized_text if snapshot is not None else ""),
                                page_confidence=(snapshot.ocr_confidence if snapshot is not None else None),
                                success_rows=[],
                                error_code=item.error.code,
                                error_message=item.error.message,
                                rescue_events=[],
                            )
                            next_rows = reduction.result_rows
                            next_items = reduction.item_results
                            page_event = reduction.events[0]
                            terminal_reduction = self._completion_reducer.reduce_pdf_task_completion_if_ready(
                                state=states[item.task_id],
                                stored_rows=next_rows,
                                item_results=next_items,
                                stage="running_extract",
                            )
                            if terminal_reduction is not None:
                                next_rows = terminal_reduction.result_rows
                                next_items = terminal_reduction.item_results
                                terminal_event = terminal_reduction.events[0]
                        self._event_publisher.publish(page_event)
                        if terminal_event is not None:
                            self._event_publisher.publish(terminal_event)
                        continue

                    result = self._extract_stage.extract_one(
                        item=item,
                        config=config,
                        extractor=extractor,
                        expected_columns=states[item.task_id].expected_columns,
                    )
                    success_rows = result.success_rows
                    rescue_events = result.rescue_events
                    extraction_input = result.extraction_input
                    page_error_code = result.error_code
                    page_error_message = result.error_message
                    assert extraction_input is not None

                    terminal_event: TaskSucceeded | TaskFailed | None = None
                    page_event: TaskPageResultStreamed | None = None
                    single_item_events: list[EngineEvent] = []
                    with state_lock:
                        state = states[item.task_id]
                        if item.source_type == "pdf":
                            snapshot = item.snapshot
                            if snapshot is None:
                                page_index = (item.page_index or 0) + 1
                                page_text = ""
                                page_confidence = None
                            else:
                                page_index = snapshot.page_index
                                page_text = snapshot.normalized_text
                                page_confidence = snapshot.ocr_confidence
                            reduction = self._completion_reducer.reduce_pdf_page_completion(
                                state=state,
                                stored_rows=next_rows,
                                item_results=next_items,
                                page_index=page_index,
                                page_text=page_text,
                                page_confidence=page_confidence,
                                success_rows=success_rows,
                                error_code=page_error_code,
                                error_message=page_error_message,
                                rescue_events=rescue_events,
                            )
                            next_rows = reduction.result_rows
                            next_items = reduction.item_results
                            page_event = reduction.events[0]
                            terminal_reduction = self._completion_reducer.reduce_pdf_task_completion_if_ready(
                                state=state,
                                stored_rows=next_rows,
                                item_results=next_items,
                                stage="running_extract",
                            )
                            if terminal_reduction is not None:
                                next_rows = terminal_reduction.result_rows
                                next_items = terminal_reduction.item_results
                                terminal_event = terminal_reduction.events[0]
                        else:
                            reduction = self._completion_reducer.reduce_single_item_completion(
                                state=state,
                                stored_rows=next_rows,
                                item_results=next_items,
                                normalized_text=extraction_input.flat_text,
                                success_rows=success_rows,
                                error_code=page_error_code,
                                error_message=page_error_message,
                                stage="running_extract",
                                ocr_event=item.ocr_event,
                                rescue_events=rescue_events,
                            )
                            next_rows = reduction.result_rows
                            next_items = reduction.item_results
                            single_item_events = reduction.events
                    if page_event is not None:
                        self._event_publisher.publish(page_event)
                    if terminal_event is not None:
                        self._event_publisher.publish(terminal_event)
                    for event in single_item_events:
                        self._event_publisher.publish(event)
            except BaseException as exc:
                _record_worker_error(exc)

        extract_thread = threading.Thread(target=_run_extract_stage, name="ocr-extract-pipeline-extract")
        ocr_thread = threading.Thread(target=_run_ocr_stage, name="ocr-extract-pipeline-ocr")
        extract_thread.start()
        ocr_thread.start()
        try:
            _feed_pending_tasks()
        finally:
            ocr_thread.join()
            extract_thread.join()
        error = _first_worker_error()
        if error is not None:
            raise error
        return next_rows, next_items

    @staticmethod
    def _resolve_active_template_name(config: AppConfig) -> str | None:
        raw_templates = list(getattr(config, "templates", []))
        active_template_id = getattr(config, "active_template_id", None)
        if not raw_templates and not active_template_id:
            return None
        catalog = TemplateCatalog.load(raw_templates, active_template_id)
        return catalog.active_template_name()
