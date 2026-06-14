from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Iterator

from .errors import PDFAdapterError


SUPPORTED_PDF_EXTENSIONS = {".pdf"}


@dataclass(frozen=True)
class PDFDocumentInfo:
    page_count: int
    file_size: int
    # Hint only: derived from a first-page probe and must not gate per-page text-layer detection.
    has_text_layer: bool = False
    text_layer_total_chars: int = 0


@dataclass(frozen=True)
class PDFPageTextLayer:
    page_index: int
    text: str
    char_count: int


@dataclass(frozen=True)
class PDFPageGeometry:
    page_width: float
    page_height: float
    text_fragments: tuple[tuple[tuple[float, float, float, float], str], ...]
    image_boxes: tuple[tuple[float, float, float, float], ...]


@dataclass
class RenderedPDFPage:
    page_index: int
    image_path: str
    _cleanup: Callable[[], None] | None = None

    def cleanup(self) -> None:
        if self._cleanup is None:
            return
        callback = self._cleanup
        self._cleanup = None
        callback()


@dataclass
class _PendingPageRender:
    page_index: int
    bitmap: Any
    output_path: Path
    _temp_dir: TemporaryDirectory

    def materialize(self) -> RenderedPDFPage:
        try:
            pil_image = self.bitmap.to_pil()
            try:
                pil_image.save(self.output_path, format="PNG")
            finally:
                if hasattr(pil_image, "close"):
                    pil_image.close()
        except Exception as exc:
            self._temp_dir.cleanup()
            raise PDFAdapterError("E_PDF_002", f"PIL save failed for page {self.page_index}") from exc
        finally:
            if hasattr(self.bitmap, "close"):
                self.bitmap.close()

        temp_dir = self._temp_dir
        output_path = self.output_path
        return RenderedPDFPage(
            page_index=self.page_index,
            image_path=str(output_path),
            _cleanup=lambda p=output_path, temp=temp_dir: (p.unlink(missing_ok=True), temp.cleanup()),
        )


@dataclass(frozen=True)
class _DocumentHandle:
    document: Any
    page_count: int
    _render_page: Callable[[int, int], RenderedPDFPage]
    _render_page_to_bitmap: Callable[[int, int], _PendingPageRender]
    _extract_text_layer: Callable[[int], PDFPageTextLayer | None]
    _extract_page_geometry: Callable[[int], PDFPageGeometry | None]

    def extract_text_layer(self, page_index: int) -> PDFPageTextLayer | None:
        return self._extract_text_layer(page_index)

    def extract_page_geometry(self, page_index: int) -> PDFPageGeometry | None:
        return self._extract_page_geometry(page_index)

    def render_page(self, page_index: int, render_dpi: int) -> RenderedPDFPage:
        return self._render_page(page_index, render_dpi)

    def render_page_to_bitmap(self, page_index: int, render_dpi: int) -> _PendingPageRender:
        return self._render_page_to_bitmap(page_index, render_dpi)


