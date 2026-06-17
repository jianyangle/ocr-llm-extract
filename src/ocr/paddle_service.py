from __future__ import annotations

import contextlib
import os
import re
import threading
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Mapping

from .errors import OCRServiceError
from .models import OCRResult, OCRTextBlock
from .tbpu import get_parser


@contextlib.contextmanager
def _suppress_ccache_probe_noise():
    """静音 Paddle 首次创建模型时的 ccache 探测噪音。

    根因：``paddle.utils.cpp_extension.extension_utils.find_ccache_home()`` 在
    Windows 上通过子进程 ``where ccache`` 探测 ccache 路径，但未重定向子进程
    stderr（同文件的 ``find_cuda_home()`` 则正确地传了 ``stderr=devnull``）。
    在中文 Windows 上 ``where`` 找不到时会把本地化提示
    “信息: 用提供的模式无法找到文件。” 写到 stderr 泄漏到控制台，并伴随一条
    “No ccache found” 的 ``UserWarning``。

    ccache 仅用于重新编译自定义算子，纯 OCR 推理不需要，因此这两条输出都是无害
    噪音，这里一并静音。子进程输出必须在 OS fd 层重定向（``contextlib.redirect_stderr``
    只替换 Python 的 ``sys.stderr``，对子进程继承的真实 fd 2 无效）。
    """
    saved_fd = None
    devnull_fd = None
    try:
        try:
            saved_fd = os.dup(2)
            devnull_fd = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull_fd, 2)
        except (OSError, ValueError):
            # stderr fd 不可用（如 PyInstaller windowed 模式），本就无控制台噪音可静音
            pass
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="No ccache found.*")
            yield
    finally:
        if saved_fd is not None:
            try:
                os.dup2(saved_fd, 2)
            finally:
                os.close(saved_fd)
        if devnull_fd is not None:
            os.close(devnull_fd)


MODEL_DIRS = {
    "det": "PP-OCRv5_mobile_det",
    "rec": "PP-OCRv5_mobile_rec",
    "ori": "PP-LCNet_x1_0_textline_ori",
    "doc_ori": "PP-LCNet_x1_0_doc_ori",
    "unwarp": "UVDoc",
}

REQUIRED_MODEL_FILES = ("inference.pdiparams", "inference.yml")
SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff"}
OCR_PROFILE_PRESETS = {
    "fast": {
        "text_det_limit_side_len": 736,
        "text_det_thresh": 0.35,
        "adaptive_retry_enabled": False,
        "retry_side_len": None,
        "retry_thresh": None,
    },
    "balanced": {
        "text_det_limit_side_len": 960,
        "text_det_thresh": 0.30,
        "adaptive_retry_enabled": True,
        "retry_side_len": 1080,
        "retry_thresh": 0.25,
    },
    "accurate": {
        "text_det_limit_side_len": 1216,
        "text_det_thresh": 0.25,
        "adaptive_retry_enabled": False,
        "retry_side_len": None,
        "retry_thresh": None,
    },
}
# PaddleOCR 3.2.0 text detection pipeline has an internal max_side_limit=4000
# that is not configurable via public OCR API parameters.
PADDLE_DET_INTERNAL_MAX_SIDE_LIMIT = 4000
CONTACT_LINE_REPAIR_PATTERN = re.compile(
    r"^(?P<label>(?:电话|传真|Tel|TEL|Fax|FAX)\s*[:：]?\s*)"
    r"(?P<body>(0\d{2,3}-\d{7,8})(?:0\d{2,3}-\d{7,8}|\d{7,8}(?:-\d{1,6})?))$"
)


@dataclass(frozen=True)
class OCRRuntimeOptions:
    profile: str = "balanced"
    use_textline_orientation: bool = True
    use_doc_orientation_classify: bool = True
    use_doc_unwarping: bool = False
    cpu_threads: int = 4
    text_det_limit_side_len: int = 960
    text_det_thresh: float = 0.30
    layout_parser: str = "auto"
    restore_paragraphs: bool = True
    ignore_areas: tuple[tuple[float, float, float, float], ...] = ()
    adaptive_retry_enabled: bool = True
    retry_confidence_threshold: float = 0.55
    retry_target_profile: str = "accurate"
    image_max_side_limit: int = 6000
    retry_low_block_count_min: int = 3
    retry_avg_threshold: float = 0.55
    retry_min_improvement: float = 0.03
    retry_max_block_drop: int = 1
    retry_side_len: int | None = 1080
    retry_thresh: float | None = 0.25


