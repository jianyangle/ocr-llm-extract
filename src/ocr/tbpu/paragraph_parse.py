# Algorithm ported from Umi-OCR (MIT License, author: hiroi-sora)
# Source: Umi-OCR_main/UmiOCR-data/py_src/ocr/tbpu/parser_tools/paragraph_parse.py

from __future__ import annotations

from typing import Any, Callable

from .separators import word_separator

TH = 1.2


def _default_get_info(block: dict[str, Any]) -> dict[str, Any]:
    bbox = block.get("normalized_bbox")
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        bbox = (0.0, 0.0, 0.0, 0.0)
    left, top, right, bottom = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    return {
        "l": left,
        "t": top,
        "r": right,
        "b": bottom,
        "h": max(0.0, bottom - top),
        "text": str(block.get("text", "")),
    }


def _default_set_end(block: dict[str, Any], end: str) -> None:
    block["end"] = end


def _line_spacing(prev_block: Any, block: Any, get_info: Callable[[Any], dict[str, Any]]) -> float:
    prev = get_info(prev_block)
    curr = get_info(block)
    return curr["t"] - prev["b"]


def _build_para_stats(paragraph: list[Any], get_info: Callable[[Any], dict[str, Any]]) -> dict[str, float]:
    infos = [get_info(block) for block in paragraph]
    lefts = [info["l"] for info in infos]
    rights = [info["r"] for info in infos]
    heights = [info["h"] for info in infos]
    spacings = [
        _line_spacing(prev, curr, get_info)
        for prev, curr in zip(paragraph, paragraph[1:], strict=False)
    ]
    return {
        "l": sum(lefts) / len(lefts),
        "r": sum(rights) / len(rights),
        "h": sum(heights) / len(heights) if heights else 0.0,
        "line_s": (sum(spacings) / len(spacings)) if spacings else 0.0,
    }


def _same_paragraph(paragraph: list[Any], block: Any, get_info: Callable[[Any], dict[str, Any]]) -> bool:
    stats = _build_para_stats(paragraph, get_info)
    info = get_info(block)
    prev_info = get_info(paragraph[-1])
    spacing = info["t"] - prev_info["b"]
    return (
        abs(stats["l"] - info["l"]) <= stats["h"] * TH
        and abs(stats["r"] - info["r"]) <= stats["h"] * TH
        and spacing <= stats["line_s"] + stats["h"] * 0.5
    )


def _can_merge_up(
    prev_para: list[Any],
    orphan: Any,
    get_info: Callable[[Any], dict[str, Any]],
) -> bool:
    orphan_info = get_info(orphan)
    prev_stats = _build_para_stats(prev_para, get_info)
    gap = orphan_info["t"] - get_info(prev_para[-1])["b"]
    return (
        abs(orphan_info["l"] - prev_stats["l"]) <= orphan_info["h"]
        and orphan_info["r"] <= prev_stats["r"]
        and gap <= prev_stats["line_s"] + orphan_info["h"] * 0.5
    )


def _can_merge_down(
    orphan: Any,
    next_para: list[Any],
    get_info: Callable[[Any], dict[str, Any]],
) -> bool:
    orphan_info = get_info(orphan)
    next_stats = _build_para_stats(next_para, get_info)
    aligned = abs(orphan_info["l"] - next_stats["l"]) <= orphan_info["h"]
    indented = orphan_info["l"] >= next_stats["l"] + orphan_info["h"]
    if not (aligned or indented):
        return False

    if len(next_para) > 1:
        return orphan_info["r"] <= next_stats["r"]
    return True


def _merge_orphans(paragraphs: list[list[Any]], get_info: Callable[[Any], dict[str, Any]]) -> list[list[Any]]:
    if len(paragraphs) <= 1:
        return paragraphs

    index = 0
    while index < len(paragraphs):
        paragraph = paragraphs[index]
        if len(paragraph) != 1:
            index += 1
            continue

        orphan = paragraph[0]
        can_up = index > 0 and _can_merge_up(paragraphs[index - 1], orphan, get_info)
        can_down = index < len(paragraphs) - 1 and _can_merge_down(orphan, paragraphs[index + 1], get_info)

        if can_up and can_down:
            prev_gap = get_info(orphan)["t"] - get_info(paragraphs[index - 1][-1])["b"]
            next_gap = get_info(paragraphs[index + 1][0])["t"] - get_info(orphan)["b"]
            if prev_gap <= next_gap:
                paragraphs[index - 1].append(orphan)
                paragraphs.pop(index)
                continue
            paragraphs[index + 1].insert(0, orphan)
            paragraphs.pop(index)
            continue

        if can_up:
            paragraphs[index - 1].append(orphan)
            paragraphs.pop(index)
            continue

        if can_down:
            paragraphs[index + 1].insert(0, orphan)
            paragraphs.pop(index)
            continue

        index += 1

    return paragraphs


def parse_paragraphs(
    blocks: list[Any],
    *,
    get_info: Callable[[Any], dict[str, Any]] | None = None,
    set_end: Callable[[Any, str], None] | None = None,
) -> list[Any]:
    if not blocks:
        return []

    getter = get_info or _default_get_info
    setter = set_end or _default_set_end

    paragraphs: list[list[Any]] = [[blocks[0]]]
    for block in blocks[1:]:
        if _same_paragraph(paragraphs[-1], block, getter):
            paragraphs[-1].append(block)
        else:
            paragraphs.append([block])

    paragraphs = _merge_orphans(paragraphs, getter)

    for para_index, paragraph in enumerate(paragraphs):
        for block_index, block in enumerate(paragraph):
            end = ""
            if block_index < len(paragraph) - 1:
                prev_text = str(getter(block).get("text", "") or "")
                next_text = str(getter(paragraph[block_index + 1]).get("text", "") or "")
                end = word_separator(prev_text[-1:], next_text[:1])
            elif para_index < len(paragraphs) - 1:
                end = "\n\n"
            setter(block, end)

    return blocks
