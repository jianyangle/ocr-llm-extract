from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.domain.schemas import LineRules


@dataclass
class LineExtractResult:
    matched: bool
    rows: list[dict[str, str]]
    unmatched_text: str
    diagnostics: list[str] = field(default_factory=list)


def extract_by_line_rules(
    *,
    text: str,
    rules: LineRules,
) -> LineExtractResult:
    state = "seeking_start"
    matched_rows: list[dict[str, str]] = []
    unmatched_buffer: list[str] = []
    diagnostics: list[str] = []
    lines = text.splitlines()

    start_re = re.compile(rules.start)
    end_re = re.compile(rules.end)
    line_re = re.compile(rules.line)
    first_line_re = re.compile(rules.first_line) if rules.first_line else None
    last_line_re = re.compile(rules.last_line) if rules.last_line else None
    skip_line_re = re.compile(rules.skip_line) if rules.skip_line else None

    for line in lines:
        if state == "seeking_start":
            if start_re.search(line):
                state = "scanning"
                if first_line_re and first_line_re.search(line):
                    continue
                line_match = line_re.search(line)
                if line_match is not None:
                    matched_rows.append({key: str(value) for key, value in line_match.groupdict().items()})
            else:
                unmatched_buffer.append(line)
            continue

        if state == "scanning":
            if end_re.search(line):
                state = "done"
                if last_line_re:
                    last_match = last_line_re.search(line)
                    if last_match is not None:
                        matched_rows.append({key: str(value) for key, value in last_match.groupdict().items()})
                continue
            if skip_line_re and skip_line_re.search(line):
                diagnostics.append(f"skipped: {line[:40]}")
                continue
            line_match = line_re.search(line)
            if line_match is not None:
                matched_rows.append({key: str(value) for key, value in line_match.groupdict().items()})
            else:
                diagnostics.append(f"unmatched in body: {line[:40]}")
            continue

        unmatched_buffer.append(line)

    if state == "seeking_start":
        return LineExtractResult(matched=False, rows=[], unmatched_text=text, diagnostics=["start not found"])
    if state == "scanning":
        diagnostics.append("end pattern not found; truncated")
        return LineExtractResult(matched=False, rows=[], unmatched_text=text, diagnostics=diagnostics)
    return LineExtractResult(
        matched=True,
        rows=matched_rows,
        unmatched_text="\n".join(unmatched_buffer),
        diagnostics=diagnostics,
    )
