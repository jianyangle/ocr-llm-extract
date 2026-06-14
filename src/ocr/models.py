from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class OCRTextBlock:
    text: str
    score: float | None
    box: list[list[float]]
    end: str = ""


@dataclass(frozen=True)
class OCRResult:
    text: str
    confidence_avg: float | None
    confidence_min: float | None
    block_count: int
    blocks: list[OCRTextBlock] = field(default_factory=list)
    retry_triggered: bool = False
    retry_applied: bool = False
    retry_profile_from: str | None = None
    retry_profile_to: str | None = None
    first_pass_confidence_min: float | None = None
    second_pass_confidence_min: float | None = None
    markdown: str | None = None
