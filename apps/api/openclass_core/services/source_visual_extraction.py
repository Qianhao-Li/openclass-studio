from __future__ import annotations
import hashlib
import io
import json
import re
import tempfile
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence
from openclass_core.models import SourceChapter, SourceChunk, SourceIngestionRecord, SourceStructure, SourceVisualAsset, SourceVisualIndexStatus
from openclass_core.services.ai_logging import ai_usage_logger
from openclass_core.services.image_ocr import extract_image_text
from openclass_core.services.source_visual_extraction_budget import SourceVisualExtractionBudget, SourceVisualExtractionBudgetError
from openclass_core.services.source_visual_extraction_markup import extract_markup_visuals, extract_standalone_image, render_svg_to_png
from openclass_core.services.source_visual_extraction_office import extract_office_visuals
from openclass_core.services.source_visual_extraction_pdf import extract_pdf_visuals
from openclass_core.services.source_visual_extraction_types import RawSourceVisual, SourceVisualAdapterResult
from openclass_core.services.source_visual_libreoffice import LibreOfficeRenderError, LibreOfficeRenderer, libreoffice_renderer
from openclass_core.services.source_visual_storage import SourceVisualStorageError, persist_source_visual_asset, resolve_source_visual_storage_key
CURRENT_SOURCE_VISUAL_INDEX_VERSION = 5
MAX_SOURCE_VISUAL_PIXELS = 40000000

@dataclass
class SourceVisualExtractionResult:
    visuals: list[SourceVisualAsset] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status: SourceVisualIndexStatus = 'ready'

class SourceVisualExtractionFatalError(RuntimeError):
    pass

