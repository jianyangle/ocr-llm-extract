from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


TASK_SOURCE_DISPLAY_PREFIX_CHARS = 15


@dataclass(frozen=True)
class TaskReviewSelection:
    task_id: str | None = None
    page_index: int | None = None
    row_id: str | None = None


@dataclass(frozen=True)
class TaskQueueRow:
    node_type: str
    task_id: str
    page_index: int | None
    source_text: str
    source_tooltip: str
    progress_text: str
    status_value: str
    is_page_row: bool

    @property
    def payload(self) -> dict[str, object]:
        return {"node_type": self.node_type, "task_id": self.task_id, "page_index": self.page_index}


@dataclass(frozen=True)
class ResultReviewRow:
    row_id: str | None
    task_id: str
    values: list[object]
    typed_values: list[object] | None = None
    action: str = "write"
    page_index: int | None = None
    ocr_confidence: float | None = None
    is_error_row: bool = False
    extraction_classification: str = "UNCERTAIN"

    @property
    def payload(self) -> dict[str, object]:
        return {
            "row_id": self.row_id,
            "task_id": self.task_id,
            "values": list(self.values),
            "typed_values": list(self.typed_values) if self.typed_values is not None else None,
            "action": self.action,
            "page_index": self.page_index,
            "ocr_confidence": self.ocr_confidence,
            "is_error_row": self.is_error_row,
            "extraction_classification": self.extraction_classification,
        }


@dataclass(frozen=True)
class TaskReviewExportRow:
    row_id: str | None
    task_id: str
    values: list[object]
    typed_values: list[object] | None = None
    action: str = "write"
    is_error_row: bool = False


@dataclass(frozen=True)
class InspectorRenderData:
    task_id: str | None
    page_index: int | None
    ocr_text: str
    task_id_text: str
    source_text: str
    status_text: str
    error_text: str
    retry_text: str
    tips_text: str
    markdown_text: str | None = None


@dataclass(frozen=True)
class TaskReviewSnapshot:
    task_queue_rows: list[TaskQueueRow]
    result_review_rows: list[ResultReviewRow]
    inspector: InspectorRenderData
    selected_task_row_index: int | None
    selected_result_row_index: int | None


