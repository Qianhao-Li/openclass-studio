from __future__ import annotations
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from openclass_core.services.source_visual_extraction_types import RawSourceVisual, SourceVisualAdapterResult
MAX_PDF_SOURCE_PAGES = 2000
MAX_PDF_RENDER_REGIONS = 1000
MAX_PDF_REGION_PIXELS = 16000000
MAX_PDF_TOTAL_RENDER_PIXELS = 128000000
MAX_PDF_TOTAL_RENDERED_BYTES = 128 * 1024 * 1024
MAX_PDF_OBJECTS_PER_PAGE = 500
MAX_VECTOR_TEXT_LAYOUT_PAD = 96.0
MAX_VECTOR_TEXT_CONFIDENT_GAP = 48.0
VECTOR_TEXT_RENDER_PADDING = 3.0
SCAN_REGION_SIDE_MARGIN_RATIO = 0.055
SCAN_REGION_CAPTION_GAP = 4.0
_FIGURE_CAPTION_RE = re.compile('^(?:图|表|figure|fig\\.?|table)\\s*[A-Za-z0-9IVXivx]+(?:\\s*[-–—－.]\\s*[A-Za-z0-9IVXivx]+)?(?=\\s|$|[:：,，。;；(（\\u3400-\\u9fff])', re.IGNORECASE)

@dataclass
class _PdfRenderBudget:
    regions: int = 0
    pixels: int = 0
    rendered_bytes: int = 0
    exhausted: bool = False

    def reserve(self, width: int, height: int) -> bool:
        pixels = width * height
        if width <= 0 or height <= 0 or pixels > MAX_PDF_REGION_PIXELS or (self.regions >= MAX_PDF_RENDER_REGIONS) or (self.pixels + pixels > MAX_PDF_TOTAL_RENDER_PIXELS):
            self.exhausted = True
            return False
        self.regions += 1
        self.pixels += pixels
        return True

    def account_rendered_bytes(self, size: int) -> bool:
        if size < 0 or self.rendered_bytes + size > MAX_PDF_TOTAL_RENDERED_BYTES:
            self.exhausted = True
            return False
        self.rendered_bytes += size
        return True

@dataclass(frozen=True)
class _PdfTextLine:
    rect: tuple[float, float, float, float]
    text: str

@dataclass(frozen=True)
class _PdfTextRow:
    rect: tuple[float, float, float, float]
    text: str
    fragment_count: int

@dataclass(frozen=True)
class _CaptionAnchoredScanRegion:
    rect: tuple[float, float, float, float]
    caption: str
    caption_rect: tuple[float, float, float, float]
    text_row_count: int
    embedded_component_count: int = 0

@dataclass(frozen=True)
class _VectorLayoutRegion:
    rect: tuple[float, float, float, float]
    included_text_lines: int
    ambiguity_reasons: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return not self.ambiguity_reasons

