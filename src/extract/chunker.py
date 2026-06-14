from __future__ import annotations

import functools
import logging
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from src.domain.schemas import AppConfig

_logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class _ChunkUnit:
    start: int
    prefix: str
    text: str


def split_passes(
    text: str,
    cfg: AppConfig,
    *,
    boundary_hints: Sequence[str] | None = None,
) -> list[str]:
    from .llm_extractor import resolve_extraction_params

    params = resolve_extraction_params(cfg)
    total_passes = max(int(params["extraction_passes"]), 1)
    if total_passes <= 1:
        return [text]

    max_buffer = int(params["extraction_max_char_buffer"])
    if max_buffer <= 0 or len(text) <= max_buffer:
        return [text]

    units = _build_units(text, max_buffer=max_buffer)
    if not units:
        return [text]

    step = int(params["extraction_passes_increment"])
    if step <= 0:
        step = max_buffer
    hard_limit = max(max_buffer, int(max_buffer * 1.2))
    protected_spans = _find_protected_spans(text, boundary_hints)

    chunks: list[str] = []
    covered_end = 0
    for pass_index in range(total_passes):
        target_start = pass_index * step
        hinted_span = _select_protected_span(protected_spans, target_start=target_start, max_buffer=max_buffer)
        hinted_chunk = _build_hint_chunk(
            text,
            protected_spans,
            target_start=target_start,
            max_buffer=max_buffer,
            hard_limit=hard_limit,
        )
        if hinted_chunk:
            if hinted_chunk not in chunks:
                chunks.append(hinted_chunk)
                if hinted_span is not None:
                    covered_end = max(covered_end, hinted_span[1])
            continue
        start_index = _find_start_index(units, target_start=target_start)
        if start_index is None:
            break
        chunk, _actual_start, actual_end = _build_chunk_with_span(units, start_index=start_index, max_buffer=max_buffer)
        if chunk and chunk not in chunks:
            chunks.append(chunk)
            covered_end = max(covered_end, actual_end)

    coverage_limit = (total_passes - 1) * step + max_buffer
    if len(text) <= coverage_limit:
        return chunks or [text]

    overlap = max(0, max_buffer - step)
    safety_iter_cap = max(1, math.ceil(len(text) / max(step, 1)) + 4)
    pass_index = total_passes
    iterations = 0
    while covered_end < len(text) and iterations < safety_iter_cap:
        iterations += 1
        target_start = pass_index * step

        hinted_span = _select_protected_span(protected_spans, target_start=target_start, max_buffer=max_buffer)
        hinted_chunk = _build_hint_chunk(
            text,
            protected_spans,
            target_start=target_start,
            max_buffer=max_buffer,
            hard_limit=hard_limit,
        )
        if hinted_chunk and hinted_span is not None and hinted_span[1] > covered_end:
            chunks.append(hinted_chunk)
            covered_end = max(covered_end, hinted_span[1])
            pass_index += 1
            continue

        start_index = _find_start_index(units, target_start=target_start)
        if start_index is None:
            chunk, actual_start, actual_end = _build_char_fallback_chunk(
                text,
                target_start=max(0, covered_end - overlap),
                max_buffer=max_buffer,
            )
        else:
            chunk, actual_start, actual_end = _build_chunk_with_span(
                units,
                start_index=start_index,
                max_buffer=max_buffer,
            )

        if actual_start > covered_end:
            chunk, actual_start, actual_end = _build_char_fallback_chunk(
                text,
                target_start=max(0, covered_end - overlap),
                max_buffer=max_buffer,
            )
        if not chunk:
            break
        if actual_end > covered_end:
            chunks.append(chunk)
            covered_end = max(covered_end, actual_end)
        pass_index += 1

    return chunks or [text]


def _build_units(text: str, *, max_buffer: int) -> list[_ChunkUnit]:
    paragraphs = _split_paragraphs(text)
    units: list[_ChunkUnit] = []
    for paragraph_index, (start, paragraph) in enumerate(paragraphs):
        paragraph_prefix = "\n\n" if paragraph_index > 0 and units else ""
        units.extend(_split_paragraph_units(paragraph, start=start, max_buffer=max_buffer, prefix=paragraph_prefix))
    return units


def _split_paragraphs(text: str) -> list[tuple[int, str]]:
    paragraphs: list[tuple[int, str]] = []
    position = 0
    for part in re.split(r"(\n\s*\n)", text):
        if not part:
            continue
        if re.fullmatch(r"\n\s*\n", part):
            position += len(part)
            continue
        if part.strip():
            paragraphs.append((position, part))
        position += len(part)
    return paragraphs


def _split_paragraph_units(paragraph: str, *, start: int, max_buffer: int, prefix: str) -> list[_ChunkUnit]:
    if len(paragraph) <= max_buffer:
        return [_ChunkUnit(start=start, prefix=prefix, text=paragraph)]

    units: list[_ChunkUnit] = []
    line_start = start
    first_line = True
    for line in paragraph.split("\n"):
        line_prefix = prefix if first_line else "\n"
        if len(line) <= max_buffer:
            units.append(_ChunkUnit(start=line_start, prefix=line_prefix, text=line))
        else:
            fragment_start = line_start
            fragment_prefix = line_prefix
            for offset in range(0, len(line), max_buffer):
                fragment = line[offset : offset + max_buffer]
                units.append(_ChunkUnit(start=fragment_start + offset, prefix=fragment_prefix, text=fragment))
                fragment_prefix = ""
        line_start += len(line) + 1
        first_line = False
    return units


