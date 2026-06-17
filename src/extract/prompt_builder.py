from __future__ import annotations

import json
from dataclasses import dataclass

from src.domain.schemas import ColumnSpec, FieldGroup, FieldRegion, LineRules


_SYSTEM_PROMPT = """你是结构化信息抽取器。
固定约束:
1. 只输出 JSON 二维数组。
2. 提取结果的列顺序必须与 examples 第一行一致。
3. 单段文本允许输出多行。
4. 缺失字段填 " "。
5. examples 仅用于字段顺序与格式参考，不要把标题行原样输出到结果里。
6. 若无法确定值是否来自原文，不要编造。
7. 文本中的制表符(Tab)表示其左右两侧文字在页面上水平分离、分属不同区块，不要把它们当作同一字段拼接。"""

_EXAMPLE_COPY_WARNING = (
    '请严格依据 <<< >>> 之间的原文输出结果。examples 仅用于展示列顺序与格式，'
    '禁止将示例中的任何具体值复制到输出；若原文未出现某字段，按规则 4 填 " "。'
)

_MARKDOWN_ADAPTER_PROMPT = """输入格式适配(markdown):
A. 待提取文本为 markdown,表格以管道符(|)表示。
B. 行列对应关系以表格结构为准;表格每一行视为一条独立记录,不得跨行混合不同行的字段。
C. 合计/小计行不要作为明细行输出。
D. 标题层级表示字段所属章节,可用于辅助定位公共字段。
E. 同一记录的公共字段与明细字段可能位于相邻结构块,应合并为同一行;不要拆成只有公共字段或只有明细字段的半行。"""


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    description: str
    examples: list[list[str]]
    columns: tuple[ColumnSpec, ...] = ()
    line_rules: LineRules | None = None
    field_regions: tuple[FieldRegion, ...] = ()
    field_groups: tuple[FieldGroup, ...] = ()
    exclusive_group_pairs: tuple[tuple[str, str], ...] = ()
    min_lines: int | None = None
    max_lines: int | None = None
    min_confidence: float | None = None


def build_messages(
    *,
    template: PromptTemplate,
    text: str,
    pass_index: int,
    total_passes: int,
    schema_hint: str | None,
    markdown_input: bool = False,
    system_prompt: str | None = None,
) -> list[dict[str, str]]:
    system_prompt = system_prompt or _SYSTEM_PROMPT
    if markdown_input:
        system_prompt += f"\n\n{_MARKDOWN_ADAPTER_PROMPT}"
    if schema_hint:
        system_prompt += f"\n8. 严格遵守以下 JSON Schema 输出，不要添加 schema 之外的字段：\n{schema_hint}"

    column_constraints = _render_column_constraints(template.columns)
    constraints_block = ""
    if column_constraints:
        constraints_block = f"\n\n字段类型约束:\n{column_constraints}"

    user_payload = (
        f"{template.description}\n\n"
        f"examples:\n{json.dumps(template.examples, ensure_ascii=False)}\n\n"
        f"{constraints_block.lstrip()}\n"
        f"抽取轮次: {pass_index}/{total_passes}\n"
        "待提取文本:\n<<<\n"
        f"{text}\n"
        ">>>\n"
        f"{_EXAMPLE_COPY_WARNING}"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_payload},
    ]


def _render_column_constraints(columns: tuple[ColumnSpec, ...]) -> str:
    lines: list[str] = []
    for column in columns:
        description = _describe_column_constraint(column)
        if description:
            lines.append(f"- {column.name}: {description}")
    return "\n".join(lines)


def _describe_column_constraint(column: ColumnSpec) -> str:
    if column.type == "date":
        if column.date_formats:
            return f"type=date, 允许格式: {' | '.join(column.date_formats)}"
        return "type=date"

    if column.type == "number":
        parts = ["type=number"]
        if column.thousands_separator:
            parts.append(f'千分位分隔符 "{column.thousands_separator}"（输出请去除）')
        if column.decimal_separator == ",":
            parts.append('小数点使用 ","')
        if column.currency_strip:
            parts.append("去除货币符号")
        return ", ".join(parts)

    return ""