def extract_pdf_visuals(path: Path, *, progress_callback: Callable[[int, int], None] | None=None, page_start: int | None=None, page_end: int | None=None) -> SourceVisualAdapterResult:
    try:
        from openclass_core.services import pdf_backend as fitz
    except Exception:
        return SourceVisualAdapterResult(status='failed', warnings=['PyMuPDF is unavailable; PDF visual regions were not indexed.'])
    visuals: list[RawSourceVisual] = []
    warnings: list[str] = []
    try:
        document = fitz.open(path)
    except Exception as exc:
        return SourceVisualAdapterResult(status='failed', warnings=[f'PDF visual parsing failed: {exc}'])
    native_order = 0
    scoped_total = 0
    render_budget = _PdfRenderBudget()
    native_image_identities: dict[int, str] = {}
    try:
        if document.page_count > MAX_PDF_SOURCE_PAGES:
            return SourceVisualAdapterResult(status='failed', warnings=[f'PDF has {document.page_count} pages; the visual indexing limit is {MAX_PDF_SOURCE_PAGES}.'])
        first_page_index = max(0, (page_start or 1) - 1)
        last_page_index = min(document.page_count - 1, (page_end or document.page_count) - 1)
        if first_page_index > last_page_index:
            return SourceVisualAdapterResult(status='failed', warnings=['The requested PDF visual range is outside the document.'])
        scoped_total = last_page_index - first_page_index + 1
        for scoped_index, page_index in enumerate(range(first_page_index, last_page_index + 1)):
            if progress_callback is not None:
                progress_callback(scoped_index, scoped_total)
            page = document.load_page(page_index)
            page_rect = page.rect
            occupied: list[tuple[float, float, float, float]] = []
            has_full_page_carrier = False
            tables = _page_tables(page)
            if len(tables) > MAX_PDF_OBJECTS_PER_PAGE:
                render_budget.exhausted = True
                break
            for table_index, table in enumerate(tables):
                rect = _rect_tuple(getattr(table, 'bbox', ()))
                if not _useful_region(rect, page_rect):
                    continue
                extracted_rows = _extract_table(table)
                table_data = _normalize_table_data(extracted_rows)
                if not table_data:
                    continue
                matrix_ambiguity_reasons = _table_matrix_ambiguity_reasons(table, extracted_rows)
                rendered, width, height = _render_region(page, rect, budget=render_budget)
                if render_budget.exhausted:
                    break
                normalized_bbox = _normalize_bbox(rect, page_rect)
                visuals.append(RawSourceVisual(kind='table', source_locator=f'pdf:page:{page_index + 1}:table:{table_index}', native_order=native_order, content=rendered, mime_type='image/png' if rendered else '', page_no=page_index + 1, bbox=normalized_bbox, caption=_caption_below(page, rect), ocr_text=_text_in_rect(page, rect), table_data=table_data, width=width, height=height, confidence=0.96, metadata={'pdf_region_type': 'table', 'table_index': table_index, 'pdf_table_matrix_complete': not matrix_ambiguity_reasons, 'pdf_table_matrix_ambiguity_reasons': list(matrix_ambiguity_reasons), 'force_unverified': bool(matrix_ambiguity_reasons), **_boundary_gap_metadata(page, rect)}))
                native_order += 1
                occupied.append(rect)
            if render_budget.exhausted:
                break
            seen_image_rects: set[tuple[float, float, float, float]] = set()
            image_occurrences: list[tuple[int, int, int, tuple[float, float, float, float], str]] = []
            logical_component_rects: list[tuple[float, float, float, float]] = []
            try:
                images = page.get_images(full=True)
            except Exception:
                images = []
            if len(images) > MAX_PDF_OBJECTS_PER_PAGE:
                render_budget.exhausted = True
                break
            for image_index, image in enumerate(images):
                if not image:
                    continue
                xref = int(image[0])
                native_object_identity = _native_image_identity(document, xref, cache=native_image_identities)
                try:
                    rects = page.get_image_rects(xref)
                except Exception:
                    rects = []
                for occurrence_index, image_rect in enumerate(rects):
                    rect = _rect_tuple(image_rect)
                    rounded = tuple((round(value, 2) for value in rect))
                    if rounded in seen_image_rects:
                        continue
                    seen_image_rects.add(rounded)
                    if _is_full_page_background_region(rect, page_rect):
                        has_full_page_carrier = True
                        continue
                    if _valid_text_rect(rect):
                        logical_component_rects.append(rect)
                    if not _useful_region(rect, page_rect):
                        continue
                    image_occurrences.append((image_index, xref, occurrence_index, rect, native_object_identity))
            logical_regions = _caption_anchored_scan_regions(page, embedded_rects=logical_component_rects)
            if len(logical_regions) > MAX_PDF_OBJECTS_PER_PAGE:
                render_budget.exhausted = True
                break
            for region_index, region in enumerate(logical_regions):
                rect = region.rect
                if any((_substantial_overlap(rect, prior, threshold=0.72) for prior in occupied)):
                    continue
                rendered, width, height = _render_region(page, rect, budget=render_budget)
                if render_budget.exhausted:
                    break
                if not rendered:
                    continue
                component_count = region.embedded_component_count
                visuals.append(RawSourceVisual(kind='image' if component_count else 'diagram', source_locator=f'pdf:page:{page_index + 1}:logical-figure:{region_index}', native_order=native_order, content=rendered, mime_type='image/png', page_no=page_index + 1, bbox=_normalize_bbox(rect, page_rect), caption=region.caption, ocr_text=_text_in_rect(page, rect), width=width, height=height, confidence=0.96 if component_count else 0.9, metadata={'pdf_region_type': 'caption_anchored_logical_region', 'scan_page_carrier': has_full_page_carrier, 'caption_bbox': _normalize_bbox(region.caption_rect, page_rect), 'visual_text_row_count': region.text_row_count, 'embedded_component_count': component_count, 'visual_completeness_verified': True, 'visual_completeness_status': 'verified_logical_region', 'requires_full_visual_capture': True, 'codex_render_policy': 'recreate_simple_or_keep_original', **_boundary_gap_metadata(page, rect)}))
                native_order += 1
                occupied.append(rect)
            if render_budget.exhausted:
                break
            for image_index, xref, occurrence_index, rect, native_object_identity in image_occurrences:
                if any((_overlap_ratio(rect, prior) > 0.92 for prior in occupied)):
                    continue
                self_contained = _embedded_image_is_self_contained(rect, page_rect, logical_component_rects)
                rendered, width, height = _render_region(page, rect, budget=render_budget)
                if render_budget.exhausted:
                    break
                if not rendered:
                    continue
                visuals.append(RawSourceVisual(kind='image', source_locator=f'pdf:page:{page_index + 1}:image:{image_index}:occurrence:{occurrence_index}', native_order=native_order, content=rendered, mime_type='image/png', page_no=page_index + 1, bbox=_normalize_bbox(rect, page_rect), caption=_caption_below(page, rect), ocr_text=_text_in_rect(page, rect), width=width, height=height, confidence=0.94, metadata={'pdf_region_type': 'embedded_image', 'xref': xref, **({'native_object_identity': native_object_identity} if native_object_identity else {}), 'visual_completeness_verified': self_contained, 'visual_completeness_status': 'verified_native_object' if self_contained else 'ambiguous_embedded_component', 'requires_full_visual_capture': True, 'force_unverified': not self_contained, **_boundary_gap_metadata(page, rect)}))
                native_order += 1
                occupied.append(rect)
            if render_budget.exhausted:
                break
            vector_clusters = _vector_clusters(page)
            if len(vector_clusters) > MAX_PDF_OBJECTS_PER_PAGE:
                render_budget.exhausted = True
                break
            for cluster_index, vector_rect in enumerate(vector_clusters):
                rect = _rect_tuple(vector_rect)
                if not _useful_region(rect, page_rect):
                    continue
                if any((_overlap_ratio(rect, prior) > 0.72 for prior in occupied)):
                    continue
                layout_region = _vector_layout_region(page, rect)
                render_rect = layout_region.rect
                rendered, width, height = _render_region(page, render_rect, budget=render_budget)
                if render_budget.exhausted:
                    break
                if not rendered:
                    continue
                inside_text = _text_in_rect(page, render_rect)
                visuals.append(RawSourceVisual(kind='diagram', source_locator=f'pdf:page:{page_index + 1}:vector:{cluster_index}', native_order=native_order, content=rendered, mime_type='image/png', page_no=page_index + 1, bbox=_normalize_bbox(render_rect, page_rect), caption=_caption_below(page, render_rect), ocr_text=inside_text, width=width, height=height, confidence=0.86 if inside_text else 0.78, metadata={'pdf_region_type': 'vector_cluster', 'vector_drawing_bbox': _normalize_bbox(rect, page_rect), 'vector_text_layout_verified': layout_region.verified, 'vector_text_lines_included': layout_region.included_text_lines, 'vector_text_ambiguity_reasons': list(layout_region.ambiguity_reasons), 'force_unverified': not layout_region.verified, **_boundary_gap_metadata(page, render_rect)}))
                native_order += 1
                occupied.append(render_rect)
            if render_budget.exhausted:
                break
    finally:
        if progress_callback is not None and (not render_budget.exhausted):
            progress_callback(scoped_total, scoped_total)
        document.close()
    if render_budget.exhausted:
        warnings.append('PDF visual indexing stopped at the configured render resource budget.')
    has_unverified_layout = any((bool(visual.metadata.get('force_unverified')) for visual in visuals))
    return SourceVisualAdapterResult(visuals=[] if render_budget.exhausted else _merge_verified_cross_page_visuals(visuals), warnings=warnings, status='failed' if render_budget.exhausted else 'partial' if warnings or has_unverified_layout else 'ready')

