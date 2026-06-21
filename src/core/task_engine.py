from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.core.engine_events import EngineEvent
from src.core.ocr_stage import OCRStage
from src.core.pdf_ocr_aggregator import PDFOCRAggregator
from src.domain.schemas import AppConfig, ExtractRow, ItemResult, TaskItem
from src.extract import Extractor
from src.ocr.paddle_service import PaddleOCRService
from src.ocr.pdf_adapter import PDFPageAdapter

from .task_pipeline import EventPublisher, ResultStore, SourceProcessor, TaskOrchestrator


class EngineError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class TaskEngine:
    """Facade that preserves TaskEngine public contract while delegating internals.

    Non-goals for this refactor:
    - No concurrency model changes (still sequential processing).
    - No OCR/LLM algorithm changes.
    - No export behavior changes.
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        ocr_service: Any,
        extractor: Extractor,
        pdf_adapter: Any | None = None,
        event_sink: Callable[[EngineEvent], None] | None = None,
        online_pdf_processor: Any | None = None,
    ) -> None:
        self.config = config
        self.ocr_service = ocr_service
        self.extractor = extractor
        self.pdf_adapter = pdf_adapter
        self.event_sink = event_sink
        self.online_pdf_processor = online_pdf_processor
        self.tasks: list[TaskItem] = []
        self.result_rows: list[ExtractRow] = []
        self.item_results: list[ItemResult] = []
        self.state: str = "idle"
        self._pdf_ocr_aggregator = PDFOCRAggregator()
        self._result_store = ResultStore()
        self._event_publisher = EventPublisher(event_sink=self.event_sink)
        self._source_processor = SourceProcessor(
            config_getter=lambda: self.config,
            ocr_service_getter=lambda: self.ocr_service,
            pdf_adapter_getter=self._get_pdf_adapter,
            pdf_ocr_aggregator=self._pdf_ocr_aggregator,
        )
        self._ocr_stage = OCRStage(
            source_processor=self._source_processor,
            pdf_ocr_aggregator=self._pdf_ocr_aggregator,
            online_pdf_processor=online_pdf_processor,
        )
        self._orchestrator = TaskOrchestrator(
            config_getter=lambda: self.config,
            extractor_getter=lambda: self.extractor,
            source_processor=self._source_processor,
            pdf_ocr_aggregator=self._pdf_ocr_aggregator,
            ocr_stage=self._ocr_stage,
            result_store=self._result_store,
            event_publisher=self._event_publisher,
            online_pdf_processor=online_pdf_processor,
        )

    def add_text(self, text: str) -> str:
        task = TaskItem(source_type="text", source_value=text)
        self._pause_if_idle(task)
        self._mark_auto_dispatch_if_running(task)
        self.tasks.append(task)
        return task.task_id

    def add_image(self, image_path: str) -> str:
        normalized_path, display_name = self._normalize_source_path(image_path)
        task = TaskItem(
            source_type="image",
            source_value=normalized_path,
            source_path=normalized_path,
            display_name=display_name,
        )
        self._pause_if_idle(task)
        self._mark_auto_dispatch_if_running(task)
        self.tasks.append(task)
        return task.task_id

    def add_pdf(self, pdf_path: str) -> str:
        normalized_path, display_name = self._normalize_source_path(pdf_path)
        task = TaskItem(
            source_type="pdf",
            source_value=normalized_path,
            source_path=normalized_path,
            display_name=display_name,
        )
        self._pause_if_idle(task)
        self._mark_auto_dispatch_if_running(task)
        self.tasks.append(task)
        return task.task_id

    def delete_task(self, task_id: str) -> None:
        task = self._get_task(task_id)
        if str(task.status).startswith("running_"):
            raise EngineError("E_UI_001", "Running tasks cannot be deleted")
        self.tasks[:] = [item for item in self.tasks if item.task_id != task_id]
        self.result_rows = [row for row in self.result_rows if row.task_id != task_id]
        self.item_results = [item for item in self.item_results if item.task_id != task_id]

    def pause_task(self, task_id: str) -> None:
        task = self._get_task(task_id)
        if task.status != "pending":
            raise EngineError("E_PAUSE_001", "Only pending tasks can be paused")
        task.status = "paused"

    def resume_task(self, task_id: str) -> None:
        task = self._get_task(task_id)
        if task.status != "paused":
            raise EngineError("E_RESUME_001", "Only paused tasks can be resumed")
        task.status = "pending"

    def pause_all(self) -> None:
        for task in self.tasks:
            if task.status == "pending":
                task.status = "paused"

    def resume_all(self) -> None:
        for task in self.tasks:
            if task.status == "paused":
                task.status = "pending"

    def clear_all(self) -> None:
        if not self.tasks:
            return
        all_paused = all(task.status == "paused" for task in self.tasks)
        all_done = all(task.status == "done" for task in self.tasks)
        if not (all_paused or all_done):
            raise EngineError("E_CLEAR_001", "Clear all requires all tasks paused or all done")
        self.tasks = []
        self.result_rows = []
        self.item_results = []

    def retry_task(self, task_id: str) -> None:
        task = self._get_task(task_id)
        if task.status != "failed":
            raise EngineError("E_RETRY_001", "Only failed tasks can be retried")
        task.status = "paused"
        task.progress = 0
        task.ocr_text = ""
        task.error_code = None
        task.error_message = None
        task.started_at = None
        task.finished_at = None
        task.pdf_processed_pages = 0
        task.pdf_page_ocr_snapshots = []
        task.pdf_page_results = []

    def request_stop(self) -> None:
        """请求中止运行中的识别（供 UI 关闭/取消调用）。"""
        self._orchestrator.request_stop()

    def start(self) -> None:
        if self.state == "running":
            return
        if not any(task.status == "pending" for task in self.tasks):
            return
        if self._pending_tasks_need_ocr_runtime_reset():
            self._reset_ocr_runtime_for_run()

        self.state = "running"
        try:
            self.result_rows, self.item_results = self._orchestrator.run(
                tasks=self.tasks,
                stored_rows=self.result_rows,
                item_results=self.item_results,
            )
        finally:
            self.state = "idle"

    def _pending_tasks_need_ocr_runtime_reset(self) -> bool:
        return any(task.status == "pending" and task.source_type in {"image", "pdf"} for task in self.tasks)

    def _reset_ocr_runtime_for_run(self) -> None:
        reset_runtime = getattr(self.ocr_service, "reset_runtime", None)
        if not callable(reset_runtime):
            return
        builder = getattr(self.ocr_service, "runtime_options_from_app_config", None)
        if not callable(builder):
            builder = PaddleOCRService.runtime_options_from_app_config
        reset_runtime(builder(self.config))

    def _get_pdf_adapter(self) -> Any:
        if self.pdf_adapter is None:
            self.pdf_adapter = PDFPageAdapter()
        return self.pdf_adapter

    def _get_task(self, task_id: str) -> TaskItem:
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        raise EngineError("E_QUEUE_001", f"Task not found: {task_id}")

    def _mark_auto_dispatch_if_running(self, task: TaskItem) -> None:
        if self.state == "running":
            task.auto_dispatch_while_running = True

    def _pause_if_idle(self, task: TaskItem) -> None:
        if self.state == "idle":
            task.status = "paused"

    @staticmethod
    def _normalize_source_path(raw_path: str) -> tuple[str, str]:
        normalized = str(Path(str(raw_path)).expanduser())
        display_name = Path(normalized).name or normalized
        return normalized, display_name
