from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterator, TypeAlias

from src.core.engine_events import TaskOcrCompleted
from src.core.pdf_ocr_aggregator import PdfOcrRun
from src.domain.schemas import PDFPageOCRSnapshot
from src.ocr.paddle_service import PaddleOCRService
from src.ocr.routing_service import RoutingOCRService
from src.ocr.models import OCRTextBlock


@dataclass(frozen=True)
class OCRPassthrough:
    pass


@dataclass(frozen=True)
class ImageRecognized:
    ocr_text: str
    ocr_confidence: float | None
    ocr_event: TaskOcrCompleted
    blocks: list[OCRTextBlock]


@dataclass(frozen=True)
class PdfDocStarted:
    expected_page_count: int


@dataclass(frozen=True)
class PageCommitted:
    aggregate: Any
    is_last: bool
    page_snapshots_so_far: list[PDFPageOCRSnapshot]


@dataclass(frozen=True)
class DocOcrCompleted:
    base_event: TaskOcrCompleted
    page_snapshots: list[PDFPageOCRSnapshot]


@dataclass(frozen=True)
class OCRFailure:
    code: str
    message: str
    stage: str


OCRSemanticOutput: TypeAlias = (
    OCRPassthrough
    | ImageRecognized
    | PdfDocStarted
    | PageCommitted
    | DocOcrCompleted
    | OCRFailure
)


class OCRStage:
    """OCR Stage 的 per-item 识别 seam，输出语义事件供 orchestrator 扇出。"""

    def __init__(
        self,
        *,
        source_processor: Any,
        pdf_ocr_aggregator: Any,
        online_pdf_processor: Any | None = None,
    ) -> None:
        self._source_processor = source_processor
        self._pdf_ocr_aggregator = pdf_ocr_aggregator
        self._online_pdf_processor = online_pdf_processor

    def recognize_one(
        self,
        *,
        item: Any,
        config: Any,
        progress_sink: Callable[[int, int], None] | None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> Iterator[OCRSemanticOutput]:
        if item.source_type == "text":
            yield OCRPassthrough()
            return

        if item.source_type == "image":
            try:
                ocr_text, ocr_confidence, ocr_event, ocr_blocks = self._source_processor._recognize_image(item.task)
            except Exception as exc:  # task 级图片 OCR 失败 → 语义 Failure（与 pdf 分支一致，不杀流水线）
                yield OCRFailure(
                    code=str(getattr(exc, "code", "E_QUEUE_001")),
                    message=str(getattr(exc, "message", str(exc))),
                    stage=item.task.status,
                )
                return
            yield ImageRecognized(
                ocr_text=ocr_text,
                ocr_confidence=ocr_confidence,
                ocr_event=ocr_event,
                blocks=ocr_blocks,
            )
            return

        try:
            yield from self._recognize_pdf(
                item=item,
                config=config,
                progress_sink=progress_sink,
                cancel_check=cancel_check,
            )
        except Exception as exc:
            yield OCRFailure(
                code=str(getattr(exc, "code", "E_QUEUE_001")),
                message=str(getattr(exc, "message", str(exc))),
                stage=item.task.status,
            )

    def _recognize_pdf(
        self,
        *,
        item: Any,
        config: Any,
        progress_sink: Callable[[int, int], None] | None,
        cancel_check: Callable[[], bool] | None,
    ) -> Iterator[OCRSemanticOutput]:
        pdf_limits = self._source_processor._build_pdf_limits(config)
        run: PdfOcrRun
        if config.ocr_use_online and self._online_pdf_processor is not None:
            run = self._online_pdf_processor.begin(
                pdf_path=item.source_value,
                online_config=RoutingOCRService.runtime_options_from_app_config(config).online_config,
                limits=pdf_limits,
                cancel_check=(cancel_check or (lambda: False)),
                progress_callback=(progress_sink or (lambda extracted, total: None)),
            )
        else:
            run = self._pdf_ocr_aggregator.begin(
                pdf_path=item.source_value,
                ocr_options=PaddleOCRService.runtime_options_from_app_config(config),
                extract_options=self._source_processor._build_extraction_options(config),
                limits=pdf_limits,
                retry_budget=self._source_processor._build_retry_budget_settings(config),
                pdf_adapter=self._source_processor.pdf_adapter,
                ocr_service=self._source_processor.ocr_service,
            )

        yield PdfDocStarted(expected_page_count=run.expected_page_count)
        for aggregate in run.iter_pages():
            yield PageCommitted(
                aggregate=aggregate,
                is_last=bool(aggregate.is_last_page),
                page_snapshots_so_far=list(run.page_snapshots),
            )
        base_event = run.build_base_ocr_event(task_id=item.task.task_id)
        yield DocOcrCompleted(base_event=base_event, page_snapshots=list(run.page_snapshots))