def _page_tables(page: Any) -> list[Any]:
    finder = getattr(page, 'find_tables', None)
    if not callable(finder):
        return []
    try:
        result = finder()
        return list(getattr(result, 'tables', []) or [])
    except Exception:
        return []

def _extract_table(table: Any) -> list[list[Any]]:
    try:
        extracted = table.extract()
    except Exception:
        return []
    return extracted if isinstance(extracted, list) else []

def _normalize_table_data(rows: Iterable[Any]) -> list[list[str]]:
    normalized: list[list[str]] = []
    for row in rows:
        if isinstance(row, (str, bytes)) or not isinstance(row, Iterable):
            continue
        values = [str(value or '').strip() for value in row]
        if any(values):
            normalized.append(values)
    return normalized

def _table_matrix_ambiguity_reasons(table: Any, rows: list[list[Any]]) -> tuple[str, ...]:
    reasons: list[str] = []
    row_lengths: list[int] = []
    for row in rows:
        if isinstance(row, (str, bytes)) or not isinstance(row, Iterable):
            reasons.append('non_row_value')
            continue
        values = list(row)
        row_lengths.append(len(values))
        if any((value is None for value in values)):
            reasons.append('null_span_placeholder')
    if row_lengths and any((length != row_lengths[0] for length in row_lengths[1:])):
        reasons.append('ragged_rows')
    declared_rows = _positive_int(getattr(table, 'row_count', None))
    declared_columns = _positive_int(getattr(table, 'col_count', None))
    if declared_rows is not None and declared_rows != len(rows):
        reasons.append('declared_row_count_mismatch')
    if declared_columns is not None and any((length != declared_columns for length in row_lengths)):
        reasons.append('declared_column_count_mismatch')
    cells = getattr(table, 'cells', None)
    if declared_rows is not None and declared_columns is not None and isinstance(cells, (list, tuple)) and (len(cells) != declared_rows * declared_columns):
        reasons.append('physical_cell_count_mismatch')
    return tuple(dict.fromkeys(reasons))

def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None

def _vector_clusters(page: Any) -> list[Any]:
    cluster_drawings = getattr(page, 'cluster_drawings', None)
    if callable(cluster_drawings):
        try:
            return list(cluster_drawings() or [])
        except Exception:
            pass
    try:
        drawings = page.get_drawings()
    except Exception:
        return []
    return [drawing.get('rect') for drawing in drawings if drawing.get('rect') is not None]

def _vector_layout_region(page: Any, drawing_rect: tuple[float, float, float, float]) -> _VectorLayoutRegion:
    """Expand a drawing cluster only when nearby text has deterministic layout ownership."""
    text_lines, extraction_reliable = _page_text_lines(page)
    if not extraction_reliable:
        return _VectorLayoutRegion(rect=drawing_rect, included_text_lines=0, ambiguity_reasons=('text_layout_unavailable',))
    if not text_lines:
        return _VectorLayoutRegion(rect=drawing_rect, included_text_lines=0)
    width = max(1.0, drawing_rect[2] - drawing_rect[0])
    height = max(1.0, drawing_rect[3] - drawing_rect[1])
    broad_horizontal_gap = min(MAX_VECTOR_TEXT_LAYOUT_PAD, max(24.0, width * 0.32))
    broad_vertical_gap = min(MAX_VECTOR_TEXT_LAYOUT_PAD, max(24.0, height * 0.35))
    confident_horizontal_gap = min(MAX_VECTOR_TEXT_CONFIDENT_GAP, max(10.0, width * 0.16))
    confident_vertical_gap = min(MAX_VECTOR_TEXT_CONFIDENT_GAP, max(10.0, height * 0.16))
    broad_envelope = _clip_rect_to_page((drawing_rect[0] - broad_horizontal_gap, drawing_rect[1] - broad_vertical_gap, drawing_rect[2] + broad_horizontal_gap, drawing_rect[3] + broad_vertical_gap), page.rect)
    included: list[tuple[_PdfTextLine, str]] = []
    ambiguous_nearby = False
    for line in text_lines:
        if not _rects_intersect(line.rect, broad_envelope):
            continue
        relationship, gap = _vector_text_relationship(drawing_rect, line.rect)
        if relationship == 'inside':
            included.append((line, relationship))
            continue
        if relationship in {'top', 'bottom'}:
            aligned = _axis_overlap_ratio((drawing_rect[0], drawing_rect[2]), (line.rect[0], line.rect[2])) >= 0.35 or drawing_rect[0] <= _rect_center_x(line.rect) <= drawing_rect[2]
            if aligned and gap <= confident_vertical_gap:
                included.append((line, relationship))
            elif aligned or gap <= confident_vertical_gap:
                ambiguous_nearby = True
            continue
        if relationship in {'left', 'right'}:
            aligned = _axis_overlap_ratio((drawing_rect[1], drawing_rect[3]), (line.rect[1], line.rect[3])) >= 0.35 or drawing_rect[1] <= _rect_center_y(line.rect) <= drawing_rect[3]
            if aligned and gap <= confident_horizontal_gap:
                included.append((line, relationship))
            elif aligned or gap <= confident_horizontal_gap:
                ambiguous_nearby = True
            continue
        ambiguous_nearby = True
    ambiguity_reasons: list[str] = []
    if ambiguous_nearby:
        ambiguity_reasons.append('nearby_text_ownership_ambiguous')
    if _has_dense_external_text(included, drawing_rect):
        ambiguity_reasons.append('dense_external_text_ownership_ambiguous')
    expanded = drawing_rect
    for line, _relationship in included:
        expanded = _union_rect(expanded, line.rect)
    if included:
        expanded = (expanded[0] - VECTOR_TEXT_RENDER_PADDING, expanded[1] - VECTOR_TEXT_RENDER_PADDING, expanded[2] + VECTOR_TEXT_RENDER_PADDING, expanded[3] + VECTOR_TEXT_RENDER_PADDING)
    return _VectorLayoutRegion(rect=_clip_rect_to_page(expanded, page.rect), included_text_lines=len(included), ambiguity_reasons=tuple(ambiguity_reasons))

