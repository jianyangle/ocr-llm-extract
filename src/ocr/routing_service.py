from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.ocr.models import OCRResult
from src.ocr.online_model_presets import (
    MODEL_MODULE_SUPPORT,
    ONLINE_OCR_MODEL_PRESETS,
    is_vl_family,
)
from src.ocr.online_service import OnlineOCRConfig, OnlineOCRService
from src.ocr.paddle_service import OCRRuntimeOptions, PaddleOCRService


@dataclass(frozen=True)
class RoutingRuntimeOptions:
    """路由 OCR 的运行时配置：本地 + 在线两侧选项与开关。"""

    use_online: bool
    paddle_options: OCRRuntimeOptions
    online_config: OnlineOCRConfig


class RoutingOCRService:
    """图片/裁剪识别的本地-在线分发器。

    仅负责 image + crop 路由；PDF 的在线文档流程由 ``OnlinePdfOCRProcessor`` 处理，
    入口为 ``TaskOrchestrator._run_ocr_stage``（依据 ``config.ocr_use_online`` 分支注入）。
    对外暴露与 ``PaddleOCRService`` 兼容的 ``recognize`` / ``reset_runtime`` /
    ``runtime_options_from_app_config`` 接口，便于上层无差别注入。
    """

    def __init__(
        self,
        *,
        local: PaddleOCRService,
        online: OnlineOCRService,
        config: Any,
    ) -> None:
        self._local = local
        self._online = online
        self._use_online = bool(getattr(config, "ocr_use_online", False))

    def maybe_preload_local(self) -> None:
        """仅当当前走本地 OCR 时，触发本地引擎预热；在线模式直接跳过。"""
        if not self._use_online:
            self._local.preload()

    def should_preload_local(self) -> bool:
        """当前运行时是否需要在启动队列前预热本地 OCR。"""
        return not self._use_online

    def recognize(
        self,
        image_path: str,
        *,
        crop: tuple[int, int, int, int] | None = None,
        allow_retry: bool = True,
    ) -> OCRResult:
        target = self._online if self._use_online else self._local
        return target.recognize(image_path, crop=crop, allow_retry=allow_retry)

    @classmethod
    def runtime_options_from_app_config(cls, config: Any) -> RoutingRuntimeOptions:
        use_online = bool(getattr(config, "ocr_use_online", False))
        paddle_options = PaddleOCRService.runtime_options_from_app_config(config)

        profiles = getattr(config, "ocr_online_profiles", {}) or {}
        platform_id = getattr(config, "ocr_online_platform_id", "")
        profile = profiles.get(platform_id, {}) if isinstance(profiles, dict) else {}

        model = str(profile.get("model", ""))
        extra_payload = _build_online_extra_payload(model, config)

        online_config = OnlineOCRConfig(
            base_url=str(profile.get("base_url", "")),
            api_key=str(profile.get("api_key", "")),
            model=model,
            use_doc_orientation_classify=bool(
                getattr(config, "ocr_use_doc_orientation_classify", False)
            ),
            use_doc_unwarping=bool(getattr(config, "ocr_use_doc_unwarping", False)),
            use_textline_orientation=bool(
                getattr(config, "ocr_use_textline_orientation", False)
            ),
            extra_payload=extra_payload,
        )

        return RoutingRuntimeOptions(
            use_online=use_online,
            paddle_options=paddle_options,
            online_config=online_config,
        )

    def reset_runtime(self, options: RoutingRuntimeOptions) -> None:
        self._use_online = options.use_online
        self._local.reset_runtime(options.paddle_options)
        self._online.update_config(options.online_config)

    def update_runtime_options(self, options: RoutingRuntimeOptions) -> None:
        """``reset_runtime`` 的别名，供 main_window 的"保存即生效"路径调用。"""
        self.reset_runtime(options)


_MODULE_TOGGLE_MAP: dict[str, str] = {
    "ocr_online_use_table_recognition": "useTableRecognition",
    "ocr_online_use_formula_recognition": "useFormulaRecognition",
    "ocr_online_use_chart_recognition": "useChartRecognition",
    "ocr_online_use_seal_recognition": "useSealRecognition",
}


def _build_online_extra_payload(model: str, config: Any) -> dict[str, Any]:
    preset = ONLINE_OCR_MODEL_PRESETS.get(model)
    if preset is None:
        return {}

    payload: dict[str, Any] = dict(preset)

    # MODEL_MODULE_SUPPORT 的 key 之间不存在前缀包含关系，新增 key 时需保证这一点。
    for model_prefix, supported_keys in MODEL_MODULE_SUPPORT.items():
        if not model.startswith(model_prefix):
            continue
        for config_attr, api_key in _MODULE_TOGGLE_MAP.items():
            if api_key in supported_keys:
                payload[api_key] = bool(getattr(config, config_attr, False))
        break

    if is_vl_family(model):
        payload.pop("useTextlineOrientation", None)

    return payload
