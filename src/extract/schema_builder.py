from __future__ import annotations

from src.domain.schemas import ColumnSpec

from .errors import ExtractServiceError


def build_rows_schema(
    column_count: int,
    *,
    header: list[str] | None = None,
    columns: tuple[ColumnSpec, ...] | None = None,
) -> dict[str, object]:
    if column_count < 2:
        raise ExtractServiceError("E_PARSE_001", "examples must define at least 2 columns")

    row_schema = {
        "type": "array",
        "minItems": column_count,
        "maxItems": column_count,
        "items": {"type": "string"},
    }
    if header is not None:
        if len(header) != column_count:
            raise ExtractServiceError("E_PARSE_001", "examples header length does not match column count")
        row_schema["prefixItems"] = [
            {"type": "string", "description": _describe_column(header, columns, index)}
            for index, _ in enumerate(header)
        ]
        row_schema["items"] = False
    return {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": row_schema,
            }
        },
        "required": ["rows"],
    }


def _describe_column(header: list[str], columns: tuple[ColumnSpec, ...] | None, index: int) -> str:
    name = str(header[index])
    if columns is None or index >= len(columns):
        return name

    column = columns[index]
    if column.type == "date" and column.date_formats:
        return f"{name}（示例格式：{' | '.join(column.date_formats)}）"
    if column.type == "number":
        return f"{name}（数值列，输出去除货币符号和千分位）"
    return name