def _page_text_lines(page: Any) -> tuple[list[_PdfTextLine], bool]:
    try:
        payload = page.get_text('dict')
    except Exception:
        return ([], False)
    if not isinstance(payload, dict):
        return ([], False)
    lines: list[_PdfTextLine] = []
    reliable = True
    for block in payload.get('blocks', []) or []:
        if not isinstance(block, dict):
            continue
        for line in block.get('lines', []) or []:
            if not isinstance(line, dict):
                continue
            text = ''.join((str(span.get('text') or '') for span in line.get('spans', []) or [] if isinstance(span, dict))).strip()
            if not text:
                continue
            rect = _rect_tuple(line.get('bbox', ()))
            if not _valid_text_rect(rect):
                reliable = False
                continue
            lines.append(_PdfTextLine(rect=rect, text=text))
    return (lines, reliable)

def _caption_anchored_scan_regions(page: Any, *, embedded_rects: Iterable[tuple[float, float, float, float]]=()) -> list[_CaptionAnchoredScanRegion]:
    """Find complete logical visuals using verified caption and component layout.

    The crop is derived from page geometry, not a document- or subject-specific
    caption. It combines nearby embedded image components with text inside the
    same caption-bounded region. If neither components nor non-prose visual rows
    establish a complete region, no crop is emitted.
    """
    lines, reliable = _page_text_lines(page)
    if not reliable or not lines:
        return []
    rows = _merge_text_lines_into_rows(lines)
    if not rows:
        return []
    page_rect = page.rect
    page_width = max(1.0, float(page_rect.width))
    page_height = max(1.0, float(page_rect.height))
    candidate_rects = [rect for rect in embedded_rects if _valid_text_rect(rect)]
    regions: list[_CaptionAnchoredScanRegion] = []
    for caption in rows:
        normalized_caption = ' '.join(caption.text.split())
        caption_left = (caption.rect[0] - float(page_rect.x0)) / page_width
        caption_width = (caption.rect[2] - caption.rect[0]) / page_width
        if not _FIGURE_CAPTION_RE.match(normalized_caption) or caption_left < 0.12 or caption_left > 0.58 or (caption_width > 0.76):
            continue
        nearby_components = [rect for rect in candidate_rects if rect[3] <= caption.rect[1] + 2.0 and 0.0 <= caption.rect[1] - rect[3] <= page_height * 0.16]
        component_top = min((rect[1] for rect in nearby_components)) if nearby_components else caption.rect[1]
        nearby_component_text_rows = [row for row in rows if nearby_components and row.rect[1] >= float(page_rect.y0) + page_height * 0.08 and (row.rect[3] <= component_top + 2.0) and (component_top - row.rect[3] <= page_height * 0.05) and (not _looks_like_body_text_row(row, page_rect))]
        component_visual_top = min([component_top, *(row.rect[1] for row in nearby_component_text_rows)])
        preceding_body_rows = [row for row in rows if row.rect[3] <= component_top - 3.0 and caption.rect[1] - row.rect[3] <= page_height * 0.48 and _looks_like_body_text_row(row, page_rect)]
        if not preceding_body_rows and (not nearby_components):
            continue
        if preceding_body_rows:
            boundary = max(preceding_body_rows, key=lambda row: row.rect[3])
            continuation_rows = [row for row in rows if boundary.rect[3] <= row.rect[1] and row.rect[3] <= component_top - 3.0 and (row.rect[1] - boundary.rect[3] <= max(8.0, (boundary.rect[3] - boundary.rect[1]) * 0.8)) and _looks_like_body_text_continuation(row, page_rect)]
            if continuation_rows:
                boundary = max(continuation_rows, key=lambda row: row.rect[3])
            line_height = max(1.0, boundary.rect[3] - boundary.rect[1])
            top = boundary.rect[3] + max(3.0, line_height * 0.35)
        else:
            top = max(float(page_rect.y0), component_visual_top - 4.0)
        bottom = caption.rect[1] - SCAN_REGION_CAPTION_GAP
        height_ratio = (bottom - top) / page_height
        minimum_height_ratio = 0.035 if nearby_components else 0.065
        if height_ratio < minimum_height_ratio or height_ratio > 0.56:
            continue
        visual_rows = [row for row in rows if row.rect[1] >= top - 2.0 and row.rect[3] <= bottom + 2.0]
        non_prose_rows = [row for row in visual_rows if row.fragment_count >= 3 or (row.rect[2] - row.rect[0]) / page_width < 0.48]
        if not nearby_components and (not visual_rows or not non_prose_rows):
            continue
        side_margin = page_width * SCAN_REGION_SIDE_MARGIN_RATIO
        rect = _clip_rect_to_page((float(page_rect.x0) + side_margin, top, float(page_rect.x1) - side_margin, bottom), page_rect)
        if not _useful_region(rect, page_rect):
            continue
        if any((_substantial_overlap(rect, existing.rect, threshold=0.8) for existing in regions)):
            continue
        regions.append(_CaptionAnchoredScanRegion(rect=rect, caption=normalized_caption, caption_rect=caption.rect, text_row_count=len(visual_rows), embedded_component_count=len(nearby_components)))
    return regions

