from __future__ import annotations


class OCRServiceError(Exception):
    """Typed OCR exception with spec-aligned error code.

    在线 OCR 错误码（与本地 E_OCR_001~004 同样以 inline ``OCRServiceError(code, message)`` 抛出）：

    - ``E_OCR_010`` 在线 OCR 未配置（缺少 base_url / api_key）
    - ``E_OCR_011`` 在线 OCR 网络 / 提交失败 / 轮询超时
    - ``E_OCR_012`` 在线 OCR 鉴权失败（401/403）
    - ``E_OCR_013`` 在线 OCR job 失败或结果结构异常
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class PDFAdapterError(Exception):
    """Typed PDF adapter exception with spec-aligned error code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")
