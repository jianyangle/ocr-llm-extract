# Algorithm ported from Umi-OCR (MIT License, author: hiroi-sora)
# Source: Umi-OCR_main/UmiOCR-data/py_src/ocr/tbpu/parser_tools/line_preprocessing.py

from __future__ import annotations

import math
from typing import Any

ROTATE_THRESHOLD = math.radians(3)


def _normalize_angle(angle: float) -> float:
    while angle >= math.pi / 2:
        angle -= math.pi
    while angle < -math.pi / 2:
        angle += math.pi
    return angle


def _edge_length(p1: list[float], p2: list[float]) -> float:
    return math.hypot(float(p2[0]) - float(p1[0]), float(p2[1]) - float(p1[1]))


def calculate_angle(box: list[list[float]]) -> float:
    if len(box) < 2:
        return 0.0

    p0 = box[0]
    p1 = box[1]
    if len(box) >= 3:
        p2 = box[2]
        v1_len = _edge_length(p0, p1)
        v2_len = _edge_length(p1, p2)
        if v2_len > v1_len:
            p0 = p1
            p1 = p2

    dx = float(p1[0]) - float(p0[0])
    dy = float(p1[1]) - float(p0[1])
    if dx == 0.0 and dy == 0.0:
        return 0.0
    return _normalize_angle(math.atan2(dy, dx))


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _box_bounds(points: list[list[float]]) -> tuple[float, float, float, float]:
    if not points:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _normalize_box_points(value: Any) -> list[list[float]]:
    if not isinstance(value, list):
        return []
    output: list[list[float]] = []
    for point in value:
        if not isinstance(point, list) or len(point) < 2:
            continue
        x = point[0]
        y = point[1]
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            continue
        output.append([float(x), float(y)])
    return output


def preprocess_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cloned: list[dict[str, Any]] = [dict(block) for block in blocks]

    angles = [
        calculate_angle(_normalize_box_points(block.get("box")))
        for block in cloned
        if len(_normalize_box_points(block.get("box"))) >= 2
    ]
    median_angle = _median(angles)

    if not angles or abs(median_angle) <= ROTATE_THRESHOLD:
        for block in cloned:
            points = _normalize_box_points(block.get("box"))
            block["normalized_bbox"] = _box_bounds(points)
        return cloned

    theta = -median_angle
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)

    rotated_bounds: list[tuple[float, float, float, float]] = []
    min_x = 0.0
    min_y = 0.0
    has_points = False

    for block in cloned:
        points = _normalize_box_points(block.get("box"))
        if not points:
            rotated_bounds.append((0.0, 0.0, 0.0, 0.0))
            continue

        has_points = True
        rotated_points: list[list[float]] = []
        for point in points:
            x = float(point[0])
            y = float(point[1])
            rx = x * cos_t - y * sin_t
            ry = x * sin_t + y * cos_t
            rotated_points.append([rx, ry])

        bounds = _box_bounds(rotated_points)
        rotated_bounds.append(bounds)
        min_x = min(min_x, bounds[0])
        min_y = min(min_y, bounds[1])

    if not has_points:
        return cloned

    offset_x = -min_x if min_x < 0 else 0.0
    offset_y = -min_y if min_y < 0 else 0.0

    for block, bounds in zip(cloned, rotated_bounds, strict=False):
        block["normalized_bbox"] = (
            bounds[0] + offset_x,
            bounds[1] + offset_y,
            bounds[2] + offset_x,
            bounds[3] + offset_y,
        )

    return cloned


def drop_normalized_bbox(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for block in blocks:
        cloned = dict(block)
        cloned.pop("normalized_bbox", None)
        output.append(cloned)
    return output
