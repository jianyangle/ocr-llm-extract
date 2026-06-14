from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

from src.domain.schemas import AppConfig
from src.extract.errors import ExtractServiceError
from src.extract.prompt_builder import PromptTemplate
from src.extract.provider_catalog import (
    PROVIDER_PLATFORM_IDS,
    ProviderProfile,
    catalog_default_profiles,
    coerce_provider_platform_id,
    get_provider_entry,
    profile_from_mapping,
    profile_with_catalog_default,
    profiles_as_dict,
    runtime_provider_for_platform,
)
from src.extract.template_catalog import TemplateCatalog, project_active_template_config
from src.ocr.online_catalog import (
    DEFAULT_ONLINE_OCR_PLATFORM_ID,
    catalog_default_online_profiles,
)


_RUNTIME_PROVIDER_KEYS = ("openai_compatible", "ollama")
_GROUNDING_MODES = ("off", "balanced", "strict")


@dataclass(frozen=True)
class SettingsFormData:
    provider: str
    provider_platform_id: str
    base_url: str
    api_key: str
    model: str
    prompts: str
    raw_examples: str
    grounding_mode: str
    ocr_layout_parser: str
    ocr_layout_parser_user_edited: bool
    ocr_profile: str
    extraction_profile: str
    ollama_num_ctx: int
    allow_thinking: bool = False
    provider_profiles: dict[str, dict[str, str]] = field(default_factory=dict)
    ocr_use_online: bool = False
    ocr_online_platform_id: str = DEFAULT_ONLINE_OCR_PLATFORM_ID
    ocr_online_profiles: dict[str, dict[str, str]] = field(default_factory=dict)
    ocr_online_use_table_recognition: bool = False
    ocr_online_use_formula_recognition: bool = False
    ocr_online_use_chart_recognition: bool = False
    ocr_online_use_seal_recognition: bool = False


