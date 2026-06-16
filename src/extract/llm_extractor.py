from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from src.domain.schemas import AppConfig, ExtractionInput, ExtractionOutcome, GroundedExtractRow

from .chunker import split_markdown_passes, split_passes
from .errors import ExtractServiceError
from .field_types import email_equivalent, phone_equivalent
from .grounding import ground_rows, normalize_value_for_dedupe
from .output_normalizer import (
    map_object_rows_to_arrays,
    normalize_rows,
    parse_object_rows_payload,
    parse_rows_payload,
)
from .prompt_builder import PromptTemplate, build_messages
from .provider_catalog import get_provider_entry
from .provider_ollama import OllamaAdapter
from .provider_openai import OpenAICompatibleAdapter
from .schema_builder import build_rows_schema
from .template_catalog import TemplateCatalog
from .template_extractor import extract_by_line_rules
from .type_inference import infer_template_columns


EXTRACTION_PROFILE_PRESETS: dict[str, dict[str, Any]] = {
    "fast": {
        "extraction_passes": 1,
        "extraction_max_char_buffer": 3000,
        "extraction_passes_increment": 3000,
        "extraction_parse_mode": "balanced",
    },
    "balanced": {
        "extraction_passes": 2,
        "extraction_max_char_buffer": 2200,
        "extraction_passes_increment": 800,
        "extraction_parse_mode": "balanced",
    },
    "accurate": {
        "extraction_passes": 3,
        "extraction_max_char_buffer": 1800,
        "extraction_passes_increment": 600,
        "extraction_parse_mode": "aggressive",
    },
}

_logger = logging.getLogger(__name__)
_SIGCARD_HEADER = ("姓名", "职位", "公司名称", "电话号码", "手机号码", "邮箱地址", "公司地址")


def resolve_extraction_params(config: AppConfig) -> dict[str, Any]:
    profile = getattr(config, "extraction_profile", "balanced")
    is_custom = bool(getattr(config, "extraction_profile_custom", False))
    if is_custom or profile not in EXTRACTION_PROFILE_PRESETS:
        return {
            "extraction_passes": int(config.extraction_passes),
            "extraction_max_char_buffer": int(config.extraction_max_char_buffer),
            "extraction_passes_increment": int(config.extraction_passes_increment),
            "extraction_parse_mode": config.extraction_parse_mode,
        }
    preset = EXTRACTION_PROFILE_PRESETS[profile]
    return {
        "extraction_passes": int(preset["extraction_passes"]),
        "extraction_max_char_buffer": int(preset["extraction_max_char_buffer"]),
        "extraction_passes_increment": int(preset["extraction_passes_increment"]),
        "extraction_parse_mode": str(preset["extraction_parse_mode"]),
    }