class SourceVisualExtractor:

    def __init__(self, *, office_renderer: LibreOfficeRenderer=libreoffice_renderer) -> None:
        self.office_renderer = office_renderer

    def extract(self, *, record: SourceIngestionRecord, path: Path | None, structure: SourceStructure, chapters: list[SourceChapter], chunks: list[SourceChunk], progress_callback: Callable[[int, int], None] | None=None) -> SourceVisualExtractionResult:
        if _is_audio_or_video(record):
            return SourceVisualExtractionResult(status='unsupported')
        if path is None or not path.is_file():
            return SourceVisualExtractionResult(status='partial', warnings=['Source visual indexing skipped because the local source file is unavailable.'])
        try:
            adapter_result = self._adapter_result(path=path, record=record, progress_callback=progress_callback)
        except Exception as exc:
            ai_usage_logger.log_event('source_visual_extraction_failed', owner_user_id=record.owner_user_id, package_id=record.package_id, source_ingestion_id=record.id, error=str(exc))
            raise SourceVisualExtractionFatalError(str(exc)) from exc
        if adapter_result.status == 'failed':
            failure_warnings = list(dict.fromkeys(adapter_result.warnings))
            ai_usage_logger.log_event('source_visual_extraction_failed', owner_user_id=record.owner_user_id, package_id=record.package_id, source_ingestion_id=record.id, warnings=failure_warnings)
            return SourceVisualExtractionResult(visuals=[], warnings=failure_warnings, status='failed')
        if adapter_result.native_chart_count:
            rendered = self._render_native_office_visuals(source_path=path, anchors=adapter_result.native_chart_anchors)
            adapter_result.visuals.extend(rendered.visuals)
            adapter_result.warnings.extend(rendered.warnings)
            if rendered.status == 'partial':
                adapter_result.status = 'partial'
        try:
            visuals, materialization_warnings = self._materialize(record=record, structure=structure, raw_visuals=adapter_result.visuals, chapters=chapters, chunks=chunks)
        except SourceVisualExtractionBudgetError as exc:
            warning = str(exc)
            ai_usage_logger.log_event('source_visual_extraction_failed', owner_user_id=record.owner_user_id, package_id=record.package_id, source_ingestion_id=record.id, warnings=[warning])
            return SourceVisualExtractionResult(visuals=[], warnings=[warning], status='failed')
        warnings = list(dict.fromkeys([*adapter_result.warnings, *materialization_warnings]))
        status: SourceVisualIndexStatus = adapter_result.status
        if materialization_warnings and status == 'ready':
            status = 'partial'
        unverified_count = sum((visual.anchor_status == 'unverified' for visual in visuals))
        if warnings:
            ai_usage_logger.log_event('source_visual_extraction_partial', owner_user_id=record.owner_user_id, package_id=record.package_id, source_ingestion_id=record.id, warning_count=len(warnings), warnings=warnings)
        if unverified_count:
            ai_usage_logger.log_event('source_visual_anchor_unverified', owner_user_id=record.owner_user_id, package_id=record.package_id, source_ingestion_id=record.id, visual_count=unverified_count, visual_ids=[visual.id for visual in visuals if visual.anchor_status == 'unverified'])
        return SourceVisualExtractionResult(visuals=visuals, warnings=warnings, status=status)

    def extract_verified_scope(self, *, record: SourceIngestionRecord, path: Path, structure: SourceStructure, chapter: SourceChapter, source_range: dict[str, Any], scope_cache_key: str, progress_callback: Callable[[int, int], None] | None=None) -> SourceVisualExtractionResult:
        """Extract only visuals inside one authenticated, inclusive source range.

        The caller has already authenticated the catalog, source identity and
        file hash.  This method keeps those exact native coordinates through
        extraction and marks only the strictly filtered results as verified.
        It never creates global text chunks or scans unrelated PDF pages.
        """
        kind = str(source_range.get('kind') or '')
        if kind in {'structured_path'} or _is_audio_or_video(record):
            return SourceVisualExtractionResult(status='ready')
        try:
            adapter_result = self._adapter_result(path=path, record=record, progress_callback=progress_callback, source_range=source_range)
        except Exception as exc:
            raise SourceVisualExtractionFatalError(str(exc)) from exc
        if adapter_result.status == 'failed':
            return SourceVisualExtractionResult(status='failed', warnings=list(dict.fromkeys(adapter_result.warnings)))
        adapter_result.visuals = _raw_visuals_for_verified_scope(adapter_result.visuals, path=path, source_range=source_range)
        adapter_result.native_chart_anchors = _raw_visuals_for_verified_scope(adapter_result.native_chart_anchors, path=path, source_range=source_range)
        adapter_result.native_chart_count = len(adapter_result.native_chart_anchors)
        if adapter_result.native_chart_count:
            rendered = self._render_native_office_visuals(source_path=path, anchors=adapter_result.native_chart_anchors)
            adapter_result.visuals.extend(rendered.visuals)
            adapter_result.warnings.extend(rendered.warnings)
            if rendered.status == 'partial':
                adapter_result.status = 'partial'
        try:
            visuals, materialization_warnings = self._materialize(record=record, structure=structure, raw_visuals=adapter_result.visuals, chapters=[], chunks=[])
        except SourceVisualExtractionBudgetError as exc:
            return SourceVisualExtractionResult(status='failed', warnings=[str(exc)])
        scoped_visuals = [visual.model_copy(update={'chapter_id': chapter.id, 'anchor_status': 'verified', 'metadata': {**visual.metadata, 'verified_scope': True, 'scope_cache_key': scope_cache_key, 'scope_range': source_range}}) for visual in visuals]
        warnings = list(dict.fromkeys([*adapter_result.warnings, *materialization_warnings]))
        status: SourceVisualIndexStatus = adapter_result.status
        if materialization_warnings and status == 'ready':
            status = 'partial'
        return SourceVisualExtractionResult(visuals=scoped_visuals, warnings=warnings, status=status)

    def _adapter_result(self, *, path: Path, record: SourceIngestionRecord, progress_callback: Callable[[int, int], None] | None=None, source_range: dict[str, Any] | None=None) -> SourceVisualAdapterResult:
        suffix = path.suffix.lower()
        if suffix == '.pdf' or record.mime_type == 'application/pdf':
            return extract_pdf_visuals(path, progress_callback=progress_callback, page_start=int(source_range['start']) if source_range and source_range.get('kind') == 'pdf_pages' else None, page_end=int(source_range['end']) if source_range and source_range.get('kind') == 'pdf_pages' else None)
        office_format = _office_format(suffix=suffix, mime_type=record.mime_type)
        if office_format:
            return extract_office_visuals(path, office_format=office_format)
        if suffix == '.epub' or suffix in {'.html', '.htm', '.md', '.markdown', '.csv'}:
            return extract_markup_visuals(path, record)
        if record.mime_type in {'application/epub+zip', 'text/html', 'text/markdown', 'text/csv'}:
            return extract_markup_visuals(path, record)
        if record.mime_type.startswith('image/') or suffix in {'.png', '.jpg', '.jpeg', '.webp', '.gif'}:
            return extract_standalone_image(path, record)
        return SourceVisualAdapterResult(status='ready')

    def _render_native_office_visuals(self, *, source_path: Path, anchors: list[RawSourceVisual]) -> SourceVisualAdapterResult:
        expected_count = len(anchors)
        if not self.office_renderer.available:
            return SourceVisualAdapterResult(status='partial', warnings=[f'{expected_count} native Office chart or diagram object(s) require OPENCLASS_LIBREOFFICE_PATH for faithful rendering.'])
        try:
            with tempfile.TemporaryDirectory(prefix='openclass-source-visual-') as temporary_directory:
                pdf_path = self.office_renderer.render_pdf(source_path, output_dir=Path(temporary_directory))
                rendered = extract_pdf_visuals(pdf_path)
        except LibreOfficeRenderError as exc:
            return SourceVisualAdapterResult(status='partial', warnings=[str(exc)])
        chart_visuals = [visual for visual in rendered.visuals if visual.kind in {'image', 'diagram'}]
        available = list(chart_visuals)
        selected: list[tuple[RawSourceVisual, RawSourceVisual, bool]] = []
        for anchor in anchors:
            if anchor.page_no is None or len(anchor.bbox) < 4:
                continue
            page_matches = [index for index, candidate in enumerate(available) if candidate.page_no == anchor.page_no and len(candidate.bbox) >= 4]
            scored_matches = sorted(((_bbox_overlap_score(anchor.bbox, available[index].bbox), index) for index in page_matches), reverse=True)
            matching_index = scored_matches[0][1] if scored_matches else None
            if matching_index is None:
                continue
            best_score = scored_matches[0][0]
            next_score = scored_matches[1][0] if len(scored_matches) > 1 else 0.0
            competing_anchor_score = max((_bbox_overlap_score(candidate_anchor.bbox, available[matching_index].bbox) for candidate_anchor in anchors if candidate_anchor is not anchor and candidate_anchor.page_no == anchor.page_no), default=0.0)
            verified_mapping = anchor.page_no is not None and best_score >= 0.55 and (next_score <= 0.2 or best_score - next_score >= 0.35) and (competing_anchor_score <= 0.2 or best_score - competing_anchor_score >= 0.35)
            if not verified_mapping:
                continue
            selected.append((anchor, available.pop(matching_index), True))
        for anchor, visual, verified_mapping in selected:
            rendered_pdf_page = visual.page_no
            visual.kind = anchor.kind
            visual.source_locator = anchor.source_locator
            visual.native_order = anchor.native_order
            visual.page_no = anchor.page_no
            visual.slide_no = anchor.slide_no
            visual.sheet_name = anchor.sheet_name
            visual.bbox = anchor.bbox
            visual.text_offset = anchor.text_offset
            visual.caption = anchor.caption or visual.caption
            visual.confidence = min(0.82, visual.confidence)
            visual.metadata = {**visual.metadata, **anchor.metadata, 'office_renderer': 'libreoffice', 'rendered_pdf_page': rendered_pdf_page, 'office_anchor_mapping_verified': verified_mapping, 'force_unverified': bool(anchor.metadata.get('force_unverified') or not verified_mapping)}
        warnings = list(rendered.warnings)
        if len(selected) < expected_count:
            warnings.append(f'LibreOffice rendered {len(selected)} of {expected_count} detected native chart or diagram object(s).')
        return SourceVisualAdapterResult(visuals=[visual for _anchor, visual, _verified in selected], warnings=warnings, status='partial' if warnings else 'ready')

    def _materialize(self, *, record: SourceIngestionRecord, structure: SourceStructure, raw_visuals: Sequence[RawSourceVisual], chapters: list[SourceChapter], chunks: list[SourceChunk]) -> tuple[list[SourceVisualAsset], list[str]]:
        visuals: list[SourceVisualAsset] = []
        usable_raw_visuals, warnings = _preflight_raw_visuals(raw_visuals)
        filtered_items: list[dict[str, str]] = []
        seen_positions: set[tuple[str, str]] = set()
        ordered_visuals = sorted(usable_raw_visuals, key=_raw_visual_sort_key)
        repetitions = _raw_visual_repetitions(ordered_visuals)
        for raw in ordered_visuals:
            filter_reason = _visual_content_shape_filter_reason(raw, repetitions=repetitions)
            if filter_reason:
                filtered_items.append({'source_locator': raw.source_locator, 'reason': filter_reason})
                continue
            try:
                storage_key, content_hash = _persist_raw_content(raw)
            except SourceVisualStorageError as exc:
                warnings.append(f'{raw.source_locator}: {exc}')
                continue
            position_hash = _position_hash(raw)
            duplicate_key = (content_hash, position_hash)
            if duplicate_key in seen_positions:
                continue
            seen_positions.add(duplicate_key)
            chapter, chapter_anchor_exact = _chapter_for_visual(raw, chapters)
            before_chunk_id, after_chunk_id, chunk_anchor_exact = _chunk_anchors(raw, chunks, chapter_id=chapter.id if chapter else None)
            anchor_verified = bool(not raw.metadata.get('force_unverified') and (raw.metadata.get('standalone_image') or chunk_anchor_exact or (chapter is not None and chapter_anchor_exact)))
            width, height = (raw.width, raw.height)
            if raw.content and (width is None or height is None):
                detected_width, detected_height = _image_dimensions(raw.content)
                width = width or detected_width
                height = height or detected_height
            ocr_text = raw.ocr_text.strip()
            if raw.content and (not ocr_text):
                ocr_text = _ocr_visual_content(raw.content, raw.mime_type)
            identity = '\x1f'.join((record.id, raw.source_locator, str(raw.native_order), content_hash, position_hash))
            visual_id = f"sourcevisual_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"
            visuals.append(SourceVisualAsset(id=visual_id, owner_user_id=record.owner_user_id, package_id=record.package_id, source_ingestion_id=record.id, structure_id=structure.id, structure_version=CURRENT_SOURCE_VISUAL_INDEX_VERSION, chapter_id=chapter.id if chapter else None, kind=raw.kind, order_index=len(visuals), source_locator=raw.source_locator, page_start=raw.page_no, page_end=_raw_visual_page_end(raw), paragraph_index=raw.paragraph_index, slide_no=raw.slide_no, sheet_name=raw.sheet_name, bbox=[round(float(value), 6) for value in raw.bbox[:4]], before_chunk_id=before_chunk_id, after_chunk_id=after_chunk_id, caption=raw.caption.strip()[:1000], extracted_text=ocr_text[:8000], surrounding_text=_surrounding_text_for_visual(before_chunk_id=before_chunk_id, after_chunk_id=after_chunk_id, chunks=chunks), anchor_status='verified' if anchor_verified else 'unverified', confidence=max(0.0, min(1.0, raw.confidence)), storage_key=storage_key, asset_path=str(resolve_source_visual_storage_key(storage_key)) if storage_key else '', mime_type=raw.mime_type if storage_key else '', content_hash=content_hash, position_hash=position_hash, width=width, height=height, table_data=raw.table_data, metadata={**raw.metadata, 'chapter_anchor_exact': chapter_anchor_exact, 'chunk_anchor_exact': chunk_anchor_exact}))
        if filtered_items:
            ai_usage_logger.log_event('source_visual_content_shape_filtered', owner_user_id=record.owner_user_id, package_id=record.package_id, source_ingestion_id=record.id, filtered_count=len(filtered_items), items=filtered_items[:100])
        return (visuals, warnings)

