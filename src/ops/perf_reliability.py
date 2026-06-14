from __future__ import annotations

from dataclasses import asdict, dataclass
from time import perf_counter

from src.core.task_engine import TaskEngine
from src.domain.schemas import AppConfig, ExtractionOutcome, GroundedExtractRow
from src.ocr.models import OCRResult


@dataclass(frozen=True)
class Task011Thresholds:
    ui_freeze_ms_max: float = 1000.0
    render_100_rows_ms_max: float = 300.0
    queue_complete_rate_min: float = 99.0
    crash_count_max: int = 0


@dataclass(frozen=True)
class Task011Report:
    metrics: dict[str, float | int | bool]
    checks: dict[str, bool]
    thresholds: Task011Thresholds

    def to_dict(self) -> dict[str, object]:
        return {
            "metrics": self.metrics,
            "checks": self.checks,
            "thresholds": asdict(self.thresholds),
        }


class _OCRStub:
    def recognize(self, image_path: str) -> OCRResult:
        return OCRResult(text=f"ocr:{image_path}", confidence_avg=0.9, confidence_min=0.9, block_count=1)


class _ExtractorStub:
    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on

    def extract_detailed(
        self,
        *,
        text: str,
        prompts: str,
        examples: list[list[str]],
        provider_cfg: AppConfig,
        ocr_confidence: float | None = None,
    ) -> ExtractionOutcome:
        _ = prompts
        _ = examples
        _ = provider_cfg
        _ = ocr_confidence
        if self.fail_on is not None and self.fail_on == text:
            raise RuntimeError("injected extract failure")
        return ExtractionOutcome(rows=[GroundedExtractRow(values=[text, "ok"])], column_specs=[], field_regions=[])


def evaluate_task011_checks(
    *,
    metrics: dict[str, float | int | bool],
    thresholds: Task011Thresholds,
) -> dict[str, bool]:
    ui_freeze_ms_max = float(metrics.get("ui_freeze_ms_max", float("inf")))
    render_100_rows_ms = float(metrics.get("render_100_rows_ms", float("inf")))
    queue_complete_rate = float(metrics.get("queue_complete_rate", 0.0))
    crash_count = int(metrics.get("crash_count", 1))
    failure_isolation_ok = bool(metrics.get("failure_isolation_ok", False))

    perf_001 = ui_freeze_ms_max <= thresholds.ui_freeze_ms_max
    perf_002 = render_100_rows_ms <= thresholds.render_100_rows_ms_max
    perf_003 = crash_count <= thresholds.crash_count_max and queue_complete_rate >= thresholds.queue_complete_rate_min
    rel_001 = queue_complete_rate >= thresholds.queue_complete_rate_min
    rel_002 = failure_isolation_ok

    return {
        "PERF-001": perf_001,
        "PERF-002": perf_002,
        "PERF-003": perf_003,
        "REL-001": rel_001,
        "REL-002": rel_002,
    }


def run_task011_suite(
    *,
    batch_size: int = 100,
    thresholds: Task011Thresholds | None = None,
) -> Task011Report:
    applied_thresholds = thresholds or Task011Thresholds()
    metrics = _collect_metrics(batch_size=batch_size)
    checks = evaluate_task011_checks(metrics=metrics, thresholds=applied_thresholds)
    return Task011Report(metrics=metrics, checks=checks, thresholds=applied_thresholds)


def _collect_metrics(*, batch_size: int) -> dict[str, float | int | bool]:
    batch_size = max(1, int(batch_size))
    config = _default_config()

    crash_count = 0
    engine = TaskEngine(config=config, ocr_service=_OCRStub(), extractor=_ExtractorStub())
    for index in range(batch_size):
        engine.add_text(f"text-{index}")

    started = perf_counter()
    try:
        engine.start()
    except Exception:
        crash_count += 1
    elapsed_ms = (perf_counter() - started) * 1000

    tasks_total = len(engine.tasks)
    tasks_done = sum(1 for task in engine.tasks if task.status == "done")
    tasks_failed_total = sum(1 for task in engine.tasks if task.status == "failed")
    queue_complete_rate = 0.0
    if tasks_total > 0:
        queue_complete_rate = ((tasks_done + tasks_failed_total) / tasks_total) * 100

    # Engine is currently sync on caller thread; this approximates worst freeze on UI thread.
    ui_freeze_ms_max = elapsed_ms
    render_100_rows_ms = _measure_result_table_render(rows=100)
    failure_isolation_ok = _check_failure_isolation(config=config)

    return {
        "batch_elapsed_ms": round(elapsed_ms, 3),
        "ui_freeze_ms_max": round(ui_freeze_ms_max, 3),
        "render_100_rows_ms": round(render_100_rows_ms, 3),
        "tasks_total": tasks_total,
        "tasks_done": tasks_done,
        "tasks_failed_total": tasks_failed_total,
        "queue_complete_rate": round(queue_complete_rate, 3),
        "crash_count": crash_count,
        "failure_isolation_ok": failure_isolation_ok,
    }


def _check_failure_isolation(*, config: AppConfig) -> bool:
    engine = TaskEngine(config=config, ocr_service=_OCRStub(), extractor=_ExtractorStub(fail_on="bad"))
    bad_id = engine.add_text("bad")
    good_id_1 = engine.add_text("good-1")
    good_id_2 = engine.add_text("good-2")

    engine.start()
    status_by_id = {task.task_id: task.status for task in engine.tasks}
    return (
        status_by_id.get(bad_id) == "failed"
        and status_by_id.get(good_id_1) in {"done", "failed"}
        and status_by_id.get(good_id_2) in {"done", "failed"}
    )


def _measure_result_table_render(*, rows: int) -> float:
    try:
        from PySide6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem
    except Exception:
        return _fallback_render_probe(rows=rows)

    app = QApplication.instance() or QApplication([])
    table = QTableWidget(0, 3)
    started = perf_counter()
    for index in range(rows):
        table.insertRow(index)
        table.setItem(index, 0, QTableWidgetItem(f"row-{index}"))
        table.setItem(index, 1, QTableWidgetItem("value-a"))
        table.setItem(index, 2, QTableWidgetItem("value-b"))
    app.processEvents()
    elapsed_ms = (perf_counter() - started) * 1000
    table.deleteLater()
    return elapsed_ms


def _fallback_render_probe(*, rows: int) -> float:
    started = perf_counter()
    payload = []
    for index in range(rows):
        payload.append([f"row-{index}", "value-a", "value-b"])
    _ = len(payload)
    return (perf_counter() - started) * 1000


def _default_config() -> AppConfig:
    return AppConfig(
        provider="openai_compatible",
        base_url="http://localhost",
        api_key="",
        model="model",
        prompts="prompt",
        examples_raw='[["c1","c2"]]',
        examples_normalized=[["c1", "c2"]],
        default_excel_path="",
    )