def _merge_text_lines_into_rows(lines: list[_PdfTextLine]) -> list[_PdfTextRow]:
    grouped: list[list[_PdfTextLine]] = []
    for line in sorted(lines, key=lambda item: (_rect_center_y(item.rect), item.rect[0])):
        center = _rect_center_y(line.rect)
        matching = next((group for group in reversed(grouped[-4:]) if abs(center - _rect_center_y(_union_line_rects(group))) <= 3.5), None)
        if matching is None:
            grouped.append([line])
        else:
            matching.append(line)
    rows: list[_PdfTextRow] = []
    for group in grouped:
        ordered = sorted(group, key=lambda item: item.rect[0])
        rect = _union_line_rects(ordered)
        text = ' '.join((item.text.strip() for item in ordered if item.text.strip()))
        if text:
            rows.append(_PdfTextRow(rect=rect, text=text, fragment_count=len(ordered)))
    return rows

def _union_line_rects(lines: list[_PdfTextLine]) -> tuple[float, float, float, float]:
    return (min((line.rect[0] for line in lines)), min((line.rect[1] for line in lines)), max((line.rect[2] for line in lines)), max((line.rect[3] for line in lines)))

def _looks_like_body_text_row(row: _PdfTextRow, page_rect: Any) -> bool:
    page_width = max(1.0, float(page_rect.width))
    width_ratio = (row.rect[2] - row.rect[0]) / page_width
    left_ratio = (row.rect[0] - float(page_rect.x0)) / page_width
    return row.fragment_count <= 4 and left_ratio <= 0.16 and (width_ratio >= 0.68)

def _looks_like_body_text_continuation(row: _PdfTextRow, page_rect: Any) -> bool:
    page_width = max(1.0, float(page_rect.width))
    left_ratio = (row.rect[0] - float(page_rect.x0)) / page_width
    compact_text = ''.join(row.text.split())
    return row.fragment_count <= 4 and left_ratio <= 0.16 and (len(compact_text) >= 6)

def _valid_text_rect(rect: tuple[float, float, float, float]) -> bool:
    return len(rect) >= 4 and all((math.isfinite(value) for value in rect[:4])) and (rect[2] > rect[0]) and (rect[3] > rect[1])

def _vector_text_relationship(drawing_rect: tuple[float, float, float, float], text_rect: tuple[float, float, float, float]) -> tuple[str, float]:
    if _rects_intersect(drawing_rect, text_rect):
        return ('inside', 0.0)
    if text_rect[3] <= drawing_rect[1]:
        return ('top', drawing_rect[1] - text_rect[3])
    if text_rect[1] >= drawing_rect[3]:
        return ('bottom', text_rect[1] - drawing_rect[3])
    if text_rect[2] <= drawing_rect[0]:
        return ('left', drawing_rect[0] - text_rect[2])
    if text_rect[0] >= drawing_rect[2]:
        return ('right', text_rect[0] - drawing_rect[2])
    return ('diagonal', math.inf)

def _has_dense_external_text(included: list[tuple[_PdfTextLine, str]], drawing_rect: tuple[float, float, float, float]) -> bool:
    drawing_width = max(1.0, drawing_rect[2] - drawing_rect[0])
    for side in ('top', 'bottom'):
        side_lines = [line for line, relationship in included if relationship == side]
        wide_lines = [line for line in side_lines if line.rect[2] - line.rect[0] >= drawing_width * 0.55]
        if len(side_lines) >= 3 and len(wide_lines) >= 2:
            return True
    return False

def _axis_overlap_ratio(first: tuple[float, float], second: tuple[float, float]) -> float:
    overlap = max(0.0, min(first[1], second[1]) - max(first[0], second[0]))
    shorter = max(1.0, min(first[1] - first[0], second[1] - second[0]))
    return overlap / shorter

def _rect_center_x(rect: tuple[float, float, float, float]) -> float:
    return (rect[0] + rect[2]) / 2.0

def _rect_center_y(rect: tuple[float, float, float, float]) -> float:
    return (rect[1] + rect[3]) / 2.0

def _rects_intersect(first: tuple[float, float, float, float], second: tuple[float, float, float, float]) -> bool:
    return min(first[2], second[2]) > max(first[0], second[0]) and min(first[3], second[3]) > max(first[1], second[1])

def _union_rect(first: tuple[float, float, float, float], second: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return (min(first[0], second[0]), min(first[1], second[1]), max(first[2], second[2]), max(first[3], second[3]))

def _clip_rect_to_page(rect: tuple[float, float, float, float], page_rect: Any) -> tuple[float, float, float, float]:
    return (max(float(page_rect.x0), rect[0]), max(float(page_rect.y0), rect[1]), min(float(page_rect.x1), rect[2]), min(float(page_rect.y1), rect[3]))

def _render_region(page: Any, rect: tuple[float, float, float, float], *, budget: _PdfRenderBudget) -> tuple[bytes, int | None, int | None]:
    try:
        from openclass_core.services import pdf_backend as fitz
        if len(rect) < 4 or not all((math.isfinite(value) for value in rect[:4])):
            budget.exhausted = True
            return (b'', None, None)
        clip = fitz.Rect(*rect) & page.rect
        width = int(math.ceil(max(0.0, float(clip.width)) * 2.0)) + 2
        height = int(math.ceil(max(0.0, float(clip.height)) * 2.0)) + 2
        if not budget.reserve(width, height):
            return (b'', None, None)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip, alpha=False)
        content = pixmap.tobytes('png')
        if not budget.account_rendered_bytes(len(content)):
            return (b'', None, None)
        return (content, int(pixmap.width), int(pixmap.height))
    except Exception:
        return (b'', None, None)