class PaddleOCRService:
    def __init__(
        self,
        models_root: str | Path,
        engine_factory: Callable[..., Callable[[str], Any]] | None = None,
        runtime_options: OCRRuntimeOptions | Mapping[str, Any] | None = None,
    ) -> None:
        self._models_root = Path(models_root)
        self._model_paths = self._validate_model_paths(self._models_root)
        self._engine_factory = engine_factory or self._default_engine_factory
        self._recognize_lock = threading.Lock()
        self._runtime_options = self._normalize_runtime_options(runtime_options)
        self._engine: Callable[[str], Any] | None = None
        self._retry_engine_cache: dict[OCRRuntimeOptions, Callable[[str], Any]] = {}

    def preload(self) -> None:
        """提前构建 OCR 引擎，使首次识别免于现场加载。幂等；供后台线程调用。"""
        with self._recognize_lock:
            self._ensure_engine_locked()

    def _ensure_engine_locked(self) -> Callable[[str], Any]:
        """懒建引擎。调用方必须已持有 ``self._recognize_lock``。"""
        if self._engine is None:
            self._engine = self._create_engine(self._runtime_options)
        return self._engine

    def update_runtime_options(self, runtime_options: OCRRuntimeOptions | Mapping[str, Any]) -> None:
        with self._recognize_lock:
            self._reset_runtime_locked(runtime_options)

    def reset_runtime(self, runtime_options: OCRRuntimeOptions | Mapping[str, Any] | None = None) -> None:
        with self._recognize_lock:
            self._reset_runtime_locked(self._runtime_options if runtime_options is None else runtime_options)

    def _reset_runtime_locked(self, runtime_options: OCRRuntimeOptions | Mapping[str, Any]) -> None:
        self._runtime_options = self._normalize_runtime_options(runtime_options)
        self._engine = None
        self._retry_engine_cache.clear()

    @classmethod
    def runtime_options_from_app_config(cls, config: Any) -> OCRRuntimeOptions:
        profile = getattr(config, "ocr_profile", "balanced")
        is_custom = bool(getattr(config, "ocr_profile_custom", False))
        if not is_custom and profile in OCR_PROFILE_PRESETS:
            adaptive_retry_enabled = bool(
                OCR_PROFILE_PRESETS[profile]["adaptive_retry_enabled"]
            )
        else:
            adaptive_retry_enabled = getattr(config, "ocr_adaptive_retry_enabled", True)
        return cls._normalize_runtime_options(
            {
                "profile": profile,
                "use_textline_orientation": getattr(config, "ocr_use_textline_orientation", True),
                "use_doc_orientation_classify": getattr(config, "ocr_use_doc_orientation_classify", True),
                # Force-disable doc unwarping to avoid first-character truncation regressions.
                "use_doc_unwarping": False,
                "cpu_threads": getattr(config, "ocr_cpu_threads", 4),
                "text_det_limit_side_len": getattr(config, "ocr_text_det_limit_side_len", None),
                "text_det_thresh": getattr(config, "ocr_text_det_thresh", None),
                "layout_parser": getattr(config, "ocr_layout_parser", "multi_para"),
                "restore_paragraphs": getattr(config, "ocr_restore_paragraphs", True),
                "ignore_areas": getattr(config, "ocr_ignore_areas", ()),
                "adaptive_retry_enabled": adaptive_retry_enabled,
                "retry_confidence_threshold": getattr(config, "ocr_retry_confidence_threshold", 0.55),
                "retry_target_profile": getattr(config, "ocr_retry_target_profile", "accurate"),
                "image_max_side_limit": getattr(config, "ocr_image_max_side_limit", 6000),
                "retry_low_block_count_min": getattr(config, "ocr_retry_low_block_count_min", 3),
                "retry_avg_threshold": getattr(config, "ocr_retry_avg_threshold", 0.55),
                "retry_min_improvement": getattr(config, "ocr_retry_min_improvement", 0.03),
                "retry_max_block_drop": getattr(config, "ocr_retry_max_block_drop", 1),
            }
        )

    def _create_engine(self, runtime_options: OCRRuntimeOptions) -> Callable[[str], Any]:
        try:
            return self._engine_factory(
                det_model_dir=str(self._model_paths["det"]),
                rec_model_dir=str(self._model_paths["rec"]),
                ori_model_dir=str(self._model_paths["ori"]),
                doc_ori_model_dir=str(self._model_paths["doc_ori"]),
                unwarp_model_dir=str(self._model_paths["unwarp"]),
                download_enabled=False,
                runtime_options=runtime_options,
            )
        except OCRServiceError:
            raise
        except Exception as exc:
            raise OCRServiceError("E_OCR_002", "Failed to initialize OCR engine") from exc

    def recognize(
        self,
        image_path: str,
        *,
        crop: tuple[int, int, int, int] | None = None,
        allow_retry: bool = True,
    ) -> OCRResult:
        with self._recognize_lock:
            return self._recognize_locked(image_path, crop=crop, allow_retry=allow_retry)

    def _recognize_locked(
        self,
        image_path: str,
        *,
        crop: tuple[int, int, int, int] | None = None,
        allow_retry: bool = True,
    ) -> OCRResult:
        image = Path(image_path)
        if not image.exists() or not image.is_file() or image.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
            raise OCRServiceError("E_OCR_003", "Invalid image path or unsupported image type")

        self._ensure_engine_locked()

        offset = (0, 0)
        max_side_limit = self._effective_preprocess_max_side_limit(self._runtime_options.image_max_side_limit)
        if crop is None:
            prepared_image_path, temp_context = self._prepare_image_for_inference(
                image,
                max_side_limit=max_side_limit,
            )
        else:
            prepared_image_path, temp_context, offset = self._prepare_cropped_image_for_inference(
                image,
                max_side_limit=max_side_limit,
                crop=crop,
            )
        try:
            first_result = self._recognize_once(prepared_image_path, self._engine, self._runtime_options)
            if offset != (0, 0):
                first_result = self._offset_result(first_result, offset)
            first_min = first_result.confidence_min
            if (
                not allow_retry
                or not self._should_adaptive_retry(first_result, self._runtime_options)
                or self._retry_has_no_new_pixels(image, self._runtime_options)
            ):
                return OCRResult(
                    text=first_result.text,
                    confidence_avg=first_result.confidence_avg,
                    confidence_min=first_result.confidence_min,
                    block_count=first_result.block_count,
                    blocks=first_result.blocks,
                    retry_triggered=False,
                    retry_applied=False,
                    retry_profile_from=self._runtime_options.profile,
                    retry_profile_to=None,
                    first_pass_confidence_min=first_min,
                    second_pass_confidence_min=None,
                )

            retry_options = self._build_retry_options(self._runtime_options)
            if retry_options is None:
                return OCRResult(
                    text=first_result.text,
                    confidence_avg=first_result.confidence_avg,
                    confidence_min=first_result.confidence_min,
                    block_count=first_result.block_count,
                    blocks=first_result.blocks,
                    retry_triggered=False,
                    retry_applied=False,
                    retry_profile_from=self._runtime_options.profile,
                    retry_profile_to=None,
                    first_pass_confidence_min=first_min,
                    second_pass_confidence_min=None,
                )
            retry_engine = self._get_retry_engine(retry_options)
            second_result = self._recognize_once(prepared_image_path, retry_engine, retry_options)
            if offset != (0, 0):
                second_result = self._offset_result(second_result, offset)
            second_min = second_result.confidence_min
            use_second = self._select_retry_result(first_result, second_result, self._runtime_options)
            selected = second_result if use_second else first_result
            return OCRResult(
                text=selected.text,
                confidence_avg=selected.confidence_avg,
                confidence_min=selected.confidence_min,
                block_count=selected.block_count,
                blocks=selected.blocks,
                retry_triggered=True,
                retry_applied=use_second,
                retry_profile_from=self._runtime_options.profile,
                retry_profile_to=f"{self._runtime_options.profile}+",
                first_pass_confidence_min=first_min,
                second_pass_confidence_min=second_min,
            )
        finally:
            if temp_context is not None:
                temp_context.cleanup()

    @staticmethod
    def _offset_result(result: OCRResult, offset: tuple[int, int]) -> OCRResult:
        x_offset, y_offset = offset
        shifted_blocks = [
            OCRTextBlock(
                text=block.text,
                score=block.score,
                box=[[float(point[0] + x_offset), float(point[1] + y_offset)] for point in block.box],
                end=block.end,
            )
            for block in result.blocks
        ]
        return OCRResult(
            text=result.text,
            confidence_avg=result.confidence_avg,
            confidence_min=result.confidence_min,
            block_count=result.block_count,
            blocks=shifted_blocks,
            retry_triggered=result.retry_triggered,
            retry_applied=result.retry_applied,
            retry_profile_from=result.retry_profile_from,
            retry_profile_to=result.retry_profile_to,
            first_pass_confidence_min=result.first_pass_confidence_min,
            second_pass_confidence_min=result.second_pass_confidence_min,
        )

    def _recognize_once(self, image_path: str, engine: Callable[[str], Any], options: OCRRuntimeOptions) -> OCRResult:
        try:
            raw_output = engine(image_path)
        except OCRServiceError:
            raise
        except Exception as exc:
            raise OCRServiceError("E_OCR_003", f"Image decode or OCR inference failed: {_format_error_detail(exc)}") from exc

        raw_blocks = self._extract_raw_blocks(raw_output)
        blocks = self._postprocess_blocks(raw_blocks, options)
        if not blocks:
            raise OCRServiceError("E_OCR_004", "OCR returned empty text")

        texts = [block.text for block in blocks if block.text]
        scores = [block.score for block in blocks if isinstance(block.score, (int, float))]
        full_text = "".join(block.text + block.end for block in blocks if block.text).strip()
        if not full_text:
            raise OCRServiceError("E_OCR_004", "OCR returned empty text")

        confidence_avg: float | None = None
        confidence_min: float | None = None
        if scores:
            confidence_avg = float(sum(float(score) for score in scores) / len(scores))
            confidence_min = float(min(float(score) for score in scores))

        return OCRResult(
            text=full_text,
            confidence_avg=confidence_avg,
            confidence_min=confidence_min,
            block_count=len(texts),
            blocks=blocks,
        )

    @staticmethod
    def _should_adaptive_retry(result: OCRResult, options: OCRRuntimeOptions) -> bool:
        if not options.adaptive_retry_enabled:
            return False
        scores = [block.score for block in result.blocks if isinstance(block.score, (int, float))]
        if not scores:
            return False
        threshold = float(options.retry_confidence_threshold)
        low_count = sum(1 for score in scores if float(score) < threshold)
        total = len(scores)
        if low_count >= int(options.retry_low_block_count_min):
            return True
        confidence_avg = result.confidence_avg
        if confidence_avg is None:
            confidence_avg = float(sum(float(score) for score in scores) / total)
        return low_count >= 2 and (low_count / total) >= 0.5 and float(confidence_avg) < float(options.retry_avg_threshold)

    @staticmethod
    def _select_retry_result(first_result: OCRResult, second_result: OCRResult, options: OCRRuntimeOptions) -> bool:
        if second_result.block_count < first_result.block_count - int(options.retry_max_block_drop):
            return False

        def _score(result: OCRResult) -> float:
            cmin = float(result.confidence_min or 0.0)
            cavg = float(result.confidence_avg or 0.0)
            return 0.5 * cmin + 0.5 * cavg

        return _score(second_result) > _score(first_result) + float(options.retry_min_improvement)

    def _get_retry_engine(self, retry_options: OCRRuntimeOptions) -> Callable[[str], Any]:
        if retry_options == self._runtime_options:
            return self._engine
        cached = self._retry_engine_cache.get(retry_options)
        if cached is not None:
            return cached
        engine = self._create_engine(retry_options)
        self._retry_engine_cache[retry_options] = engine
        return engine

    @staticmethod
    def _build_retry_options(options: OCRRuntimeOptions) -> OCRRuntimeOptions | None:
        if options.retry_side_len is None or options.retry_thresh is None:
            return None
        payload = asdict(options)
        payload["text_det_limit_side_len"] = int(options.retry_side_len)
        payload["text_det_thresh"] = float(options.retry_thresh)
        if options.use_doc_unwarping:
            payload["use_doc_unwarping"] = False
        return OCRRuntimeOptions(**payload)

    @staticmethod
    def _retry_has_no_new_pixels(image_path: Path, options: OCRRuntimeOptions) -> bool:
        try:
            from PIL import Image

            with Image.open(image_path) as image:
                width, height = image.size
        except Exception:
            return False
        return max(int(width), int(height)) <= int(options.text_det_limit_side_len)

    @staticmethod
    def _effective_preprocess_max_side_limit(configured_limit: int) -> int:
        if configured_limit <= 0:
            return PADDLE_DET_INTERNAL_MAX_SIDE_LIMIT
        return min(int(configured_limit), PADDLE_DET_INTERNAL_MAX_SIDE_LIMIT)

    @staticmethod
    def _prepare_image_for_inference(
        image_path: Path,
        *,
        max_side_limit: int,
    ) -> tuple[str, TemporaryDirectory | None]:
        if max_side_limit <= 0:
            return str(image_path), None

        try:
            import cv2  # type: ignore[import-not-found]
        except Exception:
            return str(image_path), None

        image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if image is None:
            return str(image_path), None

        height, width = image.shape[:2]
        target_width, target_height = PaddleOCRService._compute_scaled_size(
            width=width,
            height=height,
            max_side_limit=max_side_limit,
        )
        if target_width == width and target_height == height:
            return str(image_path), None

        interpolation = cv2.INTER_LANCZOS4 if (target_width < width or target_height < height) else cv2.INTER_CUBIC
        resized = cv2.resize(image, (target_width, target_height), interpolation=interpolation)

        temp_dir = TemporaryDirectory(prefix="ocr_resized_")
        output_path = Path(temp_dir.name) / f"{image_path.stem}__resized{image_path.suffix.lower()}"
        if not cv2.imwrite(str(output_path), resized):
            temp_dir.cleanup()
            return str(image_path), None
        return str(output_path), temp_dir

    @staticmethod
    def _prepare_cropped_image_for_inference(
        image_path: Path,
        *,
        max_side_limit: int,
        crop: tuple[int, int, int, int],
    ) -> tuple[str, TemporaryDirectory | None, tuple[int, int]]:
        try:
            import cv2  # type: ignore[import-not-found]
        except Exception:
            return str(image_path), None, (0, 0)

        image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if image is None:
            return str(image_path), None, (0, 0)

        height, width = image.shape[:2]
        x1, y1, x2, y2 = PaddleOCRService._clip_crop(crop, width=width, height=height)
        if x2 <= x1 or y2 <= y1:
            return str(image_path), None, (0, 0)

        cropped = image[y1:y2, x1:x2]
        temp_dir = TemporaryDirectory(prefix="ocr_crop_")
        output_path = Path(temp_dir.name) / f"{image_path.stem}__crop{image_path.suffix.lower()}"
        if not cv2.imwrite(str(output_path), cropped):
            temp_dir.cleanup()
            return str(image_path), None, (0, 0)

        prepared_path, resized_context = PaddleOCRService._prepare_image_for_inference(
            output_path,
            max_side_limit=max_side_limit,
        )
        if resized_context is None:
            return prepared_path, temp_dir, (x1, y1)

        class _CompositeTempDir:
            def cleanup(self) -> None:
                resized_context.cleanup()
                temp_dir.cleanup()

        return prepared_path, _CompositeTempDir(), (x1, y1)

    @staticmethod
    def _clip_crop(crop: tuple[int, int, int, int], *, width: int, height: int) -> tuple[int, int, int, int]:
        x1, y1, x2, y2 = crop
        left = min(max(int(x1), 0), max(width, 0))
        top = min(max(int(y1), 0), max(height, 0))
        right = min(max(int(x2), 0), max(width, 0))
        bottom = min(max(int(y2), 0), max(height, 0))
        return left, top, right, bottom

    @staticmethod
    def _compute_scaled_size(*, width: int, height: int, max_side_limit: int) -> tuple[int, int]:
        if width <= 0 or height <= 0 or max_side_limit <= 0:
            return width, height
        long_side = max(width, height)
        if long_side <= max_side_limit:
            return width, height
        scale = float(max_side_limit) / float(long_side)
        target_width = max(1, int(round(width * scale)))
        target_height = max(1, int(round(height * scale)))
        return target_width, target_height

    @staticmethod
    def _validate_model_paths(models_root: Path) -> dict[str, Path]:
        resolved: dict[str, Path] = {}
        for key, relative in MODEL_DIRS.items():
            model_dir = models_root / relative
            if key == "unwarp":
                # Doc unwarping is force-disabled in this project, so UVDoc is optional.
                resolved[key] = model_dir
                continue
            if not model_dir.is_dir():
                raise OCRServiceError("E_OCR_001", f"Missing model directory: {model_dir}")
            for filename in REQUIRED_MODEL_FILES:
                required_file = model_dir / filename
                if not required_file.is_file():
                    raise OCRServiceError("E_OCR_001", f"Missing model file: {required_file}")
            resolved[key] = model_dir
        return resolved

    @staticmethod
    def _extract_text_and_scores(raw_output: Any) -> tuple[list[str], list[float]]:
        blocks = PaddleOCRService._extract_raw_blocks(raw_output)
        texts = [block.text for block in blocks if block.text]
        scores = [float(block.score) for block in blocks if isinstance(block.score, (int, float))]
        return texts, scores

    @staticmethod
    def _extract_raw_blocks(raw_output: Any) -> list[OCRTextBlock]:
        blocks: list[OCRTextBlock] = []
        stack: list[Any] = [raw_output]
        while stack:
            current = stack.pop()

            if isinstance(current, dict):
                rec_texts = current.get("rec_texts")
                rec_scores = current.get("rec_scores")
                rec_boxes = PaddleOCRService._pick_box_candidates(current)
                if isinstance(rec_texts, list):
                    for index, text in enumerate(rec_texts):
                        if not isinstance(text, str):
                            continue
                        score_value = rec_scores[index] if isinstance(rec_scores, list) and index < len(rec_scores) else None
                        box_value = rec_boxes[index] if isinstance(rec_boxes, list) and index < len(rec_boxes) else None
                        blocks.append(
                            OCRTextBlock(
                                text=text,
                                score=PaddleOCRService._to_score(score_value),
                                box=PaddleOCRService._normalize_box(box_value),
                            )
                        )

                for value in current.values():
                    stack.append(value)
                continue

            block = PaddleOCRService._parse_block_from_legacy_pair(current)
            if block is not None:
                blocks.append(block)
                continue

            if isinstance(current, OCRTextBlock):
                blocks.append(current)
                continue

            if isinstance(current, (list, tuple)):
                for item in reversed(current):
                    stack.append(item)

        return blocks

    @staticmethod
    def _pick_box_candidates(result_dict: dict[str, Any]) -> list[Any] | None:
        for key in ("rec_polys", "dt_polys", "rec_boxes", "text_det_polys", "polys", "boxes"):
            value = result_dict.get(key)
            if isinstance(value, list):
                return value
        return None

    @staticmethod
    def _parse_text_score_pair(current: Any) -> tuple[str, float | None] | None:
        if not isinstance(current, (list, tuple)) or len(current) < 2:
            return None

        candidate = current[1]
        if not isinstance(candidate, (list, tuple)) or len(candidate) < 2:
            return None

        text = candidate[0]
        if not isinstance(text, str):
            return None

        return text, PaddleOCRService._to_score(candidate[1])

    @staticmethod
    def _parse_block_from_legacy_pair(current: Any) -> OCRTextBlock | None:
        pair = PaddleOCRService._parse_text_score_pair(current)
        if pair is None:
            return None

        text, score = pair
        box_value = current[0] if isinstance(current, (list, tuple)) and len(current) > 0 else None
        return OCRTextBlock(text=text, score=score, box=PaddleOCRService._normalize_box(box_value))

    @staticmethod
    def _normalize_box(value: Any) -> list[list[float]]:
        if hasattr(value, "tolist"):
            value = value.tolist()
        if not isinstance(value, (list, tuple)):
            return []
        normalized: list[list[float]] = []
        for point in value:
            if hasattr(point, "tolist"):
                point = point.tolist()
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            x, y = point[0], point[1]
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                continue
            normalized.append([float(x), float(y)])
        return normalized if len(normalized) >= 2 else []

    @staticmethod
    def _to_score(value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)
        return None

    @staticmethod
    def _postprocess_blocks(blocks: list[OCRTextBlock], options: OCRRuntimeOptions) -> list[OCRTextBlock]:
        prepared: list[OCRTextBlock] = []
        for block in blocks:
            text = PaddleOCRService._normalize_block_text(block.text)
            if not text:
                continue
            prepared.append(OCRTextBlock(text=text, score=block.score, box=block.box, end=""))

        filtered = PaddleOCRService._apply_ignore_areas(prepared, options.ignore_areas)
        return get_parser(options.layout_parser).run(filtered)

    @staticmethod
    def _normalize_block_text(text: str) -> str:
        compact = " ".join(text.replace("\r", " ").replace("\n", " ").split())
        return PaddleOCRService._repair_contact_line_spacing(compact.strip())

    @staticmethod
    def _repair_contact_line_spacing(text: str) -> str:
        if " " in text:
            return text
        match = CONTACT_LINE_REPAIR_PATTERN.match(text)
        if match is None:
            return text
        label = match.group("label")
        body = match.group("body")
        for prefix_match in re.finditer(r"0\d{2,3}-\d{7,8}", body):
            prefix = prefix_match.group(0)
            suffix = body[prefix_match.end() :]
            if re.fullmatch(r"(?:0\d{2,3}-\d{7,8}|\d{7,8}(?:-\d{1,6})?)", suffix):
                return f"{label}{prefix} {suffix}"
        return text

    @staticmethod
    def _apply_ignore_areas(
        blocks: list[OCRTextBlock],
        ignore_areas: tuple[tuple[float, float, float, float], ...],
    ) -> list[OCRTextBlock]:
        if not ignore_areas:
            return blocks
        kept: list[OCRTextBlock] = []
        for block in blocks:
            center = PaddleOCRService._block_center(block.box)
            if center is None:
                kept.append(block)
                continue
            if PaddleOCRService._in_any_ignore_area(center[0], center[1], ignore_areas):
                continue
            kept.append(block)
        return kept

    @staticmethod
    def _in_any_ignore_area(
        x: float,
        y: float,
        ignore_areas: tuple[tuple[float, float, float, float], ...],
    ) -> bool:
        for x1, y1, x2, y2 in ignore_areas:
            left, right = (x1, x2) if x1 <= x2 else (x2, x1)
            top, bottom = (y1, y2) if y1 <= y2 else (y2, y1)
            if left <= x <= right and top <= y <= bottom:
                return True
        return False

    @staticmethod
    def _block_center(box: list[list[float]]) -> tuple[float, float] | None:
        bounds = PaddleOCRService._box_bounds(box)
        if bounds is None:
            return None
        left, top, right, bottom = bounds
        return (left + right) / 2.0, (top + bottom) / 2.0

    @staticmethod
    def _box_bounds(box: list[list[float]]) -> tuple[float, float, float, float] | None:
        if not box:
            return None
        xs = [point[0] for point in box if isinstance(point, list) and len(point) >= 2]
        ys = [point[1] for point in box if isinstance(point, list) and len(point) >= 2]
        if not xs or not ys:
            return None
        return min(xs), min(ys), max(xs), max(ys)

    @staticmethod
    def _default_engine_factory(
        *,
        det_model_dir: str,
        rec_model_dir: str,
        ori_model_dir: str,
        doc_ori_model_dir: str,
        unwarp_model_dir: str,
        download_enabled: bool,
        runtime_options: OCRRuntimeOptions | Mapping[str, Any] | None = None,
    ) -> Callable[[str], Any]:
        try:
            from paddleocr import PaddleOCR
        except Exception as exc:
            raise OCRServiceError("E_OCR_002", "paddleocr dependency is not available") from exc

        try:
            import inspect

            signature = inspect.signature(PaddleOCR.__init__)
            kwargs = PaddleOCRService._build_engine_kwargs(
                det_model_dir=det_model_dir,
                rec_model_dir=rec_model_dir,
                ori_model_dir=ori_model_dir,
                doc_ori_model_dir=doc_ori_model_dir,
                unwarp_model_dir=unwarp_model_dir,
                supported_params=set(signature.parameters.keys()),
                download_enabled=download_enabled,
                runtime_options=runtime_options,
            )
        except OCRServiceError:
            raise
        except Exception as exc:
            raise OCRServiceError("E_OCR_002", "Failed to inspect PaddleOCR API signature") from exc

        try:
            with _suppress_ccache_probe_noise():
                engine = PaddleOCR(**kwargs)
        except Exception as exc:
            raise OCRServiceError("E_OCR_002", "Failed to load PaddleOCR models") from exc

        def _run(image_path: str) -> Any:
            predict = getattr(engine, "predict", None)
            if not callable(predict):
                raise OCRServiceError("E_OCR_002", "PaddleOCR engine does not expose official predict method")
            return predict(image_path)

        return _run

    @staticmethod
    def _build_engine_kwargs(
        *,
        det_model_dir: str,
        rec_model_dir: str,
        ori_model_dir: str,
        doc_ori_model_dir: str,
        unwarp_model_dir: str,
        supported_params: set[str],
        download_enabled: bool,
        runtime_options: OCRRuntimeOptions | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        options = PaddleOCRService._normalize_runtime_options(runtime_options)
        kwargs: dict[str, Any] = {}

        required_v3_keys = {
            "text_detection_model_dir",
            "text_recognition_model_dir",
            "textline_orientation_model_dir",
        }
        if not required_v3_keys.issubset(supported_params):
            raise OCRServiceError(
                "E_OCR_002",
                "Unsupported PaddleOCR API: required official v3 model-dir params are missing",
            )

        kwargs["text_detection_model_dir"] = det_model_dir
        kwargs["text_recognition_model_dir"] = rec_model_dir
        kwargs["textline_orientation_model_dir"] = ori_model_dir

        # PaddleOCR 3.x requires model names to match provided local model dirs.
        if "text_detection_model_name" in supported_params:
            kwargs["text_detection_model_name"] = Path(det_model_dir).name
        if "text_recognition_model_name" in supported_params:
            kwargs["text_recognition_model_name"] = Path(rec_model_dir).name
        if "textline_orientation_model_name" in supported_params:
            kwargs["textline_orientation_model_name"] = Path(ori_model_dir).name

        if "doc_orientation_classify_model_dir" in supported_params:
            kwargs["doc_orientation_classify_model_dir"] = doc_ori_model_dir
        if "doc_orientation_classify_model_name" in supported_params:
            kwargs["doc_orientation_classify_model_name"] = Path(doc_ori_model_dir).name

        if "use_textline_orientation" in supported_params:
            kwargs["use_textline_orientation"] = options.use_textline_orientation
        if "use_doc_orientation_classify" in supported_params:
            kwargs["use_doc_orientation_classify"] = options.use_doc_orientation_classify
        if "use_doc_unwarping" in supported_params:
            kwargs["use_doc_unwarping"] = False
        if "text_det_limit_side_len" in supported_params:
            kwargs["text_det_limit_side_len"] = options.text_det_limit_side_len
        if "text_det_limit_type" in supported_params:
            # Use "max" to avoid aggressive short-side upscaling that can trigger
            # large-image re-clamp warnings and unnecessary quality loss.
            kwargs["text_det_limit_type"] = "max"
        if "text_det_thresh" in supported_params:
            kwargs["text_det_thresh"] = options.text_det_thresh
        if "max_side_limit" in supported_params:
            kwargs["max_side_limit"] = options.image_max_side_limit
        if "download_enabled" in supported_params:
            kwargs["download_enabled"] = download_enabled
        elif "download_enable" in supported_params:
            kwargs["download_enable"] = download_enabled

        if "cpu_threads" in supported_params or "kwargs" in supported_params:
            kwargs["cpu_threads"] = options.cpu_threads

        # Prefer CPU inference in this project; PaddleOCR 3.x accepts it via common kwargs.
        if "device" in supported_params or "kwargs" in supported_params:
            kwargs["device"] = "cpu"

        return kwargs

    @staticmethod
    def _normalize_runtime_options(
        runtime_options: OCRRuntimeOptions | Mapping[str, Any] | None,
    ) -> OCRRuntimeOptions:
        if isinstance(runtime_options, OCRRuntimeOptions):
            return runtime_options

        options: Mapping[str, Any] = runtime_options or {}
        profile = str(options.get("profile", "balanced")).strip().lower()
        if profile not in OCR_PROFILE_PRESETS:
            profile = "balanced"
        preset = OCR_PROFILE_PRESETS[profile]

        return OCRRuntimeOptions(
            profile=profile,
            use_textline_orientation=PaddleOCRService._coerce_bool(
                options.get("use_textline_orientation"),
                fallback=True,
            ),
            use_doc_orientation_classify=PaddleOCRService._coerce_bool(
                options.get("use_doc_orientation_classify"),
                fallback=True,
            ),
            use_doc_unwarping=False,
            cpu_threads=PaddleOCRService._coerce_positive_int(
                options.get("cpu_threads"),
                fallback=4,
            ),
            text_det_limit_side_len=PaddleOCRService._coerce_positive_int(
                options.get("text_det_limit_side_len"),
                fallback=int(preset["text_det_limit_side_len"]),
            ),
            text_det_thresh=PaddleOCRService._coerce_unit_float(
                options.get("text_det_thresh"),
                fallback=float(preset["text_det_thresh"]),
            ),
            layout_parser=PaddleOCRService._normalize_layout_parser(options.get("layout_parser")),
            restore_paragraphs=PaddleOCRService._coerce_bool(
                options.get("restore_paragraphs"),
                fallback=True,
            ),
            ignore_areas=PaddleOCRService._normalize_ignore_areas(options.get("ignore_areas")),
            adaptive_retry_enabled=PaddleOCRService._coerce_bool(
                options.get("adaptive_retry_enabled"),
                fallback=bool(preset["adaptive_retry_enabled"]),
            ),
            retry_confidence_threshold=PaddleOCRService._coerce_unit_float(
                options.get("retry_confidence_threshold"),
                fallback=0.55,
            ),
            retry_target_profile=PaddleOCRService._normalize_profile(
                options.get("retry_target_profile"),
                fallback="accurate",
            ),
            image_max_side_limit=PaddleOCRService._coerce_positive_int(
                options.get("image_max_side_limit"),
                fallback=6000,
            ),
            retry_low_block_count_min=PaddleOCRService._coerce_positive_int(
                options.get("retry_low_block_count_min"),
                fallback=3,
            ),
            retry_avg_threshold=PaddleOCRService._coerce_unit_float(
                options.get("retry_avg_threshold"),
                fallback=0.55,
            ),
            retry_min_improvement=PaddleOCRService._coerce_unit_float(
                options.get("retry_min_improvement"),
                fallback=0.03,
            ),
            retry_max_block_drop=PaddleOCRService._coerce_positive_int(
                options.get("retry_max_block_drop"),
                fallback=1,
            ),
            retry_side_len=int(preset["retry_side_len"]) if preset["retry_side_len"] is not None else None,
            retry_thresh=float(preset["retry_thresh"]) if preset["retry_thresh"] is not None else None,
        )

    @staticmethod
    def _coerce_bool(value: Any, *, fallback: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in {0, 1}:
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "y", "on"}:
                return True
            if lowered in {"0", "false", "no", "n", "off"}:
                return False
        return fallback

    @staticmethod
    def _coerce_positive_int(value: Any, *, fallback: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return fallback
        return parsed if parsed > 0 else fallback

    @staticmethod
    def _coerce_unit_float(value: Any, *, fallback: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return fallback
        return parsed if 0.0 < parsed <= 1.0 else fallback

    @staticmethod
    def _normalize_layout_parser(value: Any) -> str:
        if value is None:
            return "auto"
        parser = str(value).strip().lower()
        if not parser:
            return "auto"
        if parser in {
            "auto",
            "single_column",
            "multi_column",
            "none",
            "multi_none",
            "multi_line",
            "multi_para",
            "single_none",
            "single_line",
            "single_para",
            "single_code",
        }:
            return parser
        return "auto"

    @staticmethod
    def _normalize_profile(value: Any, *, fallback: str = "balanced") -> str:
        profile = str(value).strip().lower()
        if profile in OCR_PROFILE_PRESETS:
            return profile
        return fallback

    @staticmethod
    def _normalize_ignore_areas(value: Any) -> tuple[tuple[float, float, float, float], ...]:
        if not isinstance(value, (list, tuple)):
            return ()
        normalized: list[tuple[float, float, float, float]] = []
        for item in value:
            if not isinstance(item, (list, tuple)) or len(item) < 4:
                continue
            x1, y1, x2, y2 = item[0], item[1], item[2], item[3]
            if not all(isinstance(v, (int, float)) for v in (x1, y1, x2, y2)):
                continue
            normalized.append((float(x1), float(y1), float(x2), float(y2)))
        return tuple(normalized)


def _format_error_detail(exc: Exception) -> str:
    message = str(exc).strip()
    if not message:
        return type(exc).__name__
    return f"{type(exc).__name__}: {message}"
