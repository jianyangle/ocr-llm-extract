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
from src.core.pdf_ocr_aggregator import PDFOCRAggregator, PDFRetryBudgetSettings
from src.domain.schemas import (
    AppConfig,
    ColumnSpec,
    ExtractRow,
    ExtractionInput,
    ExtractionOptions,
    ExtractionOutcome,
    ItemResult,
    PDFLimits,
    PDFPageExtractResult,
    PDFPageOCRSnapshot,
    TaskItem,
)
from src.extract import Extractor, region_attribution
from src.extract.template_catalog import TemplateCatalog
from src.extract.grounding import classify_cell, classify_row, ground_rows, stronger_status
from src.extract.output_normalizer import canonicalize_typed_cells, normalize_rows
from src.ocr.errors import PDFAdapterError
from src.ocr.models import OCRTextBlock
from src.ocr.paddle_service import PaddleOCRService
from src.ocr.routing_service import RoutingOCRService


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
        result_store: ResultStore,
        event_publisher: EventPublisher,
        online_pdf_processor: Any | None = None,
    ) -> None:
        self._config_getter = config_getter
        self._extractor_getter = extractor_getter
        self._source_processor = source_processor
        self._pdf_ocr_aggregator = pdf_ocr_aggregator
        self._online_pdf_processor = online_pdf_processor
        self._result_store = result_store
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
                pushed_any = False
                with state_lock:
                    ocr_busy = any(
                        task.task_id in seen and task.status == "running_ocr"
                        for task in tasks
                    )
                    pipeline_busy = any(
                        task.task_id in seen and task.status in {"running_ocr", "running_extract"}
                        for task in tasks
                    )
                    text_busy = any(
                        task.task_id in seen
                        and task.source_type == "text"
                        and task.status in {"running_ocr", "running_extract"}
                        for task in tasks
                    )
                    for task in tasks:
                        if task.status != "pending" or task.task_id in seen:
                            continue
                        if task.source_type == "text":
                            if pipeline_busy:
                                break
                            _start_pending_task(task)
                            seen.add(task.task_id)
                            pushed_any = True
                            break
                        if ocr_busy or text_busy:
                            break
                        _start_pending_task(task)
                        seen.add(task.task_id)
                        pushed_any = True
                        break
                if pushed_any:
                    continue
                with state_lock:
                    pipeline_busy = any(
                        task.task_id in seen and task.status in {"running_ocr", "running_extract"}
                        for task in tasks
                    )
                    has_pending = any(
                        task.task_id not in seen and task.status == "pending"
                        for task in tasks
                    )
                if not pipeline_busy and not has_pending:
                    break
                if not pipeline_busy:
                    continue
                time.sleep(_FEEDER_POLL_SECONDS)
            ocr_input_queue.put(stop_signal)

        def _run_ocr_stage() -> None:
            try:
                while True:
                    item = ocr_input_queue.get()
                    if isinstance(item, _StopSignal):
                        _put_stage_buffer(stop_signal)
                        return
                    if item.source_type == "text":
                        _put_stage_buffer(item)
                        continue
                    if item.source_type == "image":
                        ocr_text, ocr_confidence, ocr_event, ocr_blocks = self._source_processor._recognize_image(item.task)
                        with state_lock:
                            state = states[item.task_id]
                            state.normalized_text = ocr_text
                            state.ocr_confidence = ocr_confidence
                            state.ocr_completed_items = 1
                            item.task.ocr_text = ocr_text
                            item.task.status = "running_extract"
                        published_ocr_event = replace(
                            ocr_event,
                            active_template_name=active_template_name,
                            region_rescue=[],
                        )
                        self._event_publisher.publish(published_ocr_event)
                        _put_stage_buffer(
                            replace(
                                item,
                                normalized_text=ocr_text,
                                ocr_confidence=ocr_confidence,
                                markdown=published_ocr_event.markdown,
                                ocr_event=published_ocr_event,
                                blocks=ocr_blocks,
                            )
                        )
                        continue

                    page_items: list[_PipelineWorkItem] = []
                    try:
                        pdf_limits = SourceProcessor._build_pdf_limits(config)
                        if config.ocr_use_online and self._online_pdf_processor is not None:
                            def _publish_online_ocr_progress(
                                extracted: int, total: int, _item: _PipelineWorkItem = item
                            ) -> None:
                                # 整份 job 在 done 前无结果；用轮询期 extractProgress 驱动 UI 进度，
                                # 避免长任务（如 PP-StructureV3）被误判为卡死。
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

                            run = self._online_pdf_processor.begin(
                                pdf_path=item.source_value,
                                online_config=RoutingOCRService.runtime_options_from_app_config(
                                    config
                                ).online_config,
                                limits=pdf_limits,
                                cancel_check=stop_event.is_set,
                                progress_callback=_publish_online_ocr_progress,
                            )
                        else:
                            run = self._pdf_ocr_aggregator.begin(
                                pdf_path=item.source_value,
                                ocr_options=PaddleOCRService.runtime_options_from_app_config(config),
                                extract_options=SourceProcessor._build_extraction_options(config),
                                limits=pdf_limits,
                                retry_budget=SourceProcessor._build_retry_budget_settings(config),
                                pdf_adapter=self._source_processor.pdf_adapter,
                                ocr_service=self._source_processor.ocr_service,
                            )
                        expected_page_count = run.expected_page_count
                        held_item: _PipelineWorkItem | None = None
                        with state_lock:
                            state = states[item.task_id]
                            state.expected_items = expected_page_count
                            item.task.pdf_total_pages = expected_page_count
                        for aggregate in run.iter_pages():
                            snapshot = aggregate.snapshot
                            if snapshot is not None:
                                with state_lock:
                                    state = states[item.task_id]
                                    state.page_snapshots = list(run.page_snapshots)
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
                            if aggregate.is_last_page:
                                held_item = page_item
                            else:
                                _put_stage_buffer(page_item)
                        if not page_items:
                            raise PDFAdapterError("E_PDF_002", "PDF rendering produced no pages")
                        base_event = run.build_base_ocr_event(task_id=item.task.task_id)
                        with state_lock:
                            state = states[item.task_id]
                            state.normalized_text = base_event.normalized_text
                            item.task.status = "running_extract"
                            item.task.ocr_text = base_event.normalized_text
                            item.task.pdf_page_ocr_snapshots = list(run.page_snapshots)
                        self._event_publisher.publish(
                            replace(
                                base_event,
                                active_template_name=active_template_name,
                                region_rescue=[],
                            )
                        )
                        if held_item is not None:
                            _put_stage_buffer(replace(held_item, is_last_ocr_item=True, is_last_extract_item=True))
                    except Exception as exc:
                        code = str(getattr(exc, "code", "E_QUEUE_001"))
                        message = str(getattr(exc, "message", str(exc)))
                        _put_stage_buffer(
                            _PipelineTaskFailure(
                                task=item.task,
                                code=code,
                                message=message,
                                stage=item.task.status,
                            )
                        )
            except BaseException as exc:
                _record_worker_error(exc)
                _signal_extract_stage_to_stop()

        def _extract_item_text(item: _PipelineWorkItem) -> tuple[ExtractionInput, float | None]:
            if item.source_type == "pdf" and item.snapshot is not None:
                return (
                    ExtractionInput(
                        flat_text=item.snapshot.normalized_text,
                        markdown=item.snapshot.markdown_text,
                    ),
                    item.snapshot.ocr_confidence,
                )
            flat_text = item.normalized_text or item.source_value
            return ExtractionInput(flat_text=flat_text, markdown=item.markdown), item.ocr_confidence

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

                    extraction_input, ocr_confidence = _extract_item_text(item)
                    try:
                        outcome = self._extract_grounded_rows(
                            extractor=extractor,
                            text=extraction_input,
                            config=config,
                            ocr_confidence=ocr_confidence,
                        )
                        grounded_rows = outcome.rows
                        normalized_rows = normalize_rows(
                            [row.values for row in grounded_rows],
                            expected_columns=states[item.task_id].expected_columns,
                        )
                        page_index = item.snapshot.page_index if item.snapshot is not None else None
                        success_rows = self._result_store.build_success_rows(
                            task_id=task.task_id,
                            normalized_rows=normalized_rows,
                            grounded_rows=grounded_rows,
                            ocr_confidence=ocr_confidence,
                            typed_rows=None,
                            page_index=page_index,
                        )
                        crop = (
                            item.crop
                            if item.source_type == "pdf"
                            else self._build_image_crop(task.source_value) if item.source_type == "image" else None
                        )
                        success_rows, rescue_events = self._maybe_rescue_uncertain_fields(
                            success_rows=success_rows,
                            source_text=extraction_input.flat_text,
                            crop=crop,
                            outcome=outcome,
                            config=config,
                        )
                        sidecar_geometry, sidecar_events = self._build_text_layer_attribution_geometry(
                            item=item, outcome=outcome, success_rows=success_rows, config=config
                        )
                        if sidecar_events:
                            rescue_events = list(rescue_events) + sidecar_events
                        crop_image_size = getattr(crop, "image_size", None) if crop is not None else None
                        attribution_blocks = (
                            list(item.snapshot.blocks)
                            if item.source_type == "pdf" and item.snapshot is not None
                            else list(item.blocks)
                        )
                        if sidecar_geometry is not None:
                            attribution_blocks, crop_image_size = sidecar_geometry[0], sidecar_geometry[1]
                        if crop_image_size and len(crop_image_size) == 2 and attribution_blocks:
                            success_rows, attribution_events = self._maybe_correct_field_attribution(
                                success_rows=success_rows,
                                blocks=attribution_blocks,
                                page_width=int(crop_image_size[0]),
                                page_height=int(crop_image_size[1]),
                                outcome=outcome,
                                source_text=extraction_input.flat_text,
                                config=config,
                            )
                            rescue_events = list(rescue_events) + attribution_events
                        self._finalize_typed_rows(
                            success_rows=success_rows,
                            column_specs=list(outcome.column_specs),
                        )
                        page_error_code = None
                        page_error_message = None
                    except Exception as exc:
                        success_rows = []
                        rescue_events = []
                        page_error_code = str(getattr(exc, "code", "E_QUEUE_001"))
                        page_error_message = str(getattr(exc, "message", str(exc)))

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

    @staticmethod
    def _finalize_typed_rows(
        *,
        success_rows: list[ExtractRow],
        column_specs: list[ColumnSpec],
    ) -> None:
        if not column_specs:
            return
        for row in success_rows:
            typed_cells, warnings = canonicalize_typed_cells([row.values], column_specs)
            row.typed_values = typed_cells[0]
            for warning in warnings:
                if warning.get("code") not in {"W_NORM_PHONE", "W_NORM_EMAIL"}:
                    continue
                col = int(warning.get("col", -1))
                if 0 <= col < len(row.grounded_cells):
                    row.grounded_cells[col].status = "UNCERTAIN"

    @staticmethod
    def _extract_grounded_rows(
        *,
        extractor: Extractor,
        text: ExtractionInput | str,
        config: AppConfig,
        ocr_confidence: float | None,
    ) -> ExtractionOutcome:
        return extractor.extract_detailed(
            text=text,
            prompts=config.prompts,
            examples=config.examples_normalized,
            provider_cfg=config,
            ocr_confidence=ocr_confidence,
        )

    def _maybe_rescue_uncertain_fields(
        self,
        *,
        success_rows: list[ExtractRow],
        source_text: str,
        crop: Callable[[tuple[int, int, int, int]], Any] | None,
        outcome: ExtractionOutcome,
        config: AppConfig,
    ) -> tuple[list[ExtractRow], list[dict[str, Any]]]:
        field_regions = list(outcome.field_regions)
        crop_callback = crop
        if not success_rows or not field_regions or crop_callback is None:
            return success_rows, []

        image_size = getattr(crop_callback, "image_size", None)
        if not image_size or len(image_size) != 2:
            return success_rows, []
        image_width, image_height = int(image_size[0]), int(image_size[1])
        image_path = getattr(crop_callback, "image_path", None)

        header = [column.name for column in outcome.column_specs]
        regions_by_field = {region.field_name: region for region in field_regions}
        remaining_budget = max(int(getattr(config, "region_rescue_max_per_task", 5)), 0)
        if remaining_budget <= 0:
            return success_rows, []

        ocr_service = self._source_processor.ocr_service
        rescue_events: list[dict[str, Any]] = []
        for row in success_rows:
            if remaining_budget <= 0:
                break
            if not row.grounded_cells:
                continue
            for index, cell in enumerate(row.grounded_cells):
                if remaining_budget <= 0:
                    break
                if getattr(cell, "status", None) != "UNCERTAIN":
                    continue
                if index >= len(header):
                    continue
                field_name = header[index]
                region = regions_by_field.get(field_name)
                if region is None:
                    continue
                crop_box = self._clip_region_crop(region, image_width=image_width, image_height=image_height)
                if crop_box is None:
                    rescue_events.append(
                        {"kind": "ocr_rescue", "field": field_name, "triggered": True, "success": False, "reason": "invalid_crop"}
                    )
                    continue
                remaining_budget -= 1
                try:
                    cropped_image = crop_callback(crop_box)
                    if image_path is not None:
                        ocr_result = ocr_service.recognize(image_path, crop=crop_box)
                    else:
                        ocr_result = ocr_service.recognize(cropped_image)
                except Exception:
                    rescue_events.append(
                        {"kind": "ocr_rescue", "field": field_name, "triggered": True, "success": False, "reason": "ocr_failed"}
                    )
                    continue
                rescued_text = str(getattr(ocr_result, "text", "")).strip()
                if not rescued_text:
                    rescue_events.append(
                        {"kind": "ocr_rescue", "field": field_name, "triggered": True, "success": False, "reason": "empty"}
                    )
                    continue
                row.values[index] = rescued_text
                if index < len(row.grounded_cells):
                    row.grounded_cells[index] = classify_cell(rescued_text, source_text, config)
                next_classification = classify_row(row.grounded_cells)
                row.extraction_classification = stronger_status(row.extraction_classification, next_classification)
                rescue_events.append({"kind": "ocr_rescue", "field": field_name, "triggered": True, "success": True})
        return success_rows, rescue_events

    def _build_text_layer_attribution_geometry(
        self,
        *,
        item: "_PipelineWorkItem",
        outcome: ExtractionOutcome,
        success_rows: list[ExtractRow],
        config: AppConfig,
    ) -> tuple[tuple[list[OCRTextBlock], tuple[int, int]] | None, list[dict[str, Any]]]:
        """text-layer PDF 页按需补归属几何。返回 (geometry|None, degrade_events)。"""
        if not getattr(config, "pdf_text_layer_attribution_ocr", True):
            return None, []
        snapshot = item.snapshot
        if item.source_type != "pdf" or snapshot is None or snapshot.source_path != "text_layer":
            return None, []
        header = [column.name for column in outcome.column_specs]
        if not region_attribution.has_resolvable_pairs(
            outcome.field_groups, outcome.exclusive_group_pairs, header, success_rows
        ):
            return None, []
        render_dpi = max(int(getattr(config, "pdf_render_dpi", 200)), 72)
        try:
            # snapshot.page_index 是 1-based，render_page_image 内部会自行转为 0-based。
            rendered_page = self._source_processor.pdf_adapter.render_page_image(
                item.source_value, page_index=snapshot.page_index, render_dpi=render_dpi
            )
            try:
                ocr_result = self._source_processor.ocr_service.recognize(rendered_page.image_path)
                blocks = list(getattr(ocr_result, "blocks", []) or [])
                from PIL import Image

                with Image.open(rendered_page.image_path) as image:
                    image_size = image.size
            finally:
                rendered_page.cleanup()
        except Exception:
            return None, [
                {
                    "kind": "attribution_correction",
                    "action": "sidecar_unavailable",
                    "success": False,
                }
            ]
        if not blocks or not image_size or len(image_size) != 2:
            return None, []
        snapshot.source_path = "text_layer+ocr"
        return (blocks, (int(image_size[0]), int(image_size[1]))), []

    def _maybe_correct_field_attribution(
        self,
        *,
        success_rows: list[ExtractRow],
        blocks: list[OCRTextBlock],
        page_width: int,
        page_height: int,
        outcome: ExtractionOutcome,
        source_text: str,
        config: AppConfig,
    ) -> tuple[list[ExtractRow], list[dict[str, Any]]]:
        field_groups = list(getattr(outcome, "field_groups", ()) or ())
        pairs = list(getattr(outcome, "exclusive_group_pairs", ()) or ())
        if not success_rows or not field_groups or not pairs or not blocks:
            return success_rows, []
        if page_width <= 0 or page_height <= 0:
            return success_rows, []

        groups_by_name = {group.name: group for group in field_groups}
        header = [column.name for column in outcome.column_specs]
        if not region_attribution.has_resolvable_pairs(field_groups, pairs, header, success_rows):
            return success_rows, []
        col_index = {name: idx for idx, name in enumerate(header)}
        events: list[dict[str, Any]] = []

        for name_a, name_b in pairs:
            group_a = groups_by_name.get(name_a)
            group_b = groups_by_name.get(name_b)
            if group_a is None or group_b is None:
                continue
            try:
                division = region_attribution.resolve_pair_division(
                    blocks, group_a, group_b, page_width=page_width, page_height=page_height
                )
            except Exception:
                division = None
            if division is None:
                continue

            role_count = min(len(group_a.field_names), len(group_b.field_names))
            for role in range(role_count):
                field_a = group_a.field_names[role]
                field_b = group_b.field_names[role]
                idx_a = col_index.get(field_a)
                idx_b = col_index.get(field_b)
                if idx_a is None or idx_b is None:
                    continue
                for row in success_rows:
                    if idx_a >= len(row.values) or idx_b >= len(row.values):
                        continue
                    value_a = row.values[idx_a]
                    value_b = row.values[idx_b]
                    if not value_a.strip() or not value_b.strip():
                        continue
                    try:
                        region_a = region_attribution.locate_field(value_a, blocks, division)
                        region_b = region_attribution.locate_field(value_b, blocks, division)
                    except Exception:
                        region_a = region_b = None

                    crossed = region_a == name_b and region_b == name_a
                    correct = region_a == name_a and region_b == name_b
                    if crossed:
                        row.values[idx_a], row.values[idx_b] = value_b, value_a
                        if idx_a < len(row.grounded_cells):
                            row.grounded_cells[idx_a] = classify_cell(value_b, source_text, config)
                        if idx_b < len(row.grounded_cells):
                            row.grounded_cells[idx_b] = classify_cell(value_a, source_text, config)
                        if row.typed_values is not None and idx_a < len(row.typed_values) and idx_b < len(row.typed_values):
                            row.typed_values[idx_a], row.typed_values[idx_b] = (
                                row.typed_values[idx_b], row.typed_values[idx_a],
                            )
                        row.extraction_classification = stronger_status(
                            row.extraction_classification, classify_row(row.grounded_cells)
                        )
                        events.append({
                            "kind": "attribution_correction",
                            "field_pair": (field_a, field_b),
                            "from_group": name_a,
                            "to_group": name_b,
                            "action": "swap",
                            "success": True,
                        })
                    elif correct:
                        continue
                    else:
                        reason = "single_sided" if (region_a is None) ^ (region_b is None) else "geometry_unknown"
                        if region_a is None and region_b is None:
                            reason = "geometry_unknown"
                        for idx in (idx_a, idx_b):
                            if idx < len(row.grounded_cells):
                                row.grounded_cells[idx].status = "UNCERTAIN"
                        row.extraction_classification = classify_row(row.grounded_cells)
                        events.append({
                            "kind": "attribution_correction",
                            "field_pair": (field_a, field_b),
                            "action": "mark_uncertain",
                            "reason": reason,
                            "success": False,
                        })
        return success_rows, events

    @staticmethod
    def _clip_region_crop(region: Any, *, image_width: int, image_height: int) -> tuple[int, int, int, int] | None:
        x1 = int(float(region.left) * image_width)
        y1 = int(float(region.top) * image_height)
        x2 = int(float(region.right) * image_width)
        y2 = int(float(region.bottom) * image_height)
        left = max(0, min(x1, image_width))
        top = max(0, min(y1, image_height))
        right = max(0, min(x2, image_width))
        bottom = max(0, min(y2, image_height))
        if right <= left or bottom <= top:
            return None
        return left, top, right, bottom

    @staticmethod
    def _build_image_crop(image_path: str) -> Callable[[tuple[int, int, int, int]], Any] | None:
        try:
            from PIL import Image

            image = Image.open(image_path)
        except Exception:
            return None

        def _crop(bbox: tuple[int, int, int, int]) -> Any:
            return image.crop(bbox)

        _crop.image_size = image.size  # type: ignore[attr-defined]
        _crop.image_path = image_path  # type: ignore[attr-defined]
        return _crop
