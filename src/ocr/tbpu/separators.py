# Algorithm ported from Umi-OCR (MIT License, author: hiroi-sora)
# Source: Umi-OCR_main/UmiOCR-data/py_src/ocr/tbpu/parser_tools/paragraph_parse.py

from __future__ import annotations

import unicodedata
from typing import Any, Literal

VISIBLE_GAP_CHAR_RATIO = 0.35
VISIBLE_GAP_MIN_PIXELS = 4.0
# 跨栏档:间距达到字符宽的该倍数时,视为"跨栏/跨列"而非词间间距,插入制表符。
# 初值 2.5 已经真实发票 smoke 校准,依据见 spec 阈值记录。
COLUMN_GAP_CHAR_RATIO = 2.5


def _is_cjk(ch: str) -> bool:
    if not ch:
        return False
    code = ord(ch[0])
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0xF900 <= code <= 0xFAFF
        or 0x3000 <= code <= 0x303F
        or 0xFF00 <= code <= 0xFFEF
    )


def word_separator(letter1: str, letter2: str) -> str:
    if not letter1 or not letter2:
        return ""
    if _is_cjk(letter1) and _is_cjk(letter2):
        return ""
    if letter1 == "-":
        return ""
    if letter2 and unicodedata.category(letter2[0]).startswith("P"):
        return ""
    return " "


def block_separator(current_block: dict[str, Any], next_block: dict[str, Any]) -> str:
    gap_kind = _classify_gap(current_block, next_block)
    if gap_kind == "column":
        return "\t"
    if gap_kind == "visible":
        return " "

    current_text = str(current_block.get("text", "")).strip()
    next_text = str(next_block.get("text", "")).strip()
    return word_separator(current_text[-1:] or "", next_text[:1] or "")


def _classify_gap(
    current_block: dict[str, Any],
    next_block: dict[str, Any],
) -> Literal["none", "visible", "column"]:
    """把相邻块的水平间距分为三档:"none"(贴合/无间距)、"visible"(词间距)、"column"(跨栏)。"""
    current_box = _bbox(current_block)
    next_box = _bbox(next_block)
    if current_box is None or next_box is None:
        return "none"

    gap = next_box[0] - current_box[2]
    if gap <= 0:
        return "none"

    char_widths = [width for width in (_estimated_char_width(current_block), _estimated_char_width(next_block)) if width > 0]
    if not char_widths:
        return "visible" if gap >= VISIBLE_GAP_MIN_PIXELS else "none"

    min_width = min(char_widths)
    # 先过可见间距门槛,再判跨栏:字符宽极小时跨栏阈值可能低于可见阈值,门槛保证不误升级。
    if gap < max(VISIBLE_GAP_MIN_PIXELS, min_width * VISIBLE_GAP_CHAR_RATIO):
        return "none"
    if gap >= min_width * COLUMN_GAP_CHAR_RATIO:
        return "column"
    return "visible"


def _estimated_char_width(block: dict[str, Any]) -> float:
    box = _bbox(block)
    if box is None:
        return 0.0
    text = "".join(str(block.get("text", "")).split())
    if not text:
        return 0.0
    width = max(0.0, box[2] - box[0])
    if width <= 0:
        return 0.0
    return width / max(len(text), 1)


def _bbox(block: dict[str, Any]) -> tuple[float, float, float, float] | None:
    normalized = block.get("normalized_bbox")
    if isinstance(normalized, tuple) and len(normalized) == 4:
        return tuple(float(value) for value in normalized)
    if isinstance(normalized, list) and len(normalized) == 4:
        return tuple(float(value) for value in normalized)

    box = block.get("box")
    if not isinstance(box, list) or not box:
        return None
    points = [point for point in box if isinstance(point, list) and len(point) >= 2]
    if not points:
        return None
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return min(xs), min(ys), max(xs), max(ys)
