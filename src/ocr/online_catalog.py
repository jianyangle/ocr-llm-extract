from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


DEFAULT_ONLINE_OCR_PLATFORM_ID = "baidu_paddle"

AuthScheme = Literal["bearer"]


@dataclass(frozen=True)
class OnlineOCREntry:
    id: str
    display_name: str
    default_base_url: str
    logo_asset: str
    website_url: str
    api_key_url: str
    requires_api_key: bool
    auth_scheme: AuthScheme
    recommended_models: tuple[str, ...]


_CATALOG: tuple[OnlineOCREntry, ...] = (
    OnlineOCREntry(
        id="baidu_paddle",
        display_name="百度 PaddleOCR (AI Studio)",
        default_base_url="https://paddleocr.aistudio-app.com/api/v2/ocr/jobs",
        logo_asset="providers/baidu_paddle.png",
        website_url="https://aistudio.baidu.com/paddleocr/task",
        api_key_url="https://aistudio.baidu.com/account/accessToken",
        requires_api_key=True,
        auth_scheme="bearer",
        recommended_models=("PaddleOCR-VL-1.6", "PP-OCRv6", "PP-StructureV3"),
    ),
)

ONLINE_OCR_PLATFORM_IDS = tuple(entry.id for entry in _CATALOG)
_BY_ID = {entry.id: entry for entry in _CATALOG}


def get_online_ocr_catalog() -> tuple[OnlineOCREntry, ...]:
    return _CATALOG


def get_online_ocr_entry(platform_id: str) -> OnlineOCREntry:
    return _BY_ID.get(platform_id, _BY_ID[DEFAULT_ONLINE_OCR_PLATFORM_ID])


def catalog_default_online_profiles() -> dict[str, dict[str, str]]:
    return {
        entry.id: {
            "base_url": entry.default_base_url,
            "api_key": "",
            "model": entry.recommended_models[0] if entry.recommended_models else "",
        }
        for entry in _CATALOG
    }
