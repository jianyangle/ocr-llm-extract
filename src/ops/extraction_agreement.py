from __future__ import annotations

from src.extract.grounding import normalize_value_for_dedupe


def _norm_row(row: list[str]) -> tuple[str, ...]:
    return tuple(normalize_value_for_dedupe(str(cell)) for cell in row)


def _cell_matches(c_row: tuple[str, ...], r_row: tuple[str, ...]) -> int:
    return sum(1 for a, b in zip(c_row, r_row) if a == b)


def _take_key_match(
    remaining: list[int],
    cand: list[tuple[str, ...]],
    r_row: tuple[str, ...],
) -> int | None:
    """在剩余候选里挑首列 key 与参考行相同、且单元格匹配最多的一行；取走并返回其下标。

    首列为空（缺失占位）时返回 None，交给按序回退配对。
    """
    key = r_row[0] if r_row else ""
    if not key:
        return None
    pool = [i for i in remaining if cand[i] and cand[i][0] == key]
    if not pool:
        return None
    best = max(pool, key=lambda i: _cell_matches(cand[i], r_row))
    remaining.remove(best)
    return best


def score_sample(
    *,
    candidate_rows: list[list[str]],
    reference_rows: list[list[str]],
) -> tuple[int, int]:
    """以 reference 为参考答案，按首列 key 贪心对齐算字段一致。

    返回 (matched_cells, denom)。denom = max(候选行数, 参考行数) × 列数，
    同时惩罚漏抽与多抽。优先按首列归一化 key 配对（避免整行排序 zip 在
    个别单元格不同时错位低估），首列为空的参考行回退按出现顺序配对。
    归一化复用 grounding 去重口径。
    """
    cand = [_norm_row(r) for r in candidate_rows]
    ref = [_norm_row(r) for r in reference_rows]
    if not ref and not cand:
        return 0, 0
    ncols = len(ref[0]) if ref else len(cand[0])
    remaining = list(range(len(cand)))
    matched = 0
    leftover_refs: list[tuple[str, ...]] = []
    # 第一轮：首列 key 匹配
    for r_row in ref:
        idx = _take_key_match(remaining, cand, r_row)
        if idx is None:
            leftover_refs.append(r_row)
        else:
            matched += _cell_matches(cand[idx], r_row)
    # 第二轮：剩余参考行按序配对剩余候选行
    for r_row in leftover_refs:
        if not remaining:
            break
        idx = remaining.pop(0)
        matched += _cell_matches(cand[idx], r_row)
    denom = max(len(cand), len(ref)) * ncols
    return matched, denom


def aggregate_agreement(per_sample: list[tuple[int, int]]) -> float:
    total_matched = sum(m for m, _ in per_sample)
    total_denom = sum(d for _, d in per_sample)
    return total_matched / total_denom if total_denom else 0.0
