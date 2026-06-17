from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from src.domain.schemas import AppConfig, ColumnSpec, FieldRegion, LineRules
from src.extract.example_parser import format_examples
from src.extract.llm_extractor import EXTRACTION_PROFILE_PRESETS
from src.extract.provider_catalog import (
    PROVIDER_PLATFORM_IDS,
    ProviderProfile,
    catalog_default_profiles,
    coerce_provider_platform_id,
    profile_from_mapping,
    profile_from_top_level,
    profile_with_catalog_default,
    profiles_as_dict,
    runtime_provider_for_platform,
)
from src.extract.template_catalog import BUILTIN_INVOICE_ID, TemplateCatalog
from src.ocr.online_catalog import DEFAULT_ONLINE_OCR_PLATFORM_ID, ONLINE_OCR_PLATFORM_IDS, catalog_default_online_profiles
from src.ocr.paddle_service import OCR_PROFILE_PRESETS

_logger = logging.getLogger(__name__)

_RUNTIME_DERIVED_FIELDS = ("extraction_profile_custom", "ocr_profile_custom")
_DEPRECATED_PERSISTED_FIELDS = ("ocr_retry_target_profile",)
_RUNTIME_PROVIDER_KEYS = ("openai_compatible", "ollama")
_PROVIDER_PROFILE_FIELDS = ("base_url", "api_key", "model")
_OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434"


