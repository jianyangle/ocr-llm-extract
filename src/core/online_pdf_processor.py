from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterator

from src.core.engine_events import TaskOcrCompleted
from src.core.pdf_ocr_aggregator import PDFOCRAggregatePage, compose_pdf_text
from src.domain.schemas import PageError, PDFLimits, PDFPageOCRSnapshot
from src.ocr.errors import PDFAdapterError
from src.ocr.online_client import OnlineOCRClient
from src.ocr.online_service import OnlineOCRConfig
from src.ocr.pdf_adapter import PDFPageAdapter

CancelCheck = Callable[[], bool]
ClientFactory = Callable[[OnlineOCRConfig, CancelCheck], OnlineOCRClient]
ProgressCallback = Callable[[int, int], None]


def _default_client_factory(
    online_config: OnlineOCRConfig, cancel_check: CancelCheck
) -> OnlineOCRClient:
    """生产默认工厂：组装 optionalPayload 并把 ``cancel_check`` 透传给 client，
    使 ``poll_until_done`` 可被引擎 stop 中断。镜像
    ``online_service._default_client_factory`` 的装配，但额外线程 cancel_check。
    """
    optional_payload = {
        "useDocOrientationClassify": online_config.use_doc_orientation_classify,
        "useDocUnwarping": online_config.use_doc_unwarping,
        "useTextlineOrientation": online_config.use_textline_orientation,
    }
    return OnlineOCRClient(
        base_url=online_config.base_url,
        api_key=online_config.api_key,
        model=online_config.model,
        optional_payload=optional_payload,
        poll_interval=online_config.poll_interval,
        poll_timeout=online_config.poll_timeout,
        cancel_check=cancel_check,
    )


class OnlinePdfRun:
    """在线整文档异步 job 的运行对象。

    该对象与 ``PDFOCRAggregationRun`` 共享由以下 5 个成员组成的接口：
    暴露 ``expected_page_count`` / ``page_snapshots`` / ``page_errors`` /
    ``iter_pages()`` / ``build_base_ocr_event(task_id)``。
    ``OCRStage._recognize_pdf`` 通过 ``PdfOcrRun`` seam 统一消费这两种实现。

    在线 PDF 不做 hybrid / region rescue / adaptive retry（重试字段恒为 False）。
    """

    def __init__(
        self,
        *,
        client: OnlineOCRClient,
        job_id: str,
        expected_page_count: int,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self._client = client
        self._job_id = job_id
        self._expected_page_count = expected_page_count
        self._progress_callback = progress_callback
        self._page_snapshots: list[PDFPageOCRSnapshot] = []
        self._page_errors: list[PageError] = []
        self._last_aggregated_text = ""
        self._finished = False

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

        jsonl_url = self._client.poll_until_done(self._job_id, progress_callback=self._progress_callback)
        job_result = self._client.fetch_jsonl(jsonl_url)

        for zero_based_index, page in enumerate(job_result.pages):
            snapshot = PDFPageOCRSnapshot(
                page_index=zero_based_index + 1,
                normalized_text=page.text,
                ocr_confidence=page.confidence_avg,
                confidence_min=page.confidence_min,
                block_count=page.block_count,
                blocks=list(page.blocks),
                source_path="ocr",
                markdown_text=getattr(page, "markdown", None),
                adaptive_retry_triggered=False,
                adaptive_retry_applied=False,
            )
            self._page_snapshots.append(snapshot)
            self._last_aggregated_text = compose_pdf_text(self._page_snapshots)
            page_error: PageError | None = None
            if page.error_code is not None:
                page_error = PageError(
                    code=page.error_code, message="在线 OCR 单页结果错误", phase="ocr"
                )
                self._page_errors.append(page_error)
            yield PDFOCRAggregatePage(
                zero_based_page_index=zero_based_index,
                page_index=snapshot.page_index,
                expected_page_count=self._expected_page_count,
                is_last_page=(zero_based_index + 1) >= self._expected_page_count,
                page_text=snapshot.normalized_text,
                aggregated_text_after_page=self._last_aggregated_text,
                ocr_confidence=snapshot.ocr_confidence,
                snapshot=snapshot,
                error=page_error,
                crop=None,
            )

        self._finished = True

    # 在线 PDF 无 adaptive retry（见 ADR-0011）；两个返回分支都显式声明 retry 字段，避免依赖事件默认值（边界见 ADR-0005）。
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
                adaptive_retry_triggered=False,
                adaptive_retry_applied=False,
                retry_profile_from=None,
                retry_profile_to=None,
                first_pass_confidence_min=None,
                second_pass_confidence_min=None,
            )

        confidence_values = [
            float(snapshot.ocr_confidence)
            for snapshot in self._page_snapshots
            if snapshot.ocr_confidence is not None
        ]
        confidence_min_values = [
            float(snapshot.confidence_min)
            for snapshot in self._page_snapshots
            if snapshot.confidence_min is not None
        ]
        return TaskOcrCompleted(
            task_id=task_id,
            normalized_text=compose_pdf_text(self._page_snapshots),
            ocr_confidence=sum(confidence_values) / len(confidence_values) if confidence_values else None,
            page_snapshots=list(self._page_snapshots),
            active_template_name=None,
            region_rescue=[],
            block_count=sum(snapshot.block_count for snapshot in self._page_snapshots),
            confidence_min=min(confidence_min_values) if confidence_min_values else None,
            pdf_page_count=self.expected_page_count,
            adaptive_retry_triggered=False,
            adaptive_retry_applied=False,
            retry_profile_from=None,
            retry_profile_to=None,
            first_pass_confidence_min=None,
            second_pass_confidence_min=None,
        )

