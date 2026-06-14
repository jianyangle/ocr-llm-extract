from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from src.core.engine_events import TaskOcrCompleted
from src.core.pdf_page_processor import PDFDocumentError, PDFPageProcessor
from src.domain.schemas import (
    BBox,
    ExtractionOptions,
    PDFLimits,
    PDFPageOCRSnapshot,
    PageError,
)
from src.ocr.errors import PDFAdapterError
from src.ocr.paddle_service import OCRRuntimeOptions

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PDFRetryBudgetSettings:
    budget: int
    unimproved_stop: int


@dataclass
class _PDFRetryBudgetState:
    budget_remaining: int
    unimproved_stop: int
    consecutive_unimproved: int = 0
    retry_disabled: bool = False

    @classmethod
    def from_settings(cls, settings: PDFRetryBudgetSettings) -> "_PDFRetryBudgetState":
        return cls(
            budget_remaining=max(int(settings.budget), 1),
            unimproved_stop=max(int(settings.unimproved_stop), 1),
        )

    def allow_retry(self, page_index: int) -> bool:
        _ = page_index
        return not self.retry_disabled and self.budget_remaining > 0

    def record(self, snapshot: PDFPageOCRSnapshot) -> None:
        if not snapshot.adaptive_retry_triggered:
            return
        self.budget_remaining -= 1
        if snapshot.adaptive_retry_applied:
            self.consecutive_unimproved = 0
        else:
            self.consecutive_unimproved += 1
        if self.budget_remaining <= 0:
            _logger.info(
                "PDF adaptive retry budget exhausted at page %s; disabling retry for remaining pages",
                snapshot.page_index,
            )
            self.retry_disabled = True
        elif self.consecutive_unimproved >= self.unimproved_stop:
            _logger.info(
                "PDF adaptive retry early stop at page %s after %s consecutive unimproved retries",
                snapshot.page_index,
                self.consecutive_unimproved,
            )
            self.retry_disabled = True


@dataclass(frozen=True)
class PDFOCRAggregatePage:
    zero_based_page_index: int
    page_index: int
    expected_page_count: int
    is_last_page: bool
    page_text: str
    aggregated_text_after_page: str
    ocr_confidence: float | None
    snapshot: PDFPageOCRSnapshot | None
    error: PageError | None
    crop: Callable[[BBox], Any] | None