class ConfigStore:
    def __init__(self, config_dir: str | Path | None = None) -> None:
        base_dir = Path(config_dir) if config_dir else Path.home() / ".ocr_extract_app"
        self.config_dir = base_dir
        self.config_path = self.config_dir / "config.json"

    def load(self) -> AppConfig:
        if not self.config_path.exists():
            return self.default_config()
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                _logger.warning("Config JSON root is not an object, fallback to defaults")
                return self.default_config()
            return self._from_dict(data)
        except json.JSONDecodeError as exc:
            _logger.warning("Config JSON parse failed, fallback to defaults: %s", exc)
            return self.default_config()
        except (OSError, ValueError, TypeError) as exc:
            _logger.warning("Config load failed, fallback to defaults: %s", exc)
            return self.default_config()

    def save(self, config: AppConfig) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self.config_path.with_suffix(".json.tmp")
        data = asdict(config)
        for runtime_field in _RUNTIME_DERIVED_FIELDS:
            data.pop(runtime_field, None)
        for deprecated_field in _DEPRECATED_PERSISTED_FIELDS:
            data.pop(deprecated_field, None)
        provider = _coerce_provider(data.get("provider", "openai_compatible"))
        provider_platform_id = coerce_provider_platform_id(
            data.get("provider_platform_id"),
            runtime_provider=provider,
        )
        provider = runtime_provider_for_platform(provider_platform_id)
        data["provider"] = provider
        data["provider_platform_id"] = provider_platform_id
        provider_profiles = _coerce_provider_profiles(data, provider, provider_platform_id)
        provider_profiles[provider_platform_id] = {
            "base_url": _string_field(data.get("base_url")),
            "api_key": _string_field(data.get("api_key")),
            "model": _string_field(data.get("model")),
        }
        data["provider_profiles"] = provider_profiles
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        tmp_path.write_text(payload, encoding="utf-8")
        tmp_path.replace(self.config_path)

    def save_with_examples_raw(self, config: AppConfig, raw_examples: str) -> None:
        normalized = format_examples(raw_examples)
        config.examples_raw = raw_examples
        config.examples_normalized = normalized
        self.save(config)

    @staticmethod
    def default_config() -> AppConfig:
        return AppConfig(
            provider="openai_compatible",
            base_url="",
            api_key="",
            model="",
            prompts="",
            examples_raw="",
            provider_platform_id="custom",
            provider_profiles=_default_provider_profiles(),
            examples_normalized=[["", ""]],
            templates=TemplateCatalog.load([]).serialize()["templates"],  # type: ignore[arg-type]
            active_template_id=BUILTIN_INVOICE_ID,
            default_excel_path="",
            extraction_profile="balanced",
            extraction_passes=2,
            extraction_max_char_buffer=2200,
            extraction_passes_increment=800,
            extraction_parse_mode="balanced",
            use_structured_output=True,
            llm_seed=None,
            llm_prompt_cache_enabled=False,
            allow_thinking=False,
            ollama_num_ctx=8192,
            grounding_fuzzy_threshold=0.75,
            grounding_mode="off",
            ocr_profile="balanced",
            ocr_use_textline_orientation=True,
            ocr_use_doc_orientation_classify=True,
            ocr_use_doc_unwarping=False,
            ocr_text_det_limit_side_len=960,
            ocr_text_det_thresh=0.3,
            ocr_layout_parser="multi_para",
            ocr_restore_paragraphs=True,
            ocr_ignore_areas=[],
            ocr_adaptive_retry_enabled=True,
            ocr_retry_confidence_threshold=0.55,
            ocr_retry_target_profile="accurate",
            ocr_retry_low_block_count_min=3,
            ocr_retry_avg_threshold=0.55,
            ocr_retry_min_improvement=0.03,
            ocr_retry_max_block_drop=1,
            ocr_use_online=False,
            ocr_online_platform_id=DEFAULT_ONLINE_OCR_PLATFORM_ID,
            ocr_online_profiles=catalog_default_online_profiles(),
            ocr_online_use_table_recognition=False,
            ocr_online_use_formula_recognition=False,
            ocr_online_use_chart_recognition=False,
            ocr_online_use_seal_recognition=False,
            pdf_max_pages=30,
            pdf_max_file_size=20 * 1024 * 1024,
            pdf_render_dpi=200,
            pdf_page_render_parallelism=2,
            pdf_prefer_text_layer=True,
            pdf_text_layer_min_chars=40,
            pdf_text_layer_completeness_ocr=True,
            pdf_text_layer_attribution_ocr=True,
            pdf_retry_budget=8,
            pdf_retry_unimproved_stop=2,
            region_rescue_max_per_task=5,
        )

    @staticmethod
    def _from_dict(data: dict[str, Any]) -> AppConfig:
        provider = _coerce_provider(data.get("provider", "openai_compatible"))
        provider_platform_id = coerce_provider_platform_id(
            data.get("provider_platform_id"),
            runtime_provider=provider,
        )
        provider = runtime_provider_for_platform(provider_platform_id)
        provider_profiles = _coerce_provider_profiles(data, provider, provider_platform_id)
        current_profile = provider_profiles[provider_platform_id]
        ocr_profile = _coerce_profile(data.get("ocr_profile"), fallback="balanced")
        extraction_profile = _coerce_profile(data.get("extraction_profile"), fallback="balanced")
        catalog = _load_template_catalog(data)
        config = AppConfig(
            provider=provider,  # type: ignore[arg-type]
            base_url=current_profile["base_url"],
            api_key=current_profile["api_key"],
            model=current_profile["model"],
            prompts=data.get("prompts", ""),
            examples_raw=data.get("examples_raw", ""),
            provider_platform_id=provider_platform_id,
            provider_profiles=provider_profiles,
            examples_normalized=data.get("examples_normalized", [["", ""]]),
            templates=list(catalog.serialize()["templates"]),  # type: ignore[arg-type]
            active_template_id=str(catalog.serialize()["active_template_id"]),
            default_excel_path=data.get("default_excel_path", ""),
            extraction_profile=extraction_profile,  # type: ignore[arg-type]
            extraction_passes=data.get("extraction_passes", 2),
            extraction_max_char_buffer=data.get("extraction_max_char_buffer", 2200),
            extraction_passes_increment=data.get("extraction_passes_increment", 800),
            extraction_parse_mode=data.get("extraction_parse_mode", "balanced"),
            use_structured_output=_coerce_bool(data.get("use_structured_output"), fallback=True),
            llm_seed=_coerce_optional_int(data.get("llm_seed")),
            llm_prompt_cache_enabled=_coerce_bool(data.get("llm_prompt_cache_enabled"), fallback=False),
            allow_thinking=_coerce_bool(data.get("allow_thinking"), fallback=False),
            ollama_num_ctx=_coerce_positive_int(data.get("ollama_num_ctx"), fallback=8192),
            ollama_overrides=_coerce_ollama_overrides(data.get("ollama_overrides")),
            extraction_system_prompt=str(data.get("extraction_system_prompt", "") or ""),
            grounding_fuzzy_threshold=_coerce_grounding_fuzzy_threshold(
                data.get("grounding_fuzzy_threshold"),
                fallback=0.75,
            ),
            grounding_mode=_coerce_grounding_mode(
                data.get("grounding_mode")
                if "grounding_mode" in data
                else ("strict" if _coerce_bool(data.get("require_grounding"), fallback=False) else "off"),
                fallback="off",
            ),
            ocr_profile=ocr_profile,  # type: ignore[arg-type]
            ocr_use_textline_orientation=_coerce_bool(data.get("ocr_use_textline_orientation"), fallback=True),
            ocr_use_doc_orientation_classify=_coerce_bool(data.get("ocr_use_doc_orientation_classify"), fallback=True),
            ocr_use_doc_unwarping=False,
            ocr_text_det_limit_side_len=_coerce_positive_int(data.get("ocr_text_det_limit_side_len"), fallback=960),
            ocr_text_det_thresh=_coerce_unit_float(data.get("ocr_text_det_thresh"), fallback=0.3),
            ocr_layout_parser=_coerce_layout_parser(data.get("ocr_layout_parser")),  # type: ignore[arg-type]
            ocr_restore_paragraphs=_coerce_bool(data.get("ocr_restore_paragraphs"), fallback=True),
            ocr_ignore_areas=_coerce_ignore_areas(data.get("ocr_ignore_areas")),
            ocr_adaptive_retry_enabled=_coerce_bool(data.get("ocr_adaptive_retry_enabled"), fallback=True),
            ocr_retry_confidence_threshold=_coerce_unit_float(data.get("ocr_retry_confidence_threshold"), fallback=0.55),
            ocr_retry_target_profile=_coerce_profile(data.get("ocr_retry_target_profile"), fallback="accurate"),  # type: ignore[arg-type]
            ocr_retry_low_block_count_min=_coerce_positive_int(data.get("ocr_retry_low_block_count_min"), fallback=3),
            ocr_retry_avg_threshold=_coerce_unit_float(data.get("ocr_retry_avg_threshold"), fallback=0.55),
            ocr_retry_min_improvement=_coerce_unit_float(data.get("ocr_retry_min_improvement"), fallback=0.03),
            ocr_retry_max_block_drop=_coerce_positive_int(data.get("ocr_retry_max_block_drop"), fallback=1),
            ocr_use_online=_coerce_bool(data.get("ocr_use_online"), fallback=False),
            ocr_online_platform_id=_coerce_online_platform_id(data.get("ocr_online_platform_id")),
            ocr_online_profiles=_coerce_online_ocr_profiles(data.get("ocr_online_profiles")),
            ocr_online_use_table_recognition=_coerce_bool(
                data.get("ocr_online_use_table_recognition"), fallback=False,
            ),
            ocr_online_use_formula_recognition=_coerce_bool(
                data.get("ocr_online_use_formula_recognition"), fallback=False,
            ),
            ocr_online_use_chart_recognition=_coerce_bool(
                data.get("ocr_online_use_chart_recognition"), fallback=False,
            ),
            ocr_online_use_seal_recognition=_coerce_bool(
                data.get("ocr_online_use_seal_recognition"), fallback=False,
            ),
            pdf_max_pages=_coerce_positive_int(data.get("pdf_max_pages"), fallback=30),
            pdf_max_file_size=_coerce_positive_int(data.get("pdf_max_file_size"), fallback=20 * 1024 * 1024),
            pdf_render_dpi=_coerce_positive_int(data.get("pdf_render_dpi"), fallback=200),
            pdf_page_render_parallelism=_clamp_int(
                _coerce_positive_int(data.get("pdf_page_render_parallelism"), fallback=2),
                minimum=1,
                maximum=4,
            ),
            pdf_prefer_text_layer=_coerce_bool(data.get("pdf_prefer_text_layer"), fallback=True),
            pdf_text_layer_min_chars=_coerce_positive_int(data.get("pdf_text_layer_min_chars"), fallback=40),
            pdf_text_layer_completeness_ocr=_coerce_bool(
                data.get("pdf_text_layer_completeness_ocr"),
                fallback=True,
            ),
            pdf_text_layer_attribution_ocr=_coerce_bool(data.get("pdf_text_layer_attribution_ocr"), fallback=True),
            pdf_retry_budget=_coerce_positive_int(data.get("pdf_retry_budget"), fallback=8),
            pdf_retry_unimproved_stop=_coerce_positive_int(data.get("pdf_retry_unimproved_stop"), fallback=2),
            region_rescue_max_per_task=_coerce_nonnegative_int(data.get("region_rescue_max_per_task"), fallback=5),
        )
        config.extraction_profile_custom = _detect_extraction_profile_custom(data, extraction_profile)
        config.ocr_profile_custom = _detect_ocr_profile_custom(data, ocr_profile)
        return config