def _find_start_index(units: list[_ChunkUnit], *, target_start: int) -> int | None:
    for index, unit in enumerate(units):
        if unit.start >= target_start:
            return index
    return None


def _build_chunk(units: list[_ChunkUnit], *, start_index: int, max_buffer: int) -> str:
    chunk, _actual_start, _actual_end = _build_chunk_with_span(units, start_index=start_index, max_buffer=max_buffer)
    return chunk


def _build_chunk_with_span(
    units: list[_ChunkUnit],
    *,
    start_index: int,
    max_buffer: int,
) -> tuple[str, int, int]:
    current = units[start_index].text
    length = len(current)
    first_unit = units[start_index]
    actual_start = first_unit.start
    actual_end = first_unit.start + len(first_unit.text)
    for unit in units[start_index + 1 :]:
        candidate_length = length + len(unit.prefix) + len(unit.text)
        if candidate_length > max_buffer:
            break
        current += unit.prefix + unit.text
        length = candidate_length
        actual_end = unit.start + len(unit.text)
    return current, actual_start, actual_end


def _build_char_fallback_chunk(text: str, *, target_start: int, max_buffer: int) -> tuple[str, int, int]:
    actual_start = min(max(int(target_start), 0), len(text))
    actual_end = min(actual_start + max_buffer, len(text))
    return text[actual_start:actual_end], actual_start, actual_end


@functools.lru_cache(maxsize=32)
def _compile_boundary_hints(boundary_hints: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(hint) for hint in boundary_hints if str(hint).strip())


def _find_protected_spans(
    text: str,
    boundary_hints: Sequence[str] | None,
) -> list[tuple[int, int, str]]:
    if not boundary_hints:
        return []

    compiled = _compile_boundary_hints(tuple(boundary_hints))
    if not compiled:
        return []

    if len(compiled) == 1:
        return [(match.start(), len(text), compiled[0].pattern) for match in compiled[0].finditer(text)]

    start_pattern = compiled[0]
    end_pattern = compiled[1]
    spans: list[tuple[int, int, str]] = []
    for start_match in start_pattern.finditer(text):
        end_match = end_pattern.search(text, start_match.end())
        end = end_match.end() if end_match is not None else len(text)
        spans.append((start_match.start(), end, start_pattern.pattern))
    return spans


def _build_hint_chunk(
    text: str,
    protected_spans: list[tuple[int, int, str]],
    *,
    target_start: int,
    max_buffer: int,
    hard_limit: int,
) -> str | None:
    span = _select_protected_span(protected_spans, target_start=target_start, max_buffer=max_buffer)
    if span is None:
        return None

    start, end, hint = span
    size = end - start
    if size > hard_limit:
        _logger.warning(
            "extract.chunker_boundary_skipped hint=%s size=%s max_buffer=%s hard_limit=%s",
            hint,
            size,
            max_buffer,
            hard_limit,
        )
        return None
    return text[start:end]


def _select_protected_span(
    protected_spans: list[tuple[int, int, str]],
    *,
    target_start: int,
    max_buffer: int,
) -> tuple[int, int, str] | None:
    for span in protected_spans:
        start, end, _hint = span
        if start <= target_start <= end:
            return span
        if target_start <= start <= target_start + max_buffer:
            return span
    return None


def split_markdown_passes(text: str, cfg: AppConfig) -> list[str]:
    """Markdown OCR Text 的覆盖式切分：全文恰好覆盖一次，表格为不可分割原子。"""
    from .llm_extractor import resolve_extraction_params

    params = resolve_extraction_params(cfg)
    max_buffer = int(params["extraction_max_char_buffer"])
    if max_buffer <= 0 or len(text) <= max_buffer:
        return [text]

    chunks: list[str] = []
    current = ""

    def flush_current() -> None:
        nonlocal current
        if current:
            chunks.append(current)
            current = ""

    for block in _split_markdown_blocks(text):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= max_buffer:
            current = candidate
            continue

        flush_current()
        if len(block) <= max_buffer:
            current = block
            continue
        if _is_markdown_table_block(block):
            chunks.append(block)
            continue
        for start in range(0, len(block), max_buffer):
            chunks.append(block[start : start + max_buffer])

    flush_current()
    return chunks or [text]


def _split_markdown_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    normal_lines: list[str] = []
    table_lines: list[str] = []

    def flush_normal() -> None:
        if normal_lines:
            block = "\n".join(normal_lines).strip("\n")
            if block.strip():
                blocks.append(block)
            normal_lines.clear()

    def flush_table() -> None:
        if table_lines:
            blocks.append("\n".join(table_lines))
            table_lines.clear()

    for line in text.splitlines():
        if not line.strip():
            flush_table()
            flush_normal()
            continue
        if line.lstrip().startswith("|"):
            flush_normal()
            table_lines.append(line)
            continue
        flush_table()
        normal_lines.append(line)

    flush_table()
    flush_normal()
    return blocks


def _is_markdown_table_block(block: str) -> bool:
    head = block.lstrip()
    return head.startswith("|") or head.lower().startswith("<table")
