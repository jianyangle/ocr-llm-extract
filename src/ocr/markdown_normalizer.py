"""Normalize VL markdown with HTML tables into lightweight markdown."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
import logging
import re


logger = logging.getLogger(__name__)

_STRIPPED_ATTRS = {"style", "width", "border", "align"}
_TOP_LEVEL_BLOCK_TAGS = {"div", "p", "section", "html", "body"}


def normalize_markdown(markdown: str) -> str | None:
    """把在线 VL 返回的混合 markdown 规整为轻量 markdown。"""
    if not markdown or not markdown.strip():
        return None
    if "<" not in markdown:
        return markdown

    try:
        parser = _MarkdownNormalizer()
        parser.feed(markdown)
        parser.close()
        text = parser.finalize()
    except Exception:  # noqa: BLE001
        logger.warning("Failed to normalize markdown", exc_info=True)
        return None

    normalized = re.sub(r"\n{3,}", "\n\n", text).strip()
    return normalized or None


class _MarkdownNormalizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._table_parts: list[str] = []
        self._table_depth = 0
        self._nested_table = False
        self._style_depth = 0
        self._last_closed_block_kind: str | None = None

    def finalize(self) -> str:
        if self._table_depth:
            self._append_table_output(_sanitize_html("".join(self._table_parts)))
            self._table_parts = []
            self._table_depth = 0
        return "".join(self._parts)

    def _ensure_boundary(self, *, blank_line: bool = False) -> None:
        if self._parts and not self._parts[-1].endswith("\n"):
            self._parts.append("\n")
        if blank_line and self._parts and not "".join(self._parts).endswith("\n\n"):
            self._parts.append("\n")

    def _append_table_output(self, text: str) -> None:
        if not text:
            return
        self._ensure_boundary(blank_line=self._last_closed_block_kind is not None)
        self._parts.append(text)
        if not text.endswith("\n"):
            self._parts.append("\n")
        self._last_closed_block_kind = "table"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "style":
            self._style_depth += 1
            return
        if self._style_depth:
            return

        if tag == "table":
            self._ensure_boundary(blank_line=self._last_closed_block_kind is not None)
            if self._table_depth:
                self._nested_table = True
            self._table_depth += 1
            self._table_parts.append(_start_tag(tag, attrs, strip_attrs=self._nested_table))
            return

        if self._table_depth:
            self._table_parts.append(_start_tag(tag, attrs, strip_attrs=self._nested_table))
            return

        if tag == "br":
            self._parts.append("\n")
        elif tag in _TOP_LEVEL_BLOCK_TAGS:
            self._ensure_boundary(blank_line=self._last_closed_block_kind == "table")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "style" or self._style_depth:
            return

        if self._table_depth:
            self._table_parts.append(_start_tag(tag, attrs, strip_attrs=self._nested_table, self_closing=True))
            return
        if tag == "br":
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._style_depth:
            if tag == "style":
                self._style_depth -= 1
            return

        if self._table_depth:
            self._table_parts.append(f"</{tag}>")
            if tag == "table":
                self._table_depth -= 1
                if self._table_depth == 0:
                    self._flush_table()
        elif tag in _TOP_LEVEL_BLOCK_TAGS:
            self._ensure_boundary()
            self._last_closed_block_kind = "block"

    def handle_data(self, data: str) -> None:
        if self._style_depth:
            return

        if self._table_depth:
            self._table_parts.append(escape(data, quote=False))
        else:
            if self._last_closed_block_kind is not None:
                data = data.lstrip("\r\n")
            self._last_closed_block_kind = None
            self._parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.handle_data(f"&#{name};")

    def _flush_table(self) -> None:
        table_html = "".join(self._table_parts)
        self._table_parts = []
        nested = self._nested_table
        self._nested_table = False

        if nested:
            self._append_table_output(_sanitize_html(table_html))
            return

        try:
            self._append_table_output(_TableParser.parse(table_html).to_markdown())
        except Exception:  # noqa: BLE001
            logger.warning("Failed to convert markdown HTML table", exc_info=True)
            self._append_table_output(_sanitize_html(table_html))


@dataclass(frozen=True)
class _Cell:
    text: str
    colspan: int = 1
    rowspan: int = 1


@dataclass
class _Table:
    rows: list[list[str]]

    def to_markdown(self) -> str:
        if not self.rows:
            return ""
        width = max(len(row) for row in self.rows)
        padded = [row + [""] * (width - len(row)) for row in self.rows]
        lines = [_pipe_row(padded[0]), _pipe_row(["---"] * width)]
        lines.extend(_pipe_row(row) for row in padded[1:])
        return "\n".join(lines)


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._rows: list[list[_Cell]] = []
        self._current_row: list[_Cell] | None = None
        self._current_cell: list[str] | None = None
        self._cell_attrs: dict[str, str] = {}

    @classmethod
    def parse(cls, html: str) -> _Table:
        parser = cls()
        parser.feed(html)
        parser.close()
        parser._flush_cell()
        parser._flush_row()
        return _Table(_expand_spans(parser._rows))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            self._flush_row()
            self._current_row = []
        elif tag in {"td", "th"}:
            self._flush_cell()
            if self._current_row is None:
                self._current_row = []
            self._cell_attrs = {name.lower(): value or "" for name, value in attrs}
            self._current_cell = []
        elif tag == "br" and self._current_cell is not None:
            self._current_cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"}:
            self._flush_cell()
        elif tag == "tr":
            self._flush_row()

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell.append(data)

    def _flush_cell(self) -> None:
        if self._current_cell is None:
            return

        text = _normalize_cell_text("".join(self._current_cell))
        self._current_row = self._current_row or []
        self._current_row.append(
            _Cell(
                text=text,
                colspan=_positive_int(self._cell_attrs.get("colspan"), default=1),
                rowspan=_positive_int(self._cell_attrs.get("rowspan"), default=1),
            )
        )
        self._current_cell = None
        self._cell_attrs = {}

    def _flush_row(self) -> None:
        self._flush_cell()
        if self._current_row is not None:
            self._rows.append(self._current_row)
        self._current_row = None


def _expand_spans(rows: list[list[_Cell]]) -> list[list[str]]:
    expanded: list[list[str]] = []
    active_rowspans: dict[int, tuple[str, int]] = {}

    for row in rows:
        output_row: list[str] = []
        column = 0
        for cell in row:
            column = _fill_active_rowspans(output_row, column, active_rowspans)
            output_row.append(cell.text)
            for offset in range(1, cell.colspan):
                output_row.append("")
                if cell.rowspan > 1:
                    active_rowspans[column + offset] = ("", cell.rowspan - 1)
            if cell.rowspan > 1:
                active_rowspans[column] = (cell.text, cell.rowspan - 1)
            column += cell.colspan

        _fill_remaining_rowspans(output_row, column, active_rowspans)
        expanded.append(output_row)

    return expanded


def _fill_active_rowspans(row: list[str], column: int, spans: dict[int, tuple[str, int]]) -> int:
    while column in spans:
        text, remaining = spans[column]
        row.append(text)
        if remaining <= 1:
            del spans[column]
        else:
            spans[column] = (text, remaining - 1)
        column += 1
    return column


def _fill_remaining_rowspans(row: list[str], column: int, spans: dict[int, tuple[str, int]]) -> None:
    while spans and column <= max(spans):
        if column in spans:
            column = _fill_active_rowspans(row, column, spans)
        else:
            row.append("")
            column += 1


def _normalize_cell_text(text: str) -> str:
    collapsed = " ".join(text.replace("\\n", " ").split())
    return collapsed.replace("|", r"\|")


def _pipe_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _positive_int(value: str | None, *, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(parsed, 1)


def _sanitize_html(html: str) -> str:
    parser = _HtmlSanitizer()
    parser.feed(html)
    parser.close()
    return parser.output


class _HtmlSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._style_depth = 0

    @property
    def output(self) -> str:
        return "".join(self._parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "style":
            self._style_depth += 1
            return
        if self._style_depth:
            return
        if tag in {"div", "img"}:
            return
        self._parts.append(_start_tag(tag, attrs, strip_attrs=True))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "style" or self._style_depth:
            return
        if tag in {"div", "img"}:
            return
        self._parts.append(_start_tag(tag, attrs, strip_attrs=True, self_closing=True))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._style_depth:
            if tag == "style":
                self._style_depth -= 1
            return
        if tag not in {"div", "img"}:
            self._parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._style_depth:
            return
        self._parts.append(escape(data, quote=False))


def _start_tag(
    tag: str,
    attrs: list[tuple[str, str | None]],
    *,
    strip_attrs: bool,
    self_closing: bool = False,
) -> str:
    kept_attrs = []
    for name, value in attrs:
        normalized_name = name.lower()
        if strip_attrs and normalized_name in _STRIPPED_ATTRS:
            continue
        if value is None:
            kept_attrs.append(normalized_name)
        else:
            kept_attrs.append(f'{normalized_name}="{escape(value, quote=True)}"')

    attr_text = f" {' '.join(kept_attrs)}" if kept_attrs else ""
    slash = " /" if self_closing else ""
    return f"<{tag}{attr_text}{slash}>"
