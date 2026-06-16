from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from typing import Literal

from src.domain.schemas import ColumnSpec

from .errors import ExtractServiceError
from .field_types import is_valid_email
from .field_types import is_valid_phone
from .field_types import normalize_company
from .field_types import normalize_email
from .field_types import normalize_phone

ParseMode = Literal["strict", "balanced", "aggressive"]

_FULLWIDTH_MAP = str.maketrans(
    {
        "［": "[",
        "］": "]",
        "｛": "{",
        "｝": "}",
        "（": "(",
        "）": ")",
        "，": ",",
        "：": ":",
        "；": ";",
    }
)

_QUOTE_MAP = str.maketrans(
    {
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
    }
)
_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def normalize_rows(rows: list[list[object]], expected_columns: int) -> list[list[str]]:
    if expected_columns <= 0:
        raise ExtractServiceError("E_PARSE_001", "examples column definition is empty")

    normalized: list[list[str]] = []
    for raw_row in rows:
        if not isinstance(raw_row, list):
            raise ExtractServiceError("E_LLM_007", "Row must be an array")

        row = [_normalize_cell(cell) for cell in raw_row]
        if len(row) < expected_columns:
            row.extend([" "] * (expected_columns - len(row)))
        elif len(row) > expected_columns:
            row = row[:expected_columns]
        normalized.append(row)

    return normalized


def canonicalize_typed_cells(
    rows: list[list[str]],
    column_specs: list[ColumnSpec] | tuple[ColumnSpec, ...],
) -> tuple[list[list[object]], list[dict[str, Any]]]:
    typed_rows: list[list[object]] = []
    warnings: list[dict[str, Any]] = []

    for row_index, row in enumerate(rows):
        typed_row: list[object] = []
        for col_index, cell in enumerate(row):
            spec = column_specs[col_index] if col_index < len(column_specs) else None
            if spec is None or spec.type == "string" or _is_empty_cell(cell, spec):
                typed_row.append(cell)
                continue

            if spec.type == "number":
                parsed_number = _parse_number(cell, spec)
                if parsed_number is None:
                    typed_row.append(cell)
                    warnings.append({"row": row_index, "col": col_index, "code": "W_NORM_NUM"})
                else:
                    typed_row.append(parsed_number)
                continue

            if spec.type == "date":
                parsed_date = _parse_date(cell, spec.date_formats)
                if parsed_date is None:
                    typed_row.append(cell)
                    warnings.append({"row": row_index, "col": col_index, "code": "W_NORM_DATE"})
                else:
                    typed_row.append(parsed_date)
                continue

            if spec.type == "phone":
                if is_valid_phone(cell):
                    typed_row.append(normalize_phone(cell))
                else:
                    typed_row.append(cell)
                    warnings.append({"row": row_index, "col": col_index, "code": "W_NORM_PHONE"})
                continue

            if spec.type == "email":
                if is_valid_email(cell):
                    typed_row.append(normalize_email(cell))
                else:
                    typed_row.append(cell)
                    warnings.append({"row": row_index, "col": col_index, "code": "W_NORM_EMAIL"})
                continue

            if spec.type == "company":
                typed_row.append(normalize_company(cell))
                continue

            typed_row.append(cell)
        typed_rows.append(typed_row)

    return typed_rows, warnings


def _normalize_cell(value: object) -> str:
    if value is None:
        return " "
    text = str(value).strip()
    return text if text else " "


def _is_empty_cell(cell: str, spec: ColumnSpec | None) -> bool:
    if not str(cell).strip():
        return True
    if spec is None:
        return False
    return cell == spec.nullable_placeholder


def _parse_number(value: str, spec: ColumnSpec) -> float | None:
    text = value.strip()
    if not text:
        return None

    sign = ""
    if text.startswith("-"):
        sign = "-"
        text = text[1:].strip()

    if spec.currency_strip:
        text = re.sub(r"[¥￥$€£]", "", text)
    text = text.strip()
    if not text:
        return None

    if spec.thousands_separator:
        text = text.replace(spec.thousands_separator, "")
    if spec.decimal_separator == ",":
        text = text.replace(",", ".")
    text = f"{sign}{text}"
    try:
        return float(text)
    except ValueError:
        return None


def _parse_date(value: str, date_formats: tuple[str, ...]) -> object | None:
    for date_format in date_formats:
        try:
            return datetime.strptime(value.strip(), date_format).date()
        except ValueError:
            continue
    return None


