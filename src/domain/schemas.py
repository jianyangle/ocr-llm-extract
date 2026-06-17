from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Callable, Literal
from uuid import uuid4

if TYPE_CHECKING:
    from PIL import Image
    from src.ocr.models import OCRTextBlock


@dataclass
class AppConfig:
    provider: Literal["openai_compatible", "ollama"]
    base_url: str
    api_key: str
    model: str
    prompts: str
    examples_raw: str
    provider_platform_id: str = "custom"
    provider_profiles: dict[str, dict[str, str]] = field(default_factory=dict)
    examples_normalized: list[list[str]] = field(default_factory=list)
    templates: list[dict[str, Any]] = field(default_factory=list)
    active_template_id: str | None = None
    default_excel_path: str = ""
    extraction_profile: Literal["fast", "balanced", "accurate"] = "balanced"
    extraction_passes: int = 2
    extraction_max_char_buffer: int = 2200
    extraction_passes_increment: int = 800
    extraction_parse_mode: Literal["strict", "balanced", "aggressive"] = "balanced"
    use_structured_output: bool = True
    llm_seed: int | None = None
    llm_prompt_cache_enabled: bool = False
    allow_thinking: bool = False
    ollama_num_ctx: int = 8192
    ollama_overrides: dict[str, Any] = field(default_factory=dict)
    extraction_system_prompt: str = ""
    grounding_fuzzy_threshold: float = 0.75
    grounding_mode: Literal["off", "balanced", "strict"] = "off"
    ocr_profile: Literal["fast", "balanced", "accurate"] = "balanced"
    ocr_use_textline_orientation: bool = True
    ocr_use_doc_orientation_classify: bool = True
    ocr_use_doc_unwarping: bool = False
    ocr_text_det_limit_side_len: int = 960
    ocr_text_det_thresh: float = 0.3
    ocr_layout_parser: Literal[
        "auto",
        "single_column",
        "multi_column",
        "none",
        "multi_none",
        "multi_line",
        "multi_para",
        "single_none",
        "single_line",
        "single_para",
        "single_code",
    ] = "multi_para"
    ocr_restore_paragraphs: bool = True
    ocr_ignore_areas: list[list[float]] = field(default_factory=list)
    ocr_adaptive_retry_enabled: bool = True
    ocr_retry_confidence_threshold: float = 0.55
    ocr_retry_target_profile: Literal["fast", "balanced", "accurate"] = "accurate"
    ocr_retry_low_block_count_min: int = 3
    ocr_retry_avg_threshold: float = 0.55
    ocr_retry_min_improvement: float = 0.03
    ocr_retry_max_block_drop: int = 1
    ocr_use_online: bool = False
    ocr_online_platform_id: str = "baidu_paddle"
    ocr_online_profiles: dict[str, dict[str, str]] = field(default_factory=dict)
    ocr_online_use_table_recognition: bool = False
    ocr_online_use_formula_recognition: bool = False
    ocr_online_use_chart_recognition: bool = False
    ocr_online_use_seal_recognition: bool = False
    pdf_max_pages: int = 30
    pdf_max_file_size: int = 20 * 1024 * 1024
    pdf_render_dpi: int = 200
    pdf_page_render_parallelism: int = 2
    pdf_prefer_text_layer: bool = True
    pdf_text_layer_min_chars: int = 40
    pdf_text_layer_completeness_ocr: bool = True
    pdf_text_layer_attribution_ocr: bool = True
    pdf_retry_budget: int = 8
    pdf_retry_unimproved_stop: int = 2
    region_rescue_max_per_task: int = 5
    extraction_profile_custom: bool = field(init=False, default=False, repr=False)
    ocr_profile_custom: bool = field(init=False, default=False, repr=False)


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    type: Literal["string", "number", "date", "phone", "email", "company"] = "string"
    decimal_separator: Literal[".", ","] = "."
    thousands_separator: str = ""
    currency_strip: bool = True
    date_formats: tuple[str, ...] = ()
    nullable_placeholder: str = " "


@dataclass(frozen=True)
class LineRules:
    start: str
    end: str
    line: str
    first_line: str | None = None
    last_line: str | None = None
    skip_line: str | None = None
    repeating_field_from_parent: tuple[str, ...] = ()


@dataclass(frozen=True)
class FieldRegion:
    field_name: str
    left: float
    top: float
    right: float
    bottom: float


@dataclass(frozen=True)
class FieldGroup:
    name: str
    field_names: tuple[str, ...]
    anchor_keywords: tuple[str, ...]


TaskSourceType = Literal["text", "image", "pdf"]
TaskStatus = Literal["pending", "paused", "running_ocr", "running_extract", "done", "failed"]
RowAction = Literal["write", "skip"]
AlignmentStatus = Literal["ASSIGNED", "ASSIGNED_PARTIAL", "INFERRED", "INFERRED_FUZZY", "UNCERTAIN"]
PDFPageExtractStatus = Literal["success", "empty", "failed"]
PageErrorPhase = Literal["render", "ocr", "extract"]
BBox = tuple[int, int, int, int]


@dataclass
class GroundedCell:
    value: str
    source_text: str = ""
    start: int | None = None
    end: int | None = None
    status: AlignmentStatus = "UNCERTAIN"