def _preflight_raw_visuals(raw_visuals: Sequence[RawSourceVisual]) -> tuple[list[RawSourceVisual], list[str]]:
    budget = SourceVisualExtractionBudget()
    usable: list[RawSourceVisual] = []
    warnings: list[str] = []
    for raw in raw_visuals:
        budget.reserve_visual_objects()
        if raw.table_data:
            budget.account_table(raw.table_data)
        if raw.content:
            try:
                _validate_and_normalize_image(raw)
            except SourceVisualStorageError as exc:
                warnings.append(f'{raw.source_locator}: {exc}')
                continue
            budget.account_image_bytes(len(raw.content))
            budget.account_image_pixels(int(raw.width or 0), int(raw.height or 0))
            if not raw.ocr_text.strip():
                budget.reserve_ocr_objects()
        usable.append(raw)
    return (usable, warnings)

def _raw_visuals_for_verified_scope(visuals: Sequence[RawSourceVisual], *, path: Path, source_range: dict[str, Any]) -> list[RawSourceVisual]:
    """Return only native visual anchors that overlap the inclusive range."""
    kind = str(source_range.get('kind') or '')
    start = source_range.get('start')
    end = source_range.get('end')
    container = str(source_range.get('container') or '').strip()
    if kind == 'sheet_rows' and path.suffix.lower() == '.csv':
        if not isinstance(start, int) or not isinstance(end, int):
            return []
        scoped: list[RawSourceVisual] = []
        for visual in visuals:
            if visual.kind != 'table' or not visual.table_data:
                continue
            rows = visual.table_data[max(0, start - 1):end]
            if not rows:
                continue
            visual.table_data = rows
            visual.source_locator = f'{visual.source_locator}:rows:{start}-{end}'
            visual.metadata = {**visual.metadata, 'row_start': start, 'row_end': end}
            scoped.append(visual)
        return scoped
    text_bounds = _text_offset_bounds(path, source_range)
    selected: list[RawSourceVisual] = []
    for visual in visuals:
        include = False
        if kind == 'pdf_pages' and isinstance(start, int) and isinstance(end, int):
            include = visual.page_no is not None and start <= visual.page_no <= end
        elif kind == 'ppt_slides' and isinstance(start, int) and isinstance(end, int):
            coordinate = visual.slide_no or visual.page_no
            include = coordinate is not None and start <= coordinate <= end
        elif kind == 'epub_spine':
            coordinate = visual.metadata.get('epub_spine_index')
            include = isinstance(coordinate, int) and isinstance(start, int) and isinstance(end, int) and (start <= coordinate <= end)
        elif kind == 'docx_paragraphs' and isinstance(start, int) and isinstance(end, int):
            visual_start = visual.metadata.get('paragraph_start_index', visual.paragraph_index)
            visual_end = visual.metadata.get('paragraph_end_index', visual_start)
            include = isinstance(visual_start, int) and isinstance(visual_end, int) and (visual_start <= end) and (visual_end >= start)
        elif kind == 'sheet_rows' and isinstance(start, int) and isinstance(end, int):
            same_sheet = not container or visual.sheet_name == container
            max_row = visual.metadata.get('max_row')
            if same_sheet and isinstance(max_row, int) and (len(visual.bbox) >= 4):
                visual_start = max(1, int(float(visual.bbox[1]) * max_row) + 1)
                visual_end = max(visual_start, int(float(visual.bbox[3]) * max_row + 0.999999))
                include = visual_start <= end and visual_end >= start
                if include:
                    visual.metadata = {**visual.metadata, 'row_start': visual_start, 'row_end': visual_end}
            elif same_sheet:
                include = True
        elif kind in {'text_lines', 'dom_anchor'} and text_bounds is not None:
            include = visual.text_offset is not None and text_bounds[0] <= visual.text_offset < text_bounds[1]
        elif kind == 'dom_anchor' and dict(source_range.get('metadata') or {}).get('root'):
            include = True
        if include:
            selected.append(visual)
    return selected

