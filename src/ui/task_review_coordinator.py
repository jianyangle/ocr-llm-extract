from __future__ import annotations

from typing import Any, Sequence

from src.core.engine_events import EngineEvent
from src.ui.task_review_event_translator import TaskReviewEventTranslator
from src.ui.task_review_projection import TaskReviewExportRow, TaskReviewProjection, TaskReviewSnapshot


class TaskReviewCoordinator:
    def __init__(
        self,
        *,
        projection: TaskReviewProjection,
        event_translator: TaskReviewEventTranslator,
    ) -> None:
        self._projection = projection
        self._event_translator = event_translator
        self._tasks: list[Any] = []
        self._result_rows: list[Any] = []
        self._item_results: list[Any] = []

    def handle_engine_event(self, event: EngineEvent) -> None:
        self._event_translator.handle(event, self._projection)

    def reconcile_tasks(self, tasks: Sequence[Any]) -> None:
        self._tasks = list(tasks)

    def reconcile_result_rows(self, rows: Sequence[Any]) -> None:
        self._result_rows = list(rows)
        self._projection.reconcile_result_rows(self._result_rows)

    def reconcile_item_results(self, item_results: Sequence[Any]) -> None:
        self._item_results = list(item_results)

    def reconcile_all(
        self,
        *,
        tasks: Sequence[Any],
        result_rows: Sequence[Any],
        item_results: Sequence[Any],
    ) -> None:
        self.reconcile_tasks(tasks)
        self.reconcile_result_rows(result_rows)
        self.reconcile_item_results(item_results)

    def snapshot(self) -> TaskReviewSnapshot:
        return self._projection.snapshot(
            tasks=self._tasks,
            result_rows=self._result_rows,
            item_results=self._item_results,
        )

    def select_task(self, task_id: str, *, page_index: int | None = None) -> None:
        self._projection.select_task(task_id, page_index=page_index)

    def select_result(self, *, task_id: str, row_id: str | None = None, page_index: int | None = None) -> None:
        self._projection.select_result(task_id=task_id, row_id=row_id, page_index=page_index)

    def toggle_pdf_task(self, task_id: str) -> None:
        self._projection.toggle_pdf_task(task_id)

    def set_pdf_task_collapsed(self, task_id: str, collapsed: bool) -> None:
        self._projection.set_pdf_task_collapsed(task_id, collapsed)

    def set_result_action(self, row_id: str, action: str) -> None:
        self._projection.set_result_action(row_id, action)

    def export_rows(self) -> list[TaskReviewExportRow]:
        return self._projection.export_rows()

    def clear_transient_result_review_state(self) -> None:
        self._projection.clear_transient_result_review_state()

    def apply_task_deleted(self, task_id: str) -> None:
        self._tasks = [task for task in self._tasks if str(getattr(task, "task_id", "")) != task_id]
        self._result_rows = [row for row in self._result_rows if str(getattr(row, "task_id", "")) != task_id]
        self._item_results = [item for item in self._item_results if str(getattr(item, "task_id", "")) != task_id]
        self._projection.remove_task(task_id, suppress_result_reconcile=True)

    def apply_task_retried(self, task_id: str) -> None:
        self._projection.begin_retry(task_id)

    def apply_queue_cleared(self) -> None:
        task_ids = {str(getattr(task, "task_id", "")) for task in self._tasks}
        task_ids.update(str(getattr(row, "task_id", "")) for row in self._result_rows)
        self._tasks = []
        self._result_rows = []
        self._item_results = []
        self._projection.clear_all_result_review(suppress_result_reconcile_task_ids=sorted(task_ids))