@dataclass
class GroundedExtractRow:
    values: list[str]
    cells: list[GroundedCell] = field(default_factory=list)
    classification: AlignmentStatus = "UNCERTAIN"


@dataclass(frozen=True)
class ExtractionOutcome:
    rows: list[GroundedExtractRow]
    column_specs: list[ColumnSpec]
    field_regions: list[FieldRegion]
    field_groups: tuple[FieldGroup, ...] = ()
    exclusive_group_pairs: tuple[tuple[str, str], ...] = ()


@dataclass
class PDFPageOCRSnapshot:
    page_index: int
    normalized_text: str
    image_path: str | None = None
    ocr_confidence: float | None = None
    confidence_min: float | None = None
    block_count: int = 0
    blocks: list["OCRTextBlock"] = field(default_factory=list)
    adaptive_retry_triggered: bool = False
    adaptive_retry_applied: bool = False
    retry_profile_from: str | None = None
    retry_profile_to: str | None = None
    first_pass_confidence_min: float | None = None
    second_pass_confidence_min: float | None = None
    source_path: Literal["text_layer", "ocr", "text_layer+ocr", "text_layer+region_ocr"] = "ocr"
    markdown_text: str | None = None


@dataclass(frozen=True)
class ExtractionInput:
    """一次抽取调用的双格式输入单元（CONTEXT.md: Extraction Input）。

    PDF 任务为一页、图片/文本任务为整文档；flat_text 是 grounding/去重的源文本，
    markdown 存在时（Markdown OCR Text）作为 LLM 输入。from_text 原样包装，
    不添加任何标记——本地路径行为零变化的前提。
    """

    flat_text: str
    markdown: str | None = None

    @classmethod
    def from_text(cls, text: str) -> "ExtractionInput":
        return cls(flat_text=text, markdown=None)

    @property
    def extraction_text(self) -> str:
        return self.markdown if self.markdown else self.flat_text

    @property
    def has_markdown(self) -> bool:
        return bool(self.markdown)


@dataclass
class PDFPageExtractResult:
    page_index: int
    normalized_text: str
    status: PDFPageExtractStatus
    row_count: int = 0
    ocr_confidence: float | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class PageError:
    code: str
    message: str
    phase: PageErrorPhase


@dataclass(frozen=True)
class PDFPageWorkUnit:
    page_index: int
    snapshot: PDFPageOCRSnapshot | None
    outcome: ExtractionOutcome | None
    error: PageError | None
    crop: Callable[[BBox], "Image.Image"] | None

    @property
    def rows(self) -> list[GroundedExtractRow]:
        return list(self.outcome.rows) if self.outcome is not None else []


@dataclass(frozen=True)
class PDFLimits:
    max_pages: int
    max_file_size: int
    render_dpi: int
    page_render_parallelism: int = 2
    prefer_text_layer: bool = True
    text_layer_min_chars: int = 40
    text_layer_completeness_ocr: bool = True


@dataclass(frozen=True)
class ExtractionOptions:
    prompts: str
    examples_normalized: list[list[str]]
    provider: Literal["openai_compatible", "ollama"] = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    templates: list[dict[str, Any]] = field(default_factory=list)
    active_template_id: str | None = None
    extraction_passes: int = 2
    extraction_max_char_buffer: int = 2200
    extraction_passes_increment: int = 800
    extraction_parse_mode: Literal["strict", "balanced", "aggressive"] = "balanced"
    use_structured_output: bool = True
    llm_seed: int | None = None
    llm_prompt_cache_enabled: bool = False
    ollama_num_ctx: int = 8192
    grounding_fuzzy_threshold: float = 0.75
    grounding_mode: Literal["off", "balanced", "strict"] = "off"


@dataclass
class TaskItem:
    source_type: TaskSourceType
    source_value: str
    source_path: str | None = None
    display_name: str | None = None
    ocr_text: str = ""
    task_id: str = field(default_factory=lambda: str(uuid4()))
    status: TaskStatus = "pending"
    progress: int = 0
    error_code: str | None = None
    error_message: str | None = None
    pdf_total_pages: int | None = None
    pdf_processed_pages: int = 0
    pdf_page_ocr_snapshots: list[PDFPageOCRSnapshot] = field(default_factory=list)
    pdf_page_results: list[PDFPageExtractResult] = field(default_factory=list)
    auto_dispatch_while_running: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass
class ExtractRow:
    task_id: str
    values: list[str]
    typed_values: list[object] | None = None
    action: RowAction = "write"
    page_index: int | None = None
    ocr_confidence: float | None = None
    grounded_cells: list[GroundedCell] = field(default_factory=list)
    extraction_classification: AlignmentStatus = "UNCERTAIN"
    is_error_row: bool = False
    row_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass
class ItemResult:
    task_id: str
    rows: list[ExtractRow]
    normalized_text: str
    error_code: str | None = None
    error_message: str | None = None
    page_results: list[PDFPageExtractResult] = field(default_factory=list)


@dataclass
class WriteSummary:
    total_rows: int
    selected_rows: int
    written_rows: int
    skipped_rows: int
    failed_rows: int
    output_path: str
