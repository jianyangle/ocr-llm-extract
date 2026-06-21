from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.domain.schemas import TaskItem

_RUNNING_STATUSES = {"running_ocr", "running_extract"}


@dataclass(frozen=True)
class AdmissionDecision:
    """Pipeline Feeder 每轮咨询 Admission Policy 得到的三态结果。

    - ``"admit"``：准入 ``task``；Feeder 在 ``state_lock`` 内调用 ``_start_pending_task``。
    - ``"wait"`` ：当前无可准入但仍有任务在跑/待跑；Feeder 应 poll-sleep 后再问。
    - ``"done"`` ：无 pending、无在跑；Feeder 退出循环并发 ``stop_signal``。
    """

    kind: Literal["admit", "wait", "done"]
    task: TaskItem | None = None

    def __post_init__(self) -> None:
        if self.kind == "admit" and self.task is None:
            raise ValueError("AdmissionDecision(kind='admit') requires a task")


def decide_admission(tasks: list[TaskItem], seen: set[str]) -> AdmissionDecision:
    """纯函数：给定有序任务列表与本次 run 已启动的 ``seen`` 集，返回三态准入决策。

    规则（等价于旧 ``_feed_pending_tasks`` 的内联逻辑）：
      - 扫描顺序取第一个 ``status == "pending"`` 且 ``task_id`` 不在 ``seen`` 的任务。
      - text 任务：仅当流水线空闲（无 seen 任务处于 ``running_*``）才 ADMIT。
      - image/pdf 任务：仅当 OCR 未被占用且无 text 在跑（``not ocr_busy and not text_busy``）才 ADMIT。
      - 无可准入：无 pending 且无在跑 → DONE；否则 → WAIT。

    不产生副作用，仅读取 ``task_id`` / ``status`` / ``source_type``。
    """
    ocr_busy = any(t.task_id in seen and t.status == "running_ocr" for t in tasks)
    pipeline_busy = any(
        t.task_id in seen and t.status in _RUNNING_STATUSES for t in tasks
    )
    text_busy = any(
        t.task_id in seen
        and t.source_type == "text"
        and t.status in _RUNNING_STATUSES
        for t in tasks
    )

    for task in tasks:
        if task.status != "pending" or task.task_id in seen:
            continue
        if task.source_type == "text":
            if pipeline_busy:
                break
            return AdmissionDecision(kind="admit", task=task)
        if ocr_busy or text_busy:
            break
        return AdmissionDecision(kind="admit", task=task)

    has_pending = any(
        t.task_id not in seen and t.status == "pending" for t in tasks
    )
    if not pipeline_busy and not has_pending:
        return AdmissionDecision(kind="done")
    return AdmissionDecision(kind="wait")