def build_validated_config(
    form: SettingsFormData,
    *,
    format_examples: Callable[[str], list[list[str]]],
    base_config: AppConfig,
) -> tuple[bool, AppConfig | str]:
    provider_platform_id = coerce_provider_platform_id(
        form.provider_platform_id,
        runtime_provider=form.provider,
    )
    runtime_provider = runtime_provider_for_platform(provider_platform_id)
    current_profile = _normalize_current_provider_profile(form, provider_platform_id)
    if runtime_provider not in _RUNTIME_PROVIDER_KEYS:
        return False, "provider: unsupported value."
    if not is_valid_http_url(current_profile["base_url"]):
        return False, "base_url: must be a valid http(s) URL."
    if not current_profile["model"]:
        return False, "model: required."
    if not form.prompts:
        return False, "prompts: required."
    if not form.raw_examples:
        return False, "examples_raw: required."
    if runtime_provider == "openai_compatible" and get_provider_entry(provider_platform_id).requires_api_key and not form.api_key:
        return False, "api_key: required for openai_compatible."
    if form.ocr_profile not in ("fast", "balanced", "accurate"):
        return False, "ocr_profile: must be fast/balanced/accurate."
    if form.extraction_profile not in ("fast", "balanced", "accurate"):
        return False, "extraction_profile: must be fast/balanced/accurate."
    if form.grounding_mode not in _GROUNDING_MODES:
        return False, "grounding_mode: must be off/balanced/strict."
    if form.ocr_layout_parser_user_edited and form.ocr_layout_parser not in ("single_line", "multi_para", "none"):
        return False, "ocr_layout_parser: must be single_line/multi_para/none."
    try:
        ollama_num_ctx = int(form.ollama_num_ctx)
    except (TypeError, ValueError):
        return False, "ollama_num_ctx: must be integer in [2048, 32768]."
    if not 2048 <= ollama_num_ctx <= 32768:
        return False, "ollama_num_ctx: must be integer in [2048, 32768]."

    online_ocr_profiles = _merge_online_ocr_profiles(form.ocr_online_profiles)
    if form.ocr_use_online:
        active_online_profile = online_ocr_profiles.get(form.ocr_online_platform_id, {})
        if not is_valid_http_url(active_online_profile.get("base_url", "")):
            return False, "ocr_online_base_url: must be a valid http(s) URL."
        if not active_online_profile.get("api_key"):
            return False, "ocr_online_api_key: required when online OCR is enabled."

    next_layout_parser = base_config.ocr_layout_parser
    if form.ocr_layout_parser_user_edited:
        next_layout_parser = form.ocr_layout_parser

    provider_profiles = _merge_provider_profiles(
        base_config.provider_profiles,
        form.provider_profiles,
    )
    provider_profiles[provider_platform_id] = current_profile

    try:
        normalized_examples = format_examples(form.raw_examples)
    except Exception as exc:
        return False, f"examples_raw: {exc}"
    try:
        _validate_template_catalog(base_config)
    except ExtractServiceError as exc:
        return False, f"{exc.code}: {exc.message}"

    config = AppConfig(
        provider=runtime_provider,  # type: ignore[arg-type]
        provider_platform_id=provider_platform_id,
        base_url=current_profile["base_url"],
        api_key=current_profile["api_key"],
        model=current_profile["model"],
        prompts=form.prompts,
        examples_raw=form.raw_examples,
        examples_normalized=normalized_examples,
        provider_profiles=provider_profiles,
        templates=list(base_config.templates),
        active_template_id=base_config.active_template_id,
        default_excel_path=base_config.default_excel_path,
        extraction_profile=form.extraction_profile,  # type: ignore[arg-type]
        extraction_passes=base_config.extraction_passes,
        extraction_max_char_buffer=base_config.extraction_max_char_buffer,
        extraction_passes_increment=base_config.extraction_passes_increment,
        extraction_parse_mode=base_config.extraction_parse_mode,
        use_structured_output=base_config.use_structured_output,
        llm_seed=base_config.llm_seed,
        llm_prompt_cache_enabled=base_config.llm_prompt_cache_enabled,
        allow_thinking=form.allow_thinking,
        ollama_num_ctx=ollama_num_ctx,
        grounding_fuzzy_threshold=base_config.grounding_fuzzy_threshold,
        grounding_mode=form.grounding_mode,
        ocr_profile=form.ocr_profile,  # type: ignore[arg-type]
        ocr_use_textline_orientation=base_config.ocr_use_textline_orientation,
        ocr_use_doc_orientation_classify=base_config.ocr_use_doc_orientation_classify,
        ocr_use_doc_unwarping=False,
        ocr_text_det_limit_side_len=base_config.ocr_text_det_limit_side_len,
        ocr_text_det_thresh=base_config.ocr_text_det_thresh,
        ocr_layout_parser=next_layout_parser,
        ocr_restore_paragraphs=base_config.ocr_restore_paragraphs,
        ocr_ignore_areas=list(base_config.ocr_ignore_areas),
        ocr_adaptive_retry_enabled=base_config.ocr_adaptive_retry_enabled,
        ocr_retry_confidence_threshold=base_config.ocr_retry_confidence_threshold,
        ocr_retry_target_profile=base_config.ocr_retry_target_profile,
        ocr_retry_low_block_count_min=base_config.ocr_retry_low_block_count_min,
        ocr_retry_avg_threshold=base_config.ocr_retry_avg_threshold,
        ocr_retry_min_improvement=base_config.ocr_retry_min_improvement,
        ocr_retry_max_block_drop=base_config.ocr_retry_max_block_drop,
        pdf_max_pages=base_config.pdf_max_pages,
        pdf_max_file_size=base_config.pdf_max_file_size,
        pdf_render_dpi=base_config.pdf_render_dpi,
        pdf_page_render_parallelism=base_config.pdf_page_render_parallelism,
        pdf_prefer_text_layer=base_config.pdf_prefer_text_layer,
        pdf_text_layer_min_chars=base_config.pdf_text_layer_min_chars,
        pdf_text_layer_completeness_ocr=base_config.pdf_text_layer_completeness_ocr,
        pdf_retry_budget=base_config.pdf_retry_budget,
        pdf_retry_unimproved_stop=base_config.pdf_retry_unimproved_stop,
        region_rescue_max_per_task=base_config.region_rescue_max_per_task,
        ocr_use_online=form.ocr_use_online,
        ocr_online_platform_id=form.ocr_online_platform_id,
        ocr_online_profiles=online_ocr_profiles,
        ocr_online_use_table_recognition=form.ocr_online_use_table_recognition,
        ocr_online_use_formula_recognition=form.ocr_online_use_formula_recognition,
        ocr_online_use_chart_recognition=form.ocr_online_use_chart_recognition,
        ocr_online_use_seal_recognition=form.ocr_online_use_seal_recognition,
    )
    projected = project_active_template_config(config)
    projected.templates = list(base_config.templates)
    return True, projected


