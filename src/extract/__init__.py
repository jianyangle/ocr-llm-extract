from typing import Protocol

from src.domain.schemas import AppConfig, ExtractionInput, ExtractionOutcome

from .errors import ExtractServiceError
from .example_parser import format_examples
from .grounding import ground_rows
from .llm_extractor import LLMExtractor
from .output_normalizer import normalize_rows, parse_rows_payload


class Extractor(Protocol):
    def extract_detailed(
        self,
        *,
        text: ExtractionInput | str,
        prompts: str,
        examples: list[list[str]],
        provider_cfg: AppConfig,
        ocr_confidence: float | None = None,
    ) -> ExtractionOutcome:
        ...

__all__ = [
    "ExtractionOutcome",
    "Extractor",
    "ExtractServiceError",
    "format_examples",
    "ground_rows",
    "LLMExtractor",
    "normalize_rows",
    "parse_rows_payload",
]
