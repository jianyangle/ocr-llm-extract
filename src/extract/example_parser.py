from __future__ import annotations

import json
import re

from .errors import ExtractServiceError


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


def format_examples(raw: str) -> list[list[str]]:
    try:
        normalized = _strip_markdown_code_fence(raw or "")
        normalized = normalized.translate(_FULLWIDTH_MAP)
        normalized = normalized.translate(_QUOTE_MAP)
        normalized = _fix_trailing_commas(normalized)
        normalized = _replace_single_quoted_strings(normalized)
        data = json.loads(normalized)
        return _validate_examples_2d_array(data)
    except ExtractServiceError:
        raise
    except json.JSONDecodeError as exc:
        raise ExtractServiceError(
            "E_PARSE_002",
            f"examples JSON parse failed at line {exc.lineno} column {exc.colno}",
        ) from exc
    except Exception as exc:
        raise ExtractServiceError("E_PARSE_002", "examples formatting failed") from exc


def _strip_markdown_code_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


def _fix_trailing_commas(text: str) -> str:
    return re.sub(r",\s*([\]}])", r"\1", text)


def _replace_single_quoted_strings(text: str) -> str:
    pattern = re.compile(r"'([^'\\]*(?:\\.[^'\\]*)*)'")

    def _replace(match: re.Match[str]) -> str:
        return json.dumps(match.group(1), ensure_ascii=False)

    return pattern.sub(_replace, text)


def _validate_examples_2d_array(data: object) -> list[list[str]]:
    if not isinstance(data, list):
        raise ExtractServiceError("E_PARSE_002", "examples must be a 2D array")
    if not data:
        raise ExtractServiceError("E_PARSE_002", "examples must contain at least one row")

    rows: list[list[str]] = []
    expected_columns: int | None = None
    for row in data:
        if not isinstance(row, list):
            raise ExtractServiceError("E_PARSE_002", "examples row must be an array")
        if expected_columns is None:
            expected_columns = len(row)
            if expected_columns < 2:
                raise ExtractServiceError("E_PARSE_002", "examples must define at least 2 columns")
        elif len(row) != expected_columns:
            raise ExtractServiceError("E_PARSE_002", "examples rows must have uniform column count")

        normalized_row: list[str] = []
        for cell in row:
            if not isinstance(cell, str):
                raise ExtractServiceError("E_PARSE_002", "examples cells must be strings")
            normalized_row.append(cell)
        rows.append(normalized_row)
    return rows