def mask_api_key(value: str) -> str:
    return "****" if value else ""


def _coerce_ollama_overrides(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in value.items()}


def _default_provider_profiles() -> dict[str, dict[str, str]]:
    return profiles_as_dict(catalog_default_profiles())


def _coerce_provider(value: Any) -> str:
    provider = str(value or "").strip()
    if provider in _RUNTIME_PROVIDER_KEYS:
        return provider
    return "openai_compatible"


def _string_field(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _has_text(value: str) -> bool:
    return bool(value.strip())


def _profile_has_value(profile: ProviderProfile | dict[str, str]) -> bool:
    if isinstance(profile, ProviderProfile):
        values = (profile.base_url, profile.api_key, profile.model)
    else:
        values = (profile.get("base_url", ""), profile.get("api_key", ""), profile.get("model", ""))
    return any(_has_text(value) for value in values)


def _coerce_provider_profiles(
    data: dict[str, Any],
    provider: str,
    provider_platform_id: str,
) -> dict[str, dict[str, str]]:
    profiles = catalog_default_profiles()
    raw_profiles = data.get("provider_profiles")
    top_profile = profile_from_top_level(data)

    if isinstance(raw_profiles, dict):
        for platform_id in PROVIDER_PLATFORM_IDS:
            profiles[platform_id] = profile_from_mapping(raw_profiles.get(platform_id), profiles[platform_id])

        legacy_openai = raw_profiles.get("openai_compatible")
        if isinstance(legacy_openai, dict) and not _profile_has_value(profiles["custom"]):
            profiles["custom"] = profile_from_mapping(legacy_openai, profiles["custom"])

        legacy_ollama = raw_profiles.get("ollama")
        if isinstance(legacy_ollama, dict):
            profiles["ollama"] = profile_from_mapping(legacy_ollama, profiles["ollama"])

    if _profile_has_value(top_profile):
        selected_profile = profiles[provider_platform_id]
        if not _profile_has_value(selected_profile):
            profiles[provider_platform_id] = top_profile
        elif provider_platform_id == "custom" and not isinstance(raw_profiles, dict):
            profiles["custom"] = top_profile

    profiles = {
        platform_id: profile_with_catalog_default(platform_id, profile)
        for platform_id, profile in profiles.items()
    }
    return profiles_as_dict(profiles)


def _coerce_positive_int(value: Any, *, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _clamp_int(value: int, *, minimum: int, maximum: int) -> int:
    return min(max(int(value), minimum), maximum)


def _coerce_unit_float(value: Any, *, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if 0.0 < parsed <= 1.0 else fallback


def _coerce_nonnegative_int(value: Any, *, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed >= 0 else fallback


def _coerce_optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _coerce_optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any, *, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
    return fallback


def _coerce_grounding_fuzzy_threshold(value: Any, *, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    if 0.0 < parsed <= 1.0:
        return parsed
    _logger.warning("Invalid grounding_fuzzy_threshold=%r, fallback to %s", value, fallback)
    return fallback


_VALID_GROUNDING_MODES = {"off", "balanced", "strict"}


def _coerce_grounding_mode(value: object, *, fallback: str) -> str:
    if isinstance(value, str) and value in _VALID_GROUNDING_MODES:
        return value
    return fallback


def _coerce_optional_unit_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if 0.0 < parsed <= 1.0:
        return parsed
    return None


def _coerce_layout_parser(value: Any) -> str:
    if value is None:
        return "multi_para"
    parser = str(value).strip().lower()
    if not parser:
        return "multi_para"
    if parser in {
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
    }:
        return parser
    return "multi_para"


def _coerce_profile(value: Any, *, fallback: str) -> str:
    profile = str(value).strip().lower()
    if profile in {"fast", "balanced", "accurate"}:
        return profile
    return fallback


_EXTRACTION_PROFILE_FIELDS = (
    "extraction_passes",
    "extraction_max_char_buffer",
    "extraction_passes_increment",
    "extraction_parse_mode",
)

_OCR_PROFILE_FIELDS = (
    "text_det_limit_side_len",
    "text_det_thresh",
    "adaptive_retry_enabled",
)


def _detect_extraction_profile_custom(data: dict[str, Any], profile: str) -> bool:
    preset = EXTRACTION_PROFILE_PRESETS.get(profile)
    if preset is None:
        return True
    for field_name in _EXTRACTION_PROFILE_FIELDS:
        if field_name not in data:
            continue
        if data[field_name] != preset[field_name]:
            return True
    return False


def _detect_ocr_profile_custom(data: dict[str, Any], profile: str) -> bool:
    preset = OCR_PROFILE_PRESETS.get(profile)
    if preset is None:
        return True
    for field_name in _OCR_PROFILE_FIELDS:
        config_key = f"ocr_{field_name}"
        if config_key not in data:
            continue
        if data[config_key] != preset[field_name]:
            return True
    return False


def _coerce_online_platform_id(value: Any) -> str:
    if isinstance(value, str) and value.strip() in ONLINE_OCR_PLATFORM_IDS:
        return value.strip()
    return DEFAULT_ONLINE_OCR_PLATFORM_ID


def _coerce_online_ocr_profiles(value: Any) -> dict[str, dict[str, str]]:
    defaults = catalog_default_online_profiles()
    if not isinstance(value, dict):
        return defaults
    result: dict[str, dict[str, str]] = dict(defaults)
    for platform_id, profile in value.items():
        if not isinstance(profile, dict):
            continue
        base_url = str(profile.get("base_url", "")).strip()
        api_key = str(profile.get("api_key", ""))
        model = str(profile.get("model", "")).strip()
        entry: dict[str, str] = dict(result.get(platform_id, {}))
        if base_url:
            entry["base_url"] = base_url
        if api_key:
            entry["api_key"] = api_key
        if model:
            entry["model"] = model
        result[platform_id] = entry
    return result


def _coerce_ignore_areas(value: Any) -> list[list[float]]:
    if not isinstance(value, list):
        return []
    output: list[list[float]] = []
    for item in value:
        if not isinstance(item, list) or len(item) < 4:
            continue
        x1, y1, x2, y2 = item[0], item[1], item[2], item[3]
        if not all(isinstance(v, (int, float)) for v in (x1, y1, x2, y2)):
            continue
        output.append([float(x1), float(y1), float(x2), float(y2)])
    return output


def _load_template_catalog(data: dict[str, Any]) -> TemplateCatalog:
    raw_templates = data.get("templates")
    active_template_id = data.get("active_template_id")
    if _is_legacy_templates_payload(raw_templates, active_template_id):
        return TemplateCatalog.load([], BUILTIN_INVOICE_ID)
    return TemplateCatalog.load(raw_templates, str(active_template_id).strip() or None)


def _is_legacy_templates_payload(raw_templates: Any, active_template_id: Any) -> bool:
    if not isinstance(raw_templates, list):
        return active_template_id is None
    if not raw_templates:
        return False
    for item in raw_templates:
        if not isinstance(item, dict):
            return True
        kind = str(item.get("kind", "")).strip()
        template_id = str(item.get("id", "")).strip()
        if kind not in {"user", "builtin_override"} or not template_id:
            return True
    return False


def _coerce_column_specs(value: Any) -> tuple[ColumnSpec, ...]:
    if not isinstance(value, list):
        return ()
    columns: list[ColumnSpec] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        column_type = str(item.get("type", "string")).strip().lower()
        if column_type not in {"string", "number", "date", "phone", "email", "company"}:
            column_type = "string"
        decimal_separator = "," if item.get("decimal_separator") == "," else "."
        thousands_separator = str(item.get("thousands_separator", ""))
        currency_strip = _coerce_bool(item.get("currency_strip"), fallback=True)
        date_formats = tuple(str(fmt) for fmt in item.get("date_formats", ()) if str(fmt).strip())
        nullable_placeholder = str(item.get("nullable_placeholder", " ") or " ")
        columns.append(
            ColumnSpec(
                name=name,
                type=column_type,  # type: ignore[arg-type]
                decimal_separator=decimal_separator,  # type: ignore[arg-type]
                thousands_separator=thousands_separator,
                currency_strip=currency_strip,
                date_formats=date_formats,
                nullable_placeholder=nullable_placeholder,
            )
        )
    return tuple(columns)


def _coerce_line_rules(value: Any) -> LineRules | None:
    if not isinstance(value, dict):
        return None
    start = str(value.get("start", "")).strip()
    end = str(value.get("end", "")).strip()
    line = str(value.get("line", "")).strip()
    if not start or not end or not line:
        return None
    return LineRules(
        start=start,
        end=end,
        line=line,
        first_line=_coerce_optional_str(value.get("first_line")),
        last_line=_coerce_optional_str(value.get("last_line")),
        skip_line=_coerce_optional_str(value.get("skip_line")),
        repeating_field_from_parent=tuple(
            str(item) for item in value.get("repeating_field_from_parent", ()) if str(item).strip()
        ),
    )


def _coerce_field_regions(value: Any) -> tuple[FieldRegion, ...]:
    if not isinstance(value, list):
        return ()
    regions: list[FieldRegion] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        field_name = str(item.get("field_name", "")).strip()
        if not field_name:
            continue
        try:
            left = float(item.get("left"))
            top = float(item.get("top"))
            right = float(item.get("right"))
            bottom = float(item.get("bottom"))
        except (TypeError, ValueError):
            continue
        regions.append(
            FieldRegion(
                field_name=field_name,
                left=left,
                top=top,
                right=right,
                bottom=bottom,
            )
        )
    return tuple(regions)


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
