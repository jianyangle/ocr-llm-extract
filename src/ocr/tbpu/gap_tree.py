# Algorithm ported from Umi-OCR (MIT License, author: hiroi-sora)
# Source: Umi-OCR_main/UmiOCR-data/py_src/ocr/tbpu/parser_tools/gap_tree.py

from __future__ import annotations

import bisect
from typing import Any

GapCut = dict[str, Any]
TreeNode = dict[str, Any]


def _bbox(block: dict[str, Any]) -> tuple[float, float, float, float]:
    normalized = block.get("normalized_bbox")
    if isinstance(normalized, tuple) and len(normalized) == 4:
        x0, y0, x1, y1 = normalized
        return float(x0), float(y0), float(x1), float(y1)
    if isinstance(normalized, list) and len(normalized) == 4:
        return float(normalized[0]), float(normalized[1]), float(normalized[2]), float(normalized[3])

    box = block.get("box")
    if not isinstance(box, list) or not box:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [float(point[0]) for point in box if isinstance(point, list) and len(point) >= 2]
    ys = [float(point[1]) for point in box if isinstance(point, list) and len(point) >= 2]
    if not xs or not ys:
        return (0.0, 0.0, 0.0, 0.0)
    return min(xs), min(ys), max(xs), max(ys)


def _height(block: dict[str, Any]) -> float:
    bbox = _bbox(block)
    return max(0.0, bbox[3] - bbox[1])


def _overlap_len(a_top: float, a_bottom: float, b_top: float, b_bottom: float) -> float:
    return max(0.0, min(a_bottom, b_bottom) - max(a_top, b_top))