def _text_offset_bounds(path: Path, source_range: dict[str, Any]) -> tuple[int, int] | None:
    kind = str(source_range.get('kind') or '')
    if kind == 'text_lines':
        start = source_range.get('start')
        end = source_range.get('end')
        if not isinstance(start, int) or not isinstance(end, int):
            return None
        text = path.read_text(encoding='utf-8', errors='replace')
        lines = text.splitlines(keepends=True)
        if not lines or start < 1 or end > len(lines):
            return None
        return (sum(map(len, lines[:start - 1])), sum(map(len, lines[:end])))
    if kind != 'dom_anchor':
        return None
    html = path.read_text(encoding='utf-8', errors='replace')
    start_anchor = str(source_range.get('start_anchor') or '').strip()
    end_anchor = str(source_range.get('end_anchor') or '').strip()
    start_raw = _html_anchor_raw_offset(html, start_anchor) if start_anchor else 0
    end_raw = _html_anchor_raw_offset(html, end_anchor) if end_anchor else len(html)
    if start_raw is None or end_raw is None or end_raw <= start_raw:
        return None
    from openclass_core.services.source_visual_extraction_markup import _MarkupVisualParser
    prefix_parser = _MarkupVisualParser(html[:start_raw])
    prefix_parser.feed(html[:start_raw])
    end_parser = _MarkupVisualParser(html[:end_raw])
    end_parser.feed(html[:end_raw])
    return (prefix_parser.text_length, end_parser.text_length)

