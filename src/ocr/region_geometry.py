from __future__ import annotations

from dataclasses import dataclass

PointBox = tuple[float, float, float, float]

MIN_AREA_FRAC = 0.05
MAX_TEXT_COVER_FRAC = 0.20


@dataclass(frozen=True)
class TextFragment:
    box: PointBox
    text: str


def detect_uncovered_raster_regions(
    image_boxes: list[PointBox],
    text_boxes: list[PointBox],
    page_size: tuple[float, float],
    *,
    min_area_frac: float = MIN_AREA_FRAC,
    max_text_cover_frac: float = MAX_TEXT_COVER_FRAC,
) -> list[PointBox]:
    page_area = page_size[0] * page_size[1]
    if page_area <= 0:
        return []

    uncovered_regions: list[PointBox] = []
    for image_box in image_boxes:
        image_area = _box_area(image_box)
        if image_area <= 0:
            continue
        if image_area / page_area < min_area_frac:
            continue

        overlaps = [
            overlap
            for text_box in text_boxes
            if (overlap := _intersect_box(image_box, text_box)) is not None
        ]
        text_cover_frac = _union_area(overlaps) / image_area
        if text_cover_frac < max_text_cover_frac:
            uncovered_regions.append(image_box)

    return uncovered_regions


def merge_fragments_by_reading_order(
    text_fragments: list[TextFragment],
    region_fragments: list[TextFragment],
) -> str:
    fragments = [
        fragment
        for fragment in [*text_fragments, *region_fragments]
        if fragment.text.strip()
    ]
    ordered_fragments = sorted(fragments, key=lambda fragment: (-fragment.box[3], fragment.box[0]))
    return "\n".join(fragment.text for fragment in ordered_fragments)


def _box_area(box: PointBox) -> float:
    left, bottom, right, top = box
    return max(0.0, right - left) * max(0.0, top - bottom)


def _intersect_box(first: PointBox, second: PointBox) -> PointBox | None:
    left = max(first[0], second[0])
    bottom = max(first[1], second[1])
    right = min(first[2], second[2])
    top = min(first[3], second[3])
    if right <= left or top <= bottom:
        return None
    return (left, bottom, right, top)


def _union_area(boxes: list[PointBox]) -> float:
    if not boxes:
        return 0.0

    x_edges = sorted({edge for box in boxes for edge in (box[0], box[2])})
    area = 0.0
    for left, right in zip(x_edges, x_edges[1:]):
        width = right - left
        if width <= 0:
            continue

        y_ranges = sorted(
            (box[1], box[3])
            for box in boxes
            if box[0] < right and box[2] > left
        )
        area += width * _merged_length(y_ranges)
    return area


def _merged_length(ranges: list[tuple[float, float]]) -> float:
    if not ranges:
        return 0.0

    total = 0.0
    current_start, current_end = ranges[0]
    for start, end in ranges[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            continue

        total += current_end - current_start
        current_start, current_end = start, end

    return total + current_end - current_start