class LLMExtractor:
    def __init__(
        self,
        openai_adapter: OpenAICompatibleAdapter | Any | None = None,
        ollama_adapter: OllamaAdapter | Any | None = None,
    ) -> None:
        self._openai = openai_adapter or OpenAICompatibleAdapter()
        self._ollama = ollama_adapter or OllamaAdapter()

    def extract_detailed(
        self,
        *,
        text: ExtractionInput | str,
        prompts: str,
        examples: list[list[str]],
        provider_cfg: AppConfig,
        ocr_confidence: float | None = None,
    ) -> ExtractionOutcome:
        source = text if isinstance(text, ExtractionInput) else ExtractionInput.from_text(str(text))
        template = self._resolve_template(
            text=source.flat_text,
            prompts=prompts,
            examples=examples,
            provider_cfg=provider_cfg,
            ocr_confidence=ocr_confidence,
        )
        expected_columns = _validate_examples(template.examples)
        all_grounded_rows = self._extract_template_rows(
            source=source,
            template=template,
            expected_columns=expected_columns,
            provider_cfg=provider_cfg,
        )

        deduped_rows = _dedupe_grounded_rows(all_grounded_rows, template=template)
        grounding_mode = getattr(provider_cfg, "grounding_mode", "off")
        if grounding_mode != "off":
            deduped_rows = [
                row
                for row in deduped_rows
                if _row_passes_grounding(row, grounding_mode)
            ]

        inferred_columns = infer_template_columns(template.examples, template.columns)
        return ExtractionOutcome(
            rows=deduped_rows,
            column_specs=list(inferred_columns),
            field_regions=list(template.field_regions),
            field_groups=tuple(template.field_groups),
            exclusive_group_pairs=tuple(template.exclusive_group_pairs),
        )

    def _call_provider_batch(
        self,
        *,
        messages_list: list[list[dict[str, str]]],
        provider_cfg: AppConfig,
        schema: dict[str, Any] | None,
    ) -> list[str]:
        provider = provider_cfg.provider
        thinking_params = None
        if not getattr(provider_cfg, "allow_thinking", False):
            platform_id = getattr(provider_cfg, "provider_platform_id", "custom") or "custom"
            params = get_provider_entry(platform_id).thinking_disable_params
            thinking_params = params or None
        if provider == "openai_compatible":
            return self._openai.chat_batch(
                base_url=provider_cfg.base_url,
                api_key=provider_cfg.api_key,
                model=provider_cfg.model,
                messages_list=messages_list,
                schema=schema,
                seed=getattr(provider_cfg, "llm_seed", None),
                prompt_cache=bool(getattr(provider_cfg, "llm_prompt_cache_enabled", False)),
                thinking_disable_params=thinking_params,
            )
        if provider == "ollama":
            return self._ollama.chat_batch(
                base_url=provider_cfg.base_url,
                model=provider_cfg.model,
                messages_list=messages_list,
                schema=schema,
                seed=getattr(provider_cfg, "llm_seed", None),
                prompt_cache=bool(getattr(provider_cfg, "llm_prompt_cache_enabled", False)),
                num_ctx=getattr(provider_cfg, "ollama_num_ctx", None),
                thinking_disable_params=thinking_params,
            )
        raise ExtractServiceError("E_LLM_003", f"Unsupported provider: {provider}")

    def _resolve_template(
        self,
        *,
        text: str,
        prompts: str,
        examples: list[list[str]],
        provider_cfg: AppConfig,
        ocr_confidence: float | None,
    ) -> PromptTemplate:
        _ = text
        _ = ocr_confidence
        raw_templates = list(getattr(provider_cfg, "templates", []))
        active_template_id = getattr(provider_cfg, "active_template_id", None)
        if raw_templates and all(isinstance(item, PromptTemplate) for item in raw_templates):
            return raw_templates[0]
        if raw_templates or active_template_id:
            catalog = TemplateCatalog.load(raw_templates, active_template_id)
            return catalog.active_template()
        return PromptTemplate(name="custom", description=prompts, examples=examples)

    def _extract_template_rows(
        self,
        *,
        source: ExtractionInput,
        template: PromptTemplate,
        expected_columns: int,
        provider_cfg: AppConfig,
    ) -> list[GroundedExtractRow]:
        if template.line_rules is not None and not source.has_markdown:
            line_result = extract_by_line_rules(
                text=source.flat_text,
                rules=template.line_rules,
            )
            if line_result.matched and line_result.rows:
                return self._extract_grounded_rows_with_line_rules(
                    text=source.flat_text,
                    unmatched_text=line_result.unmatched_text,
                    template=template,
                    expected_columns=expected_columns,
                    provider_cfg=provider_cfg,
                    line_rows=line_result.rows,
                )
        return self._run_llm_grounded_extract(
            source=source,
            template=template,
            expected_columns=expected_columns,
            provider_cfg=provider_cfg,
        )

    def _extract_grounded_rows_with_line_rules(
        self,
        *,
        text: str,
        unmatched_text: str,
        template: PromptTemplate,
        expected_columns: int,
        provider_cfg: AppConfig,
        line_rows: list[dict[str, str]],
    ) -> list[GroundedExtractRow]:
        header = template.examples[0]
        parent_text = unmatched_text.strip()
        if len(parent_text) < 5:
            _logger.warning("Skipping parent-field backfill because unmatched_text is too short")
            parent_values = [" "] * expected_columns
        else:
            try:
                parent_grounded = self._run_llm_grounded_extract(
                    source=ExtractionInput.from_text(parent_text),
                    template=template,
                    expected_columns=expected_columns,
                    provider_cfg=provider_cfg,
                )
                parent_values = parent_grounded[0].values if parent_grounded else [" "] * expected_columns
            except ExtractServiceError:
                parent_values = [" "] * expected_columns
        normalized_rows: list[list[str]] = []
        for row in line_rows:
            values = [str(row.get(column_name, " ")).strip() or " " for column_name in header]
            for field_name in template.line_rules.repeating_field_from_parent:
                if field_name not in header:
                    continue
                column_index = header.index(field_name)
                if values[column_index].strip():
                    continue
                values[column_index] = parent_values[column_index]
            normalized_rows.append(values)
        return ground_rows(normalized_rows, source_text=text, cfg=provider_cfg)

    def _run_llm_grounded_extract(
        self,
        *,
        source: ExtractionInput,
        template: PromptTemplate,
        expected_columns: int,
        provider_cfg: AppConfig,
    ) -> list[GroundedExtractRow]:
        parse_mode = _resolve_parse_mode(provider_cfg)
        header = template.examples[0]
        if source.has_markdown:
            pass_texts = split_markdown_passes(source.extraction_text, provider_cfg)
        else:
            pass_texts = _build_pass_texts(source.flat_text, provider_cfg, template=template)

        use_object_schema = False
        if getattr(provider_cfg, "provider", None) == "ollama" and provider_cfg.use_structured_output:
            if _has_duplicate_columns(header):
                _logger.warning(
                    "Ollama object-schema disabled: duplicate column names in header; "
                    "falling back to array schema"
                )
            else:
                use_object_schema = True

        if provider_cfg.use_structured_output:
            schema = build_rows_schema(
                expected_columns,
                header=header,
                columns=template.columns,
                row_format="object" if use_object_schema else "array",
            )
        else:
            schema = None
        schema_hint = json.dumps(schema, ensure_ascii=False) if schema is not None else None
        messages_list = [
            build_messages(
                template=template,
                text=pass_text,
                pass_index=pass_index,
                total_passes=len(pass_texts),
                schema_hint=schema_hint,
                markdown_input=source.has_markdown,
            )
            for pass_index, pass_text in enumerate(pass_texts, start=1)
        ]
        raw_contents = self._call_provider_batch(
            messages_list=messages_list,
            provider_cfg=provider_cfg,
            schema=schema,
        )

        grounded_rows: list[GroundedExtractRow] = []
        for raw_content in raw_contents:
            normalized = self._parse_to_normalized_rows(
                raw_content,
                parse_mode=parse_mode,
                expected_columns=expected_columns,
                header=header,
                use_object_schema=use_object_schema,
            )
            grounded_rows.extend(ground_rows(normalized, source_text=source.flat_text, cfg=provider_cfg))
        return grounded_rows

    def _parse_to_normalized_rows(
        self,
        raw_content: str,
        *,
        parse_mode: str,
        expected_columns: int,
        header: list[str],
        use_object_schema: bool,
    ) -> list[list[str]]:
        if use_object_schema:
            try:
                object_rows = parse_object_rows_payload(raw_content, parse_mode=parse_mode)
                mapped = map_object_rows_to_arrays(object_rows, header)
                return normalize_rows(mapped, expected_columns=expected_columns)
            except ExtractServiceError:
                _logger.warning(
                    "Ollama object-schema parse failed; falling back to array parsing"
                )
        rows = parse_rows_payload(raw_content, parse_mode=parse_mode)
        return normalize_rows(rows, expected_columns=expected_columns)