def parse_rows_payload(raw_content: str, *, parse_mode: ParseMode = "balanced") -> list[list[object]]:
    if not isinstance(raw_content, str) or not raw_content.strip():
        raise ExtractServiceError("E_LLM_002", "LLM response content is empty")

    cleaned = _THINK_TAG_RE.sub("", raw_content)
    if not cleaned.strip():
        raise ExtractServiceError("E_LLM_002", "LLM response content is empty")

    candidates = _build_parse_candidates(cleaned, parse_mode=parse_mode)
    saw_invalid_row_shape = False
    for candidate in candidates:
        data = _try_parse_json(candidate)
        if data is None:
            continue
        if _is_array_but_not_row_array(data):
            saw_invalid_row_shape = True
            continue
        rows = _unwrap_rows(data)
        if rows is None:
            continue
        return _validate_rows(rows)

    if saw_invalid_row_shape:
        raise ExtractServiceError("E_LLM_007", "LLM row item is not array")
    raise ExtractServiceError("E_LLM_002", "LLM response is not structured JSON array")


def _build_parse_candidates(raw_content: str, *, parse_mode: ParseMode) -> list[str]:
    base = raw_content.strip()
    candidates = [base]

    fence_stripped = _strip_markdown_code_fence(base)
    if fence_stripped:
        candidates.append(fence_stripped)

    extracted = _extract_first_json_payload(fence_stripped or base)
    if extracted:
        candidates.append(extracted)

    if parse_mode != "strict":
        repaired_candidates: list[str] = []
        for candidate in list(candidates):
            repaired_candidates.append(_repair_json(candidate, aggressive=parse_mode == "aggressive"))
            extracted_inner = _extract_first_json_payload(candidate)
            if extracted_inner:
                repaired_candidates.append(_repair_json(extracted_inner, aggressive=parse_mode == "aggressive"))
        candidates.extend(repaired_candidates)

    return _dedupe_keep_order(candidates)


def _strip_markdown_code_fence(raw: str) -> str:
    text = raw.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines:
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_first_json_payload(text: str) -> str | None:
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch not in "[{":
            continue
        try:
            obj, end = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, (list, dict)):
            return text[idx : idx + end]
    return None


def _repair_json(text: str, *, aggressive: bool) -> str:
    repaired = text.translate(_FULLWIDTH_MAP).translate(_QUOTE_MAP)
    repaired = re.sub(r",\s*([\]}])", r"\1", repaired)
    if aggressive:
        repaired = _replace_single_quoted_strings(repaired)
    return repaired


def _replace_single_quoted_strings(text: str) -> str:
    pattern = re.compile(r"'([^'\\]*(?:\\.[^'\\]*)*)'")
    return pattern.sub(lambda match: json.dumps(match.group(1), ensure_ascii=False), text)


def _try_parse_json(candidate: str) -> object | None:
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _unwrap_rows(data: object) -> list[object] | None:
    if isinstance(data, list):
        if _looks_like_2d_rows(data):
            return data
        return None
    if not isinstance(data, dict):
        return None

    value = data.get("rows")
    if isinstance(value, list) and _looks_like_2d_rows(value):
        return value
    return None


def _looks_like_2d_rows(rows: list[object]) -> bool:
    if not rows:
        return True
    return all(isinstance(row, list) for row in rows)


def _is_array_but_not_row_array(data: object) -> bool:
    if isinstance(data, list):
        return not _looks_like_2d_rows(data)
    if isinstance(data, dict):
        value = data.get("rows")
        if isinstance(value, list) and not _looks_like_2d_rows(value):
            return True
    return False


def _validate_rows(rows: list[object]) -> list[list[object]]:
    validated: list[list[object]] = []
    for row in rows:
        if not isinstance(row, list):
            raise ExtractServiceError("E_LLM_007", "LLM row item is not array")
        validated.append(row)
    return validated


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def parse_object_rows_payload(raw_content: str, *, parse_mode: ParseMode = "balanced") -> list[dict[str, object]]:
    if not isinstance(raw_content, str) or not raw_content.strip():
        raise ExtractServiceError("E_LLM_002", "LLM response content is empty")

    cleaned = _THINK_TAG_RE.sub("", raw_content)
    if not cleaned.strip():
        raise ExtractServiceError("E_LLM_002", "LLM response content is empty")

    candidates = _build_parse_candidates(cleaned, parse_mode=parse_mode)
    for candidate in candidates:
        data = _try_parse_json(candidate)
        if data is None:
            continue
        rows = _unwrap_object_rows(data)
        if rows is None:
            continue
        return rows

    raise ExtractServiceError("E_LLM_002", "LLM response is not structured object rows")


def _unwrap_object_rows(data: object) -> list[dict[str, object]] | None:
    if isinstance(data, list):
        if _looks_like_object_rows(data):
            return data
        return None
    if not isinstance(data, dict):
        return None

    value = data.get("rows")
    if isinstance(value, list) and _looks_like_object_rows(value):
        return value
    return None


def _looks_like_object_rows(rows: list[object]) -> bool:
    if not rows:
        return True
    return all(isinstance(row, dict) for row in rows)