def is_valid_http_url(value: str) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        return False
    return bool(parsed.netloc)


def _default_provider_profiles() -> dict[str, dict[str, str]]:
    return profiles_as_dict(catalog_default_profiles())


def _merge_provider_profiles(
    base_profiles: dict[str, dict[str, str]],
    form_profiles: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    profiles = catalog_default_profiles()
    for source in (base_profiles, form_profiles):
        if not isinstance(source, dict):
            continue
        for platform_id in PROVIDER_PLATFORM_IDS:
            profiles[platform_id] = profile_from_mapping(source.get(platform_id), profiles[platform_id])
    return profiles_as_dict(
        {
            platform_id: profile_with_catalog_default(platform_id, profile)
            for platform_id, profile in profiles.items()
        }
    )


def _merge_online_ocr_profiles(
    form_profiles: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    profiles = catalog_default_online_profiles()
    if not isinstance(form_profiles, dict):
        return profiles
    for platform_id, entry in profiles.items():
        provided = form_profiles.get(platform_id)
        if not isinstance(provided, dict):
            continue
        for key in ("base_url", "model"):
            if key in provided:
                entry[key] = str(provided[key]).strip()
        if "api_key" in provided:
            entry["api_key"] = str(provided["api_key"])
    return profiles


def _normalize_current_provider_profile(form: SettingsFormData, provider_platform_id: str) -> dict[str, str]:
    profile = ProviderProfile(
        base_url=form.base_url.strip(),
        api_key=form.api_key,
        model=form.model.strip(),
    )
    return profile_with_catalog_default(provider_platform_id, profile).as_dict()


def sanitize_positive_int(value: object, *, fallback: int, minimum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed >= minimum else fallback


def sanitize_nonnegative_int(value: object, *, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed >= 0 else fallback


def normalize_parse_mode(value: object) -> str:
    mode = str(value).strip()
    if mode in ("strict", "balanced", "aggressive"):
        return mode
    return "balanced"


def normalize_ocr_profile(value: object) -> str:
    profile = str(value).strip().lower()
    if profile in ("fast", "balanced", "accurate"):
        return profile
    return "balanced"


def normalize_extraction_profile(value: object) -> str:
    profile = str(value).strip().lower()
    if profile in ("fast", "balanced", "accurate"):
        return profile
    return "balanced"


def normalize_layout_parser(value: object) -> str:
    if value is None:
        return "auto"
    parser = str(value).strip().lower()
    if not parser:
        return "auto"
    if parser in (
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
    ):
        return parser
    return "auto"


def sanitize_unit_float(value: object, *, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    if 0.0 < parsed <= 1.0:
        return parsed
    return fallback


def parse_ignore_areas_json(value: str) -> tuple[bool, list[list[float]] | str]:
    text = value.strip()
    if not text:
        return True, []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False, "ocr_ignore_areas: must be valid JSON list."
    if not isinstance(payload, list):
        return False, "ocr_ignore_areas: must be a JSON list."

    output: list[list[float]] = []
    for item in payload:
        if not isinstance(item, list) or len(item) < 4:
            return False, "ocr_ignore_areas: each area must be [x1,y1,x2,y2]."
        x1, y1, x2, y2 = item[0], item[1], item[2], item[3]
        if not all(isinstance(v, (int, float)) for v in (x1, y1, x2, y2)):
            return False, "ocr_ignore_areas: coordinates must be numeric."
        output.append([float(x1), float(y1), float(x2), float(y2)])
    return True, output


def _validate_templates(templates: list[PromptTemplate], normalized_examples: list[list[str]]) -> None:
    fallback_header = normalized_examples[0] if normalized_examples else []
    for template in templates:
        header = template.examples[0] if template.examples else fallback_header
        if template.columns:
            if len(template.columns) != len(header):
                raise ExtractServiceError("E_TMPL_002", f"template {template.name}: columns/header length mismatch")
            for column, expected_name in zip(template.columns, header):
                if column.name != expected_name:
                    raise ExtractServiceError("E_TMPL_002", f"template {template.name}: column header mismatch")
                if column.type == "date" and not column.date_formats:
                    raise ExtractServiceError("E_TMPL_003", f"template {template.name}: date column missing formats")
        if template.line_rules is not None:
            _validate_line_rules(template)
        for region in template.field_regions:
            if not (0.0 <= region.left <= 1.0 and 0.0 <= region.top <= 1.0 and 0.0 <= region.right <= 1.0 and 0.0 <= region.bottom <= 1.0):
                raise ExtractServiceError("E_TMPL_005", f"template {template.name}: field region out of range")
            if region.right <= region.left or region.bottom <= region.top:
                raise ExtractServiceError("E_TMPL_005", f"template {template.name}: field region bounds invalid")


def _validate_line_rules(template: PromptTemplate) -> None:
    assert template.line_rules is not None
    for attr in ("start", "end", "line", "first_line", "last_line", "skip_line"):
        pattern = getattr(template.line_rules, attr)
        if pattern is None:
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ExtractServiceError("E_TMPL_004", f"template {template.name}: invalid regex for {attr}") from exc
    if not re.compile(template.line_rules.line).groupindex:
        raise ExtractServiceError("E_TMPL_004", f"template {template.name}: line regex requires named groups")


def _validate_template_catalog(config: AppConfig) -> None:
    raw_templates = list(getattr(config, "templates", []))
    active_template_id = getattr(config, "active_template_id", None)
    if raw_templates and isinstance(raw_templates[0], PromptTemplate):
        normalized_examples = getattr(config, "examples_normalized", [])
        _validate_templates(raw_templates, normalized_examples)
        return

    catalog = TemplateCatalog.load(raw_templates, active_template_id)
    if not isinstance(raw_templates, list):
        raw_templates = []
    seen_ids: set[str] = set()
    for item in raw_templates:
        if not isinstance(item, dict):
            raise ExtractServiceError("E_TMPL_006", "模板条目必须是对象")
        template_id = str(item.get("id", "")).strip()
        if not template_id or template_id in seen_ids:
            raise ExtractServiceError("E_TMPL_006", "模板 id 缺失或重复")
        seen_ids.add(template_id)
        kind = str(item.get("kind", "")).strip()
        if kind == "user":
            result = catalog.validate_draft(
                name=str(item.get("name", "")),
                prompts=str(item.get("prompts", "")),
                examples=item.get("examples", []),
                exclude_id=template_id,
            )
            if not result.ok:
                raise ExtractServiceError("E_TMPL_006", f"{str(item.get('name', template_id))}: {result.reason}")
        elif kind == "builtin_override":
            template = catalog.template_by_id(template_id)
            result = catalog.validate_draft(
                name=str(item.get("name", template.name)),
                prompts=str(item.get("prompts", template.description)),
                examples=item.get("examples", template.examples),
                exclude_id=template_id,
            )
            if not result.ok:
                raise ExtractServiceError("E_TMPL_006", f"{template.name}: {result.reason}")
        else:
            raise ExtractServiceError("E_TMPL_006", "模板 kind 非法")
