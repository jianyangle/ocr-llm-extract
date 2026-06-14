# 移植自 Umi-OCR (MIT, hiroi-sora)
# 源文件：UmiOCR-data/py_src/ocr/tbpu/

from __future__ import annotations

from src.ocr.models import OCRTextBlock


class Tbpu:
    def run(self, blocks: list[OCRTextBlock]) -> list[OCRTextBlock]:
        raise NotImplementedError


from .parsers import (
    MultiLine,
    MultiNone,
    MultiPara,
    ParserNone,
    SingleCode,
    SingleLine,
    SingleNone,
    SinglePara,
)

Parser = {
    "none": ParserNone,
    "multi_none": MultiNone,
    "multi_line": MultiLine,
    "multi_para": MultiPara,
    "single_none": SingleNone,
    "single_line": SingleLine,
    "single_para": SinglePara,
    "single_code": SingleCode,
}

_LEGACY_LAYOUT_MAP = {
    "auto": "multi_para",
    "single_column": "single_para",
    "multi_column": "multi_para",
}


def get_parser(key: str) -> Tbpu:
    normalized = _LEGACY_LAYOUT_MAP.get(str(key).strip().lower(), str(key).strip().lower())
    parser_cls = Parser.get(normalized, ParserNone)
    return parser_cls()


__all__ = ["Parser", "Tbpu", "get_parser"]