def _validate_examples(examples: list[list[str]]) -> int:
    if not examples or not isinstance(examples, list):
        raise ExtractServiceError("E_PARSE_001", "examples is empty")

    first_row = examples[0]
    if not isinstance(first_row, list):
        raise ExtractServiceError("E_PARSE_001", "examples must be 2D array")

    expected_columns = len(first_row)
    if expected_columns < 2:
        raise ExtractServiceError("E_PARSE_001", "examples must define at least 2 columns")

    for row in examples:
        if not isinstance(row, list) or len(row) != expected_columns:
            raise ExtractServiceError("E_PARSE_001", "examples columns are inconsistent")

    return expected_columns


def _has_duplicate_columns(header: list[str]) -> bool:
    names = [str(name).strip() for name in header]
    return len(names) != len(set(names))


def _resolve_parse_mode(provider_cfg: AppConfig) -> str:
    params = resolve_extraction_params(provider_cfg)
    parse_mode = params["extraction_parse_mode"]
    if parse_mode in {"strict", "balanced", "aggressive"}:
        return parse_mode
    return "balanced"


def _build_pass_texts(text: str, provider_cfg: AppConfig, *, template: PromptTemplate) -> list[str]:
    boundary_hints: tuple[str, ...] | None = None
    if template.line_rules is not None:
        boundary_hints = (template.line_rules.start, template.line_rules.end)
    return split_passes(text, provider_cfg, boundary_hints=boundary_hints)


