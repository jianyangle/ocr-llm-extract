from __future__ import annotations

from src.core.engine_events import (
    EngineEvent,
    TaskFailed,
    TaskOcrCompleted,
    TaskPageResultStreamed,
    TaskProgressed,
    TaskSucceeded,
)
from src.ui.task_review_projection import TaskReviewProjection


class TaskReviewEventTranslator:
    def handle(self, event: EngineEvent, projection: TaskReviewProjection) -> None:
        match event:
            case TaskOcrCompleted(task_id=task_id):
                if not task_id:
                    return
                projection.record_ocr_text(task_id, event.normalized_text)
                projection.clear_markdown_text(task_id)
                projection.record_markdown_text(task_id, getattr(event, "markdown", None))
                projection.record_retry_status(
                    task_id,
                    triggered=event.adaptive_retry_triggered,
                    applied=event.adaptive_retry_applied,
                )
                for snapshot in event.page_snapshots:
                    projection.record_ocr_text(task_id, snapshot.normalized_text, page_index=snapshot.page_index)
                    projection.record_markdown_text(
                        task_id,
                        getattr(snapshot, "markdown_text", None),
                        page_index=snapshot.page_index,
                    )
                    projection.record_retry_status(
                        task_id,
                        triggered=snapshot.adaptive_retry_triggered,
                        applied=snapshot.adaptive_retry_applied,
                        page_index=snapshot.page_index,
                    )
            case TaskPageResultStreamed(task_id=task_id):
                if not task_id:
                    return
                projection.record_ocr_text(task_id, event.aggregated_text)
                projection.record_ocr_text(task_id, event.page_text, page_index=event.page_index)
                projection.replace_page_result_review(task_id=task_id, page_index=event.page_index, rows=event.rows)
            case TaskProgressed(task_id=task_id) | TaskSucceeded(task_id=task_id) | TaskFailed(task_id=task_id):
                if not task_id:
                    return
                projection.record_ocr_text(task_id, event.normalized_text)
                projection.replace_task_result_review(task_id=task_id, rows=event.rows)
            case _:
                return
