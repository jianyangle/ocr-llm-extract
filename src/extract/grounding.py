from __future__ import annotations

import functools
import unicodedata

from src.domain.schemas import AppConfig, GroundedCell, GroundedExtractRow

_SEGMENT_SEPARATORS = "；;、，,\n"
_PARTIAL_BOUNDARIES = set("。，；！？\n,:：;")
_MIN_SEGMENT_LEN = 2
_MIN_FUZZY_LEN = 4
_DEFAULT_FUZZY_THRESHOLD = 0.75


def ground_rows(rows: list[list[str]], source_text: str, cfg: AppConfig | None = None) -> list[GroundedExtractRow]:
    grounded_rows: list[GroundedExtractRow] = []
    for row in rows:
        cells: list[GroundedCell] = []
        for value in row:
            cells.append(_ground_cell(value=value, source_text=source_text, cfg=cfg))
        grounded_rows.append(GroundedExtractRow(values=list(row), cells=cells, classification=classify_row(cells)))
    return grounded_rows


def classify_cell(value: str, source_text: str, cfg: AppConfig | None = None) -> GroundedCell:
    return _ground_cell(value=value, source_text=source_text, cfg=cfg)


def classify_row(cells: list[GroundedCell]) -> str:
    key_cells = [cell for cell in cells[:2] if cell.value.strip()]
    if not key_cells:
        return "UNCERTAIN"
    allowed_statuses = {"ASSIGNED", "ASSIGNED_PARTIAL"}
    if any(cell.status not in allowed_statuses for cell in key_cells):
        return "INFERRED"
    if any(cell.value.strip() and cell.status not in allowed_statuses for cell in cells):
        return "INFERRED"
    return "ASSIGNED"


def stronger_status(left: str, right: str) -> str:
    return left if _status_rank(left) >= _status_rank(right) else right


def normalize_value_for_dedupe(value: str) -> str:
    return _normalize_space_token(unicodedata.normalize("NFKC", value))


def _split_value_segments(value: str) -> list[str]:
    segments: list[str] = []
    current: list[str] = []
    for ch in value:
        if ch in _SEGMENT_SEPARATORS:
            segment = "".join(current)
            if segment.strip():
                segments.append(segment)
            current = []
            continue
        current.append(ch)

    segment = "".join(current)
    if segment.strip():
        segments.append(segment)
    return segments


def _try_match_single(value: str, source_text: str, cfg: AppConfig | None = None) -> GroundedCell:
    exact_start = source_text.find(value)
    if exact_start >= 0:
        exact_end = exact_start + len(value)
        if _is_partial_assigned(value=value, source_text=source_text, start=exact_start, end=exact_end):
            segment_start, segment_end = _semantic_segment_bounds(source_text, exact_start, exact_end)
            return GroundedCell(
                value=value,
                source_text=source_text[segment_start:segment_end],
                start=segment_start,
                end=segment_end,
                status="ASSIGNED_PARTIAL",
            )
        return GroundedCell(
            value=value,
            source_text=source_text[exact_start:exact_end],
            start=exact_start,
            end=exact_end,
            status="ASSIGNED",
        )

    compact_span = _find_by_compacted(source_text=source_text, value=value, keep_mode="space")
    if compact_span is not None:
        start, end = compact_span
        return GroundedCell(
            value=value,
            source_text=source_text[start:end],
            start=start,
            end=end,
            status="ASSIGNED",
        )

    canonical_span = _find_by_compacted(source_text=source_text, value=value, keep_mode="alnum")
    if canonical_span is not None:
        start, end = canonical_span
        return GroundedCell(
            value=value,
            source_text=source_text[start:end],
            start=start,
            end=end,
            status="ASSIGNED",
        )

    fuzzy_match = _find_fuzzy_match(value=value, source_text=source_text, cfg=cfg)
    if fuzzy_match is not None:
        start, end = fuzzy_match
        return GroundedCell(
            value=value,
            source_text=source_text[start:end],
            start=start,
            end=end,
            status="INFERRED_FUZZY",
        )

    return GroundedCell(value=value, status="INFERRED")


def _ground_cell(*, value: str, source_text: str, cfg: AppConfig | None = None) -> GroundedCell:
    if not value.strip():
        return GroundedCell(value=value, status="UNCERTAIN")

    segments = _split_value_segments(value)
    has_short_segment = any(len(segment.strip()) < _MIN_SEGMENT_LEN for segment in segments)
    if len(segments) <= 1 or has_short_segment:
        return _try_match_single(value=value, source_text=source_text, cfg=cfg)

    segment_matches = [_try_match_single(value=segment, source_text=source_text, cfg=cfg) for segment in segments]
    if any(match.status == "INFERRED" for match in segment_matches):
        return GroundedCell(value=value, status="UNCERTAIN")

    worst_match = min(segment_matches, key=lambda match: _status_rank(match.status))
    return GroundedCell(
        value=value,
        source_text=worst_match.source_text,
        start=worst_match.start,
        end=worst_match.end,
        status=worst_match.status,
    )