def _dedupe_grounded_rows(
    rows: Iterable[GroundedExtractRow],
    *,
    template: PromptTemplate | None = None,
) -> list[GroundedExtractRow]:
    ranking = {
        "ASSIGNED": 5,
        "ASSIGNED_PARTIAL": 4,
        "INFERRED": 3,
        "INFERRED_FUZZY": 2,
        "UNCERTAIN": 1,
    }
    deduped: list[GroundedExtractRow] = []
    index_by_key: dict[tuple[str, ...], int] = {}
    use_sigcard_fuzzy_dedupe = _is_sigcard_like_template(template)

    for row in rows:
        key = tuple(normalize_value_for_dedupe(value) for value in row.values)
        existing_index = index_by_key.get(key)
        if existing_index is None and use_sigcard_fuzzy_dedupe:
            existing_index = _find_sigcard_duplicate_index(deduped, row)
        if existing_index is None:
            index_by_key[key] = len(deduped)
            deduped.append(row)
            continue

        existing_row = deduped[existing_index]
        if ranking.get(row.classification, 0) > ranking.get(existing_row.classification, 0):
            deduped[existing_index] = row
            index_by_key[key] = existing_index
            continue
        if use_sigcard_fuzzy_dedupe and _sigcard_row_fill_count(row) > _sigcard_row_fill_count(existing_row):
            deduped[existing_index] = row
            index_by_key[key] = existing_index

    return deduped


def _row_passes_grounding(row: GroundedExtractRow, mode: str) -> bool:
    if mode == "strict":
        return row.classification == "ASSIGNED"
    if mode != "balanced":
        return True
    key_cells = [cell for cell in row.cells[:2] if cell.value.strip()]
    if not key_cells:
        return False
    allowed = {"ASSIGNED", "ASSIGNED_PARTIAL", "INFERRED_FUZZY"}
    return all(
        cell.status in allowed
        for cell in row.cells
        if cell.value.strip()
    )


def _is_sigcard_like_template(template: PromptTemplate | None) -> bool:
    if template is None or not template.examples:
        return False
    header = tuple(str(value).strip() for value in template.examples[0])
    return header == _SIGCARD_HEADER


def _find_sigcard_duplicate_index(rows: list[GroundedExtractRow], candidate: GroundedExtractRow) -> int | None:
    for index, existing in enumerate(rows):
        if _sigcard_rows_are_near_duplicates(existing.values, candidate.values):
            return index
    return None


def _sigcard_rows_are_near_duplicates(left: list[str], right: list[str]) -> bool:
    if len(left) < len(_SIGCARD_HEADER) or len(right) < len(_SIGCARD_HEADER):
        return False
    left_name = normalize_value_for_dedupe(left[0]).strip()
    right_name = normalize_value_for_dedupe(right[0]).strip()
    if not left_name or left_name != right_name:
        return False
    if not _sigcard_field_compatible(left[2], right[2]):
        return False
    if not _sigcard_field_compatible(left[6], right[6]):
        return False

    left_email = normalize_value_for_dedupe(left[5]).strip()
    right_email = normalize_value_for_dedupe(right[5]).strip()
    if left_email and right_email:
        return email_equivalent(left[5], right[5])

    return phone_equivalent(left[3], right[3]) or phone_equivalent(left[4], right[4])


def _sigcard_field_compatible(left: str, right: str) -> bool:
    left_normalized = normalize_value_for_dedupe(left).strip()
    right_normalized = normalize_value_for_dedupe(right).strip()
    if left_normalized and right_normalized:
        return left_normalized == right_normalized
    return True


def _sigcard_row_fill_count(row: GroundedExtractRow) -> int:
    return sum(1 for value in row.values if str(value).strip())