def group_rows(blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    if not blocks:
        return []

    ordered = sorted(blocks, key=lambda block: (_bbox(block)[1], _bbox(block)[0]))
    rows: list[list[dict[str, Any]]] = []
    row_bounds: list[tuple[float, float]] = []

    for block in ordered:
        left, top, right, bottom = _bbox(block)
        block_h = max(0.0, bottom - top)
        placed = False

        for index, (row_top, row_bottom) in enumerate(row_bounds):
            row_h = max(0.0, row_bottom - row_top)
            overlap = _overlap_len(top, bottom, row_top, row_bottom)
            baseline_h = min(block_h, row_h)
            if baseline_h <= 0:
                continue
            if overlap > baseline_h * 0.5:
                rows[index].append(block)
                new_top = min(row_top, top)
                new_bottom = max(row_bottom, bottom)
                row_bounds[index] = (new_top, new_bottom)
                placed = True
                break

        if not placed:
            rows.append([block])
            row_bounds.append((top, bottom))

    for row in rows:
        row.sort(key=lambda block: (_bbox(block)[0], _bbox(block)[1]))

    rows.sort(key=lambda row: (_bbox(row[0])[1], _bbox(row[0])[0]))
    return rows


def compute_row_gaps(row: list[dict[str, Any]]) -> list[tuple[float, float]]:
    if len(row) < 2:
        return []

    ordered = sorted(row, key=lambda block: (_bbox(block)[0], _bbox(block)[1]))
    gaps: list[tuple[float, float]] = []
    for left_block, right_block in zip(ordered, ordered[1:], strict=False):
        left = _bbox(left_block)[2]
        right = _bbox(right_block)[0]
        if right > left:
            gaps.append((left, right))
    return gaps


def update_gaps(
    current: list[GapCut],
    new_row: list[tuple[float, float]],
    row_index: int,
) -> tuple[list[GapCut], list[GapCut]]:
    if not current:
        seeded = [
            {
                "x_left": gap_left,
                "x_right": gap_right,
                "r_top": row_index,
                "r_bottom": row_index,
                "units": [],
                "children": [],
            }
            for gap_left, gap_right in new_row
        ]
        return seeded, []

    next_current: list[GapCut] = []
    completed: list[GapCut] = []
    used_new = [False] * len(new_row)

    for cut in current:
        matched = False
        for index, (gap_left, gap_right) in enumerate(new_row):
            inter_left = max(float(cut["x_left"]), float(gap_left))
            inter_right = min(float(cut["x_right"]), float(gap_right))
            if inter_right <= inter_left:
                continue
            matched = True
            used_new[index] = True
            next_current.append(
                {
                    "x_left": inter_left,
                    "x_right": inter_right,
                    "r_top": int(cut["r_top"]),
                    "r_bottom": row_index,
                    "units": [],
                    "children": [],
                }
            )
        if not matched:
            completed.append(cut)

    for index, used in enumerate(used_new):
        if used:
            continue
        gap_left, gap_right = new_row[index]
        next_current.append(
            {
                "x_left": gap_left,
                "x_right": gap_right,
                "r_top": row_index,
                "r_bottom": row_index,
                "units": [],
                "children": [],
            }
        )

    next_current.sort(key=lambda cut: (float(cut["x_left"]), float(cut["x_right"])))
    return next_current, completed


def complete(node: TreeNode, candidates: list[TreeNode]) -> TreeNode | None:
    parents: list[TreeNode] = []
    node_left = float(node["x_left"])
    node_right = float(node["x_right"])
    node_top = int(node["r_top"])
    node_bottom = int(node["r_bottom"])

    for candidate in candidates:
        c_left = float(candidate["x_left"])
        c_right = float(candidate["x_right"])
        c_top = int(candidate["r_top"])
        c_bottom = int(candidate["r_bottom"])
        if c_left <= node_left and c_right >= node_right and c_top <= node_top and c_bottom >= node_bottom:
            parents.append(candidate)

    if not parents:
        return None

    parents.sort(key=lambda item: (int(item["r_bottom"]), -float(item["x_right"])))
    return parents[0]


def _collect_cuts(rows: list[list[dict[str, Any]]]) -> list[GapCut]:
    current: list[GapCut] = []
    completed: list[GapCut] = []

    for row_index, row in enumerate(rows):
        row_gaps = compute_row_gaps(row)
        current, done = update_gaps(current, row_gaps, row_index)
        completed.extend(done)

    completed.extend(current)

    # Keep only cuts that span at least two rows.
    filtered = [cut for cut in completed if int(cut["r_bottom"]) > int(cut["r_top"])]
    filtered.sort(key=lambda cut: (float(cut["x_left"]), float(cut["x_right"])))

    merged: list[GapCut] = []
    for cut in filtered:
        if not merged:
            merged.append(cut)
            continue
        last = merged[-1]
        overlap = min(float(last["x_right"]), float(cut["x_right"])) - max(float(last["x_left"]), float(cut["x_left"]))
        if overlap > 0:
            last["x_left"] = max(float(last["x_left"]), float(cut["x_left"]))
            last["x_right"] = min(float(last["x_right"]), float(cut["x_right"]))
            last["r_top"] = min(int(last["r_top"]), int(cut["r_top"]))
            last["r_bottom"] = max(int(last["r_bottom"]), int(cut["r_bottom"]))
            continue
        merged.append(cut)

    return merged


def _fallback_single_column(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(blocks, key=lambda block: (_bbox(block)[1], _bbox(block)[0]))


def _make_node(x_left: float, x_right: float, r_top: int, r_bottom: int) -> TreeNode:
    return {
        "x_left": x_left,
        "x_right": x_right,
        "r_top": r_top,
        "r_bottom": r_bottom,
        "units": [],
        "children": [],
    }


def _assign_blocks_to_tree(blocks: list[dict[str, Any]], cuts: list[GapCut], rows_len: int) -> TreeNode:
    all_left = min(_bbox(block)[0] for block in blocks)
    all_right = max(_bbox(block)[2] for block in blocks)
    root = _make_node(all_left, all_right, 0, max(rows_len - 1, 0))

    centers = [((float(cut["x_left"]) + float(cut["x_right"])) / 2.0) for cut in cuts]
    boundaries = [all_left, *centers, all_right]
    children = [
        _make_node(boundaries[index], boundaries[index + 1], 0, max(rows_len - 1, 0))
        for index in range(len(boundaries) - 1)
    ]

    for block in blocks:
        left, top, right, bottom = _bbox(block)

        if any(left < float(cut["x_left"]) and right > float(cut["x_right"]) for cut in cuts):
            root["units"].append(block)
            continue

        center_x = (left + right) / 2.0
        column_index = bisect.bisect_left(centers, center_x)
        children[column_index]["units"].append(block)

    children = [child for child in children if child["units"]]

    root["units"].sort(key=lambda block: (_bbox(block)[1], _bbox(block)[0]))
    for child in children:
        child["units"].sort(key=lambda block: (_bbox(block)[1], _bbox(block)[0]))

    children.sort(key=lambda node: float(node["x_left"]))
    root["children"] = children
    return root


def get_nodes_text_blocks(tree: TreeNode) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    units = tree.get("units", [])
    if isinstance(units, list):
        output.extend(units)

    children = tree.get("children", [])
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                output.extend(get_nodes_text_blocks(child))

    return output


def sort_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(blocks) <= 1:
        return list(blocks)

    rows = group_rows(blocks)
    cuts = _collect_cuts(rows)
    if not cuts:
        return _fallback_single_column(blocks)

    tree = _assign_blocks_to_tree(blocks, cuts, len(rows))
    return get_nodes_text_blocks(tree)
