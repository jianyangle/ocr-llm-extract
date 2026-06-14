from __future__ import annotations

from typing import Any

_PP_OCRV5_BASE: dict[str, Any] = {
    "useDocOrientationClassify": True,
    "useTextlineOrientation": True,
    "useDocUnwarping": False,
    "textDetThresh": 0.25,
    "textRecScoreThresh": 0.0,
    "textDetLimitSideLen": 960,
    "textDetLimitType": "max",
    "textDetBoxThresh": 0.6,
    "textDetUnclipRatio": 1.5,
}

ONLINE_OCR_MODEL_PRESETS: dict[str, dict[str, Any]] = {
    "PP-OCRv6": dict(_PP_OCRV5_BASE),
    "PP-OCRv5": dict(_PP_OCRV5_BASE),
    "PP-StructureV3": {
        **_PP_OCRV5_BASE,
        "useRegionDetection": True,
        "layoutNms": True,
        "layoutThreshold": 0.5,
        "layoutUnclipRatio": 1.0,
    },
    "PaddleOCR-VL-1.6": {
        "useDocOrientationClassify": True,
        "useDocUnwarping": False,
        "useLayoutDetection": True,
        "temperature": 0,
        "repetitionPenalty": 1.08,
        "topP": 1.0,
        "layoutThreshold": 0.5,
        "layoutNms": True,
        "layoutUnclipRatio": 1.0,
        "layoutMergeBboxesMode": "large",
    },
}

MODEL_MODULE_SUPPORT: dict[str, set[str]] = {
    "PP-OCRv6": set(),
    "PP-OCRv5": set(),
    "PP-StructureV3": {
        "useTableRecognition",
        "useFormulaRecognition",
        "useChartRecognition",
        "useSealRecognition",
    },
    "PaddleOCR-VL": {
        "useChartRecognition",
    },
}

_VL_PREFIX = "PaddleOCR-VL"


def is_vl_family(model: str) -> bool:
    return model.startswith(_VL_PREFIX)