class TaskReviewProjection:
    def __init__(self, *, ocr_placeholder: str, empty_tips: str) -> None:
        self.ocr_placeholder = ocr_placeholder
        self.empty_tips = empty_tips
        self._selection = TaskReviewSelection()
        self._selection_mode = "auto"
        self._collapsed_task_ids: set[str] = set()
        self._base_task_order: list[str] = []
        self._base_rows_by_task: dict[str, list[ResultReviewRow]] = {}
        self._stream_task_order: list[str] = []
        self._stream_rows_by_task: dict[str, list[ResultReviewRow]] = {}
        self._retry_pending_task_ids: set[str] = set()
        self._suppressed_result_task_ids: set[str] = set()
        self._ocr_text_by_task: dict[str, str] = {}
        self._ocr_text_by_page: dict[tuple[str, int], str] = {}
        self._markdown_by_task: dict[str, str] = {}
        self._markdown_by_page: dict[tuple[str, int], str] = {}
        self._retry_by_task: dict[str, tuple[bool, bool]] = {}
        self._retry_by_page: dict[tuple[str, int], tuple[bool, bool]] = {}

    def select_task(self, task_id: str, *, page_index: int | None = None) -> None:
        coerced_page_index = _coerce_page_index(page_index)
        if coerced_page_index is not None:
            self._collapsed_task_ids.discard(task_id)
        self._selection = TaskReviewSelection(task_id=task_id, page_index=coerced_page_index)
        self._selection_mode = "pinned"

    def select_result(self, *, task_id: str, row_id: str | None = None, page_index: int | None = None) -> None:
        coerced_page_index = _coerce_page_index(page_index)
        if coerced_page_index is not None:
            self._collapsed_task_ids.discard(task_id)
        self._selection = TaskReviewSelection(task_id=task_id, page_index=coerced_page_index, row_id=row_id)
        self._selection_mode = "pinned"

    def toggle_pdf_task(self, task_id: str) -> None:
        if task_id in self._collapsed_task_ids:
            self._collapsed_task_ids.remove(task_id)
            return
        self._collapsed_task_ids.add(task_id)

    def set_pdf_task_collapsed(self, task_id: str, collapsed: bool) -> None:
        if collapsed:
            self._collapsed_task_ids.add(task_id)
            return
        self._collapsed_task_ids.discard(task_id)

    def is_task_collapsed(self, task_id: str) -> bool:
        return task_id in self._collapsed_task_ids

    def task_page_count(self, task: Any) -> int:
        return _task_page_count(task, self._ocr_text_by_page)

    def task_has_page_children(self, task: Any) -> bool:
        return _task_has_page_children(task, self._ocr_text_by_page)

    def stream_task_count(self) -> int:
        return len(self._stream_rows_by_task)

    def record_ocr_text(self, task_id: str, text: object, *, page_index: int | None = None) -> None:
        text_value = str(text or "").strip()
        if not text_value:
            return
        coerced_page = _coerce_page_index(page_index)
        if coerced_page is not None:
            self._ocr_text_by_page[(task_id, coerced_page)] = text_value
            return
        self._ocr_text_by_task[task_id] = text_value

    def record_markdown_text(self, task_id: str, text: object, *, page_index: int | None = None) -> None:
        text_value = str(text or "").strip()
        if not text_value:
            return
        coerced_page = _coerce_page_index(page_index)
        if coerced_page is not None:
            self._markdown_by_page[(task_id, coerced_page)] = text_value
            return
        self._markdown_by_task[task_id] = text_value

    def markdown_text_for(self, task_id: str, *, page_index: int | None = None) -> str | None:
        coerced_page = _coerce_page_index(page_index)
        if coerced_page is not None:
            return self._markdown_by_page.get((task_id, coerced_page))
        return self._markdown_by_task.get(task_id)

    def clear_markdown_text(self, task_id: str) -> None:
        self._markdown_by_task.pop(task_id, None)
        self._markdown_by_page = {
            key: value for key, value in self._markdown_by_page.items() if key[0] != task_id
        }

    def record_retry_status(
        self,
        task_id: str,
        *,
        triggered: bool,
        applied: bool,
        page_index: int | None = None,
    ) -> None:
        status = (bool(triggered), bool(applied))
        coerced_page = _coerce_page_index(page_index)
        if coerced_page is not None:
            self._retry_by_page[(task_id, coerced_page)] = status
            return
        self._retry_by_task[task_id] = status

    def replace_stream_rows(self, task_id: str, rows: Sequence[Mapping[str, object] | Any]) -> None:
        self.replace_task_result_review(task_id=task_id, rows=rows)

    def replace_task_result_review(self, *, task_id: str, rows: Sequence[Mapping[str, object] | Any]) -> None:
        self._prepare_result_review_update(task_id)
        if task_id not in self._stream_rows_by_task:
            self._stream_task_order.append(task_id)
        self._stream_rows_by_task[task_id] = self._merge_task_rows(task_id, rows)

    def append_stream_rows(self, task_id: str, rows: Sequence[Mapping[str, object] | Any]) -> None:
        if task_id not in self._stream_rows_by_task:
            self._stream_task_order.append(task_id)
            self._stream_rows_by_task[task_id] = []
        existing_rows = list(self._stream_rows_by_task.get(task_id, []))
        row_indexes = {str(row.row_id): index for index, row in enumerate(existing_rows) if row.row_id is not None}
        for raw_row in rows:
            row = self._merge_task_rows(task_id, [raw_row])[0]
            row_key = str(row.row_id) if row.row_id is not None else ""
            if row_key and row_key in row_indexes:
                existing_rows[row_indexes[row_key]] = row
                continue
            existing_rows.append(row)
            if row_key:
                row_indexes[row_key] = len(existing_rows) - 1
        self._stream_rows_by_task[task_id] = existing_rows

    def replace_page_result_review(
        self,
        *,
        task_id: str,
        page_index: int,
        rows: Sequence[Mapping[str, object] | Any],
    ) -> None:
        self._prepare_result_review_update(task_id)
        if task_id not in self._stream_rows_by_task:
            self._stream_task_order.append(task_id)
        existing_rows = list(self._stream_rows_by_task.get(task_id, []))
        merged_rows = self._merge_task_rows(task_id, rows)
        next_rows: list[ResultReviewRow] = []
        inserted = False
        for row in existing_rows:
            if row.page_index == page_index:
                if not inserted:
                    next_rows.extend(merged_rows)
                    inserted = True
                continue
            next_rows.append(row)
        if not inserted:
            next_rows.extend(merged_rows)
        self._stream_rows_by_task[task_id] = next_rows

    def reconcile_result_rows(self, rows: Sequence[Mapping[str, object] | Any]) -> None:
        grouped_rows = _group_rows_by_task(
            [
                row
                for row in rows
                if _result_row_from_any(row).task_id not in self._suppressed_result_task_ids
                and _result_row_from_any(row).task_id not in self._retry_pending_task_ids
            ]
        )
        incoming_task_ids = set(grouped_rows.keys())
        self._base_task_order = [task_id for task_id in self._base_task_order if task_id in incoming_task_ids]
        for task_id, task_rows in grouped_rows.items():
            if task_id not in self._base_task_order:
                self._base_task_order.append(task_id)
            self._base_rows_by_task[task_id] = self._merge_task_rows(task_id, task_rows)
        for task_id in list(self._base_rows_by_task.keys()):
            if task_id not in incoming_task_ids:
                del self._base_rows_by_task[task_id]

    def set_result_action(self, row_id: str, action: str) -> None:
        normalized_action = _normalize_action(action, is_error_row=False)
        self._base_rows_by_task = {
            task_id: [_row_with_action(row, normalized_action) if row.row_id == row_id else row for row in rows]
            for task_id, rows in self._base_rows_by_task.items()
        }
        self._stream_rows_by_task = {
            task_id: [_row_with_action(row, normalized_action) if row.row_id == row_id else row for row in rows]
            for task_id, rows in self._stream_rows_by_task.items()
        }

    def export_rows(self) -> list[TaskReviewExportRow]:
        export_rows: list[TaskReviewExportRow] = []
        for row in self._current_result_rows():
            if row.action != "write" or row.is_error_row:
                continue
            export_rows.append(
                TaskReviewExportRow(
                    row_id=row.row_id,
                    task_id=row.task_id,
                    values=list(row.values),
                    typed_values=list(row.typed_values) if row.typed_values is not None else None,
                    action=row.action,
                    is_error_row=row.is_error_row,
                )
            )
        return export_rows

    def clear_transient_result_review_state(self) -> None:
        self._stream_task_order = []
        self._stream_rows_by_task = {}

    def clear_stream_rows(self) -> None:
        self.clear_transient_result_review_state()

    def begin_retry(self, task_id: str) -> None:
        self._retry_pending_task_ids.add(task_id)

    def remove_task(self, task_id: str, *, suppress_result_reconcile: bool = False) -> None:
        self._base_task_order = [current_task_id for current_task_id in self._base_task_order if current_task_id != task_id]
        self._stream_task_order = [current_task_id for current_task_id in self._stream_task_order if current_task_id != task_id]
        self._base_rows_by_task.pop(task_id, None)
        self._stream_rows_by_task.pop(task_id, None)
        self._retry_pending_task_ids.discard(task_id)
        if suppress_result_reconcile:
            self._suppressed_result_task_ids.add(task_id)
        else:
            self._suppressed_result_task_ids.discard(task_id)
        self._ocr_text_by_task.pop(task_id, None)
        self._markdown_by_task.pop(task_id, None)
        self._retry_by_task.pop(task_id, None)
        self._ocr_text_by_page = {
            key: value for key, value in self._ocr_text_by_page.items() if key[0] != task_id
        }
        self._markdown_by_page = {
            key: value for key, value in self._markdown_by_page.items() if key[0] != task_id
        }
        self._retry_by_page = {
            key: value for key, value in self._retry_by_page.items() if key[0] != task_id
        }

    def clear_all_result_review(self, *, suppress_result_reconcile_task_ids: Sequence[str] = ()) -> None:
        self._base_task_order = []
        self._stream_task_order = []
        self._base_rows_by_task = {}
        self._stream_rows_by_task = {}
        self._markdown_by_task = {}
        self._markdown_by_page = {}
        self._retry_pending_task_ids = set()
        self._suppressed_result_task_ids.update(str(task_id) for task_id in suppress_result_reconcile_task_ids)

    def snapshot(
        self,
        *,
        tasks: Sequence[Any],
        result_rows: Sequence[Mapping[str, object] | Any],
        item_results: Sequence[Any] | None = None,
    ) -> TaskReviewSnapshot:
        task_list = list(tasks)
        task_by_id = {str(getattr(task, "task_id")): task for task in task_list}
        selection = self._repair_selection(task_list, task_by_id)
        task_queue_rows = self._build_task_queue_rows(task_list)
        result_review_rows = self._current_result_rows()
        inspector = self._build_inspector(selection, task_by_id, item_results or [])
        return TaskReviewSnapshot(
            task_queue_rows=task_queue_rows,
            result_review_rows=result_review_rows,
            inspector=inspector,
            selected_task_row_index=_find_task_row_index(task_queue_rows, selection),
            selected_result_row_index=_find_result_row_index(result_review_rows, selection.row_id),
        )

    def _build_task_queue_rows(self, tasks: Sequence[Any]) -> list[TaskQueueRow]:
        rows: list[TaskQueueRow] = []
        for task in tasks:
            task_id = str(getattr(task, "task_id"))
            source_full_value = _task_source_full_value(task)
            source_text = _task_source_display_text(task)
            status_value = str(getattr(task, "status", "") or "")
            rows.append(
                TaskQueueRow(
                    node_type="task",
                    task_id=task_id,
                    page_index=None,
                    source_text=source_text,
                    source_tooltip=source_full_value,
                    progress_text=_task_progress_cell_text(status_value, getattr(task, "progress", 0)),
                    status_value=status_value,
                    is_page_row=False,
                )
            )
            if _task_has_page_children(task, self._ocr_text_by_page) and task_id not in self._collapsed_task_ids:
                for page_index in range(1, _task_page_count(task, self._ocr_text_by_page) + 1):
                    display_status, progress_value, _error_code = _task_page_status_payload(task, page_index)
                    rows.append(
                        TaskQueueRow(
                            node_type="page",
                            task_id=task_id,
                            page_index=page_index,
                            source_text=f"    第 {page_index} 页",
                            source_tooltip=f"{source_full_value} | 第 {page_index} 页",
                            progress_text=_task_progress_cell_text(display_status, progress_value),
                            status_value=display_status,
                            is_page_row=True,
                        )
                    )
        return rows

    def _current_result_rows(self) -> list[ResultReviewRow]:
        base_rows: list[ResultReviewRow] = []
        for task_id in self._base_task_order:
            base_rows.extend(self._base_rows_by_task.get(task_id, []))
        if self._stream_task_order:
            stream_task_ids = set(self._stream_task_order)
            rows = [row for row in base_rows if row.task_id not in stream_task_ids]
            for task_id in self._stream_task_order:
                rows.extend(self._stream_rows_by_task.get(task_id, []))
            return rows
        return base_rows

    def _merge_task_rows(self, task_id: str, rows: Sequence[Mapping[str, object] | Any]) -> list[ResultReviewRow]:
        action_by_row_id = self._action_by_row_id(task_id)
        merged_rows: list[ResultReviewRow] = []
        for raw_row in rows:
            row = _result_row_from_any(raw_row, default_task_id=task_id)
            row_key = str(row.row_id) if row.row_id is not None else ""
            preserved_action = action_by_row_id.get(row_key)
            if preserved_action is not None and not row.is_error_row:
                row = _row_with_action(row, preserved_action)
            merged_rows.append(row)
        return merged_rows

    def _action_by_row_id(self, task_id: str) -> dict[str, str]:
        action_by_row_id: dict[str, str] = {}
        for row in self._base_rows_by_task.get(task_id, []):
            if row.row_id is not None:
                action_by_row_id[str(row.row_id)] = row.action
        for row in self._stream_rows_by_task.get(task_id, []):
            if row.row_id is not None:
                action_by_row_id[str(row.row_id)] = row.action
        return action_by_row_id

    def _prepare_result_review_update(self, task_id: str) -> None:
        if task_id in self._retry_pending_task_ids:
            self._base_task_order = [current_task_id for current_task_id in self._base_task_order if current_task_id != task_id]
            self._stream_task_order = [current_task_id for current_task_id in self._stream_task_order if current_task_id != task_id]
            self._base_rows_by_task.pop(task_id, None)
            self._stream_rows_by_task.pop(task_id, None)
            self._retry_pending_task_ids.discard(task_id)
        self._suppressed_result_task_ids.discard(task_id)

    def _repair_selection(self, tasks: Sequence[Any], task_by_id: Mapping[str, Any]) -> TaskReviewSelection:
        selection = self._selection
        selected_task_id = selection.task_id
        selected_page_index = selection.page_index
        if selected_task_id not in task_by_id:
            selected_task_id = str(getattr(tasks[0], "task_id")) if tasks else None
            selected_page_index = None
            self._selection_mode = "auto"
        if selected_task_id is None:
            repaired = TaskReviewSelection()
            self._selection = repaired
            return repaired
        task = task_by_id[selected_task_id]
        if selected_page_index is not None:
            if selected_task_id in self._collapsed_task_ids:
                self._collapsed_task_ids.discard(selected_task_id)
            if not _task_page_exists(task, selected_page_index, self._ocr_text_by_page):
                selected_page_index = None
        repaired = TaskReviewSelection(task_id=selected_task_id, page_index=selected_page_index, row_id=selection.row_id)
        self._selection = repaired
        return repaired

    def _build_inspector(
        self,
        selection: TaskReviewSelection,
        task_by_id: Mapping[str, Any],
        item_results: Sequence[Any],
    ) -> InspectorRenderData:
        if selection.task_id is None:
            return InspectorRenderData(
                task_id=None,
                page_index=None,
                ocr_text=self.ocr_placeholder,
                task_id_text="-",
                source_text="-",
                status_text="-",
                error_text="-",
                retry_text="-",
                tips_text=self.empty_tips,
            )
        task = task_by_id.get(selection.task_id)
        if task is None:
            return InspectorRenderData(
                task_id=None,
                page_index=None,
                ocr_text=self.ocr_placeholder,
                task_id_text="-",
                source_text="-",
                status_text="-",
                error_text="-",
                retry_text="-",
                tips_text=self.empty_tips,
            )
        source_full_value = _task_source_full_value(task)
        if selection.page_index is not None:
            source_full_value = f"{source_full_value} | 第 {selection.page_index} 页"
            status_text, _progress, error_text = _task_page_status_payload(task, selection.page_index)
            tips_text = (
                f"正在查看第 {selection.page_index} 页 OCR。"
                if status_text != "失败"
                else f"正在查看第 {selection.page_index} 页 OCR（该页失败）。"
            )
        else:
            status_value = str(getattr(task, "status", "") or "")
            status_text = _display_task_status(status_value)
            error_text = str(getattr(task, "error_code", "") or "") or "-"
            tips_text = _inspector_tip_for_status(status_value)
        return InspectorRenderData(
            task_id=selection.task_id,
            page_index=selection.page_index,
            ocr_text=self._resolve_ocr_text(task, selection.page_index, item_results),
            task_id_text=selection.task_id,
            source_text=source_full_value,
            status_text=status_text,
            error_text=error_text or "-",
            retry_text=self._resolve_retry_text(task, selection.page_index),
            tips_text=tips_text,
            markdown_text=self.markdown_text_for(selection.task_id, page_index=selection.page_index),
        )

    def _resolve_ocr_text(self, task: Any, page_index: int | None, item_results: Sequence[Any]) -> str:
        task_id = str(getattr(task, "task_id"))
        if page_index is not None:
            cached_page = self._ocr_text_by_page.get((task_id, page_index), "").strip()
            if cached_page:
                return cached_page
            snapshot = _task_page_snapshot(task, page_index)
            page_text = str(getattr(snapshot, "normalized_text", "") or "").strip() if snapshot is not None else ""
            if page_text:
                return page_text
            for item_result in reversed(list(item_results)):
                if getattr(item_result, "task_id", None) != task_id:
                    continue
                for page_result in list(getattr(item_result, "page_results", []) or []):
                    if _coerce_page_index(getattr(page_result, "page_index", None)) != page_index:
                        continue
                    result_text = str(getattr(page_result, "normalized_text", "") or "").strip()
                    if result_text:
                        return result_text
            return self.ocr_placeholder
        cached_task = self._ocr_text_by_task.get(task_id, "").strip()
        if cached_task:
            return cached_task
        for item_result in reversed(list(item_results)):
            if getattr(item_result, "task_id", None) != task_id:
                continue
            result_text = str(getattr(item_result, "normalized_text", "") or "").strip()
            return result_text or self.ocr_placeholder
        task_text = str(getattr(task, "ocr_text", "") or "").strip()
        return task_text or self.ocr_placeholder

    def _resolve_retry_text(self, task: Any, page_index: int | None) -> str:
        task_id = str(getattr(task, "task_id"))
        source_type = str(getattr(task, "source_type", "") or "")
        if page_index is not None:
            snapshot = _task_page_snapshot(task, page_index)
            if snapshot is not None:
                return _retry_status_text(
                    triggered=bool(getattr(snapshot, "adaptive_retry_triggered", False)),
                    applied=bool(getattr(snapshot, "adaptive_retry_applied", False)),
                )
            triggered, applied = self._retry_by_page.get((task_id, page_index), (False, False))
            return _retry_status_text(triggered=triggered, applied=applied)
        if source_type != "image":
            return "-"
        triggered, applied = self._retry_by_task.get(task_id, (False, False))
        return _retry_status_text(triggered=triggered, applied=applied)


