from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from src.domain.schemas import AppConfig, ColumnSpec, ExtractRow, ExtractionInput, ExtractionOutcome
from src.extract import Extractor, region_attribution
from src.extract.grounding import classify_cell, classify_row, stronger_status
from src.extract.output_normalizer import canonicalize_typed_cells, normalize_rows
from src.ocr.models import OCRTextBlock


@dataclass(frozen=True)
class ExtractResult:
    success_rows: list[ExtractRow]
    rescue_events: list[dict[str, Any]]
    extraction_input: ExtractionInput | None
    error_code: str | None = None
    error_message: str | None = None


class ExtractStage:
    """Extract Stage 的纯 per-item 抽取 seam，止于 reducer 之前。"""

    def __init__(self, *, source_processor: Any, result_store: Any) -> None:
        self._source_processor = source_processor
        self._result_store = result_store

    def extract_one(
        self,
        *,
        item: Any,
        config: AppConfig,
        extractor: Extractor,
        expected_columns: int,
    ) -> ExtractResult:
        extraction_input, ocr_confidence = self._extract_item_text(item)
        try:
            outcome, extraction_input, ocr_confidence = self._extract_pdf_rows_with_ocr_fallback(
                extractor=extractor,
                item=item,
                config=config,
                extraction_input=extraction_input,
                ocr_confidence=ocr_confidence,
            )
            grounded_rows = outcome.rows
            normalized_rows = normalize_rows(
                [row.values for row in grounded_rows],
                expected_columns=expected_columns,
            )
            page_index = item.snapshot.page_index if item.snapshot is not None else None
            success_rows = self._result_store.build_success_rows(
                task_id=item.task.task_id,
                normalized_rows=normalized_rows,
                grounded_rows=grounded_rows,
                ocr_confidence=ocr_confidence,
                typed_rows=None,
                page_index=page_index,
            )
            crop = (
                item.crop
                if item.source_type == "pdf"
                else self._build_image_crop(item.task.source_value) if item.source_type == "image" else None
            )
            success_rows, rescue_events = self._maybe_rescue_uncertain_fields(
                success_rows=success_rows,
                source_text=extraction_input.flat_text,
                crop=crop,
                outcome=outcome,
                config=config,
            )
            sidecar_geometry, sidecar_events = self._build_text_layer_attribution_geometry(
                item=item, outcome=outcome, success_rows=success_rows, config=config
            )
            if sidecar_events:
                rescue_events = list(rescue_events) + sidecar_events
            crop_image_size = getattr(crop, "image_size", None) if crop is not None else None
            attribution_blocks = (
                list(item.snapshot.blocks)
                if item.source_type == "pdf" and item.snapshot is not None
                else list(item.blocks)
            )
            if sidecar_geometry is not None:
                attribution_blocks, crop_image_size = sidecar_geometry[0], sidecar_geometry[1]
            if crop_image_size and len(crop_image_size) == 2 and attribution_blocks:
                success_rows, attribution_events = self._maybe_correct_field_attribution(
                    success_rows=success_rows,
                    blocks=attribution_blocks,
                    page_width=int(crop_image_size[0]),
                    page_height=int(crop_image_size[1]),
                    outcome=outcome,
                    source_text=extraction_input.flat_text,
                    config=config,
                )
                rescue_events = list(rescue_events) + attribution_events
            self._finalize_typed_rows(
                success_rows=success_rows,
                column_specs=list(outcome.column_specs),
            )
            return ExtractResult(
                success_rows=success_rows,
                rescue_events=rescue_events,
                extraction_input=extraction_input,
            )
        except Exception as exc:
            return ExtractResult(
                success_rows=[],
                rescue_events=[],
                extraction_input=extraction_input,
                error_code=str(getattr(exc, "code", "E_QUEUE_001")),
                error_message=str(getattr(exc, "message", str(exc))),
            )

    def _extract_item_text(self, item: Any) -> tuple[ExtractionInput, float | None]:
        if item.source_type == "pdf" and item.snapshot is not None:
            return (
                ExtractionInput(
                    flat_text=item.snapshot.normalized_text,
                    markdown=item.snapshot.markdown_text,
                ),
                item.snapshot.ocr_confidence,
            )
        flat_text = item.normalized_text or item.source_value
        return ExtractionInput(flat_text=flat_text, markdown=item.markdown), item.ocr_confidence

    @staticmethod
    def _finalize_typed_rows(
        *,
        success_rows: list[ExtractRow],
        column_specs: list[ColumnSpec],
    ) -> None:
        if not column_specs:
            return
        for row in success_rows:
            typed_cells, warnings = canonicalize_typed_cells([row.values], column_specs)
            row.typed_values = typed_cells[0]
            for warning in warnings:
                if warning.get("code") not in {"W_NORM_PHONE", "W_NORM_EMAIL"}:
                    continue
                col = int(warning.get("col", -1))
                if 0 <= col < len(row.grounded_cells):
                    row.grounded_cells[col].status = "UNCERTAIN"

    @staticmethod
    def _extract_grounded_rows(
        *,
        extractor: Extractor,
        text: ExtractionInput | str,
        config: AppConfig,
        ocr_confidence: float | None,
    ) -> ExtractionOutcome:
        return extractor.extract_detailed(
            text=text,
            prompts=config.prompts,
            examples=config.examples_normalized,
            provider_cfg=config,
            ocr_confidence=ocr_confidence,
        )

    def _extract_pdf_rows_with_ocr_fallback(
        self,
        *,
        extractor: Extractor,
        item: Any,
        config: AppConfig,
        extraction_input: ExtractionInput,
        ocr_confidence: float | None,
    ) -> tuple[ExtractionOutcome, ExtractionInput, float | None]:
        """先按原输入抽取；PDF 文本层页若得 0 行，则整页 OCR 重抽该页。

        部分电子发票 PDF 的文本层顺序错乱，LLM 抽取结果在 strict/balanced grounding
        下无法精确定位 → 0 行 → 全页 empty → E_PDF_005。OCR（视觉阅读顺序）能产出
        可定位的行。仅在文本层路径产出 0 行时回退，保留文本层的速度优势；OCR 仍为空
        或渲染/识别失败时，返回原始（空）结果，行为与未回退一致。
        """
        outcome = self._extract_grounded_rows(
            extractor=extractor,
            text=extraction_input,
            config=config,
            ocr_confidence=ocr_confidence,
        )
        if outcome.rows:
            return outcome, extraction_input, ocr_confidence

        snapshot = item.snapshot
        if item.source_type != "pdf" or snapshot is None:
            return outcome, extraction_input, ocr_confidence
        if not str(getattr(snapshot, "source_path", "") or "").startswith("text_layer"):
            return outcome, extraction_input, ocr_confidence

        render_dpi = max(int(getattr(config, "pdf_render_dpi", 200)), 72)
        try:
            rendered = self._source_processor.pdf_adapter.render_page_image(
                item.source_value, page_index=snapshot.page_index, render_dpi=render_dpi
            )
            try:
                ocr_result = self._source_processor.ocr_service.recognize(rendered.image_path)
            finally:
                rendered.cleanup()
        except Exception:
            return outcome, extraction_input, ocr_confidence

        ocr_text = str(getattr(ocr_result, "text", "") or "").strip()
        if not ocr_text:
            return outcome, extraction_input, ocr_confidence

        fallback_input = ExtractionInput.from_text(ocr_text)
        fallback_confidence = getattr(ocr_result, "confidence_avg", None)
        fallback_outcome = self._extract_grounded_rows(
            extractor=extractor,
            text=fallback_input,
            config=config,
            ocr_confidence=fallback_confidence,
        )
        if not fallback_outcome.rows:
            return outcome, extraction_input, ocr_confidence

        # 回退命中：改写快照为 OCR 页，使后续归属/统计按 OCR 处理。
        snapshot.normalized_text = ocr_text
        snapshot.ocr_confidence = fallback_confidence
        snapshot.blocks = list(getattr(ocr_result, "blocks", []) or [])
        snapshot.source_path = "ocr"
        return fallback_outcome, fallback_input, fallback_confidence

    def _maybe_rescue_uncertain_fields(
        self,
        *,
        success_rows: list[ExtractRow],
        source_text: str,
        crop: Callable[[tuple[int, int, int, int]], Any] | None,
        outcome: ExtractionOutcome,
        config: AppConfig,
    ) -> tuple[list[ExtractRow], list[dict[str, Any]]]:
        field_regions = list(outcome.field_regions)
        crop_callback = crop
        if not success_rows or not field_regions or crop_callback is None:
            return success_rows, []

        image_size = getattr(crop_callback, "image_size", None)
        if not image_size or len(image_size) != 2:
            return success_rows, []
        image_width, image_height = int(image_size[0]), int(image_size[1])
        image_path = getattr(crop_callback, "image_path", None)

        header = [column.name for column in outcome.column_specs]
        regions_by_field = {region.field_name: region for region in field_regions}
        remaining_budget = max(int(getattr(config, "region_rescue_max_per_task", 5)), 0)
        if remaining_budget <= 0:
            return success_rows, []

        ocr_service = self._source_processor.ocr_service
        rescue_events: list[dict[str, Any]] = []
        for row in success_rows:
            if remaining_budget <= 0:
                break
            if not row.grounded_cells:
                continue
            for index, cell in enumerate(row.grounded_cells):
                if remaining_budget <= 0:
                    break
                if getattr(cell, "status", None) != "UNCERTAIN":
                    continue
                if index >= len(header):
                    continue
                field_name = header[index]
                region = regions_by_field.get(field_name)
                if region is None:
                    continue
                crop_box = self._clip_region_crop(region, image_width=image_width, image_height=image_height)
                if crop_box is None:
                    rescue_events.append(
                        {"kind": "ocr_rescue", "field": field_name, "triggered": True, "success": False, "reason": "invalid_crop"}
                    )
                    continue
                remaining_budget -= 1
                try:
                    cropped_image = crop_callback(crop_box)
                    if image_path is not None:
                        ocr_result = ocr_service.recognize(image_path, crop=crop_box)
                    else:
                        ocr_result = ocr_service.recognize(cropped_image)
                except Exception:
                    rescue_events.append(
                        {"kind": "ocr_rescue", "field": field_name, "triggered": True, "success": False, "reason": "ocr_failed"}
                    )
                    continue
                rescued_text = str(getattr(ocr_result, "text", "")).strip()
                if not rescued_text:
                    rescue_events.append(
                        {"kind": "ocr_rescue", "field": field_name, "triggered": True, "success": False, "reason": "empty"}
                    )
                    continue
                row.values[index] = rescued_text
                if index < len(row.grounded_cells):
                    row.grounded_cells[index] = classify_cell(rescued_text, source_text, config)
                next_classification = classify_row(row.grounded_cells)
                row.extraction_classification = stronger_status(row.extraction_classification, next_classification)
                rescue_events.append({"kind": "ocr_rescue", "field": field_name, "triggered": True, "success": True})
        return success_rows, rescue_events

    def _build_text_layer_attribution_geometry(
        self,
        *,
        item: Any,
        outcome: ExtractionOutcome,
        success_rows: list[ExtractRow],
        config: AppConfig,
    ) -> tuple[tuple[list[OCRTextBlock], tuple[int, int]] | None, list[dict[str, Any]]]:
        """text-layer PDF 页按需补归属几何。返回 (geometry|None, degrade_events)。"""
        if not getattr(config, "pdf_text_layer_attribution_ocr", True):
            return None, []
        snapshot = item.snapshot
        if item.source_type != "pdf" or snapshot is None or snapshot.source_path != "text_layer":
            return None, []
        header = [column.name for column in outcome.column_specs]
        if not region_attribution.has_resolvable_pairs(
            outcome.field_groups, outcome.exclusive_group_pairs, header, success_rows
        ):
            return None, []
        render_dpi = max(int(getattr(config, "pdf_render_dpi", 200)), 72)
        try:
            # snapshot.page_index 是 1-based，render_page_image 内部会自行转为 0-based。
            rendered_page = self._source_processor.pdf_adapter.render_page_image(
                item.source_value, page_index=snapshot.page_index, render_dpi=render_dpi
            )
            try:
                ocr_result = self._source_processor.ocr_service.recognize(rendered_page.image_path)
                blocks = list(getattr(ocr_result, "blocks", []) or [])
                from PIL import Image

                with Image.open(rendered_page.image_path) as image:
                    image_size = image.size
            finally:
                rendered_page.cleanup()
        except Exception:
            return None, [
                {
                    "kind": "attribution_correction",
                    "action": "sidecar_unavailable",
                    "success": False,
                }
            ]
        if not blocks or not image_size or len(image_size) != 2:
            return None, []
        snapshot.source_path = "text_layer+ocr"
        return (blocks, (int(image_size[0]), int(image_size[1]))), []

    def _maybe_correct_field_attribution(
        self,
        *,
        success_rows: list[ExtractRow],
        blocks: list[OCRTextBlock],
        page_width: int,
        page_height: int,
        outcome: ExtractionOutcome,
        source_text: str,
        config: AppConfig,
    ) -> tuple[list[ExtractRow], list[dict[str, Any]]]:
        field_groups = list(getattr(outcome, "field_groups", ()) or ())
        pairs = list(getattr(outcome, "exclusive_group_pairs", ()) or ())
        if not success_rows or not field_groups or not pairs or not blocks:
            return success_rows, []
        if page_width <= 0 or page_height <= 0:
            return success_rows, []

        groups_by_name = {group.name: group for group in field_groups}
        header = [column.name for column in outcome.column_specs]
        if not region_attribution.has_resolvable_pairs(field_groups, pairs, header, success_rows):
            return success_rows, []
        col_index = {name: idx for idx, name in enumerate(header)}
        events: list[dict[str, Any]] = []

        for name_a, name_b in pairs:
            group_a = groups_by_name.get(name_a)
            group_b = groups_by_name.get(name_b)
            if group_a is None or group_b is None:
                continue
            try:
                division = region_attribution.resolve_pair_division(
                    blocks, group_a, group_b, page_width=page_width, page_height=page_height
                )
            except Exception:
                division = None
            if division is None:
                continue

            role_count = min(len(group_a.field_names), len(group_b.field_names))
            for role in range(role_count):
                field_a = group_a.field_names[role]
                field_b = group_b.field_names[role]
                idx_a = col_index.get(field_a)
                idx_b = col_index.get(field_b)
                if idx_a is None or idx_b is None:
                    continue
                for row in success_rows:
                    if idx_a >= len(row.values) or idx_b >= len(row.values):
                        continue
                    value_a = row.values[idx_a]
                    value_b = row.values[idx_b]
                    if not value_a.strip() or not value_b.strip():
                        continue
                    try:
                        region_a = region_attribution.locate_field(value_a, blocks, division)
                        region_b = region_attribution.locate_field(value_b, blocks, division)
                    except Exception:
                        region_a = region_b = None

                    crossed = region_a == name_b and region_b == name_a
                    correct = region_a == name_a and region_b == name_b
                    if crossed:
                        row.values[idx_a], row.values[idx_b] = value_b, value_a
                        if idx_a < len(row.grounded_cells):
                            row.grounded_cells[idx_a] = classify_cell(value_b, source_text, config)
                        if idx_b < len(row.grounded_cells):
                            row.grounded_cells[idx_b] = classify_cell(value_a, source_text, config)
                        if row.typed_values is not None and idx_a < len(row.typed_values) and idx_b < len(row.typed_values):
                            row.typed_values[idx_a], row.typed_values[idx_b] = (
                                row.typed_values[idx_b], row.typed_values[idx_a],
                            )
                        row.extraction_classification = stronger_status(
                            row.extraction_classification, classify_row(row.grounded_cells)
                        )
                        events.append({
                            "kind": "attribution_correction",
                            "field_pair": (field_a, field_b),
                            "from_group": name_a,
                            "to_group": name_b,
                            "action": "swap",
                            "success": True,
                        })
                    elif correct:
                        continue
                    else:
                        reason = "single_sided" if (region_a is None) ^ (region_b is None) else "geometry_unknown"
                        if region_a is None and region_b is None:
                            reason = "geometry_unknown"
                        for idx in (idx_a, idx_b):
                            if idx < len(row.grounded_cells):
                                row.grounded_cells[idx].status = "UNCERTAIN"
                        row.extraction_classification = classify_row(row.grounded_cells)
                        events.append({
                            "kind": "attribution_correction",
                            "field_pair": (field_a, field_b),
                            "action": "mark_uncertain",
                            "reason": reason,
                            "success": False,
                        })
        return success_rows, events

    @staticmethod
    def _clip_region_crop(region: Any, *, image_width: int, image_height: int) -> tuple[int, int, int, int] | None:
        x1 = int(float(region.left) * image_width)
        y1 = int(float(region.top) * image_height)
        x2 = int(float(region.right) * image_width)
        y2 = int(float(region.bottom) * image_height)
        left = max(0, min(x1, image_width))
        top = max(0, min(y1, image_height))
        right = max(0, min(x2, image_width))
        bottom = max(0, min(y2, image_height))
        if right <= left or bottom <= top:
            return None
        return left, top, right, bottom

    @staticmethod
    def _build_image_crop(image_path: str) -> Callable[[tuple[int, int, int, int]], Any] | None:
        try:
            from PIL import Image

            image = Image.open(image_path)
        except Exception:
            return None

        def _crop(bbox: tuple[int, int, int, int]) -> Any:
            return image.crop(bbox)

        _crop.image_size = image.size  # type: ignore[attr-defined]
        _crop.image_path = image_path  # type: ignore[attr-defined]
        return _crop
