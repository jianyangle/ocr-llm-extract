from __future__ import annotations

import dataclasses
import logging

from src.domain.schemas import AppConfig

_logger = logging.getLogger(__name__)

_OVERRIDABLE_FIELDS = frozenset(
    f.name for f in dataclasses.fields(AppConfig) if f.init and f.name != "ollama_overrides"
)


def apply_ollama_overrides(config: AppConfig) -> AppConfig:
    """仅当 provider==ollama 且有 overrides 时，按覆盖层生成生效配置。

    对在线 provider 或空 overrides 返回原对象（恒等），保证在线链路零影响。
    """
    if getattr(config, "provider", None) != "ollama":
        return config
    overrides = getattr(config, "ollama_overrides", None)
    if not overrides:
        return config

    filtered: dict[str, object] = {}
    for key, value in overrides.items():
        if key in _OVERRIDABLE_FIELDS:
            filtered[key] = value
        else:
            _logger.warning("apply_ollama_overrides: 忽略未知/不可覆盖字段 %r", key)
    if not filtered:
        return config

    effective = dataclasses.replace(config, **filtered)
    # init=False 字段会被 replace 重置为默认值，显式回填原值
    effective.extraction_profile_custom = config.extraction_profile_custom
    effective.ocr_profile_custom = config.ocr_profile_custom
    return effective
