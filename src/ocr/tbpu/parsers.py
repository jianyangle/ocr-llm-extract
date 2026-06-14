# Algorithm ported from Umi-OCR (MIT License, author: hiroi-sora)
# Source: Umi-OCR_main/UmiOCR-data/py_src/ocr/tbpu/parsers.py

from __future__ import annotations

from typing import Any

from src.ocr.models import OCRTextBlock

from . import Tbpu
from .gap_tree import group_rows, sort_blocks
from .line_preprocessing import drop_normalized_bbox, preprocess_blocks
from .paragraph_parse import parse_paragraphs
from .separators import block_separator


def _to_payload(blocks: list[OCRTextBlock]) -> list[dict[str, Any]]:
    return [
        {
            "text": block.text,
            "score": block.score,
            "box": [list(point) for point in block.box],
            "end": block.end,
        }
        for block in blocks
    ]


def _to_blocks(payload: list[dict[str, Any]]) -> list[OCRTextBlock]:
    cleaned = drop_normalized_bbox(payload)
    return [
        OCRTextBlock(
            text=str(block.get("text", "")),
            score=block.get("score"),
            box=block.get("box", []),
            end=str(block.get("end", "")),
        )
        for block in cleaned
    ]


def _line_order_single(payload: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    prepared = preprocess_blocks(payload)
    rows = group_rows(prepared)
    ordered: list[dict[str, Any]] = []
    for row in rows:
        row.sort(key=lambda block: (float(block["normalized_bbox"][0]), float(block["normalized_bbox"][1])))
        ordered.extend(row)
    return ordered, rows


def _row_bounds(row: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    left = min(float(block["normalized_bbox"][0]) for block in row)
    top = min(float(block["normalized_bbox"][1]) for block in row)
    right = max(float(block["normalized_bbox"][2]) for block in row)
    bottom = max(float(block["normalized_bbox"][3]) for block in row)
    return left, top, right, bottom


def _group_adjacent_rows(ordered: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    if not ordered:
        return []

    rows: list[list[dict[str, Any]]] = [[ordered[0]]]
    for block in ordered[1:]:
        current_row = rows[-1]
        row_left, row_top, row_right, row_bottom = _row_bounds(current_row)
        left, top, right, bottom = (
            float(block["normalized_bbox"][0]),
            float(block["normalized_bbox"][1]),
            float(block["normalized_bbox"][2]),
            float(block["normalized_bbox"][3]),
        )
        row_h = max(0.0, row_bottom - row_top)
        block_h = max(0.0, bottom - top)
        overlap = max(0.0, min(row_bottom, bottom) - max(row_top, top))
        baseline = min(row_h, block_h)
        if baseline > 0 and overlap > baseline * 0.5:
            current_row.append(block)
            current_row.sort(key=lambda item: (float(item["normalized_bbox"][0]), float(item["normalized_bbox"][1])))
            continue
        rows.append([block])
    return rows


def _rows_to_paragraph_units(rows: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for row in rows:
        left, top, right, bottom = _row_bounds(row)
        texts = [str(block.get("text", "")) for block in row]
        units.append(
            {
                "blocks": row,
                "normalized_bbox": (left, top, right, bottom),
                "text": "".join(texts),
                "end": "",
            }
        )
    return units


def _materialize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        row_blocks = row["blocks"]
        for index, block in enumerate(row_blocks):
            end = ""
            if index < len(row_blocks) - 1:
                end = block_separator(block, row_blocks[index + 1])
            else:
                end = str(row.get("end", ""))
            block["end"] = end
            output.append(block)
    return output


def _assign_line_endings(rows: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows):
        for block_index, block in enumerate(row):
            end = ""
            if block_index < len(row) - 1:
                end = block_separator(block, row[block_index + 1])
            elif row_index < len(rows) - 1:
                end = "\n"
            block["end"] = end
            output.append(block)
    return output


class ParserNone(Tbpu):
    def run(self, blocks: list[OCRTextBlock]) -> list[OCRTextBlock]:
        return [OCRTextBlock(text=block.text, score=block.score, box=block.box, end="") for block in blocks]


class MultiNone(Tbpu):
    def run(self, blocks: list[OCRTextBlock]) -> list[OCRTextBlock]:
        payload = preprocess_blocks(_to_payload(blocks))
        ordered = sort_blocks(payload)
        for index, block in enumerate(ordered):
            block["end"] = "\n" if index < len(ordered) - 1 else ""
        return _to_blocks(ordered)


class MultiLine(Tbpu):
    def run(self, blocks: list[OCRTextBlock]) -> list[OCRTextBlock]:
        payload = preprocess_blocks(_to_payload(blocks))
        ordered = sort_blocks(payload)
        rows = _group_adjacent_rows(ordered)
        lined = _assign_line_endings(rows)
        return _to_blocks(lined)


class MultiPara(Tbpu):
    def run(self, blocks: list[OCRTextBlock]) -> list[OCRTextBlock]:
        payload = preprocess_blocks(_to_payload(blocks))
        ordered = sort_blocks(payload)
        rows = _group_adjacent_rows(ordered)
        row_units = _rows_to_paragraph_units(rows)
        parse_paragraphs(row_units)
        return _to_blocks(_materialize_rows(row_units))


class SingleNone(Tbpu):
    def run(self, blocks: list[OCRTextBlock]) -> list[OCRTextBlock]:
        ordered, _ = _line_order_single(_to_payload(blocks))
        for index, block in enumerate(ordered):
            block["end"] = "\n" if index < len(ordered) - 1 else ""
        return _to_blocks(ordered)


class SingleLine(Tbpu):
    def run(self, blocks: list[OCRTextBlock]) -> list[OCRTextBlock]:
        _, rows = _line_order_single(_to_payload(blocks))
        lined = _assign_line_endings(rows)
        return _to_blocks(lined)


class SinglePara(Tbpu):
    def run(self, blocks: list[OCRTextBlock]) -> list[OCRTextBlock]:
        ordered, _ = _line_order_single(_to_payload(blocks))
        rows = _group_adjacent_rows(ordered)
        row_units = _rows_to_paragraph_units(rows)
        parse_paragraphs(row_units)
        return _to_blocks(_materialize_rows(row_units))


class SingleCode(SingleLine):
    pass