def _text_in_rect(page: Any, rect: tuple[float, float, float, float]) -> str:
    try:
        from openclass_core.services import pdf_backend as fitz
        return ' '.join(str(page.get_textbox(fitz.Rect(*rect)) or '').split())[:4000]
    except Exception:
        return ''

def _caption_below(page: Any, rect: tuple[float, float, float, float]) -> str:
    try:
        from openclass_core.services import pdf_backend as fitz
        page_rect = page.rect
        height = max(18.0, min(72.0, float(page_rect.height) * 0.08))
        caption_rect = fitz.Rect(rect[0], rect[3], rect[2], min(float(page_rect.y1), rect[3] + height))
        return ' '.join(str(page.get_textbox(caption_rect) or '').split())[:500]
    except Exception:
        return ''

def _boundary_gap_metadata(page: Any, rect: tuple[float, float, float, float]) -> dict[str, Any]:
    page_rect = page.rect
    page_height = max(1.0, float(page_rect.height))
    top_gap = max(0.0, (rect[1] - float(page_rect.y0)) / page_height)
    bottom_gap = max(0.0, (float(page_rect.y1) - rect[3]) / page_height)
    return {'boundary_top_gap': round(top_gap, 6), 'boundary_bottom_gap': round(bottom_gap, 6), 'boundary_top_clear': _boundary_gap_is_clear(page, rect, edge='top'), 'boundary_bottom_clear': _boundary_gap_is_clear(page, rect, edge='bottom')}

def _boundary_gap_is_clear(page: Any, rect: tuple[float, float, float, float], *, edge: str) -> bool:
    try:
        from openclass_core.services import pdf_backend as fitz
        page_rect = page.rect
        if edge == 'top':
            start, end = (float(page_rect.y0), rect[1])
        else:
            start, end = (rect[3], float(page_rect.y1))
        if end - start <= 0.5:
            return True
        gap = fitz.Rect(float(page_rect.x0), start, float(page_rect.x1), end)
        return not ' '.join(str(page.get_textbox(gap) or '').split())
    except Exception:
        return False

def _native_image_identity(document: Any, xref: int, *, cache: dict[int, str]) -> str:
    if xref in cache:
        return cache[xref]
    identity = ''
    try:
        extracted = document.extract_image(xref)
        content = extracted.get('image') if isinstance(extracted, dict) else None
        if isinstance(content, bytes) and content:
            content_hash = hashlib.sha256(content).hexdigest()
            identity = f'pdf-xobject:{xref}:{content_hash}'
    except Exception:
        identity = ''
    cache[xref] = identity
    return identity

def _merge_verified_cross_page_visuals(visuals: list[RawSourceVisual]) -> list[RawSourceVisual]:
    """Merge only unambiguous, pixel-continuous regions on consecutive PDF pages."""
    by_page: dict[int, list[RawSourceVisual]] = {}
    for visual in visuals:
        if visual.page_no is not None:
            by_page.setdefault(visual.page_no, []).append(visual)
    edges: dict[int, tuple[RawSourceVisual, float]] = {}
    incoming: set[int] = set()
    for page_no in sorted(by_page):
        next_page_no = page_no + 1
        if next_page_no not in by_page:
            continue
        bottoms = [visual for visual in by_page[page_no] if _touches_page_edge(visual, edge='bottom')]
        tops = [visual for visual in by_page[next_page_no] if _touches_page_edge(visual, edge='top')]
        if len(bottoms) != 1 or len(tops) != 1:
            continue
        first, second = (bottoms[0], tops[0])
        score = _cross_page_match_score(first, second)
        if score is None:
            continue
        edges[id(first)] = (second, score)
        incoming.add(id(second))
    replacements: dict[int, RawSourceVisual] = {}
    consumed: set[int] = set()
    for visual in visuals:
        visual_identity = id(visual)
        if visual_identity in incoming or visual_identity not in edges:
            continue
        segments = [visual]
        seam_scores: list[float] = []
        current = visual
        while id(current) in edges:
            following, score = edges[id(current)]
            if id(following) in {id(segment) for segment in segments}:
                break
            segments.append(following)
            seam_scores.append(score)
            current = following
        merged = _stitch_cross_page_segments(segments, seam_scores)
        if merged is None:
            continue
        replacements[visual_identity] = merged
        consumed.update((id(segment) for segment in segments[1:]))
    if not replacements:
        return visuals
    return [replacements.get(id(visual), visual) for visual in visuals if id(visual) not in consumed]

def _touches_page_edge(visual: RawSourceVisual, *, edge: str) -> bool:
    if len(visual.bbox) < 4:
        return False
    left, top, right, bottom = (float(value) for value in visual.bbox[:4])
    width = right - left
    height = bottom - top
    if width < 0.2 or height < 0.08 or height > 0.78 or (width * height > 0.72):
        return False
    if edge == 'top':
        return top <= 0.025 and visual.metadata.get('boundary_top_clear') is True
    return bottom >= 0.975 and visual.metadata.get('boundary_bottom_clear') is True