def _html_anchor_raw_offset(html: str, anchor: str) -> int | None:
    escaped = re.escape(anchor)
    match = re.search(f"""<[^>]+\\b(?:id|name)\\s*=\\s*(['\\"]){escaped}\\1[^>]*>""", html, flags=re.IGNORECASE)
    return match.start() if match else None

def _persist_raw_content(raw: RawSourceVisual) -> tuple[str, str]:
    if raw.content:
        _validate_and_normalize_image(raw)
        return persist_source_visual_asset(raw.content, mime_type=raw.mime_type)
    if raw.kind != 'table' or not raw.table_data:
        raise SourceVisualStorageError('Visual has neither an image asset nor structured table data.')
    canonical = json.dumps(raw.table_data, ensure_ascii=False, separators=(',', ':'), sort_keys=False)
    return ('', hashlib.sha256(canonical.encode('utf-8')).hexdigest())

def _validate_and_normalize_image(raw: RawSourceVisual) -> None:
    if raw.mime_type == 'image/svg+xml':
        rendered = render_svg_to_png(raw.content)
        if not rendered:
            raise SourceVisualStorageError('SVG visual could not be rendered safely.')
        raw.content = rendered
        raw.mime_type = 'image/png'
        raw.metadata = {**raw.metadata, 'original_mime_type': 'image/svg+xml'}
    try:
        from PIL import Image
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw.content)) as image:
                width, height = (int(image.width), int(image.height))
                image_format = str(image.format or '').upper()
                if width <= 0 or height <= 0 or width * height > MAX_SOURCE_VISUAL_PIXELS:
                    raise SourceVisualStorageError('Source visual decompressed dimensions are too large.')
                image.verify()
    except SourceVisualStorageError:
        raise
    except Exception as exc:
        raise SourceVisualStorageError('Source visual bytes are not a valid raster image.') from exc
    detected_mime = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'GIF': 'image/gif', 'WEBP': 'image/webp', 'TIFF': 'image/tiff', 'BMP': 'image/bmp'}.get(image_format)
    if detected_mime is None or raw.mime_type != detected_mime:
        raise SourceVisualStorageError('Source visual media type does not match its image bytes.')
    raw.width = raw.width or width
    raw.height = raw.height or height