def _find_by_compacted(
    *,
    source_text: str,
    value: str,
    keep_mode: str,
) -> tuple[int, int] | None:
    compact_text, text_index = _compact_with_index(source_text, keep_mode)
    compact_value, _ = _compact_with_index(value, keep_mode)
    if not compact_text or not compact_value:
        return None

    start_in_compact = compact_text.find(compact_value)
    if start_in_compact < 0:
        return None

    end_in_compact = start_in_compact + len(compact_value) - 1
    start = text_index[start_in_compact]
    end = text_index[end_in_compact] + 1
    return start, end


@functools.lru_cache(maxsize=8)
def _compact_with_index(value: str, keep_mode: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    indices: list[int] = []
    for idx, ch in enumerate(value):
        if keep_mode == "space":
            keep = not ch.isspace()
        else:
            keep = ch.isalnum()
        if keep:
            chars.append(ch.lower())
            indices.append(idx)
    return "".join(chars), indices


def _is_partial_assigned(*, value: str, source_text: str, start: int, end: int) -> bool:
    segment_start, segment_end = _semantic_segment_bounds(source_text, start, end)
    segment = source_text[segment_start:segment_end]
    return bool(segment) and segment != value and value in segment


def _semantic_segment_bounds(source_text: str, start: int, end: int) -> tuple[int, int]:
    left = start
    while left > 0 and not _is_partial_boundary(source_text[left - 1]):
        left -= 1

    right = end
    while right < len(source_text) and not _is_partial_boundary(source_text[right]):
        right += 1
    return left, right


def _is_partial_boundary(ch: str) -> bool:
    return ch.isspace() or ch in _PARTIAL_BOUNDARIES


def _find_fuzzy_match(value: str, source_text: str, cfg: AppConfig | None = None) -> tuple[int, int] | None:
    threshold = _DEFAULT_FUZZY_THRESHOLD
    if cfg is not None:
        threshold = float(getattr(cfg, "grounding_fuzzy_threshold", threshold) or threshold)
    return _find_fuzzy_match_cached(value, source_text, threshold)


@functools.lru_cache(maxsize=16)
def _find_fuzzy_match_cached(value: str, source_text: str, threshold: float) -> tuple[int, int] | None:
    normalized_value = _normalize_alnum_token(value)
    if len(normalized_value) < _MIN_FUZZY_LEN:
        return None
    window_size = len(value)
    if window_size <= 0 or len(source_text) < window_size:
        return None

    best_score = 0.0
    best_span: tuple[int, int] | None = None
    step = min(4, max(1, window_size // 8))
    starts = list(range(0, len(source_text) - window_size + 1, step))
    tail_start = len(source_text) - window_size
    if not starts or starts[-1] != tail_start:
        starts.append(tail_start)

    for start in starts:
        end = start + window_size
        score = _char_ngram_jaccard(normalized_value, _normalize_alnum_token(source_text[start:end]))
        if score >= threshold and score > best_score:
            best_score = score
            best_span = (start, end)
    return best_span


@functools.lru_cache(maxsize=4096)
def _normalize_space_token(value: str) -> str:
    return "".join(ch.lower() for ch in value if not ch.isspace())


@functools.lru_cache(maxsize=4096)
def _normalize_alnum_token(value: str) -> str:
    return "".join(ch.lower() for ch in value if ch.isalnum())


@functools.lru_cache(maxsize=4096)
def _char_bigrams(value: str) -> frozenset[str]:
    if len(value) < 2:
        return frozenset(value)
    return frozenset(value[i:i+2] for i in range(len(value) - 1))


def _char_ngram_jaccard(left: str, right: str) -> float:
    left_bigrams = _char_bigrams(left)
    right_bigrams = _char_bigrams(right)
    if not left_bigrams or not right_bigrams:
        return 0.0
    intersection = len(left_bigrams & right_bigrams)
    union = len(left_bigrams | right_bigrams)
    return intersection / union if union else 0.0


def _status_rank(status: str) -> int:
    ranking = {
        "ASSIGNED": 5,
        "ASSIGNED_PARTIAL": 4,
        "INFERRED": 3,
        "INFERRED_FUZZY": 2,
        "UNCERTAIN": 1,
    }
    return ranking.get(status, 0)