def _result_row_from_any(row: Mapping[str, object] | Any, *, default_task_id: str = "") -> ResultReviewRow:
    if isinstance(row, Mapping):
        is_error_row = bool(row.get("is_error_row", False))
        return ResultReviewRow(
            row_id=_optional_str(row.get("row_id")),
            task_id=str(row.get("task_id") or default_task_id),
            values=list(row.get("values") or []),
            typed_values=_optional_list(row.get("typed_values")),
            action=_normalize_action(str(row.get("action") or "write"), is_error_row=is_error_row),
            page_index=_coerce_page_index(row.get("page_index")),
            ocr_confidence=_optional_float(row.get("ocr_confidence")),
            is_error_row=is_error_row,
            extraction_classification=str(row.get("extraction_classification") or "UNCERTAIN"),
        )
    is_error_row = bool(getattr(row, "is_error_row", False))
    return ResultReviewRow(
        row_id=_optional_str(getattr(row, "row_id", None)),
        task_id=str(getattr(row, "task_id", default_task_id) or default_task_id),
        values=list(getattr(row, "values", []) or []),
        typed_values=_optional_list(getattr(row, "typed_values", None)),
        action=_normalize_action(str(getattr(row, "action", "write") or "write"), is_error_row=is_error_row),
        page_index=_coerce_page_index(getattr(row, "page_index", None)),
        ocr_confidence=_optional_float(getattr(row, "ocr_confidence", None)),
        is_error_row=is_error_row,
        extraction_classification=str(getattr(row, "extraction_classification", "UNCERTAIN") or "UNCERTAIN"),
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_list(value: object) -> list[object] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return list(value)
    return None


def _coerce_page_index(raw_value: object) -> int | None:
    if isinstance(raw_value, int):
        return raw_value if raw_value > 0 else None
    if isinstance(raw_value, str) and raw_value.strip().isdigit():
        value = int(raw_value.strip())
        return value if value > 0 else None
    return None


def _normalize_action(action: str, *, is_error_row: bool) -> str:
    if is_error_row:
        return "skip"
    return action if action in {"write", "skip"} else "write"


def _row_with_action(row: ResultReviewRow, action: str) -> ResultReviewRow:
    if row.is_error_row:
        return row
    return ResultReviewRow(
        row_id=row.row_id,
        task_id=row.task_id,
        values=list(row.values),
        typed_values=list(row.typed_values) if row.typed_values is not None else None,
        action=_normalize_action(action, is_error_row=row.is_error_row),
        page_index=row.page_index,
        ocr_confidence=row.ocr_confidence,
        is_error_row=row.is_error_row,
        extraction_classification=row.extraction_classification,
    )


def _group_rows_by_task(rows: Sequence[Mapping[str, object] | Any]) -> dict[str, list[Mapping[str, object] | Any]]:
    grouped_rows: dict[str, list[Mapping[str, object] | Any]] = {}
    for row in rows:
        task_id = _result_row_from_any(row).task_id
        grouped_rows.setdefault(task_id, []).append(row)
    return grouped_rows


def _display_task_status(status: str) -> str:
    mapping = {
        "pending": "等待中",
        "paused": "已暂停",
        "running_ocr": "OCR中",
        "running_extract": "抽取中",
        "done": "已完成",
        "success": "已完成",
        "completed": "已完成",
        "failed": "失败",
        "empty": "跳过",
    }
    return mapping.get(status, status)


def _task_progress_status_text(status: str) -> str:
    mapping = {
        "pending": "等待中",
        "paused": "已暂停",
        "running_ocr": "OCR中",
        "running_extract": "抽取中",
        "done": "已完成",
        "success": "已完成",
        "completed": "已完成",
        "failed": "失败",
        "empty": "跳过",
        "Pending": "等待中",
        "Paused": "已暂停",
        "Running OCR": "OCR中",
        "Running Extract": "抽取中",
        "Done": "已完成",
        "Failed": "失败",
        "Skipped": "跳过",
    }
    return mapping.get(status, status)


def _clamp_progress(progress: object) -> int:
    try:
        value = int(float(progress))
    except (TypeError, ValueError):
        value = 0
    return max(0, min(100, value))


def _task_progress_cell_text(status: str, progress: object) -> str:
    percent = 100 if status == "done" else _clamp_progress(progress)
    return f"{_task_progress_status_text(status)}\n{percent}%"


def _task_source_full_value(task: Any) -> str:
    return str(getattr(task, "source_path", "") or getattr(task, "source_value", "") or "")


def _task_source_display_text(task: Any) -> str:
    source_type = str(getattr(task, "source_type", "") or "")
    full_value = _task_source_full_value(task)
    if source_type == "text":
        body = full_value
        if len(body) > TASK_SOURCE_DISPLAY_PREFIX_CHARS:
            return f"文本:{body[:TASK_SOURCE_DISPLAY_PREFIX_CHARS]}…"
        return f"文本:{body}"
    basename = Path(full_value).name or full_value
    path_value = Path(basename)
    suffix = path_value.suffix
    stem = basename[: -len(suffix)] if suffix else basename
    if len(stem) > TASK_SOURCE_DISPLAY_PREFIX_CHARS:
        return f"{stem[:TASK_SOURCE_DISPLAY_PREFIX_CHARS]}…{suffix}"
    return basename


def _task_page_count(task: Any, ocr_text_by_page: Mapping[tuple[str, int], str]) -> int:
    task_id = str(getattr(task, "task_id", "") or "")
    page_count = int(getattr(task, "pdf_total_pages", 0) or 0)
    page_count = max(page_count, len(list(getattr(task, "pdf_page_ocr_snapshots", []) or [])))
    page_count = max(page_count, len(list(getattr(task, "pdf_page_results", []) or [])))
    cached_page_indexes = [page_index for cached_task_id, page_index in ocr_text_by_page.keys() if cached_task_id == task_id]
    if cached_page_indexes:
        page_count = max(page_count, max(cached_page_indexes))
    return page_count


def _task_has_page_children(task: Any, ocr_text_by_page: Mapping[tuple[str, int], str]) -> bool:
    return str(getattr(task, "source_type", "")) == "pdf" and _task_page_count(task, ocr_text_by_page) > 1


def _task_page_snapshot(task: Any, page_index: int) -> Any | None:
    for snapshot in list(getattr(task, "pdf_page_ocr_snapshots", []) or []):
        if _coerce_page_index(getattr(snapshot, "page_index", None)) == page_index:
            return snapshot
    return None


def _task_page_result(task: Any, page_index: int) -> Any | None:
    for page_result in list(getattr(task, "pdf_page_results", []) or []):
        if _coerce_page_index(getattr(page_result, "page_index", None)) == page_index:
            return page_result
    return None


def _task_page_exists(task: Any, page_index: int, ocr_text_by_page: Mapping[tuple[str, int], str]) -> bool:
    return 1 <= page_index <= max(_task_page_count(task, ocr_text_by_page), 0)


def _task_page_status_payload(task: Any, page_index: int) -> tuple[str, str, str]:
    page_result = _task_page_result(task, page_index)
    if page_result is not None:
        status_value = str(getattr(page_result, "status", "") or "")
        error_code = str(getattr(page_result, "error_code", "") or "")
        return _display_task_status(status_value), "100", error_code
    if _task_page_snapshot(task, page_index) is not None:
        status_value = "running_extract" if str(getattr(task, "status", "")) == "running_extract" else "pending"
        return _display_task_status(status_value), "0", ""
    return _display_task_status("pending"), "0", ""


def _retry_status_text(*, triggered: bool, applied: bool) -> str:
    if not triggered:
        return "-"
    return "本页已重试 (采纳)" if applied else "本页已重试 (未采纳)"


def _inspector_tip_for_status(status: str) -> str:
    if status == "failed":
        return "建议：修复配置或输入后，使用「重试失败」重试该任务。"
    if status == "pending":
        return "建议：点击全部开始开始识别。"
    if status == "paused":
        return "建议：点击全部开始或行内继续恢复任务处理。"
    if status == "done":
        return "建议：在结果表审核 action 后执行「写入 Excel」。"
    return "任务处理中，请观察状态与日志。"


def _find_task_row_index(rows: Sequence[TaskQueueRow], selection: TaskReviewSelection) -> int | None:
    if selection.task_id is None:
        return None
    for index, row in enumerate(rows):
        if row.task_id == selection.task_id and row.page_index == selection.page_index:
            return index
    return None


def _find_result_row_index(rows: Sequence[ResultReviewRow], row_id: str | None) -> int | None:
    if row_id is None:
        return None
    for index, row in enumerate(rows):
        if row.row_id == row_id:
            return index
    return None