class PDFPageAdapter:
    def __init__(self, document_factory: Callable[[str], Any] | None = None) -> None:
        self._document_factory = document_factory or self._default_document_factory

    def inspect(self, pdf_path: str) -> PDFDocumentInfo:
        path = self._validate_pdf_path(pdf_path)
        try:
            file_size = path.stat().st_size
        except OSError as exc:
            raise PDFAdapterError("E_PDF_001", "Unable to read PDF file metadata") from exc

        with self.open_document(str(path)) as handle:
            page_count = handle.page_count
            first_page_layer = handle.extract_text_layer(1) if page_count > 0 else None

        if page_count <= 0:
            raise PDFAdapterError("E_PDF_002", "PDF has no pages")
        return PDFDocumentInfo(
            page_count=page_count,
            file_size=file_size,
            has_text_layer=first_page_layer is not None,
            text_layer_total_chars=first_page_layer.char_count if first_page_layer is not None else 0,
        )

    @contextmanager
    def open_document(self, pdf_path: str) -> Iterator[_DocumentHandle]:
        path = self._validate_pdf_path(pdf_path)
        document = self._create_document(path)
        try:
            page_count = self._get_page_count(document)
            if page_count <= 0:
                raise PDFAdapterError("E_PDF_002", "PDF has no pages")
            yield _DocumentHandle(
                document=document,
                page_count=page_count,
                _extract_text_layer=lambda page_index: self._extract_text_layer_for_handle(document, page_count, page_index),
                _extract_page_geometry=lambda page_index: self._extract_page_geometry_for_handle(
                    document,
                    page_count,
                    page_index,
                ),
                _render_page=lambda page_index, render_dpi: self._render_page_for_handle(
                    document,
                    page_count,
                    page_index,
                    render_dpi,
                ),
                _render_page_to_bitmap=lambda page_index, render_dpi: self._render_page_to_bitmap_for_handle(
                    document,
                    page_count,
                    page_index,
                    render_dpi,
                ),
            )
        finally:
            self._close_document(document)

    def extract_text_layer(self, pdf_path: str, page_index: int) -> PDFPageTextLayer | None:
        path = self._validate_pdf_path(pdf_path)
        document = self._create_document(path)
        try:
            if page_index <= 0 or page_index > self._get_page_count(document):
                return None
            return self._extract_text_layer_from_document(document, page_index)
        finally:
            self._close_document(document)

    def extract_page_geometry(self, pdf_path: str, page_index: int) -> PDFPageGeometry | None:
        path = self._validate_pdf_path(pdf_path)
        document = self._create_document(path)
        try:
            if page_index <= 0 or page_index > self._get_page_count(document):
                return None
            return self._extract_page_geometry_from_document(document, page_index)
        finally:
            self._close_document(document)

    def iter_page_images(self, pdf_path: str, *, render_dpi: int) -> Iterator[RenderedPDFPage]:
        with self.open_document(pdf_path) as handle:
            for page_index in range(1, handle.page_count + 1):
                page = handle.render_page(page_index, render_dpi)
                try:
                    yield page
                finally:
                    page.cleanup()

    def render_page_image(self, pdf_path: str, *, page_index: int, render_dpi: int) -> RenderedPDFPage:
        path = self._validate_pdf_path(pdf_path)
        document = self._create_document(path)
        temp_dir = TemporaryDirectory(prefix="pdf_page_")
        scale = max(int(render_dpi), 72) / 72.0
        try:
            page_count = self._get_page_count(document)
            if page_count <= 0:
                raise PDFAdapterError("E_PDF_002", "PDF has no pages")
            if page_index <= 0 or page_index > page_count:
                raise PDFAdapterError("E_PDF_002", f"PDF page {page_index} is out of range")

            output_path = Path(temp_dir.name) / f"page_{page_index:04d}.png"
            self._render_page_to_image(
                document=document,
                page_index=page_index - 1,
                scale=scale,
                output_path=output_path,
            )
        except Exception:
            temp_dir.cleanup()
            self._close_document(document)
            raise

        def _cleanup() -> None:
            output_path.unlink(missing_ok=True)
            self._close_document(document)
            temp_dir.cleanup()

        return RenderedPDFPage(
            page_index=page_index,
            image_path=str(output_path),
            _cleanup=_cleanup,
        )

    def _create_document(self, path: Path) -> Any:
        try:
            return self._document_factory(str(path))
        except PDFAdapterError:
            raise
        except Exception as exc:
            raise PDFAdapterError("E_PDF_002", "Failed to parse PDF document") from exc

    @staticmethod
    def _default_document_factory(pdf_path: str) -> Any:
        try:
            import pypdfium2 as pdfium  # type: ignore[import-not-found]
        except Exception as exc:
            raise PDFAdapterError("E_PDF_001", "pypdfium2 dependency is not available") from exc
        return pdfium.PdfDocument(pdf_path)

    @staticmethod
    def _validate_pdf_path(pdf_path: str) -> Path:
        path = Path(pdf_path)
        if path.suffix.lower() not in SUPPORTED_PDF_EXTENSIONS:
            raise PDFAdapterError("E_PDF_001", "Invalid PDF path or unsupported file type")
        if not path.exists() or not path.is_file():
            raise PDFAdapterError("E_PDF_001", "Invalid PDF path or unsupported file type")
        return path

    @staticmethod
    def _get_page_count(document: Any) -> int:
        try:
            return int(len(document))
        except Exception as exc:
            raise PDFAdapterError("E_PDF_002", "Failed to read PDF page count") from exc

    @staticmethod
    def _close_document(document: Any) -> None:
        close = getattr(document, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _extract_text_layer_from_document(document: Any, page_index: int) -> PDFPageTextLayer | None:
        page: Any | None = None
        text_page: Any | None = None
        try:
            page = document[page_index - 1]
            get_textpage = getattr(page, "get_textpage", None)
            if not callable(get_textpage):
                return None
            text_page = get_textpage()
            # Requires pypdfium2 >= 4.x where text pages expose get_text_bounded().
            get_text_bounded = getattr(text_page, "get_text_bounded", None)
            if not callable(get_text_bounded):
                return None
            text = str(get_text_bounded() or "").strip()
            if not text:
                return None
            return PDFPageTextLayer(page_index=page_index, text=text, char_count=len(text))
        except Exception:
            return None
        finally:
            if text_page is not None and hasattr(text_page, "close"):
                text_page.close()
            if page is not None and hasattr(page, "close"):
                page.close()

    @staticmethod
    def _extract_page_geometry_from_document(document: Any, page_index: int) -> PDFPageGeometry | None:
        page: Any | None = None
        text_page: Any | None = None
        try:
            page = document[page_index - 1]
            width, height = page.get_size()

            text_fragments: list[tuple[tuple[float, float, float, float], str]] = []
            get_textpage = getattr(page, "get_textpage", None)
            if callable(get_textpage):
                text_page = get_textpage()
                count_rects = getattr(text_page, "count_rects", None)
                get_rect = getattr(text_page, "get_rect", None)
                get_text_bounded = getattr(text_page, "get_text_bounded", None)
                if callable(count_rects) and callable(get_rect) and callable(get_text_bounded):
                    for rect_index in range(int(count_rects())):
                        left, bottom, right, top = get_rect(rect_index)
                        text = str(
                            get_text_bounded(
                                left=left,
                                bottom=bottom,
                                right=right,
                                top=top,
                            )
                            or ""
                        ).strip()
                        if text:
                            text_fragments.append(((float(left), float(bottom), float(right), float(top)), text))

            image_boxes: list[tuple[float, float, float, float]] = []
            get_objects = getattr(page, "get_objects", None)
            if callable(get_objects):
                for obj in get_objects():
                    try:
                        object_type = getattr(obj, "type", None)
                        if callable(object_type):
                            object_type = object_type()
                        if object_type != 3:
                            continue
                        get_pos = getattr(obj, "get_pos", None)
                        if not callable(get_pos):
                            continue
                        left, bottom, right, top = get_pos()
                        image_boxes.append((float(left), float(bottom), float(right), float(top)))
                    except Exception:
                        continue

            return PDFPageGeometry(
                page_width=float(width),
                page_height=float(height),
                text_fragments=tuple(text_fragments),
                image_boxes=tuple(image_boxes),
            )
        except Exception:
            return None
        finally:
            if text_page is not None and hasattr(text_page, "close"):
                text_page.close()
            if page is not None and hasattr(page, "close"):
                page.close()

    @staticmethod
    def _render_page_to_image(
        *,
        document: Any,
        page_index: int,
        scale: float,
        output_path: Path,
    ) -> None:
        page: Any | None = None
        bitmap: Any | None = None
        pil_image: Any | None = None
        try:
            page = document[page_index]
            bitmap = page.render(scale=scale)
            to_pil = getattr(bitmap, "to_pil", None)
            if not callable(to_pil):
                raise PDFAdapterError("E_PDF_002", "PDF rendering backend does not support PIL output")
            pil_image = to_pil()
            pil_image.save(output_path, format="PNG")
        except PDFAdapterError:
            raise
        except Exception as exc:
            raise PDFAdapterError("E_PDF_002", f"Failed to render PDF page {page_index + 1}") from exc
        finally:
            if pil_image is not None and hasattr(pil_image, "close"):
                pil_image.close()
            if bitmap is not None and hasattr(bitmap, "close"):
                bitmap.close()
            if page is not None and hasattr(page, "close"):
                page.close()

    def _extract_text_layer_for_handle(
        self,
        document: Any,
        page_count: int,
        page_index: int,
    ) -> PDFPageTextLayer | None:
        if page_index <= 0 or page_index > page_count:
            return None
        return self._extract_text_layer_from_document(document, page_index)

    def _extract_page_geometry_for_handle(
        self,
        document: Any,
        page_count: int,
        page_index: int,
    ) -> PDFPageGeometry | None:
        if page_index <= 0 or page_index > page_count:
            return None
        return self._extract_page_geometry_from_document(document, page_index)

    def _render_page_for_handle(
        self,
        document: Any,
        page_count: int,
        page_index: int,
        render_dpi: int,
    ) -> RenderedPDFPage:
        if page_index <= 0 or page_index > page_count:
            raise PDFAdapterError("E_PDF_002", f"PDF page {page_index} is out of range")

        temp_dir = TemporaryDirectory(prefix="pdf_page_")
        output_path = Path(temp_dir.name) / f"page_{page_index:04d}.png"
        scale = max(int(render_dpi), 72) / 72.0
        try:
            self._render_page_to_image(
                document=document,
                page_index=page_index - 1,
                scale=scale,
                output_path=output_path,
            )
        except Exception:
            temp_dir.cleanup()
            raise

        return RenderedPDFPage(
            page_index=page_index,
            image_path=str(output_path),
            _cleanup=lambda p=output_path, temp=temp_dir: (p.unlink(missing_ok=True), temp.cleanup()),
        )

    def _render_page_to_bitmap_for_handle(
        self,
        document: Any,
        page_count: int,
        page_index: int,
        render_dpi: int,
    ) -> _PendingPageRender:
        if page_index <= 0 or page_index > page_count:
            raise PDFAdapterError("E_PDF_002", f"PDF page {page_index} is out of range")

        temp_dir = TemporaryDirectory(prefix="pdf_page_")
        output_path = Path(temp_dir.name) / f"page_{page_index:04d}.png"
        scale = max(int(render_dpi), 72) / 72.0
        page: Any | None = None
        try:
            page = document[page_index - 1]
            bitmap = page.render(scale=scale)
        except Exception as exc:
            temp_dir.cleanup()
            raise PDFAdapterError("E_PDF_002", f"Failed to render PDF page {page_index}") from exc
        finally:
            if page is not None and hasattr(page, "close"):
                page.close()

        return _PendingPageRender(
            page_index=page_index,
            bitmap=bitmap,
            output_path=output_path,
            _temp_dir=temp_dir,
        )
