from __future__ import annotations

import unicodedata
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Literal, Sequence

if TYPE_CHECKING:
    from src.ocr.models import OCRTextBlock
    from src.domain.schemas import FieldGroup

# --- 几何阈值常量(已用真实反例/对照 blocks fixture 校准,禁止运行时学习或暴露用户配置)---
_DIRECTION_RATIO = 1.5
_MIN_AXIS_FRAC = 0.20
_VERTICAL_W_OVER_H_MAX = 0.6   # 短词 anchor 降级要求:窄高竖排
# 有意义片段下限(反向拼接才用到):>=4 中文字符 或 >=8 字母数字字符
_MIN_MEANINGFUL_CJK = 4
_MIN_MEANINGFUL_ALNUM = 8


@dataclass(frozen=True)
class AnchorMatch:
    text: str
    bbox: tuple[float, float, float, float]
    center: tuple[float, float]


@dataclass(frozen=True)
class RegionDivision:
    mode: Literal["left_right", "top_bottom"]
    split: float
    low_group: str    # 沿分区轴坐标较小一侧的组名
    high_group: str   # 坐标较大一侧的组名


def normalize(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", text) if not ch.isspace()).lower()


def bbox_of_block(block: "OCRTextBlock") -> tuple[float, float, float, float] | None:
    box = getattr(block, "box", None)
    if not box:
        return None
    xs = [float(p[0]) for p in box if isinstance(p, (list, tuple)) and len(p) >= 2]
    ys = [float(p[1]) for p in box if isinstance(p, (list, tuple)) and len(p) >= 2]
    if not xs or not ys:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    x1, y1, x2, y2 = bbox
    return (x1 + x2) / 2.0, (y1 + y2) / 2.0


def _is_vertical(bbox: tuple[float, float, float, float]) -> bool:
    x1, y1, x2, y2 = bbox
    width = x2 - x1
    height = y2 - y1
    if height <= 0:
        return False
    return (width / height) <= _VERTICAL_W_OVER_H_MAX


def find_anchor(
    blocks: Sequence["OCRTextBlock"],
    *,
    full_keywords: Sequence[str],
    short_keywords: Sequence[str],
    exclude_keywords: Sequence[str],
) -> AnchorMatch | None:
    norm_full = [normalize(k) for k in full_keywords if k.strip()]
    norm_short = [normalize(k) for k in short_keywords if k.strip()]
    norm_exclude = [normalize(k) for k in exclude_keywords if k.strip()]

    def _candidates(keywords: Sequence[str], require_vertical: bool) -> list[AnchorMatch]:
        found: list[AnchorMatch] = []
        for block in blocks:
            bbox = bbox_of_block(block)
            if bbox is None:
                continue
            norm_text = normalize(block.text)
            if not norm_text:
                continue
            if any(ex and ex in norm_text for ex in norm_exclude):
                continue
            if not any(kw and kw in norm_text for kw in keywords):
                continue
            if require_vertical and not _is_vertical(bbox):
                continue
            found.append(AnchorMatch(text=block.text, bbox=bbox, center=_center(bbox)))
        return found

    full_hits = _candidates(norm_full, require_vertical=False)
    if len(full_hits) == 1:
        return full_hits[0]
    if len(full_hits) > 1:
        return None  # 同组多个完整 anchor 候选 -> 未知

    short_hits = _candidates(norm_short, require_vertical=True)
    if len(short_hits) == 1:
        return short_hits[0]
    return None


def compute_division(
    *,
    anchor_a: AnchorMatch,
    name_a: str,
    anchor_b: AnchorMatch,
    name_b: str,
    page_width: float,
    page_height: float,
) -> RegionDivision | None:
    ax, ay = anchor_a.center
    bx, by = anchor_b.center
    dx = abs(ax - bx)
    dy = abs(ay - by)

    if dy >= _DIRECTION_RATIO * dx and dy >= _MIN_AXIS_FRAC * page_height:
        split = (ay + by) / 2.0
        if ay <= by:
            return RegionDivision(mode="top_bottom", split=split, low_group=name_a, high_group=name_b)
        return RegionDivision(mode="top_bottom", split=split, low_group=name_b, high_group=name_a)

    if dx >= _DIRECTION_RATIO * dy and dx >= _MIN_AXIS_FRAC * page_width:
        split = (ax + bx) / 2.0
        if ax <= bx:
            return RegionDivision(mode="left_right", split=split, low_group=name_a, high_group=name_b)
        return RegionDivision(mode="left_right", split=split, low_group=name_b, high_group=name_a)

    return None


def region_of_block(block: "OCRTextBlock", division: RegionDivision) -> str | None:
    bbox = bbox_of_block(block)
    if bbox is None:
        return None
    cx, cy = _center(bbox)
    coord = cx if division.mode == "left_right" else cy
    return division.low_group if coord < division.split else division.high_group


def _count_cjk(text: str) -> int:
    return sum(1 for ch in text if "一" <= ch <= "鿿")


def _count_alnum(text: str) -> int:
    return sum(1 for ch in text if ch.isalnum())


def _is_meaningful(text: str) -> bool:
    norm = normalize(text)
    return _count_cjk(norm) >= _MIN_MEANINGFUL_CJK or _count_alnum(norm) >= _MIN_MEANINGFUL_ALNUM


def _value_overlap(norm_text: str, norm_value: str) -> str:
    """返回 norm_text 中最长的、出现在 norm_value 内的后缀(用于剥离'名称:'等标签前缀)。"""
    for start in range(len(norm_text)):
        suffix = norm_text[start:]
        if suffix in norm_value:
            return suffix
    return ""


def _fragments_cover_value(fragments: Sequence[str], norm_value: str) -> bool:
    counts_by_fragment = Counter(fragments)
    unique_fragments = tuple(counts_by_fragment)
    initial_counts = tuple(counts_by_fragment[fragment] for fragment in unique_fragments)

    @lru_cache(maxsize=None)
    def _covers(covered_until: int, counts: tuple[int, ...]) -> bool:
        if covered_until >= len(norm_value):
            return True
        for index, fragment in enumerate(unique_fragments):
            if counts[index] <= 0:
                continue
            if not norm_value.startswith(fragment, covered_until):
                continue
            next_counts = list(counts)
            next_counts[index] -= 1
            if _covers(covered_until + len(fragment), tuple(next_counts)):
                return True
        return False

    return _covers(0, initial_counts)


def locate_field(
    field_value: str,
    blocks: Sequence["OCRTextBlock"],
    division: RegionDivision,
) -> str | None:
    norm_value = normalize(field_value)
    if not norm_value:
        return None

    # 1) 直接单块:归一化后 value 是 block.text 的子串,唯一命中。
    direct_regions: set[str] = set()
    direct_count = 0
    for block in blocks:
        norm_text = normalize(block.text)
        if not norm_text or norm_value not in norm_text:
            continue
        region = region_of_block(block, division)
        if region is None:
            continue
        direct_count += 1
        direct_regions.add(region)
    if direct_count == 1:
        return next(iter(direct_regions))
    if direct_count > 1:
        return None  # 多候选 -> 未知

    # 2) 有意义片段拼接:提取 block.text 中出现在 value 内的片段,且本身"有意义"。
    #    对含"名称:"等标签前缀的块,_value_overlap 剥离前缀后取有效后缀参与覆盖计算。
    fragments: list[tuple[str, str]] = []  # (region, overlap_text)
    for block in blocks:
        norm_text = normalize(block.text)
        if not norm_text:
            continue
        overlap = norm_text if norm_text in norm_value else _value_overlap(norm_text, norm_value)
        if not overlap:
            continue
        if not _is_meaningful(overlap):
            continue
        region = region_of_block(block, division)
        if region is None:
            continue
        fragments.append((region, overlap))

    if not fragments:
        return None
    regions = {region for region, _ in fragments}
    if len(regions) != 1:
        return None  # 跨区域拼接 -> 拒绝
    if not _fragments_cover_value([text for _, text in fragments], norm_value):
        return None  # 拼接未覆盖完整字段值
    return next(iter(regions))


def resolve_pair_division(
    blocks: Sequence["OCRTextBlock"],
    group_a: "FieldGroup",
    group_b: "FieldGroup",
    *,
    page_width: float,
    page_height: float,
) -> RegionDivision | None:
    exclude = tuple(group_a.field_names) + tuple(group_b.field_names)
    anchor_a = find_anchor(
        blocks,
        full_keywords=group_a.anchor_keywords[:1],
        short_keywords=group_a.anchor_keywords[1:],
        exclude_keywords=exclude,
    )
    anchor_b = find_anchor(
        blocks,
        full_keywords=group_b.anchor_keywords[:1],
        short_keywords=group_b.anchor_keywords[1:],
        exclude_keywords=exclude,
    )
    if anchor_a is None or anchor_b is None:
        return None
    return compute_division(
        anchor_a=anchor_a,
        name_a=group_a.name,
        anchor_b=anchor_b,
        name_b=group_b.name,
        page_width=page_width,
        page_height=page_height,
    )


def has_resolvable_pairs(
    field_groups: Sequence["FieldGroup"],
    exclusive_group_pairs: Sequence[tuple[str, str]],
    header: Sequence[str],
    rows: Sequence[object],
) -> bool:
    """是否存在一对互斥字段组,两侧列均可定位且至少一行两列都有值。仅看抽取结果,不碰几何。"""
    if not field_groups or not exclusive_group_pairs or not rows:
        return False
    groups_by_name = {group.name: group for group in field_groups}
    col_index = {name: idx for idx, name in enumerate(header)}
    for name_a, name_b in exclusive_group_pairs:
        group_a = groups_by_name.get(name_a)
        group_b = groups_by_name.get(name_b)
        if group_a is None or group_b is None:
            continue
        role_count = min(len(group_a.field_names), len(group_b.field_names))
        for role in range(role_count):
            idx_a = col_index.get(group_a.field_names[role])
            idx_b = col_index.get(group_b.field_names[role])
            if idx_a is None or idx_b is None:
                continue
            for row in rows:
                values = getattr(row, "values", None)
                if values is None or idx_a >= len(values) or idx_b >= len(values):
                    continue
                value_a = values[idx_a]
                value_b = values[idx_b]
                if value_a is None or value_b is None:
                    continue
                if str(value_a).strip() and str(value_b).strip():
                    return True
    return False