def _cross_page_match_score(first: RawSourceVisual, second: RawSourceVisual) -> float | None:
    if first.kind not in {'image', 'diagram'} or first.kind != second.kind or (not first.content) or (not second.content) or (first.mime_type != 'image/png') or (second.mime_type != 'image/png') or (first.page_no is None) or (second.page_no != first.page_no + 1) or first.caption.strip() or (first.metadata.get('pdf_region_type') != second.metadata.get('pdf_region_type')):
        return None
    if _shared_cross_page_identity(first, second) is None:
        return None
    first_bbox = [float(value) for value in first.bbox[:4]]
    second_bbox = [float(value) for value in second.bbox[:4]]
    if len(first_bbox) < 4 or len(second_bbox) < 4:
        return None
    first_width = first_bbox[2] - first_bbox[0]
    second_width = second_bbox[2] - second_bbox[0]
    if abs(first_bbox[0] - second_bbox[0]) > 0.025 or abs(first_bbox[2] - second_bbox[2]) > 0.025 or abs(first_width - second_width) > 0.025:
        return None
    first_gap = float(first.metadata.get('boundary_bottom_gap') or 0.0)
    second_gap = float(second.metadata.get('boundary_top_gap') or 0.0)
    if first_gap > 0.025 or second_gap > 0.025 or abs(first_gap - second_gap) > 0.015:
        return None
    return _pixel_seam_continuity_score(first.content, second.content)

def _shared_cross_page_identity(first: RawSourceVisual, second: RawSourceVisual) -> tuple[str, str] | None:
    first_native = str(first.metadata.get('native_object_identity') or '').strip()
    second_native = str(second.metadata.get('native_object_identity') or '').strip()
    if first_native and first_native == second_native:
        return ('native_object_identity', first_native)
    first_marker = str(first.metadata.get('explicit_continuation_id') or '').strip()
    second_marker = str(second.metadata.get('explicit_continuation_id') or '').strip()
    if first_marker and first_marker == second_marker:
        return ('explicit_continuation_id', first_marker)
    return None

def _pixel_seam_continuity_score(first_content: bytes, second_content: bytes) -> float | None:
    try:
        from PIL import Image, ImageChops, ImageStat
        with Image.open(io.BytesIO(first_content)) as first_source:
            first = first_source.convert('RGB')
        with Image.open(io.BytesIO(second_content)) as second_source:
            second = second_source.convert('RGB')
        if min(first.width, first.height, second.width, second.height) < 8:
            return None
        if _images_are_near_duplicates(first, second):
            return None
        sample_width = min(256, max(64, min(first.width, second.width)))
        strip_height = min(5, first.height, second.height)
        first_edge = first.crop((0, first.height - strip_height, first.width, first.height)).resize((sample_width, 1), Image.Resampling.LANCZOS)
        second_edge = second.crop((0, 0, second.width, strip_height)).resize((sample_width, 1), Image.Resampling.LANCZOS)
        mean_difference = sum(ImageStat.Stat(ImageChops.difference(first_edge, second_edge)).mean) / 3.0
        first_profile = [255.0 - float(value) for value in first_edge.convert('L').getdata()]
        second_profile = [255.0 - float(value) for value in second_edge.convert('L').getdata()]
        combined_range = max(first_profile + second_profile) - min(first_profile + second_profile)
        correlation = _profile_correlation(first_profile, second_profile)
        overlap = _active_profile_overlap(first_profile, second_profile)
        if combined_range < 24.0 or mean_difference > 20.0:
            return None
        if correlation < 0.65 and overlap < 0.55:
            return None
        similarity = max(0.0, 1.0 - mean_difference / 40.0)
        structural = max((correlation + 1.0) / 2.0, overlap)
        return round(min(0.98, 0.6 * similarity + 0.4 * structural), 6)
    except Exception:
        return None

def _images_are_near_duplicates(first: Any, second: Any) -> bool:
    try:
        from PIL import Image, ImageChops, ImageStat
        if abs(first.width / first.height - second.width / second.height) > 0.02:
            return False
        size = (64, 64)
        first_sample = first.resize(size, Image.Resampling.LANCZOS)
        second_sample = second.resize(size, Image.Resampling.LANCZOS)
        difference = ImageStat.Stat(ImageChops.difference(first_sample, second_sample)).mean
        return sum(difference) / 3.0 <= 2.5
    except Exception:
        return True

def _profile_correlation(first: list[float], second: list[float]) -> float:
    if len(first) != len(second) or not first:
        return -1.0
    first_mean = sum(first) / len(first)
    second_mean = sum(second) / len(second)
    numerator = sum(((first_value - first_mean) * (second_value - second_mean) for first_value, second_value in zip(first, second, strict=True)))
    first_energy = sum(((value - first_mean) ** 2 for value in first))
    second_energy = sum(((value - second_mean) ** 2 for value in second))
    denominator = (first_energy * second_energy) ** 0.5
    return numerator / denominator if denominator > 1e-09 else -1.0

def _active_profile_overlap(first: list[float], second: list[float]) -> float:
    first_threshold = min(first) + max(12.0, (max(first) - min(first)) * 0.35)
    second_threshold = min(second) + max(12.0, (max(second) - min(second)) * 0.35)
    first_active = {index for index, value in enumerate(first) if value >= first_threshold}
    second_active = {index for index, value in enumerate(second) if value >= second_threshold}
    if not first_active or not second_active:
        return 0.0
    expanded_second = {nearby for index in second_active for nearby in range(max(0, index - 2), min(len(second), index + 3))}
    return len(first_active & expanded_second) / max(1, len(first_active | second_active))

