from __future__ import annotations

from typing import Any, Callable

import cv2
import numpy as np

from src.extract.connection_check import ConnectionCheckResult
from src.ocr.errors import OCRServiceError
from src.ocr.online_client import OnlineOCRClient


def _tiny_png_bytes() -> bytes:
    """生成一张 1x1 PNG 作为连通性探针文件。"""
    success, buf = cv2.imencode(".png", np.zeros((1, 1, 3), np.uint8))
    if not success:
        raise RuntimeError("_tiny_png_bytes: cv2.imencode failed")
    return buf.tobytes()


def check_online_ocr_connection(
    *,
    base_url: str,
    api_key: str,
    model: str = "",
    http_post: Callable[..., Any] | None = None,
) -> ConnectionCheckResult:
    """以「仅提交」探针校验在线 OCR 连通性：提交一张 1x1 PNG，拿到 jobId 即视为成功，不轮询。"""
    if not (base_url or "").strip():
        return ConnectionCheckResult(ok=False, detail="服务地址未填写")
    if not (api_key or "").strip():
        return ConnectionCheckResult(ok=False, detail="API Key 未填写")

    client = OnlineOCRClient(
        base_url=base_url,
        api_key=api_key,
        model=model,
        optional_payload={},
        http_post=http_post,
    )

    try:
        client.submit(file_bytes=_tiny_png_bytes(), filename="probe.png")
    except OCRServiceError as exc:
        if exc.code == "E_OCR_012":
            return ConnectionCheckResult(ok=False, detail="鉴权失败：API Key 无效或无权限")
        if exc.code == "E_OCR_013":
            return ConnectionCheckResult(ok=False, detail=f"服务返回错误：{exc.message}")
        return ConnectionCheckResult(ok=False, detail=f"连接失败：{exc.message}")

    return ConnectionCheckResult(ok=True, detail="连接成功")
