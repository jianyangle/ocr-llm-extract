from __future__ import annotations

import inspect
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterator

from src.domain.schemas import (
    BBox,
    ExtractionOptions,
    PDFLimits,
    PDFPageOCRSnapshot,
    PDFPageWorkUnit,
    PageError,
)
from src.ocr.errors import PDFAdapterError
from src.ocr.paddle_service import OCRRuntimeOptions, SUPPORTED_IMAGE_EXTENSIONS
from src.ocr.region_geometry import (
    TextFragment,
    detect_uncovered_raster_regions,
    merge_fragments_by_reading_order,
)

logger = logging.getLogger(__name__)


class PDFDocumentError(PDFAdapterError):
    """Document-level PDF failure."""


class _RegionOCRError(Exception):
    """Region OCR failed while building a hybrid text layer snapshot."""


class PDFPageProcessor:
    def __init__(self, *, pdf_adapter: Any, ocr_service: Any, extractor: Any) -> None:
        self._pdf_adapter = pdf_adapter
        self._ocr_service = ocr_service
        self._extractor = extractor

    def process(
        self,
        *,
        pdf_path: str | Path,
        ocr_options: OCRRuntimeOptions,
        extract_options: ExtractionOptions,
        limits: PDFLimits,
        allow_retry_for_page: Callable[[int], bool] | None = None,
        record_ocr_snapshot: Callable[[PDFPageOCRSnapshot], None] | None = None,
    ) -> Iterator[PDFPageWorkUnit]:
        _ = ocr_options
        path = str(pdf_path)
        pdf_info = self._inspect_pdf(path, limits=limits)
        prefer_text_layer = bool(limits.prefer_text_layer)
        min_chars = max(int(limits.text_layer_min_chars), 1)
        completeness_enabled = bool(getattr(limits, "text_layer_completeness_ocr", True))

        try:
            open_document = getattr(self._pdf_adapter, "open_document", None)
            if callable(open_document):
                with open_document(path) as document_handle:
                    parallelism = self._coerce_parallelism(limits.page_render_parallelism)
                    render_page_to_bitmap = getattr(document_handle, "render_page_to_bitmap", None)
                    if parallelism > 1 and callable(render_page_to_bitmap) and pdf_info.page_count > 1:
                        yield from self._process_pages_with_document_parallel(
                            document_handle=document_handle,
                            page_count=pdf_info.page_count,
                            prefer_text_layer=prefer_text_layer,
                            min_chars=min_chars,
                            render_dpi=limits.render_dpi,
                            extract_options=extract_options,
                            parallelism=parallelism,
                            completeness_enabled=completeness_enabled,
                            allow_retry_for_page=allow_retry_for_page,
                            record_ocr_snapshot=record_ocr_snapshot,
                        )
                        return
                    for one_based_page_index in range(1, pdf_info.page_count + 1):
                        yield from self._process_page_with_document(
                            document_handle=document_handle,
                            zero_based_page_index=one_based_page_index - 1,
                            one_based_page_index=one_based_page_index,
                            prefer_text_layer=prefer_text_layer,
                            min_chars=min_chars,
                            render_dpi=limits.render_dpi,
                            extract_options=extract_options,
                            completeness_enabled=completeness_enabled,
                            allow_retry_for_page=allow_retry_for_page,
                            record_ocr_snapshot=record_ocr_snapshot,
                        )
                return
        except PDFAdapterError as exc:
            raise PDFDocumentError(exc.code, exc.message) from exc

        for one_based_page_index in range(1, pdf_info.page_count + 1):
            yield from self._process_page_without_document(
                path=path,
                zero_based_page_index=one_based_page_index - 1,
                one_based_page_index=one_based_page_index,
                prefer_text_layer=prefer_text_layer,
                min_chars=min_chars,
                render_dpi=limits.render_dpi,
                extract_options=extract_options,
                allow_retry_for_page=allow_retry_for_page,
                record_ocr_snapshot=record_ocr_snapshot,
            )

    def inspect(self, *, pdf_path: str | Path, limits: PDFLimits) -> Any:
        return self._inspect_pdf(str(pdf_path), limits=limits)

    def _inspect_pdf(self, pdf_path: str, *, limits: PDFLimits) -> Any:
        try:
            pdf_info = self._pdf_adapter.inspect(pdf_path)
        except PDFAdapterError as exc:
            raise PDFDocumentError(exc.code, exc.message) from exc
        except Exception as exc:
            raise PDFDocumentError("E_PDF_002", str(exc)) from exc

        max_file_size = max(int(limits.max_file_size), 1)
        max_pages = max(int(limits.max_pages), 1)
        if int(pdf_info.file_size) > max_file_size:
            raise PDFDocumentError(
                "E_PDF_004",
                f"PDF file size exceeds configured limit ({pdf_info.file_size} > {max_file_size})",
            )
        if int(pdf_info.page_count) > max_pages:
            raise PDFDocumentError(
                "E_PDF_003",
                f"PDF page count exceeds configured limit ({pdf_info.page_count} > {max_pages})",
            )
        if Path(pdf_path).suffix.lower() != ".pdf":
            raise PDFDocumentError("E_PDF_001", "Invalid PDF path or unsupported file type")
        return pdf_info

    def _process_pages_with_document_parallel(
        self,
        *,
        document_handle: Any,
        page_count: int,
        prefer_text_layer: bool,
        min_chars: int,
        render_dpi: int,
        extract_options: ExtractionOptions,
        parallelism: int,
        completeness_enabled: bool,
        allow_retry_for_page: Callable[[int], bool] | None,
        record_ocr_snapshot: Callable[[PDFPageOCRSnapshot], None] | None,
    ) -> Iterator[PDFPageWorkUnit]:
        render_lock = threading.Lock()
        text_units: dict[int, PDFPageWorkUnit] = {}
        futures_by_page: dict[int, Future[Any]] = {}
        consumed_pages: set[int] = set()

        def _render_one(one_based_page_index: int) -> Any:
            with render_lock:
                pending = document_handle.render_page_to_bitmap(one_based_page_index, render_dpi)
            return pending.materialize()

        executor = ThreadPoolExecutor(max_workers=parallelism)
        try:
            for one_based_page_index in range(1, page_count + 1):
                zero_based_page_index = one_based_page_index - 1
                if prefer_text_layer:
                    try:
                        with render_lock:
                            text_layer = document_handle.extract_text_layer(one_based_page_index)
                    except Exception:
                        text_layer = None
                    if text_layer is not None and text_layer.char_count >= min_chars:
                        hybrid_unit = self._maybe_build_hybrid_unit(
                            document_handle=document_handle,
                            zero_based_page_index=zero_based_page_index,
                            one_based_page_index=one_based_page_index,
                            render_dpi=render_dpi,
                            extract_options=extract_options,
                            completeness_enabled=completeness_enabled,
                            allow_retry_for_page=allow_retry_for_page,
                            record_ocr_snapshot=record_ocr_snapshot,
                            document_lock=render_lock,
                        )
                        if hybrid_unit is not None:
                            text_units[one_based_page_index] = hybrid_unit
                        else:
                            text_units[one_based_page_index] = self._build_text_layer_unit(
                                zero_based_page_index=zero_based_page_index,
                                one_based_page_index=one_based_page_index,
                                normalized_text=text_layer.text,
                                extract_options=extract_options,
                            )
                        continue
                futures_by_page[one_based_page_index] = executor.submit(_render_one, one_based_page_index)

            for one_based_page_index in range(1, page_count + 1):
                zero_based_page_index = one_based_page_index - 1
                text_unit = text_units.get(one_based_page_index)
                if text_unit is not None:
                    yield text_unit
                    continue

                future = futures_by_page.get(one_based_page_index)
                if future is None:
                    yield self._failed_unit(
                        zero_based_page_index,
                        PDFAdapterError("E_PDF_002", "PDF rendering produced no page"),
                        phase="render",
                    )
                    continue
                try:
                    rendered_page = future.result()
                    consumed_pages.add(one_based_page_index)
                except Exception as exc:
                    yield self._failed_unit(zero_based_page_index, exc, phase="render")
                    continue
                try:
                    yield self._build_rendered_unit(
                        zero_based_page_index=zero_based_page_index,
                        rendered_page=rendered_page,
                        extract_options=extract_options,
                        allow_retry_for_page=allow_retry_for_page,
                        record_ocr_snapshot=record_ocr_snapshot,
                    )
                finally:
                    rendered_page.cleanup()
        finally:
            for future in futures_by_page.values():
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
            for one_based_page_index, future in futures_by_page.items():
                if one_based_page_index in consumed_pages or future.cancelled() or not future.done():
                    continue
                try:
                    rendered_page = future.result()
                except Exception:
                    continue
                try:
                    rendered_page.cleanup()
                except Exception:
                    pass

    def _process_page_with_document(
        self,
        *,
        document_handle: Any,
        zero_based_page_index: int,
        one_based_page_index: int,
        prefer_text_layer: bool,
        min_chars: int,
        render_dpi: int,
        extract_options: ExtractionOptions,
        completeness_enabled: bool,
        allow_retry_for_page: Callable[[int], bool] | None,
        record_ocr_snapshot: Callable[[PDFPageOCRSnapshot], None] | None,
    ) -> Iterator[PDFPageWorkUnit]:
        if prefer_text_layer:
            try:
                text_layer = document_handle.extract_text_layer(one_based_page_index)
            except Exception:
                text_layer = None
            if text_layer is not None and text_layer.char_count >= min_chars:
                hybrid_unit = self._maybe_build_hybrid_unit(
                    document_handle=document_handle,
                    zero_based_page_index=zero_based_page_index,
                    one_based_page_index=one_based_page_index,
                    render_dpi=render_dpi,
                    extract_options=extract_options,
                    completeness_enabled=completeness_enabled,
                    allow_retry_for_page=allow_retry_for_page,
                    record_ocr_snapshot=record_ocr_snapshot,
                )
                if hybrid_unit is not None:
                    yield hybrid_unit
                    return
                yield self._build_text_layer_unit(
                    zero_based_page_index=zero_based_page_index,
                    one_based_page_index=one_based_page_index,
                    normalized_text=text_layer.text,
                    extract_options=extract_options,
                )
                return

        try:
            render_page = getattr(document_handle, "render_page", None)
            if callable(render_page):
                rendered_page = render_page(one_based_page_index, render_dpi)
            else:
                render_page_to_bitmap = getattr(document_handle, "render_page_to_bitmap", None)
                if not callable(render_page_to_bitmap):
                    raise PDFAdapterError("E_PDF_002", "PDF document handle cannot render pages")
                rendered_page = render_page_to_bitmap(one_based_page_index, render_dpi).materialize()
        except Exception as exc:
            yield self._failed_unit(zero_based_page_index, exc, phase="render")
            return
        try:
            yield self._build_rendered_unit(
                zero_based_page_index=zero_based_page_index,
                rendered_page=rendered_page,
                extract_options=extract_options,
                allow_retry_for_page=allow_retry_for_page,
                record_ocr_snapshot=record_ocr_snapshot,
            )
        finally:
            rendered_page.cleanup()

    def _maybe_build_hybrid_unit(
        self,
        *,
        document_handle: Any,
        zero_based_page_index: int,
        one_based_page_index: int,
        render_dpi: int,
        extract_options: ExtractionOptions,
        completeness_enabled: bool,
        allow_retry_for_page: Callable[[int], bool] | None,
        record_ocr_snapshot: Callable[[PDFPageOCRSnapshot], None] | None,
        document_lock: threading.Lock | None = None,
    ) -> PDFPageWorkUnit | None:
        if not completeness_enabled:
            return None

        extract_page_geometry = getattr(document_handle, "extract_page_geometry", None)
        if not callable(extract_page_geometry):
            return None

        try:
            if document_lock is None:
                geometry = extract_page_geometry(one_based_page_index)
            else:
                with document_lock:
                    geometry = extract_page_geometry(one_based_page_index)
        except Exception:
            return None
        if geometry is None:
            return None

        text_fragments = [
            TextFragment(box=box, text=text)
            for box, text in geometry.text_fragments
        ]
        text_boxes = [fragment.box for fragment in text_fragments]
        uncovered_regions = detect_uncovered_raster_regions(
            list(geometry.image_boxes),
            text_boxes,
            (float(geometry.page_width), float(geometry.page_height)),
        )
        if not uncovered_regions:
            return None

        try:
            if document_lock is None:
                rendered_page = self._render_hybrid_page(document_handle, one_based_page_index, render_dpi)
            else:
                with document_lock:
                    rendered_page = self._render_hybrid_page(document_handle, one_based_page_index, render_dpi)
        except Exception:
            return None

        try:
            try:
                return self._build_hybrid_page_unit(
                    zero_based_page_index=zero_based_page_index,
                    one_based_page_index=one_based_page_index,
                    rendered_page=rendered_page,
                    page_width=float(geometry.page_width),
                    page_height=float(geometry.page_height),
                    text_fragments=text_fragments,
                    uncovered_regions=uncovered_regions,
                    extract_options=extract_options,
                )
            except _RegionOCRError as exc:
                logger.warning(
                    "Region OCR failed for PDF page %s; falling back to full-page OCR",
                    one_based_page_index,
                    exc_info=exc,
                )
                return self._build_rendered_unit(
                    zero_based_page_index=zero_based_page_index,
                    rendered_page=rendered_page,
                    extract_options=extract_options,
                    allow_retry_for_page=allow_retry_for_page,
                    record_ocr_snapshot=record_ocr_snapshot,
                )
            except Exception:
                return None
        finally:
            rendered_page.cleanup()

    @staticmethod
    def _render_hybrid_page(
        document_handle: Any,
        one_based_page_index: int,
        render_dpi: int,
    ) -> Any:
        render_page = getattr(document_handle, "render_page", None)
        if callable(render_page):
            return render_page(one_based_page_index, render_dpi)

        render_page_to_bitmap = getattr(document_handle, "render_page_to_bitmap", None)
        if not callable(render_page_to_bitmap):
            raise PDFAdapterError("E_PDF_002", "PDF document handle cannot render pages")
        return render_page_to_bitmap(one_based_page_index, render_dpi).materialize()

    def _build_hybrid_page_unit(
        self,
        *,
        zero_based_page_index: int,
        one_based_page_index: int,
        rendered_page: Any,
        page_width: float,
        page_height: float,
        text_fragments: list[TextFragment],
        uncovered_regions: list[tuple[float, float, float, float]],
        extract_options: ExtractionOptions,
    ) -> PDFPageWorkUnit:
        from PIL import Image

        with Image.open(rendered_page.image_path) as image:
            image_width, image_height = image.size

        x_scale = image_width / page_width if page_width > 0 else 1.0
        y_scale = image_height / page_height if page_height > 0 else 1.0
        region_fragments: list[TextFragment] = []
        for region in uncovered_regions:
            left, bottom, right, top = region
            crop_box = (
                round(left * x_scale),
                round(image_height - top * y_scale),
                round(right * x_scale),
                round(image_height - bottom * y_scale),
            )
            region_text = self._recognize_region(rendered_page.image_path, crop_box)
            region_fragments.append(TextFragment(box=region, text=region_text))

        normalized_text = merge_fragments_by_reading_order(text_fragments, region_fragments)
        block_count = max(1, len([line for line in normalized_text.splitlines() if line.strip()]))
        snapshot = PDFPageOCRSnapshot(
            page_index=one_based_page_index,
            normalized_text=normalized_text,
            image_path=None,
            ocr_confidence=1.0,
            confidence_min=1.0,
            block_count=block_count,
            source_path="text_layer+region_ocr",
        )
        try:
            outcome = self._extract_page_outcome(snapshot.normalized_text, extract_options)
        except Exception as exc:
            return self._failed_unit(
                zero_based_page_index,
                exc,
                phase="extract",
                snapshot=snapshot,
            )
        return PDFPageWorkUnit(
            page_index=zero_based_page_index,
            snapshot=snapshot,
            outcome=outcome,
            error=None,
            crop=None,
        )

    def _recognize_region(self, image_path: str, crop_box: tuple[int, int, int, int]) -> str:
        try:
            if self._ocr_service_supports_allow_retry():
                ocr_result = self._ocr_service.recognize(image_path, crop=crop_box, allow_retry=False)
            else:
                ocr_result = self._ocr_service.recognize(image_path, crop=crop_box)
        except Exception as exc:
            raise _RegionOCRError(str(exc)) from exc
        return str(getattr(ocr_result, "text", ""))

    @staticmethod
    def _coerce_parallelism(value: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 1
        return min(max(parsed, 1), 4)

    def _process_page_without_document(
        self,
        *,
        path: str,
        zero_based_page_index: int,
        one_based_page_index: int,
        prefer_text_layer: bool,
        min_chars: int,
        render_dpi: int,
        extract_options: ExtractionOptions,
        allow_retry_for_page: Callable[[int], bool] | None,
        record_ocr_snapshot: Callable[[PDFPageOCRSnapshot], None] | None,
    ) -> Iterator[PDFPageWorkUnit]:
        extract_text_layer = getattr(self._pdf_adapter, "extract_text_layer", None)
        if prefer_text_layer and callable(extract_text_layer):
            try:
                text_layer = extract_text_layer(path, one_based_page_index)
            except Exception:
                text_layer = None
            if text_layer is not None and text_layer.char_count >= min_chars:
                yield self._build_text_layer_unit(
                    zero_based_page_index=zero_based_page_index,
                    one_based_page_index=one_based_page_index,
                    normalized_text=text_layer.text,
                    extract_options=extract_options,
                )
                return

        render_page_image = getattr(self._pdf_adapter, "render_page_image", None)
        if callable(render_page_image):
            try:
                rendered_page = render_page_image(path, page_index=one_based_page_index, render_dpi=render_dpi)
            except Exception as exc:
                yield self._failed_unit(zero_based_page_index, exc, phase="render")
                return
            try:
                yield self._build_rendered_unit(
                    zero_based_page_index=zero_based_page_index,
                    rendered_page=rendered_page,
                    extract_options=extract_options,
                    allow_retry_for_page=allow_retry_for_page,
                    record_ocr_snapshot=record_ocr_snapshot,
                )
            finally:
                rendered_page.cleanup()
            return

        yielded = False
        try:
            for rendered_page in self._pdf_adapter.iter_page_images(path, render_dpi=render_dpi):
                if rendered_page.page_index != one_based_page_index:
                    continue
                yielded = True
                yield self._build_rendered_unit(
                    zero_based_page_index=zero_based_page_index,
                    rendered_page=rendered_page,
                    extract_options=extract_options,
                    allow_retry_for_page=allow_retry_for_page,
                    record_ocr_snapshot=record_ocr_snapshot,
                )
                break
        except Exception as exc:
            yield self._failed_unit(zero_based_page_index, exc, phase="render")
            return
        if not yielded:
            yield self._failed_unit(zero_based_page_index, PDFAdapterError("E_PDF_002", "PDF rendering produced no page"), phase="render")

    def _build_text_layer_unit(
        self,
        *,
        zero_based_page_index: int,
        one_based_page_index: int,
        normalized_text: str,
        extract_options: ExtractionOptions,
    ) -> PDFPageWorkUnit:
        _ = extract_options
        block_count = max(1, len([line for line in normalized_text.splitlines() if line.strip()]))
        snapshot = PDFPageOCRSnapshot(
            page_index=one_based_page_index,
            normalized_text=normalized_text,
            image_path=None,
            ocr_confidence=1.0,
            confidence_min=1.0,
            block_count=block_count,
            source_path="text_layer",
        )
        try:
            outcome = self._extract_page_outcome(snapshot.normalized_text, extract_options)
        except Exception as exc:
            return self._failed_unit(
                zero_based_page_index,
                exc,
                phase="extract",
                snapshot=snapshot,
            )
        return PDFPageWorkUnit(
            page_index=zero_based_page_index,
            snapshot=snapshot,
            outcome=outcome,
            error=None,
            crop=None,
        )

    def _build_rendered_unit(
        self,
        *,
        zero_based_page_index: int,
        rendered_page: Any,
        extract_options: ExtractionOptions,
        allow_retry_for_page: Callable[[int], bool] | None = None,
        record_ocr_snapshot: Callable[[PDFPageOCRSnapshot], None] | None = None,
    ) -> PDFPageWorkUnit:
        try:
            self._validate_rendered_image_path(rendered_page.image_path)
        except Exception as exc:
            return self._failed_unit(zero_based_page_index, exc, phase="render")

        try:
            ocr_result = self._recognize_rendered_page(rendered_page, allow_retry_for_page)
        except Exception as exc:
            return self._failed_unit(zero_based_page_index, exc, phase="ocr", crop=self._build_crop(rendered_page.image_path))

        snapshot = PDFPageOCRSnapshot(
            page_index=rendered_page.page_index,
            normalized_text=ocr_result.text,
            image_path=rendered_page.image_path,
            ocr_confidence=ocr_result.confidence_avg,
            confidence_min=ocr_result.confidence_min,
            block_count=int(ocr_result.block_count),
            blocks=list(ocr_result.blocks),
            adaptive_retry_triggered=bool(getattr(ocr_result, "retry_triggered", False)),
            adaptive_retry_applied=bool(getattr(ocr_result, "retry_applied", False)),
            retry_profile_from=getattr(ocr_result, "retry_profile_from", None),
            retry_profile_to=getattr(ocr_result, "retry_profile_to", None),
            first_pass_confidence_min=getattr(ocr_result, "first_pass_confidence_min", None),
            second_pass_confidence_min=getattr(ocr_result, "second_pass_confidence_min", None),
            source_path="ocr",
        )
        if record_ocr_snapshot is not None:
            record_ocr_snapshot(snapshot)
        crop = self._build_crop(rendered_page.image_path)
        try:
            outcome = self._extract_page_outcome(snapshot.normalized_text, extract_options)
        except Exception as exc:
            return self._failed_unit(zero_based_page_index, exc, phase="extract", snapshot=snapshot, crop=crop)
        return PDFPageWorkUnit(
            page_index=zero_based_page_index,
            snapshot=snapshot,
            outcome=outcome,
            error=None,
            crop=crop,
        )

    def _recognize_rendered_page(
        self,
        rendered_page: Any,
        allow_retry_for_page: Callable[[int], bool] | None,
    ) -> Any:
        if allow_retry_for_page is None or not self._ocr_service_supports_allow_retry():
            return self._ocr_service.recognize(rendered_page.image_path)
        return self._ocr_service.recognize(
            rendered_page.image_path,
            allow_retry=allow_retry_for_page(int(rendered_page.page_index)),
        )

    def _ocr_service_supports_allow_retry(self) -> bool:
        recognize = getattr(self._ocr_service, "recognize", None)
        try:
            signature = inspect.signature(recognize)
        except (TypeError, ValueError):
            return True
        return any(
            parameter.name == "allow_retry" or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )

    def _extract_page_outcome(self, normalized_text: str, extract_options: ExtractionOptions) -> Any:
        return self._extractor.extract_detailed(
            normalized_text=normalized_text,
            options=extract_options,
        )

    @staticmethod
    def _failed_unit(
        zero_based_page_index: int,
        exc: Exception,
        *,
        phase: str,
        snapshot: PDFPageOCRSnapshot | None = None,
        crop: Callable[[BBox], Any] | None = None,
    ) -> PDFPageWorkUnit:
        return PDFPageWorkUnit(
            page_index=zero_based_page_index,
            snapshot=snapshot,
            outcome=None,
            error=PageError(
                code=str(getattr(exc, "code", "E_QUEUE_001")),
                message=str(getattr(exc, "message", str(exc))),
                phase=phase,  # type: ignore[arg-type]
            ),
            crop=crop,
        )

    @staticmethod
    def _build_crop(image_path: str) -> Callable[[BBox], Any] | None:
        try:
            from PIL import Image

            image = Image.open(image_path).copy()
        except Exception:
            return None

        def _crop(bbox: BBox) -> Any:
            return image.crop(bbox)

        _crop.image_size = image.size  # type: ignore[attr-defined]
        _crop.image_path = image_path  # type: ignore[attr-defined]
        return _crop

    @staticmethod
    def _validate_rendered_image_path(image_path: Any) -> Path:
        if not isinstance(image_path, (str, Path)) or not str(image_path):
            raise PDFAdapterError("E_PDF_002", "PDF rendering produced invalid rendered image path")

        path = Path(image_path)
        if path.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
            raise PDFAdapterError("E_PDF_002", "PDF rendering produced unsupported rendered image type")
        if not path.exists() or not path.is_file():
            raise PDFAdapterError("E_PDF_002", "PDF rendering produced invalid rendered image path")

        try:
            from PIL import Image

            with Image.open(path) as image:
                image.verify()
        except Exception as exc:
            raise PDFAdapterError("E_PDF_002", "PDF rendering produced unreadable rendered image") from exc
        return path