def _stitch_cross_page_segments(segments: list[RawSourceVisual], seam_scores: list[float]) -> RawSourceVisual | None:
    if len(segments) < 2 or len(seam_scores) != len(segments) - 1:
        return None
    try:
        from PIL import Image
        images = []
        for segment in segments:
            with Image.open(io.BytesIO(segment.content)) as source:
                images.append(source.convert('RGB'))
        width = max((image.width for image in images))
        height = sum((image.height for image in images))
        if width <= 0 or height <= 0 or width * height > 40000000:
            return None
        stitched = Image.new('RGB', (width, height), 'white')
        cursor = 0
        for image in images:
            stitched.paste(image, ((width - image.width) // 2, cursor))
            cursor += image.height
        output = io.BytesIO()
        stitched.save(output, format='PNG', compress_level=9)
    except Exception:
        return None
    page_spans = [{'page_no': segment.page_no, 'bbox': [round(float(value), 6) for value in segment.bbox[:4]], 'source_locator': segment.source_locator} for segment in segments]
    identity_payload = json.dumps(page_spans, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    locator_digest = hashlib.sha256(identity_payload.encode('utf-8')).hexdigest()[:20]
    captions = [segment.caption.strip() for segment in segments if segment.caption.strip()]
    ocr_parts: list[str] = []
    for segment in segments:
        text = segment.ocr_text.strip()
        if text and text not in ocr_parts:
            ocr_parts.append(text)
    first, last = (segments[0], segments[-1])
    continuation_evidence = [{'kind': evidence[0], 'value': evidence[1]} for evidence in (_shared_cross_page_identity(first_segment, second_segment) for first_segment, second_segment in zip(segments, segments[1:])) if evidence is not None]
    return RawSourceVisual(kind=first.kind, source_locator=f'pdf:cross-page:{first.page_no}-{last.page_no}:{locator_digest}', native_order=first.native_order, content=output.getvalue(), mime_type='image/png', page_no=first.page_no, bbox=list(first.bbox[:4]), text_offset=first.text_offset, caption=captions[-1] if captions else '', ocr_text='\n'.join(ocr_parts), width=width, height=height, confidence=min(0.92, *(segment.confidence for segment in segments), *seam_scores), metadata={'pdf_region_type': 'cross_page_visual', 'cross_page': True, 'page_start': first.page_no, 'page_end': last.page_no, 'page_spans': page_spans, 'segment_count': len(segments), 'segment_source_locators': [segment.source_locator for segment in segments], 'seam_continuity_scores': seam_scores, 'continuation_evidence': continuation_evidence, 'stable_position_key': locator_digest})

def _rect_tuple(rect: Any) -> tuple[float, float, float, float]:
    if hasattr(rect, 'x0'):
        return (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1))
    try:
        values = tuple((float(value) for value in rect))
    except Exception:
        return (0.0, 0.0, 0.0, 0.0)
    return values[:4] if len(values) >= 4 else (0.0, 0.0, 0.0, 0.0)

def _normalize_bbox(rect: tuple[float, float, float, float], page_rect: Any) -> list[float]:
    width = max(1.0, float(page_rect.width))
    height = max(1.0, float(page_rect.height))
    return [round(max(0.0, min(1.0, rect[0] / width)), 6), round(max(0.0, min(1.0, rect[1] / height)), 6), round(max(0.0, min(1.0, rect[2] / width)), 6), round(max(0.0, min(1.0, rect[3] / height)), 6)]

def _useful_region(rect: tuple[float, float, float, float], page_rect: Any) -> bool:
    width = max(0.0, rect[2] - rect[0])
    height = max(0.0, rect[3] - rect[1])
    page_width = max(1.0, float(page_rect.width))
    page_height = max(1.0, float(page_rect.height))
    area_ratio = width * height / (page_width * page_height)
    if width < 36 or height < 24 or area_ratio < 0.004:
        return False
    normalized_top = rect[1] / page_height
    normalized_bottom = rect[3] / page_height
    normalized_height = height / page_height
    if normalized_height < 0.07 and (normalized_top < 0.06 or normalized_bottom > 0.94):
        return False
    return area_ratio < 0.96

def _is_full_page_background_region(rect: tuple[float, float, float, float], page_rect: Any) -> bool:
    page_width = max(1.0, float(page_rect.width))
    page_height = max(1.0, float(page_rect.height))
    left = (rect[0] - float(page_rect.x0)) / page_width
    top = (rect[1] - float(page_rect.y0)) / page_height
    right = (float(page_rect.x1) - rect[2]) / page_width
    bottom = (float(page_rect.y1) - rect[3]) / page_height
    width = max(0.0, rect[2] - rect[0])
    height = max(0.0, rect[3] - rect[1])
    area_ratio = width * height / (page_width * page_height)
    return area_ratio >= 0.8 and left <= 0.06 and (top <= 0.06) and (right <= 0.06) and (bottom <= 0.06)

def _overlap_ratio(first: tuple[float, float, float, float], second: tuple[float, float, float, float]) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    first_area = max(1.0, (first[2] - first[0]) * (first[3] - first[1]))
    return intersection / first_area

def _substantial_overlap(first: tuple[float, float, float, float], second: tuple[float, float, float, float], *, threshold: float) -> bool:
    return max(_overlap_ratio(first, second), _overlap_ratio(second, first)) > threshold

def _embedded_image_is_self_contained(rect: tuple[float, float, float, float], page_rect: Any, page_image_rects: Iterable[tuple[float, float, float, float]]) -> bool:
    page_width = max(1.0, float(page_rect.width))
    page_height = max(1.0, float(page_rect.height))
    width_ratio = max(0.0, rect[2] - rect[0]) / page_width
    height_ratio = max(0.0, rect[3] - rect[1]) / page_height
    if width_ratio < 0.16 or height_ratio < 0.07 or width_ratio * height_ratio < 0.015:
        return False
    for other in page_image_rects:
        if other == rect:
            continue
        horizontal_overlap = _axis_overlap_ratio((rect[0], rect[2]), (other[0], other[2]))
        vertical_overlap = _axis_overlap_ratio((rect[1], rect[3]), (other[1], other[3]))
        horizontal_gap = max(0.0, max(rect[0], other[0]) - min(rect[2], other[2]))
        vertical_gap = max(0.0, max(rect[1], other[1]) - min(rect[3], other[3]))
        same_visual_cluster = vertical_overlap >= 0.25 and horizontal_gap <= page_width * 0.08 or (horizontal_overlap >= 0.25 and vertical_gap <= page_height * 0.06)
        if same_visual_cluster:
            return False
    return True
