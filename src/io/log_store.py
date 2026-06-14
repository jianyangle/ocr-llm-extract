from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.core.engine_events import EngineEvent
from src.domain.schemas import ExtractRow, PDFPageExtractResult


class LogStore:
    def __init__(self, log_dir: str | Path | None = None) -> None:
        self.log_dir = Path(log_dir) if log_dir else Path.home() / ".ocr_extract_app" / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def log_event(self, event: EngineEvent) -> Path:
        return self.log_record(type(event).code, _event_payload(event))

    def log_record(self, event_type: str, payload: dict[str, Any]) -> Path:
        now = datetime.now(UTC)
        log_path = self.log_dir / f"{now.date().isoformat()}.log"
        record = {
            "timestamp": now.isoformat(),
            "event_type": event_type,
            "payload": _mask_sensitive(payload),
        }
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return log_path


def _event_payload(event: EngineEvent) -> dict[str, Any]:
    payload = asdict(event)
    rows = getattr(event, "rows", None)
    if isinstance(rows, list):
        payload.pop("rows", None)
        payload["result_rows"] = [_serialize_row(row) for row in rows if isinstance(row, ExtractRow)]
    page_results = getattr(event, "page_results", None)
    if isinstance(page_results, list):
        payload["page_results"] = [
            _serialize_pdf_page_result(result) if isinstance(result, PDFPageExtractResult) else result
            for result in page_results
        ]
    page_result = getattr(event, "page_result", None)
    if isinstance(page_result, PDFPageExtractResult):
        payload["page_result"] = _serialize_pdf_page_result(page_result)
    page_snapshots = payload.get("page_snapshots")
    if isinstance(page_snapshots, list):
        # OCR blocks(含多边形坐标)不写入日志:多页 PDF 会使每条事件膨胀到数百 KB。
        for snapshot in page_snapshots:
            if isinstance(snapshot, dict):
                snapshot.pop("blocks", None)
    return payload


def _serialize_row(row: ExtractRow) -> dict[str, Any]:
    return {
        "row_id": row.row_id,
        "task_id": row.task_id,
        "values": list(row.values),
        "action": row.action,
        "page_index": row.page_index,
        "ocr_confidence": row.ocr_confidence,
        "is_error_row": row.is_error_row,
    }


def _serialize_pdf_page_result(page_result: PDFPageExtractResult) -> dict[str, Any]:
    return {
        "page_index": page_result.page_index,
        "normalized_text": page_result.normalized_text,
        "status": page_result.status,
        "row_count": page_result.row_count,
        "ocr_confidence": page_result.ocr_confidence,
        "error_code": page_result.error_code,
        "error_message": page_result.error_message,
    }


def _mask_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        masked: dict[str, Any] = {}
        for key, item in value.items():
            key_lower = key.lower()
            if "api_key" in key_lower or "authorization" in key_lower:
                masked[key] = "****"
            else:
                masked[key] = _mask_sensitive(item)
        return masked
    if isinstance(value, list):
        return [_mask_sensitive(item) for item in value]
    return value
