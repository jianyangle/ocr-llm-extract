from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.domain.schemas import LineRules


@dataclass
class LineBlock:
    rows: list[dict[str, str]]


@dataclass
class LineExtractResult:
    matched: bool
    rows: list[dict[str, str]]
    unmatched_text: str
    diagnostics: list[str] = field(default_factory=list)
    blocks: list[LineBlock] = field(default_factory=list)


def extract_by_line_rules(
    *,
    text: str,
    rules: LineRules,
) -> LineExtractResult:
    """按 LineRules 从文本中抽取明细行，支持多块（多张发票）场景。

    状态机流转：seeking_start → scanning → seeking_start（循环）。
    遇到 end 命中后回到 seeking_start，而非进入永久 done 终态，
    使后续发票的表头行能再次触发 scanning，抓取更多明细。

    matched 判定：不依赖最终状态，而是看是否命中过至少一个完整块（start→end）。
    兼容性保证：
      - start 未命中 → matched=False
      - start 命中但全程无 end → matched=False（仍保持原语义）
      - 单块（一个 start→end）→ 与旧实现行为相同
    """
    state = "seeking_start"
    matched_rows: list[dict[str, str]] = []
    matched_blocks: list[LineBlock] = []
    current_block_rows: list[dict[str, str]] | None = None
    unmatched_buffer: list[str] = []
    diagnostics: list[str] = []
    lines = text.splitlines()

    # 是否曾进入过 scanning 状态（命中过 start）
    found_start = False
    # 是否命中过至少一个完整块（start→end 均命中）
    found_complete_block = False

    start_re = re.compile(rules.start)
    end_re = re.compile(rules.end)
    line_re = re.compile(rules.line)
    first_line_re = re.compile(rules.first_line) if rules.first_line else None
    last_line_re = re.compile(rules.last_line) if rules.last_line else None
    skip_line_re = re.compile(rules.skip_line) if rules.skip_line else None

    for line in lines:
        if state == "seeking_start":
            if start_re.search(line):
                # 命中表头行，进入扫描状态
                found_start = True
                state = "scanning"
                current_block_rows = []
                if first_line_re and first_line_re.search(line):
                    # first_line 匹配说明该行是纯表头，跳过不抽数据
                    continue
                line_match = line_re.search(line)
                if line_match is not None:
                    row = {key: str(value) for key, value in line_match.groupdict().items()}
                    matched_rows.append(row)
                    current_block_rows.append(row)
            else:
                unmatched_buffer.append(line)
            continue

        if state == "scanning":
            if end_re.search(line):
                # 命中 end：记录完整块，回到 seeking_start 以支持多块
                found_complete_block = True
                if last_line_re:
                    last_match = last_line_re.search(line)
                    if last_match is not None:
                        row = {key: str(value) for key, value in last_match.groupdict().items()}
                        matched_rows.append(row)
                        if current_block_rows is not None:
                            current_block_rows.append(row)
                matched_blocks.append(LineBlock(rows=list(current_block_rows or [])))
                current_block_rows = None
                state = "seeking_start"
                continue
            if skip_line_re and skip_line_re.search(line):
                diagnostics.append(f"skipped: {line[:40]}")
                continue
            line_match = line_re.search(line)
            if line_match is not None:
                row = {key: str(value) for key, value in line_match.groupdict().items()}
                matched_rows.append(row)
                if current_block_rows is not None:
                    current_block_rows.append(row)
            else:
                diagnostics.append(f"unmatched in body: {line[:40]}")
            continue

        # 状态机只有 seeking_start / scanning 两态，上面两个分支已覆盖全部状态并
        # 各以 continue 收尾。走到这里说明引入了未处理的新状态——立即暴露，不静默兜底。
        raise AssertionError(f"unreachable state: {state}")

    # 终态判断：
    # 1. start 从未命中 → matched=False
    if not found_start:
        return LineExtractResult(matched=False, rows=[], unmatched_text=text, diagnostics=["start not found"])
    # 2. start 命中但全程无 end（当前仍在 scanning 态） → matched=False（截断语义）
    if state == "scanning":
        diagnostics.append("end pattern not found; truncated")
        return LineExtractResult(matched=False, rows=[], unmatched_text=text, diagnostics=diagnostics)
    # 3. 命中过完整块（found_complete_block=True）→ matched=True
    #    注：多块后最后一块 end 命中会回到 seeking_start，故不能用 state=="done" 来判断
    return LineExtractResult(
        matched=found_complete_block,
        rows=matched_rows,
        unmatched_text="\n".join(unmatched_buffer),
        diagnostics=diagnostics,
        blocks=matched_blocks,
    )
