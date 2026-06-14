from __future__ import annotations

from typing import Any, Mapping, Sequence


DEFAULT_PHASES: tuple[str, ...] = ("baseline", "phase1", "phase2", "phase3")


def evaluate_accuracy_dataset(
    payload: Mapping[str, Any],
    *,
    phases: Sequence[str] = DEFAULT_PHASES,
) -> dict[str, Any]:
    sample_items = payload.get("samples")
    if not isinstance(sample_items, list):
        raise ValueError("dataset payload must contain a 'samples' list")

    resolved_phases = _normalize_phases(phases)

    totals: dict[str, dict[str, float]] = {
        phase: {
            "char_distance_total": 0.0,
            "char_ref_total": 0.0,
            "word_distance_total": 0.0,
            "word_ref_total": 0.0,
            "field_correct_total": 0.0,
            "field_total": 0.0,
        }
        for phase in resolved_phases
    }

    for sample in sample_items:
        if not isinstance(sample, dict):
            continue
        gt_text = _to_text(sample.get("ground_truth_text"))
        gt_words = _tokenize_words(gt_text)
        gt_fields = _to_text_map(sample.get("ground_truth_fields"))
        ocr_predictions = sample.get("ocr_predictions")
        if not isinstance(ocr_predictions, Mapping):
            ocr_predictions = {}
        field_predictions = sample.get("field_predictions")
        if not isinstance(field_predictions, Mapping):
            field_predictions = {}

        for phase in resolved_phases:
            pred_text = _to_text(ocr_predictions.get(phase))
            totals[phase]["char_distance_total"] += float(_levenshtein_distance(gt_text, pred_text))
            if len(gt_text) > 0:
                totals[phase]["char_ref_total"] += float(len(gt_text))

            pred_words = _tokenize_words(pred_text)
            totals[phase]["word_distance_total"] += float(_levenshtein_distance(gt_words, pred_words))
            if len(gt_words) > 0:
                totals[phase]["word_ref_total"] += float(len(gt_words))

            phase_field_predictions = field_predictions.get(phase)
            if not isinstance(phase_field_predictions, Mapping):
                phase_field_predictions = {}
            field_correct, field_total = _calc_field_match(gt_fields, phase_field_predictions)
            totals[phase]["field_correct_total"] += float(field_correct)
            totals[phase]["field_total"] += float(field_total)

    phase_metrics: dict[str, dict[str, Any]] = {}
    for phase in resolved_phases:
        phase_totals = totals[phase]
        phase_metrics[phase] = {
            "cer": _safe_div(phase_totals["char_distance_total"], phase_totals["char_ref_total"]),
            "wer": _safe_div(phase_totals["word_distance_total"], phase_totals["word_ref_total"]),
            "field_accuracy": _safe_div(phase_totals["field_correct_total"], phase_totals["field_total"]),
            "char_distance_total": int(phase_totals["char_distance_total"]),
            "char_ref_total": int(phase_totals["char_ref_total"]),
            "word_distance_total": int(phase_totals["word_distance_total"]),
            "word_ref_total": int(phase_totals["word_ref_total"]),
            "field_correct_total": int(phase_totals["field_correct_total"]),
            "field_total": int(phase_totals["field_total"]),
        }

    baseline = phase_metrics.get("baseline")
    comparisons: dict[str, dict[str, float]] = {}
    if baseline is not None:
        for phase, metrics in phase_metrics.items():
            if phase == "baseline":
                continue
            comparisons[phase] = {
                "cer_relative_improvement_vs_baseline": _relative_improvement(
                    float(baseline["cer"]),
                    float(metrics["cer"]),
                ),
                "wer_relative_improvement_vs_baseline": _relative_improvement(
                    float(baseline["wer"]),
                    float(metrics["wer"]),
                ),
                "field_accuracy_absolute_gain_vs_baseline": round(
                    float(metrics["field_accuracy"]) - float(baseline["field_accuracy"]),
                    6,
                ),
            }

    return {
        "sample_count": len(sample_items),
        "phases": phase_metrics,
        "comparisons": comparisons,
    }


def _normalize_phases(phases: Sequence[str]) -> tuple[str, ...]:
    resolved = []
    for phase in phases:
        key = str(phase).strip()
        if not key:
            continue
        if key in resolved:
            continue
        resolved.append(key)
    if not resolved:
        raise ValueError("phases must contain at least one phase name")
    return tuple(resolved)


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _tokenize_words(text: str) -> list[str]:
    return [token for token in text.split() if token]


def _to_text_map(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    output: dict[str, str] = {}
    for key, raw in value.items():
        output[str(key)] = _to_text(raw).strip()
    return output


def _calc_field_match(ground_truth: Mapping[str, str], predictions: Mapping[str, Any]) -> tuple[int, int]:
    if not ground_truth:
        return 0, 0
    pred_map = _to_text_map(predictions)
    correct = 0
    total = 0
    for key, gt_value in ground_truth.items():
        total += 1
        if pred_map.get(key, "").strip() == gt_value.strip():
            correct += 1
    return correct, total


def _safe_div(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


def _relative_improvement(baseline_error: float, candidate_error: float) -> float:
    if baseline_error <= 0:
        return 0.0
    return round((baseline_error - candidate_error) / baseline_error, 6)


def _levenshtein_distance(source: Sequence[Any], target: Sequence[Any]) -> int:
    source_items = list(source)
    target_items = list(target)
    if source_items == target_items:
        return 0
    if not source_items:
        return len(target_items)
    if not target_items:
        return len(source_items)

    previous = list(range(len(target_items) + 1))
    for row, src_value in enumerate(source_items, start=1):
        current = [row]
        for col, tgt_value in enumerate(target_items, start=1):
            insert_cost = current[col - 1] + 1
            delete_cost = previous[col] + 1
            replace_cost = previous[col - 1] + (0 if src_value == tgt_value else 1)
            current.append(min(insert_cost, delete_cost, replace_cost))
        previous = current
    return previous[-1]
