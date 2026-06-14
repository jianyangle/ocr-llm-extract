from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable

from src.ocr.errors import OCRServiceError
from src.ocr.models import OCRResult
from src.ocr.online_client import OnlineOCRClient
from src.ocr.paddle_service import SUPPORTED_IMAGE_EXTENSIONS, PaddleOCRService

ClientFactory = Callable[["OnlineOCRConfig"], Any]


@dataclass(frozen=True)
class OnlineOCRConfig:
    """在线 OCR 服务配置。

    ``model`` 留有默认值，使 ``OnlineOCRConfig(base_url="", api_key="")``（未配置）合法。
    三个 optionalPayload 布尔位组装成远端约定的 camelCase 字段。
    """

    base_url: str
    api_key: str
    model: str = ""
    use_doc_orientation_classify: bool = False
    use_doc_unwarping: bool = False
    use_textline_orientation: bool = False
    poll_interval: float = 5
    poll_timeout: float = 1800
    extra_payload: dict[str, Any] = field(default_factory=dict)


def _default_client_factory(online_config: OnlineOCRConfig) -> OnlineOCRClient:
    optional_payload = {
        "useDocOrientationClassify": online_config.use_doc_orientation_classify,
        "useDocUnwarping": online_config.use_doc_unwarping,
        "useTextlineOrientation": online_config.use_textline_orientation,
    }
    # extra_payload 来自模型 preset，其中的同名键有意覆盖上面三个方向布尔——
    # preset 值是按模型调优的最优参数，在线模式下优先于用户全局设置。
    optional_payload.update(online_config.extra_payload)
    return OnlineOCRClient(
        base_url=online_config.base_url,
        api_key=online_config.api_key,
        model=online_config.model,
        optional_payload=optional_payload,
        poll_interval=online_config.poll_interval,
        poll_timeout=online_config.poll_timeout,
    )


class OnlineOCRService:
    """在线（异步 job）OCR 服务。

    与 ``PaddleOCRService.recognize`` 同签名，但走远端整文档异步流程：
    submit → poll_until_done → fetch_jsonl，取首页结果包装为 ``OCRResult``。
    封套解析已在 ``OnlineOCRClient`` 完成，此处直接透传，不做任何本地后处理
    （tbpu / ignore_areas / regularization），见 ADR-0010。
    """

    def __init__(
        self,
        *,
        online_config: OnlineOCRConfig,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._online_config = online_config
        self._client_factory = client_factory or _default_client_factory

    def update_config(self, online_config: OnlineOCRConfig) -> None:
        """刷新存储的配置（无引擎重建语义）。"""
        self._online_config = online_config

    def recognize(
        self,
        image_path: str,
        *,
        crop: tuple[int, int, int, int] | None = None,
        allow_retry: bool = True,
    ) -> OCRResult:
        """识别单张图片。``allow_retry`` 仅为签名兼容，在线流程无重试，忽略。"""
        config = self._online_config
        if not config.base_url or not config.api_key:
            raise OCRServiceError("E_OCR_010", "在线 OCR 未配置 base_url / api_key")

        image = Path(image_path)
        if (
            not image.exists()
            or not image.is_file()
            or image.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS
        ):
            raise OCRServiceError("E_OCR_003", "Invalid image path or unsupported image type")

        offset = (0, 0)
        temp_context: TemporaryDirectory | None = None
        try:
            if crop is None:
                file_bytes = self._read_bytes(image)
                filename = image.name
            else:
                upload_path, temp_context, offset = self._crop_for_upload(image, crop)
                file_bytes = self._read_bytes(upload_path)
                filename = upload_path.name

            client = self._client_factory(config)
            job_id = client.submit(file_bytes=file_bytes, filename=filename)
            jsonl_url = client.poll_until_done(job_id)
            job_result = client.fetch_jsonl(jsonl_url)
        finally:
            if temp_context is not None:
                temp_context.cleanup()

        if not job_result.pages:
            raise OCRServiceError("E_OCR_004", "OCR returned empty text")
        page = job_result.pages[0]

        result = OCRResult(
            text=page.text,
            confidence_avg=page.confidence_avg,
            confidence_min=page.confidence_min,
            block_count=page.block_count,
            blocks=page.blocks,
            retry_triggered=False,
            retry_applied=False,
            retry_profile_from=None,
            retry_profile_to=None,
            first_pass_confidence_min=None,
            second_pass_confidence_min=None,
            markdown=getattr(page, "markdown", None),
        )

        if not result.text.strip():
            raise OCRServiceError("E_OCR_004", "OCR returned empty text")

        if result.blocks and offset != (0, 0):
            result = PaddleOCRService._offset_result(result, offset)
        return result

    @staticmethod
    def _read_bytes(path: Path) -> bytes:
        try:
            return path.read_bytes()
        except OSError as exc:
            raise OCRServiceError("E_OCR_003", "Invalid image path or unsupported image type") from exc

    @staticmethod
    def _crop_for_upload(
        image: Path, crop: tuple[int, int, int, int]
    ) -> tuple[Path, TemporaryDirectory, tuple[int, int]]:
        """客户端裁剪后写入临时文件供上传；不缩放。返回 (路径, 临时目录, 偏移)。"""
        try:
            import cv2  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001
            raise OCRServiceError("E_OCR_003", "Image decode failed: cv2 unavailable") from exc

        decoded = cv2.imread(str(image), cv2.IMREAD_COLOR)
        if decoded is None:
            raise OCRServiceError("E_OCR_003", "Invalid image path or unsupported image type")

        height, width = decoded.shape[:2]
        x1, y1, x2, y2 = PaddleOCRService._clip_crop(crop, width=width, height=height)
        if x2 <= x1 or y2 <= y1:
            raise OCRServiceError("E_OCR_003", "Invalid crop region")

        cropped = decoded[y1:y2, x1:x2]
        temp_dir = TemporaryDirectory(prefix="online_ocr_crop_")
        output_path = Path(temp_dir.name) / f"{image.stem}__crop{image.suffix.lower()}"
        if not cv2.imwrite(str(output_path), cropped):
            temp_dir.cleanup()
            raise OCRServiceError("E_OCR_003", "Failed to write cropped image")
        return output_path, temp_dir, (x1, y1)