class OnlinePdfOCRProcessor:
    """在线 PDF OCR 处理器（整文档异步 job，spec §3.6）。

    流程：precheck（page_count / file_size，复用本地 ``E_PDF_*`` 语义）→ submit →
    返回 ``OnlinePdfRun``；轮询与下载在 ``iter_pages()`` 中惰性进行。
    """

    def __init__(self, *, client_factory: ClientFactory | None = None) -> None:
        self._client_factory = client_factory or _default_client_factory
        self._pdf_adapter = PDFPageAdapter()

    def begin(
        self,
        *,
        pdf_path: str,
        online_config: OnlineOCRConfig,
        limits: PDFLimits,
        cancel_check: CancelCheck,
        progress_callback: ProgressCallback | None = None,
    ) -> OnlinePdfRun:
        expected_page_count = self._precheck(pdf_path=pdf_path, limits=limits)

        file_bytes = Path(pdf_path).read_bytes()
        client = self._client_factory(online_config, cancel_check)
        job_id = client.submit(file_bytes=file_bytes, filename=Path(pdf_path).name)

        return OnlinePdfRun(
            client=client,
            job_id=job_id,
            expected_page_count=expected_page_count,
            progress_callback=progress_callback,
        )

    def _precheck(self, *, pdf_path: str, limits: PDFLimits) -> int:
        """复用本地 PDF inspect 的限制语义：file_size → E_PDF_004，page_count → E_PDF_003。"""
        pdf_info = self._pdf_adapter.inspect(pdf_path)
        max_file_size = max(int(limits.max_file_size), 1)
        max_pages = max(int(limits.max_pages), 1)
        if int(pdf_info.file_size) > max_file_size:
            raise PDFAdapterError(
                "E_PDF_004",
                f"PDF file size exceeds configured limit ({pdf_info.file_size} > {max_file_size})",
            )
        if int(pdf_info.page_count) > max_pages:
            raise PDFAdapterError(
                "E_PDF_003",
                f"PDF page count exceeds configured limit ({pdf_info.page_count} > {max_pages})",
            )
        return int(pdf_info.page_count)