def _position_hash(raw: RawSourceVisual) -> str:
    payload = {'source_locator': raw.source_locator, 'page_no': raw.page_no, 'page_end': _raw_visual_page_end(raw), 'paragraph_index': raw.paragraph_index, 'slide_no': raw.slide_no, 'sheet_name': raw.sheet_name, 'bbox': [round(float(value), 6) for value in raw.bbox[:4]], 'text_offset': raw.text_offset, 'native_order': raw.native_order}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()

def _chapter_for_visual(raw: RawSourceVisual, chapters: list[SourceChapter]) -> tuple[SourceChapter | None, bool]:
    verified = [chapter for chapter in chapters if chapter.anchor_status == 'verified']
    page_no = raw.slide_no or raw.page_no
    if page_no is not None:
        page_candidates = [chapter for chapter in verified if chapter.page_start is not None and _page_in_range(page_no, chapter.page_start, chapter.page_end)]
        if page_candidates:
            return (max(page_candidates, key=lambda chapter: (chapter.level, chapter.order_index)), True)
    sheet_match = _locator_number(raw.source_locator, 'xlsx:sheet:')
    if sheet_match is not None:
        sheet_candidates = [chapter for chapter in verified if int(chapter.metadata.get('sheet') or 0) == sheet_match]
        if len(sheet_candidates) == 1:
            return (sheet_candidates[0], True)
    if raw.text_offset is not None and raw.metadata.get('text_offset_anchor_safe', True):
        offset_candidates = [chapter for chapter in verified if chapter.body_start_offset is not None and chapter.body_end_offset is not None and (chapter.body_start_offset <= raw.text_offset < chapter.body_end_offset)]
        if offset_candidates:
            return (max(offset_candidates, key=lambda chapter: (chapter.level, chapter.order_index)), True)
    locator_candidates = [chapter for chapter in verified if chapter.source_locator and _locator_prefix_matches(raw.source_locator, chapter.source_locator)]
    if len(locator_candidates) == 1:
        return (locator_candidates[0], True)
    if locator_candidates:
        return (max(locator_candidates, key=lambda chapter: (chapter.level, chapter.order_index)), False)
    return (None, False)