class PDFOCRAggregationRun:
    def __init__(
        self,
        *,
        processor: PDFPageProcessor,
        expected_page_count: int,
        pdf_path: str | Path,
        ocr_options: OCRRuntimeOptions,
        extract_options: ExtractionOptions,
        limits: PDFLimits,
        retry_budget: PDFRetryBudgetSettings,
    ) -> None:
        self._processor = processor
        self._pdf_path = str(pdf_path)
        self._ocr_options = ocr_options
        self._extract_options = extract_options
        self._limits = limits
        self._budget_state = _PDFRetryBudgetState.from_settings(retry_budget)
        self._expected_page_count = expected_page_count
        self._page_snapshots: list[PDFPageOCRSnapshot] = []
        self._page_errors: list[PageError] = []
        self._yielded_any_page = False
        self._finished = False
        self._last_aggregated_text = ""

    @property
    def expected_page_count(self) -> int:
        return self._expected_page_count

    @property
    def page_snapshots(self) -> list[PDFPageOCRSnapshot]:
        return list(self._page_snapshots)

    @property
    def page_errors(self) -> list[PageError]:
        return list(self._page_errors)

    def iter_pages(self) -> Iterator[PDFOCRAggregatePage]:
        if self._finished:
            raise RuntimeError("iter_pages() can only be consumed once")

        yielded_count = 0
        for unit in self._processor.process(
            pdf_path=self._pdf_path,
            ocr_options=self._ocr_options,
            extract_options=self._extract_options,
            limits=self._limits,
            allow_retry_for_page=self._budget_state.allow_retry,
            record_ocr_snapshot=self._budget_state.record,
        ):
            yielded_count += 1
            self._yielded_any_page = True
            snapshot = unit.snapshot
            if snapshot is not None:
                self._page_snapshots.append(snapshot)
                self._page_snapshots.sort(key=lambda item: item.page_index)
                self._last_aggregated_text = self.compose_pdf_text(self._page_snapshots)
            if unit.error is not None:
                self._page_errors.append(unit.error)
            yield PDFOCRAggregatePage(
                zero_based_page_index=unit.page_index,
                page_index=unit.page_index + 1,
                expected_page_count=self.expected_page_count,
                is_last_page=(unit.page_index + 1) >= self.expected_page_count,
                page_text=snapshot.normalized_text if snapshot is not None else "",
                aggregated_text_after_page=self._last_aggregated_text,
                ocr_confidence=snapshot.ocr_confidence if snapshot is not None else None,
                snapshot=snapshot,
                error=unit.error,
                crop=unit.crop,
            )

        self._finished = True
        if yielded_count == 0:
            raise PDFAdapterError("E_PDF_002", "PDF rendering produced no pages")

    def build_base_ocr_event(self, *, task_id: str) -> TaskOcrCompleted:
        if not self._finished:
            raise RuntimeError("build_base_ocr_event() requires iter_pages() to finish first")

        if not self._page_snapshots:
            return TaskOcrCompleted(
                task_id=task_id,
                normalized_text="",
                ocr_confidence=None,
                page_snapshots=[],
                active_template_name=None,
                region_rescue=[],
                block_count=0,
            )

        confidence_values = [
            float(snapshot.ocr_confidence) for snapshot in self._page_snapshots if snapshot.ocr_confidence is not None
        ]
        confidence_min_values = [
            float(snapshot.confidence_min) for snapshot in self._page_snapshots if snapshot.confidence_min is not None
        ]
        first_pass_confidence_values = [
            float(snapshot.first_pass_confidence_min)
            for snapshot in self._page_snapshots
            if snapshot.first_pass_confidence_min is not None
        ]
        second_pass_confidence_values = [
            float(snapshot.second_pass_confidence_min)
            for snapshot in self._page_snapshots
            if snapshot.second_pass_confidence_min is not None
        ]
        retry_profile_from = next((snapshot.retry_profile_from for snapshot in self._page_snapshots if snapshot.retry_profile_from), None)
        retry_profile_to = next((snapshot.retry_profile_to for snapshot in self._page_snapshots if snapshot.retry_profile_to), None)
        return TaskOcrCompleted(
            task_id=task_id,
            normalized_text=self.compose_pdf_text(self._page_snapshots),
            ocr_confidence=sum(confidence_values) / len(confidence_values) if confidence_values else None,
            page_snapshots=list(self._page_snapshots),
            active_template_name=None,
            region_rescue=[],
            block_count=sum(snapshot.block_count for snapshot in self._page_snapshots),
            confidence_min=min(confidence_min_values) if confidence_min_values else None,
            pdf_page_count=self.expected_page_count,
            adaptive_retry_triggered=any(snapshot.adaptive_retry_triggered for snapshot in self._page_snapshots),
            adaptive_retry_applied=any(snapshot.adaptive_retry_applied for snapshot in self._page_snapshots),
            retry_profile_from=retry_profile_from,
            retry_profile_to=retry_profile_to,
            first_pass_confidence_min=min(first_pass_confidence_values) if first_pass_confidence_values else None,
            second_pass_confidence_min=min(second_pass_confidence_values) if second_pass_confidence_values else None,
        )

    @staticmethod
    def compose_pdf_text(page_snapshots: list[PDFPageOCRSnapshot]) -> str:
        return "\n\n".join(
            f"[PAGE {snapshot.page_index}]\n{snapshot.normalized_text}" for snapshot in page_snapshots
        ).strip()


class PDFOCRAggregator:
    def begin(
        self,
        *,
        pdf_path: str | Path,
        ocr_options: OCRRuntimeOptions,
        extract_options: ExtractionOptions,
        limits: PDFLimits,
        retry_budget: PDFRetryBudgetSettings,
        pdf_adapter: Any,
        ocr_service: Any,
    ) -> PDFOCRAggregationRun:
        processor = PDFPageProcessor(
            pdf_adapter=pdf_adapter,
            ocr_service=ocr_service,
            extractor=_EmptyPDFExtractor(),
        )

        # Fail fast on document-level inspect/limit errors so callers don't need to enter iter_pages().
        pdf_info = processor.inspect(pdf_path=pdf_path, limits=limits)

        return PDFOCRAggregationRun(
            processor=processor,
            expected_page_count=int(pdf_info.page_count),
            pdf_path=pdf_path,
            ocr_options=ocr_options,
            extract_options=extract_options,
            limits=limits,
            retry_budget=retry_budget,
        )


class _EmptyPDFExtractor:
    def extract_detailed(self, **kwargs: Any):
        _ = kwargs
        from src.domain.schemas import ExtractionOutcome

        return ExtractionOutcome(rows=[], column_specs=[], field_regions=[])
