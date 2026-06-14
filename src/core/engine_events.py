from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from src.domain.schemas import ExtractRow, PDFPageExtractResult, PDFPageOCRSnapshot, TaskItem

_ = TaskItem


@dataclass(frozen=True)
class TaskStarted:
    task_id: str
    source_type: str

    code: ClassVar[str] = "EVT-TASK-001"


@dataclass(frozen=True)
class TaskOcrCompleted:
    task_id: str
    normalized_text: str
    ocr_confidence: float | None
    page_snapshots: list[PDFPageOCRSnapshot]
    active_template_name: str | None
    region_rescue: list[dict[str, Any]]
    block_count: int = 0
    confidence_min: float | None = None
    pdf_page_count: int | None = None
    adaptive_retry_triggered: bool = False
    adaptive_retry_applied: bool = False
    retry_profile_from: str | None = None
    retry_profile_to: str | None = None
    first_pass_confidence_min: float | None = None
    second_pass_confidence_min: float | None = None
    markdown: str | None = None

    code: ClassVar[str] = "EVT-TASK-002"


@dataclass(frozen=True)
class TaskProgressed:
    task_id: str
    status: str
    row_count: int
    normalized_text: str
    rows: list[ExtractRow]
    page_results: list[PDFPageExtractResult]

    code: ClassVar[str] = "EVT-TASK-003"


@dataclass(frozen=True)
class TaskSucceeded:
    task_id: str
    status: str
    row_count: int
    latency_ms: int
    normalized_text: str
    rows: list[ExtractRow]
    page_results: list[PDFPageExtractResult] | None

    code: ClassVar[str] = "EVT-TASK-003"


@dataclass(frozen=True)
class TaskFailed:
    task_id: str
    status: str
    error_code: str | None
    error_message: str | None
    stage: str
    row_count: int
    normalized_text: str
    rows: list[ExtractRow]
    page_results: list[PDFPageExtractResult] | None

    code: ClassVar[str] = "EVT-TASK-004"


@dataclass(frozen=True)
class TaskAutoDispatchTriggered:
    task_id: str
    source_type: str
    trigger_mode: str = "auto_dispatch_while_running"

    code: ClassVar[str] = "EVT-TASK-005"


@dataclass(frozen=True)
class TaskPageResultStreamed:
    task_id: str
    page_index: int
    page_text: str
    aggregated_text: str
    status: str
    row_count: int
    error_code: str | None
    error_message: str | None
    rows: list[ExtractRow]
    page_result: PDFPageExtractResult
    region_rescue: list[dict[str, Any]] = field(default_factory=list)

    code: ClassVar[str] = "EVT-TASK-006"


@dataclass(frozen=True)
class TaskOcrProgressed:
    """在线 OCR 整份 job 轮询期间的页级进度心跳（驱动 UI 进度，避免误判卡死）。"""

    task_id: str
    processed_pages: int
    total_pages: int

    code: ClassVar[str] = "EVT-TASK-007"


EngineEvent = (
    TaskStarted
    | TaskOcrCompleted
    | TaskProgressed
    | TaskOcrProgressed
    | TaskSucceeded
    | TaskFailed
    | TaskAutoDispatchTriggered
    | TaskPageResultStreamed
)