def _chunk_anchors(raw: RawSourceVisual, chunks: list[SourceChunk], *, chapter_id: str | None) -> tuple[str | None, str | None, bool]:
    ordered = sorted(chunks, key=lambda chunk: chunk.order_index)
    if not ordered:
        return (None, None, False)
    if raw.text_offset is not None and raw.metadata.get('text_offset_anchor_safe', True):
        containing = next((chunk for chunk in ordered if chunk.start_offset <= raw.text_offset < chunk.end_offset), None)
        if containing is not None:
            index = ordered.index(containing)
            after = ordered[index + 1] if index + 1 < len(ordered) else None
            return (containing.id, after.id if after else None, True)
        before = next((chunk for chunk in reversed(ordered) if chunk.end_offset <= raw.text_offset), None)
        after = next((chunk for chunk in ordered if chunk.start_offset >= raw.text_offset), None)
        if before is not None or after is not None:
            return (before.id if before else None, after.id if after else None, True)
    page_no = raw.slide_no or raw.page_no
    if page_no is not None:
        page_chunks = [chunk for chunk in ordered if chunk.page_start is not None and _page_in_range(page_no, chunk.page_start, chunk.page_end)]
        if page_chunks:
            first = page_chunks[0]
            index = ordered.index(first)
            before = ordered[index - 1] if index > 0 else first
            after = page_chunks[-1]
            return (before.id, after.id, True)
        locator_pages = [(_locator_number(chunk.source_locator, 'page:'), index, chunk) for index, chunk in enumerate(ordered)]
        located = [item for item in locator_pages if item[0] is not None]
        for located_index, (start_page, index, chunk) in enumerate(located):
            next_page = located[located_index + 1][0] if located_index + 1 < len(located) else None
            if start_page is None or start_page > page_no:
                continue
            if next_page is not None and page_no >= next_page:
                continue
            after = ordered[index + 1] if index + 1 < len(ordered) else None
            return (chunk.id, after.id if after else None, True)
    if chapter_id:
        chapter_chunks = [chunk for chunk in ordered if chunk.chapter_id == chapter_id]
        if chapter_chunks:
            return (chapter_chunks[0].id, chapter_chunks[-1].id, True)
    return (None, None, False)

def _raw_visual_page_end(raw: RawSourceVisual) -> int | None:
    page_end = raw.metadata.get('page_end')
    if isinstance(page_end, int) and raw.page_no is not None and (page_end >= raw.page_no):
        return page_end
    return raw.page_no

def _locator_prefix_matches(first: str, second: str) -> bool:
    return first == second or first.startswith(f'{second}:') or second.startswith(f'{first}:')

def _surrounding_text_for_visual(*, before_chunk_id: str | None, after_chunk_id: str | None, chunks: list[SourceChunk]) -> str:
    by_id = {chunk.id: chunk for chunk in chunks}
    selected = [by_id[chunk_id].text.strip() for chunk_id in (before_chunk_id, after_chunk_id) if chunk_id and chunk_id in by_id and by_id[chunk_id].text.strip()]
    return '\n\n'.join(dict.fromkeys(selected))[:2000]

def _raw_visual_sort_key(raw: RawSourceVisual) -> tuple[int, float, float, float, int, str]:
    if raw.text_offset is not None:
        return (0, float(raw.text_offset), 0.0, 0.0, raw.native_order, raw.source_locator)
    page = raw.slide_no or raw.page_no or 0
    sheet = _locator_number(raw.source_locator, 'xlsx:sheet:') or 0
    top = raw.bbox[1] if len(raw.bbox) >= 4 else 0.0
    left = raw.bbox[0] if len(raw.bbox) >= 4 else 0.0
    return (1, float(sheet), float(page), top * 10000 + left, raw.native_order, raw.source_locator)

def _bbox_overlap_score(first: list[float], second: list[float]) -> float:
    if len(first) < 4 or len(second) < 4:
        return 0.0
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    second_area = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
    smaller_area = min(first_area, second_area)
    return intersection / smaller_area if smaller_area > 0 else 0.0

def _raw_visual_repetitions(raw_visuals: Sequence[RawSourceVisual]) -> dict[str, tuple[int, int]]:
    locations_by_hash: dict[str, set[str]] = {}
    count_by_hash: dict[str, int] = {}
    for raw in raw_visuals:
        if not raw.content:
            continue
        digest = hashlib.sha256(raw.content).hexdigest()
        count_by_hash[digest] = count_by_hash.get(digest, 0) + 1
        location = _raw_visual_location(raw)
        if location:
            locations_by_hash.setdefault(digest, set()).add(location)
    return {digest: (count, len(locations_by_hash.get(digest, set()))) for digest, count in count_by_hash.items()}

def _raw_visual_location(raw: RawSourceVisual) -> str:
    if raw.slide_no is not None:
        return f'slide:{raw.slide_no}'
    if raw.page_no is not None:
        return f'page:{raw.page_no}'
    if raw.sheet_name:
        return f'sheet:{raw.sheet_name}'
    spine_index = raw.metadata.get('epub_spine_index')
    return f'epub-spine:{spine_index}' if spine_index is not None else ''

def _visual_content_shape_filter_reason(raw: RawSourceVisual, *, repetitions: dict[str, tuple[int, int]]) -> str:
    if not raw.content or raw.metadata.get('standalone_image'):
        return ''
    width, height = (raw.width, raw.height)
    if width is None or height is None:
        width, height = _image_dimensions(raw.content)
    has_semantic_signal = bool(raw.caption.strip() or raw.ocr_text.strip() or raw.table_data)
    if width is not None and height is not None and (width <= 32) and (height <= 32) and (not has_semantic_signal):
        return 'micro_visual'
    bbox_area = _normalized_bbox_area(raw.bbox)
    if bbox_area is not None and bbox_area < 0.0015 and (not has_semantic_signal):
        return 'micro_visual_region'
    if bbox_area is not None and bbox_area >= 0.96 and (not raw.caption.strip()) and (not raw.ocr_text.strip()):
        return 'background_visual'
    digest = hashlib.sha256(raw.content).hexdigest()
    repeated_count, distinct_locations = repetitions.get(digest, (1, 0))
    if repeated_count >= 2 and distinct_locations >= 2 and (len(raw.bbox) >= 4):
        if raw.caption.strip() or raw.ocr_text.strip():
            return ''
        raw.ocr_text = _ocr_visual_content(raw.content, raw.mime_type)
        if raw.ocr_text.strip():
            return ''
        height_ratio = max(0.0, raw.bbox[3] - raw.bbox[1])
        width_ratio = max(0.0, raw.bbox[2] - raw.bbox[0])
        in_page_margin = raw.bbox[1] <= 0.08 or raw.bbox[3] >= 0.92 or raw.bbox[0] <= 0.05 or (raw.bbox[2] >= 0.95)
        if in_page_margin and height_ratio <= 0.15 and ((bbox_area or 0.0) <= 0.08):
            return 'repeated_page_margin_visual'
        if in_page_margin and width_ratio <= 0.15 and ((bbox_area or 0.0) <= 0.08):
            return 'repeated_page_margin_visual'
    return ''

def _normalized_bbox_area(bbox: list[float]) -> float | None:
    if len(bbox) < 4:
        return None
    width = max(0.0, min(1.0, bbox[2]) - max(0.0, bbox[0]))
    height = max(0.0, min(1.0, bbox[3]) - max(0.0, bbox[1]))
    return width * height

def _locator_number(locator: str, prefix: str) -> int | None:
    if prefix not in locator:
        return None
    trailing = locator.split(prefix, 1)[1].split(':', 1)[0]
    return int(trailing) if trailing.isdigit() else None

def _page_in_range(page_no: int, page_start: int, page_end: int | None) -> bool:
    if page_end is None or page_end <= page_start:
        return page_no == page_start
    return page_start <= page_no < page_end

def _image_dimensions(content: bytes) -> tuple[int | None, int | None]:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(content)) as image:
            return (int(image.width), int(image.height))
    except Exception:
        return (None, None)

def _ocr_visual_content(content: bytes, mime_type: str) -> str:
    suffix = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif', 'image/webp': '.webp', 'image/tiff': '.tiff', 'image/bmp': '.bmp', 'image/svg+xml': '.svg'}.get(mime_type, '.img')
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix='openclass-visual-ocr-', suffix=suffix, delete=False) as handle:
            handle.write(content)
            path = Path(handle.name)
        return str(extract_image_text(path) or '').strip()
    except Exception:
        return ''
    finally:
        if path is not None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

def _is_audio_or_video(record: SourceIngestionRecord) -> bool:
    return record.source_type in {'audio_file', 'video_file', 'video_url', 'transcript'} or record.mime_type.startswith(('audio/', 'video/')) or str(record.metadata.get('original_mime_type') or '').startswith(('audio/', 'video/'))

def _office_format(*, suffix: str, mime_type: str) -> str:
    if suffix in {'.docx', '.pptx', '.xlsx'}:
        return suffix.removeprefix('.')
    normalized_mime = mime_type.split(';', 1)[0].strip().lower()
    if 'wordprocessingml.document' in normalized_mime:
        return 'docx'
    if 'presentationml.presentation' in normalized_mime:
        return 'pptx'
    if 'spreadsheetml.sheet' in normalized_mime:
        return 'xlsx'
    return ''
source_visual_extractor = SourceVisualExtractor()
