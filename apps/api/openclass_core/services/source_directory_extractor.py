from __future__ import annotations
import csv
import difflib
import html
import json
import posixpath
import re
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable, Literal
from urllib.parse import unquote
from openclass_core.models import SourceCatalogEvidence, SourceIngestionRecord, SourceRange
from openclass_core.services.image_ocr import extract_image_text
from openclass_core.services.pdf_toc_parser import extract_pdf_toc_from_range, is_toc_heading, normalize_toc_text, parse_structural_heading, probe_pdf_toc_from_leading_pages
from openclass_core.services.source_archive import SafeSourceArchive
from openclass_core.services.source_ooxml_navigation import OoxmlNavigationError, ordered_pptx_slide_parts, read_docx_paragraph_blocks
from openclass_core.services.source_xml import parse_untrusted_xml
MAX_DIRECTORY_NODES = 5000
MAX_PDF_TOC_PROBE_PAGES = 48
MAX_PDF_TOC_PAGES = 24
MIN_NATIVE_TOC_TEXT_NODES = 3
PDF_HEADING_REGION_RATIO = 0.28
CatalogProgressCallback = Callable[[str, int], None]
MappingStatus = Literal['verified', 'partial', 'unverified', 'unmapped']

@dataclass(frozen=True)
class DirectoryCandidate:
    local_key: str
    title: str
    number: str = ''
    level: int = 1
    order_index: int = 0
    source_locator: str = ''
    source_range: SourceRange | None = None
    mapping_status: MappingStatus = 'unmapped'
    confidence: float = 0.0
    evidence: tuple[SourceCatalogEvidence, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)

@dataclass(frozen=True)
class DirectoryExtraction:
    candidates: tuple[DirectoryCandidate, ...]
    warnings: tuple[str, ...] = ()
    page_count: int = 0
    inspected_page_count: int = 0
    ocr_page_count: int = 0
    metadata: dict[str, object] = field(default_factory=dict)

class DirectoryExtractionError(RuntimeError):
    pass

def supports_directory_catalog(record: SourceIngestionRecord) -> bool:
    if record.source_type not in {'local_file', 'web_url'}:
        return False
    suffix = Path(record.file_name).suffix.lower()
    return suffix in {'.pdf', '.epub', '.docx', '.pptx', '.xlsx', '.csv', '.txt', '.md', '.markdown', '.html', '.htm', '.json', '.xml'}

def extract_directory(record: SourceIngestionRecord, path: Path, *, progress_callback: CatalogProgressCallback | None=None) -> DirectoryExtraction:
    """Extract navigation evidence without materializing the document body.

    The extractor keeps only headings, package navigation, physical page anchors,
    and native structural locators. PDF fallback inspection is restricted to the
    heading region of each page; full-page OCR is used only after a TOC page has
    been identified.
    """
    suffix = Path(record.file_name or path.name).suffix.lower()
    _report(progress_callback, 'reading_directory_metadata', 30)
    if suffix == '.pdf' or record.mime_type.lower() == 'application/pdf':
        return _extract_pdf(path, progress_callback=progress_callback)
    if suffix == '.epub' or record.mime_type.lower() == 'application/epub+zip':
        return _extract_epub(path)
    if suffix == '.docx' or 'wordprocessingml' in record.mime_type.lower():
        return _extract_docx(path, fallback_title=record.title)
    if suffix == '.pptx' or 'presentationml' in record.mime_type.lower():
        return _extract_pptx(path)
    if suffix == '.xlsx' or 'spreadsheetml' in record.mime_type.lower():
        return _extract_xlsx(path)
    if suffix == '.csv':
        return _extract_csv(path, fallback_title=record.title)
    if suffix in {'.html', '.htm'} or record.mime_type.lower() == 'text/html':
        return _extract_html(path, fallback_title=record.title)
    if suffix == '.json':
        return _extract_json(path, fallback_title=record.title)
    if suffix == '.xml':
        return _extract_xml(path, fallback_title=record.title)
    if suffix in {'.txt', '.md', '.markdown'} or record.mime_type.lower().startswith('text/'):
        return _extract_text(path, fallback_title=record.title, markdown=suffix != '.txt')
    raise DirectoryExtractionError('This file format has no directory-only extractor.')

def extract_authoritative_native_directory(record: SourceIngestionRecord, path: Path, *, progress_callback: CatalogProgressCallback | None=None) -> DirectoryExtraction:
    """Extract only native TOC body indexes: PDF outlines / EPUB toc.ncx·nav.

    Printed-TOC OCR, heading-region inference, and EPUB spine-only fallbacks are
    intentionally excluded so the catalog pipeline can fall through to AI.
    """
    suffix = Path(record.file_name or path.name).suffix.lower()
    mime = (record.mime_type or '').lower()
    _report(progress_callback, 'reading_native_directory_index', 30)
    if suffix == '.pdf' or mime == 'application/pdf':
        return _extract_pdf_native_outline(path, progress_callback=progress_callback)
    if suffix == '.epub' or mime == 'application/epub+zip':
        return _extract_epub_native_navigation(path)
    return DirectoryExtraction(candidates=(), warnings=('No authoritative native directory body index for this format.',), metadata={'format': suffix.lstrip('.') or 'unknown', 'directory_source': 'unsupported', 'body_text_extracted': False, 'native_body_index': False})

def has_usable_native_body_index(extraction: DirectoryExtraction) -> bool:
    """True when at least one native node can open a body location."""
    for candidate in extraction.candidates:
        if candidate.source_range is None:
            continue
        if candidate.mapping_status not in {'verified', 'partial'}:
            continue
        kind = candidate.source_range.kind
        if kind == 'pdf_pages' and isinstance(candidate.source_range.start, int):
            return True
        if kind == 'epub_spine' and isinstance(candidate.source_range.start, int):
            return True
    return False

@dataclass(frozen=True)
class _PdfHeadingLine:
    text: str
    page_no: int
    y_ratio: float
    font_size: float
    source: str

@dataclass(frozen=True)
class _PdfTocCandidate:
    title: str
    number: str
    level: int
    printed_page: int | None
    toc_page: int
    confidence: float
    source: str

def _extract_pdf_native_outline(path: Path, *, progress_callback: CatalogProgressCallback | None=None) -> DirectoryExtraction:
    """PDF Outlines / Named Destinations → physical page body indexes only."""
    del progress_callback
    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise DirectoryExtractionError('PDF directory extraction dependencies are unavailable.') from exc
    reader = PdfReader(str(path))
    page_count = len(reader.pages)
    if page_count < 1:
        return DirectoryExtraction(candidates=(), warnings=('PDF has no pages.',), metadata={'format': 'pdf', 'directory_source': 'native_outline', 'body_text_extracted': False, 'native_body_index': False})
    outline_candidates = _pdf_outline_candidates(reader, page_count=page_count)
    if not outline_candidates:
        return DirectoryExtraction(candidates=(), warnings=('No PDF outline destinations were found.',), page_count=page_count, metadata={'format': 'pdf', 'directory_source': 'native_outline', 'body_text_extracted': False, 'native_body_index': False})
    outline_candidates, inspected_outline_pages = _validate_pdf_outline_destination_collisions(path, outline_candidates)
    closed = _close_numeric_ranges(outline_candidates, maximum=page_count, kind='pdf_pages')
    warnings: tuple[str, ...] = ()
    if any((candidate.mapping_status != 'verified' for candidate in closed)):
        warnings = ('Some native PDF outline destinations are invalid or do not provide a safe range boundary.',)
    return DirectoryExtraction(candidates=tuple(closed), warnings=warnings, page_count=page_count, inspected_page_count=len(inspected_outline_pages), ocr_page_count=0, metadata={'format': 'pdf', 'directory_source': 'native_outline', 'body_text_extracted': False, 'heading_region_scan': bool(inspected_outline_pages), 'outline_collision_validation': bool(inspected_outline_pages), 'native_body_index': True})

def _extract_pdf(path: Path, *, progress_callback: CatalogProgressCallback | None) -> DirectoryExtraction:
    try:
        from openclass_core.services import pdf_backend as fitz
        from pypdf import PdfReader
    except Exception as exc:
        raise DirectoryExtractionError('PDF directory extraction dependencies are unavailable.') from exc
    reader = PdfReader(str(path))
    page_count = len(reader.pages)
    if page_count < 1:
        return DirectoryExtraction(candidates=(), warnings=('PDF has no pages.',), metadata={'format': 'pdf'})
    outline_extraction = _extract_pdf_native_outline(path, progress_callback=progress_callback)
    if outline_extraction.candidates:
        return outline_extraction
    toc_outline_range = _pdf_outline_toc_range(reader, page_count=page_count)
    toc_outline_page = toc_outline_range[0] if toc_outline_range is not None else None
    toc_outline_page_end = toc_outline_range[1] if toc_outline_range is not None else None
    printed_page_map = _pdf_printed_page_navigation(reader, page_count=page_count)
    document = fitz.open(str(path))
    inspected_pages: set[int] = set()
    ocr_pages: set[int] = set()
    heading_cache: dict[int, list[_PdfHeadingLine]] = {}
    temporary_directory = tempfile.TemporaryDirectory(prefix='openclass-directory-')
    temp_root = Path(temporary_directory.name)

    def page_headings(page_no: int) -> list[_PdfHeadingLine]:
        if page_no in heading_cache:
            return heading_cache[page_no]
        page = document.load_page(page_no - 1)
        clip = fitz.Rect(0, 0, page.rect.width, page.rect.height * PDF_HEADING_REGION_RATIO)
        inspected_pages.add(page_no)
        lines = _native_pdf_heading_lines(page, clip=clip, page_no=page_no)
        if not _has_meaningful_pdf_heading_lines(lines):
            image_path = temp_root / f'page-{page_no}-heading.png'
            try:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=clip, alpha=False)
                pixmap.save(str(image_path))
                ocr_text = extract_image_text(image_path) or ''
            except Exception:
                ocr_text = ''
            if ocr_text.strip():
                ocr_pages.add(page_no)
                raw_lines = [line.strip() for line in ocr_text.splitlines() if line.strip()]
                lines = [_PdfHeadingLine(text=line, page_no=page_no, y_ratio=min(0.27, index * 0.025), font_size=0.0, source='heading_region_ocr') for index, line in enumerate(raw_lines[:16])]
        heading_cache[page_no] = lines
        return lines
    try:
        _report(progress_callback, 'locating_toc_pages', 38)
        probe_end = min(page_count, MAX_PDF_TOC_PROBE_PAGES)
        toc_start: int | None = toc_outline_page
        if toc_start is None:
            for page_no in range(1, probe_end + 1):
                lines = page_headings(page_no)
                if _pdf_page_has_toc_heading(lines):
                    toc_start = page_no
                    break
        toc_nodes: list[_PdfTocCandidate] = []
        toc_end: int | None = None
        if toc_start is not None:
            toc_nodes, toc_end, toc_ocr_pages = _read_pdf_toc_pages(document, path=path, page_start=toc_start, page_count=page_count, inspected_pages=inspected_pages, page_end_limit=toc_outline_page_end)
            ocr_pages.update(toc_ocr_pages)
        else:
            probed_toc = probe_pdf_toc_from_leading_pages(path, page_count=page_count, max_probe_pages=probe_end)
            if probed_toc.nodes:
                toc_start = probed_toc.toc_page_start
                toc_end = probed_toc.toc_page_end
                toc_nodes = [_PdfTocCandidate(title=_clean_title(node.title), number=node.number or _heading_number_and_level(node.title)[0], level=max(1, node.level), printed_page=node.printed_page if node.printed_page > 0 else None, toc_page=node.toc_page, confidence=node.confidence, source='pdf_toc_ocr_probe') for node in probed_toc.nodes if _clean_title(node.title)]
                ocr_pages.update(range(toc_start or 1, (toc_end or toc_start or 1) + 1))
        _report(progress_callback, 'mapping_directory_to_pages', 48)
        if toc_nodes:
            if printed_page_map:
                body_heading_cache: dict[int, list[_PdfHeadingLine]] = {}
            else:
                for page_no in range(1, page_count + 1):
                    page_headings(page_no)
                body_heading_cache = {page_no: lines for page_no, lines in heading_cache.items() if not (toc_start is not None and toc_end is not None and (toc_start <= page_no <= toc_end))}
            mapped = _map_pdf_toc_nodes(toc_nodes, body_heading_cache, page_count=page_count, printed_page_map=printed_page_map)
            closed = _close_numeric_ranges(mapped, maximum=page_count, kind='pdf_pages')
            warnings = []
            if any((candidate.mapping_status != 'verified' for candidate in closed)):
                warnings.append('Some directory nodes could not be verified against physical PDF pages and cannot be cited.')
            return DirectoryExtraction(candidates=tuple(closed), warnings=tuple(warnings), page_count=page_count, inspected_page_count=len(inspected_pages), ocr_page_count=len(ocr_pages), metadata={'format': 'pdf', 'directory_source': 'toc_pages', 'toc_page_start': toc_start, 'toc_page_end': toc_end, 'body_text_extracted': False, 'heading_region_scan': not bool(printed_page_map), 'printed_page_navigation': bool(printed_page_map)})
        _report(progress_callback, 'scanning_heading_regions', 54)
        for page_no in range(1, page_count + 1):
            page_headings(page_no)
        headings = _directory_from_pdf_heading_regions(heading_cache)
        closed = _close_numeric_ranges(headings, maximum=page_count, kind='pdf_pages')
        warning = 'No PDF outline or TOC page was found; the directory was inferred only from page heading regions.' if closed else 'No usable directory heading was found without reading PDF body text.'
        return DirectoryExtraction(candidates=tuple(closed), warnings=(warning,), page_count=page_count, inspected_page_count=len(inspected_pages), ocr_page_count=len(ocr_pages), metadata={'format': 'pdf', 'directory_source': 'heading_regions', 'body_text_extracted': False, 'heading_region_scan': True})
    finally:
        document.close()
        temporary_directory.cleanup()

def _pdf_outline_candidates(reader: object, *, page_count: int) -> list[DirectoryCandidate]:
    flattened = _pdf_outline_items(reader)
    candidates: list[DirectoryCandidate] = []
    navigable_count = 0
    meaningful_count = 0
    for item, level in flattened:
        title = _clean_title(getattr(item, 'title', '') or str(item))
        if not _is_meaningful_pdf_outline_title(title) or is_toc_heading(title):
            continue
        meaningful_count += 1
        try:
            page_index = reader.get_destination_page_number(item)
        except Exception:
            page_index = None
        if not isinstance(page_index, int) or not 0 <= page_index < page_count:
            number, inferred_level = _heading_number_and_level(title)
            candidates.append(DirectoryCandidate(local_key=f'pdf-outline-{len(candidates)}', title=title, number=number, level=max(1, level, inferred_level), order_index=len(candidates), source_locator='pdf:outline:unresolved', source_range=None, mapping_status='unmapped', confidence=0.35, evidence=(SourceCatalogEvidence(method='pdf_native_outline_unresolved', source_locator='pdf:outline:unresolved', excerpt=title, confidence=0.35),), metadata={'navigation_provenance': 'native', 'invalid_destination': True}))
            continue
        navigable_count += 1
        page_no = page_index + 1
        number, inferred_level = _heading_number_and_level(title)
        resolved_level = max(1, level, inferred_level)
        candidates.append(DirectoryCandidate(local_key=f'pdf-outline-{len(candidates)}', title=title, number=number, level=resolved_level, order_index=len(candidates), source_locator=f'pdf:outline:{page_no}', source_range=SourceRange(kind='pdf_pages', start=page_no, end=page_no, display_label=f'PDF p. {page_no}', metadata={'index_base': 1, 'physical_pages': True}), mapping_status='verified', confidence=0.98, evidence=(SourceCatalogEvidence(method='pdf_native_outline', source_locator=f'pdf:outline:{page_no}', page_start=page_no, page_end=page_no, excerpt=title, confidence=0.98),), metadata={'navigation_provenance': 'native', 'destination_fingerprint': _pdf_outline_destination_fingerprint(item, page_index=page_index)}))
    if navigable_count >= 8 and meaningful_count / navigable_count < 0.25:
        return []
    return _mark_pdf_outline_destination_collisions(candidates)

def _pdf_outline_items(reader: object) -> list[tuple[object, int]]:
    try:
        outline = reader.outline
    except Exception:
        return []
    flattened: list[tuple[object, int]] = []

    def visit(items: object, level: int=1) -> None:
        if isinstance(items, list):
            for item in items:
                if isinstance(item, list):
                    visit(item, level + 1)
                else:
                    flattened.append((item, level))
            return
        flattened.append((items, level))
    visit(outline)
    return flattened

def _pdf_outline_toc_range(reader: object, *, page_count: int) -> tuple[int, int] | None:
    """Return a conservative physical-page range for an outlined TOC.

    A semantic TOC bookmark identifies the first candidate page. The first
    later outline destination that advances to a larger physical page is a
    navigation boundary, so it cannot itself still be part of the TOC range.
    The normal TOC page budget remains a hard upper bound when outline
    navigation is sparse or malformed.
    """
    items = _pdf_outline_items(reader)
    for index, (item, _level) in enumerate(items):
        title = _clean_title(getattr(item, 'title', '') or str(item))
        if not is_toc_heading(title):
            continue
        try:
            page_index = reader.get_destination_page_number(item)
        except Exception:
            continue
        if not isinstance(page_index, int) or not 0 <= page_index < page_count:
            continue
        page_start = page_index + 1
        page_end = min(page_count, page_start + MAX_PDF_TOC_PAGES - 1)
        for candidate, _candidate_level in items[index + 1:]:
            try:
                candidate_index = reader.get_destination_page_number(candidate)
            except Exception:
                continue
            if not isinstance(candidate_index, int) or not 0 <= candidate_index < page_count:
                continue
            candidate_page = candidate_index + 1
            if candidate_page <= page_start:
                continue
            page_end = min(page_end, candidate_page - 1)
            break
        return (page_start, max(page_start, page_end))
    return None

def _pdf_printed_page_navigation(reader: object, *, page_count: int) -> dict[int, int]:
    physical_by_printed: dict[int, int] = {}
    conflicting_printed_pages: set[int] = set()
    for item, _level in _pdf_outline_items(reader):
        title = _clean_title(getattr(item, 'title', '') or str(item))
        if not re.fullmatch('\\d{1,7}', title):
            continue
        printed_page = int(title)
        if printed_page < 1:
            continue
        try:
            page_index = reader.get_destination_page_number(item)
        except Exception:
            continue
        if isinstance(page_index, int) and 0 <= page_index < page_count:
            physical_page = page_index + 1
            previous = physical_by_printed.get(printed_page)
            if previous is not None and previous != physical_page:
                conflicting_printed_pages.add(printed_page)
                continue
            physical_by_printed[printed_page] = physical_page
    pairs = [pair for pair in physical_by_printed.items() if pair[0] not in conflicting_printed_pages]
    if len(pairs) < 4:
        return {}
    offset_counts = Counter((physical - printed for printed, physical in pairs))
    page_offset, support = offset_counts.most_common(1)[0]
    if support < 4 or support / len(pairs) < 0.8:
        return {}
    consistent = {printed: physical for printed, physical in pairs if physical - printed == page_offset}
    ordered = sorted(consistent.items())
    if any((printed <= previous_printed or physical <= previous_physical for (previous_printed, previous_physical), (printed, physical) in zip(ordered, ordered[1:]))):
        return {}
    return consistent

def _is_meaningful_pdf_outline_title(value: str) -> bool:
    title = _clean_title(value)
    if not title or len(title) > 240:
        return False
    if re.fullmatch('(?:\\d+|[ivxlcdm]+)', title, flags=re.I):
        return False
    return sum((unicodedata.category(character).startswith('L') for character in title)) >= 2

def _pdf_outline_destination_fingerprint(item: object, *, page_index: int) -> str:

    def coordinate(name: str) -> str:
        value = getattr(item, name, None)
        if isinstance(value, (int, float)):
            return f'{float(value):.4f}'
        return str(value or '')
    return '|'.join([str(page_index + 1), str(getattr(item, 'typ', '') or ''), coordinate('left'), coordinate('top'), coordinate('zoom')])

def _mark_pdf_outline_destination_collisions(candidates: list[DirectoryCandidate]) -> list[DirectoryCandidate]:
    groups: dict[tuple[int, str], list[int]] = {}
    for index, candidate in enumerate(candidates):
        fingerprint = str(candidate.metadata.get('destination_fingerprint') or '')
        if not fingerprint:
            continue
        groups.setdefault((candidate.level, fingerprint), []).append(index)
    collision_indexes = {index for indexes in groups.values() if len({_normalize_title(candidates[index].title) for index in indexes}) > 1 for index in indexes}
    if not collision_indexes:
        return candidates
    return [replace(candidate, metadata={**candidate.metadata, 'destination_collision': True}) if index in collision_indexes else candidate for index, candidate in enumerate(candidates)]

def _validate_pdf_outline_destination_collisions(path: Path, candidates: list[DirectoryCandidate]) -> tuple[list[DirectoryCandidate], set[int]]:
    del path
    return ([replace(candidate, metadata={**candidate.metadata, 'destination_collision_validation': 'resolved_native_destination'}) if candidate.metadata.get('destination_collision') is True else candidate for candidate in candidates], set())

def _native_pdf_heading_lines(page: object, *, clip: object, page_no: int, limit: int | None=24) -> list[_PdfHeadingLine]:
    try:
        from openclass_core.services import pdf_backend as fitz
        text_flags = int(fitz.TEXTFLAGS_DICT) & ~int(fitz.TEXT_PRESERVE_IMAGES)
        payload = page.get_text('dict', clip=clip, flags=text_flags)
    except Exception:
        return []
    page_height = max(1.0, float(page.rect.height))
    result: list[_PdfHeadingLine] = []
    for block in payload.get('blocks', []) if isinstance(payload, dict) else []:
        if not isinstance(block, dict):
            continue
        for line in block.get('lines', []):
            if not isinstance(line, dict):
                continue
            spans = line.get('spans', [])
            text = ''.join((str(span.get('text') or '') for span in spans if isinstance(span, dict))).strip()
            if not text:
                continue
            sizes = [float(span.get('size') or 0.0) for span in spans if isinstance(span, dict)]
            bbox = line.get('bbox') if isinstance(line.get('bbox'), (list, tuple)) else (0, 0, 0, 0)
            y_ratio = float(bbox[1] or 0.0) / page_height if len(bbox) >= 2 else 0.0
            result.append(_PdfHeadingLine(text=_clean_title(text), page_no=page_no, y_ratio=y_ratio, font_size=max(sizes, default=0.0), source='pdf_heading_region_text'))
    return result if limit is None else result[:limit]

def _read_pdf_toc_pages(document: object, *, path: Path, page_start: int, page_count: int, inspected_pages: set[int], page_end_limit: int | None=None) -> tuple[list[_PdfTocCandidate], int, set[int]]:
    native_nodes: list[_PdfTocCandidate] = []
    page_end = min(page_count, page_start + MAX_PDF_TOC_PAGES - 1)
    if page_end_limit is not None:
        page_end = min(page_end, max(page_start, page_end_limit))
    last_useful_page = page_start
    empty_after_content = 0
    for page_no in range(page_start, page_end + 1):
        page = document.load_page(page_no - 1)
        inspected_pages.add(page_no)
        try:
            text = str(page.get_text('text') or '')
        except Exception:
            text = ''
        page_nodes = _parse_native_toc_text(text, toc_page=page_no)
        if page_nodes:
            native_nodes.extend(page_nodes)
            last_useful_page = page_no
            empty_after_content = 0
        elif native_nodes:
            empty_after_content += 1
            if empty_after_content >= 2:
                break
    if len(native_nodes) >= MIN_NATIVE_TOC_TEXT_NODES:
        return (_dedupe_toc_nodes(native_nodes), last_useful_page, set())
    extraction = extract_pdf_toc_from_range(path, page_start=page_start, page_end=page_end)
    recovered_nodes = [_PdfTocCandidate(title=_clean_title(node.title), number=node.number or _heading_number_and_level(node.title)[0], level=max(1, node.level), printed_page=node.printed_page if node.printed_page > 0 else None, toc_page=node.toc_page, confidence=node.confidence, source='pdf_toc_ocr') for node in _contiguous_printed_toc_nodes(extraction.nodes, page_start=page_start, page_end=page_end) if _clean_title(node.title)]
    recovered_nodes = _repair_nonmonotonic_printed_pages(recovered_nodes)
    if len(recovered_nodes) > len(native_nodes):
        recovered_end = max((node.toc_page for node in recovered_nodes), default=page_start)
        ocr_pages = set(range(page_start, recovered_end + 1))
        return (_dedupe_toc_nodes(recovered_nodes), recovered_end, ocr_pages)
    return (_dedupe_toc_nodes(native_nodes), last_useful_page, set())

def _contiguous_printed_toc_nodes(nodes: Iterable[object], *, page_start: int, page_end: int) -> list[object]:
    """Keep the contiguous OCR TOC run and reject following body headings.

    Layout OCR deliberately scans only a bounded range, but structural headings
    in the first body pages can otherwise look like extra TOC rows.  A genuine
    printed TOC run has page destinations on every page; once that run ends, do
    not absorb later chapter headings or running headers.
    """
    nodes_by_page: dict[int, list[object]] = {}
    for node in nodes:
        toc_page = getattr(node, 'toc_page', None)
        printed_page = getattr(node, 'printed_page', None)
        if not isinstance(toc_page, int) or not isinstance(printed_page, int) or printed_page < 1:
            continue
        nodes_by_page.setdefault(toc_page, []).append(node)
    selected: list[object] = []
    started = False
    for toc_page in range(page_start, page_end + 1):
        page_nodes = nodes_by_page.get(toc_page, [])
        if not started:
            if len(page_nodes) < 3:
                continue
            started = True
        elif not page_nodes:
            break
        selected.extend(page_nodes)
    return selected

def _repair_nonmonotonic_printed_pages(nodes: list[_PdfTocCandidate]) -> list[_PdfTocCandidate]:
    """Repair a single OCR digit outlier using the authored tree order.

    A first child cannot start after its following sibling.  When its OCR page
    is the sole local inversion and the parent page fits the surrounding run,
    inherit that parent destination.  The global printed-to-physical offset is
    still established independently from multiple exact body-title anchors.
    """
    repaired = list(nodes)
    for index in range(1, len(repaired) - 1):
        previous = repaired[index - 1]
        current = repaired[index]
        following = repaired[index + 1]
        if current.printed_page is None or previous.printed_page is None or following.printed_page is None or (current.printed_page <= following.printed_page) or (previous.printed_page > following.printed_page) or (current.level <= previous.level):
            continue
        repaired[index] = replace(current, printed_page=previous.printed_page, confidence=min(current.confidence, 0.7), source=f'{current.source}_hierarchy_page_repair')
    return repaired
_TOC_LINE_RE = re.compile('^(?P<title>.+?)(?:\\.{2,}|…{2,}|\\s{2,}|\\s)(?P<page>[ivxlcdm]+|\\d+)\\s*$', re.I)
_TOC_PAGE_TOKEN_RE = re.compile('^(?:[ivxlcdm]+|\\d+)$', re.I)

def _pdf_page_has_toc_heading(lines: list[_PdfHeadingLine]) -> bool:
    texts = [line.text for line in lines if line.text.strip()]
    for index in range(len(texts)):
        for width in range(1, min(4, len(texts) - index) + 1):
            if is_toc_heading(' '.join(texts[index:index + width])):
                return True
    return False

def _parse_native_toc_text(text: str, *, toc_page: int) -> list[_PdfTocCandidate]:
    nodes: list[_PdfTocCandidate] = []
    lines = [_clean_title(normalize_toc_text(raw_line)) for raw_line in text.splitlines() if _clean_title(normalize_toc_text(raw_line))]
    pending_title_lines: list[str] = []
    index = 0
    while index < len(lines):
        heading_width = next((width for width in range(1, min(4, len(lines) - index) + 1) if is_toc_heading(' '.join(lines[index:index + width]))), None)
        if heading_width is not None:
            pending_title_lines = []
            index += heading_width
            continue
        line = lines[index]
        index += 1
        if _is_toc_leader_line(line):
            continue
        match = _TOC_LINE_RE.match(line)
        raw_page = ''
        title = ''
        if match is not None:
            raw_page = match.group('page')
            title = _clean_title(match.group('title').rstrip('.·…-–— '))
            pending_title_lines = []
        elif _TOC_PAGE_TOKEN_RE.fullmatch(line) and pending_title_lines:
            raw_page = line
            title = _clean_title(' '.join(pending_title_lines).rstrip('.·…-–— '))
            pending_title_lines = []
        else:
            pending_title_lines.append(line)
            pending_title_lines = pending_title_lines[-3:]
            continue
        printed_page = int(raw_page) if raw_page.isdigit() else None
        if not _is_plausible_native_toc_title(title):
            continue
        number, level = _heading_number_and_level(title)
        nodes.append(_PdfTocCandidate(title=title, number=number, level=level, printed_page=printed_page, toc_page=toc_page, confidence=0.82 if printed_page is not None else 0.62, source='pdf_toc_text_layer'))
    return nodes

def _is_toc_leader_line(value: str) -> bool:
    return bool(value) and all((character.isspace() or character in '.…·•_—–-' or unicodedata.category(character) == 'Co' for character in value))

def _is_plausible_native_toc_title(value: str) -> bool:
    if len(value) < 2 or len(value) > 240:
        return False
    punctuation_probe = re.sub('(?<=\\d)\\.(?=\\d)', '', value)
    return not any((character in punctuation_probe for character in '。.!?！？；;'))

def _dedupe_toc_nodes(nodes: Iterable[_PdfTocCandidate]) -> list[_PdfTocCandidate]:
    result: list[_PdfTocCandidate] = []
    seen: set[tuple[str, int | None]] = set()
    for node in nodes:
        key = (_normalize_title(node.title), node.printed_page)
        if not key[0] or key in seen:
            continue
        seen.add(key)
        result.append(node)
    return result[:MAX_DIRECTORY_NODES]

def _map_pdf_toc_nodes(nodes: list[_PdfTocCandidate], heading_cache: dict[int, list[_PdfHeadingLine]], *, page_count: int, printed_page_map: dict[int, int] | None=None) -> list[DirectoryCandidate]:
    direct_matches: dict[int, int] = {}
    offset_votes: Counter[int] = Counter()
    offset_printed_pages: dict[int, set[int]] = {}
    printed_page_map = printed_page_map or {}
    for printed_page, physical_page in printed_page_map.items():
        offset = physical_page - printed_page
        offset_votes[offset] += 1
        offset_printed_pages.setdefault(offset, set()).add(printed_page)
    for node_index, node in enumerate(nodes):
        if node.printed_page is not None and node.printed_page in printed_page_map:
            direct_matches[node_index] = printed_page_map[node.printed_page]
            continue
        page_no = _best_heading_page(node, heading_cache)
        if page_no is None:
            continue
        direct_matches[node_index] = page_no
        if node.printed_page is not None:
            offset = page_no - node.printed_page
            offset_votes[offset] += 1
            offset_printed_pages.setdefault(offset, set()).add(node.printed_page)
    page_offset: int | None = None
    offset_support = 0
    offset_printed_page_start: int | None = None
    offset_printed_page_end: int | None = None
    if offset_votes:
        page_offset, offset_support = offset_votes.most_common(1)[0]
        if offset_support < 2:
            page_offset = None
        else:
            observed_printed_pages = offset_printed_pages.get(page_offset, set())
            if observed_printed_pages:
                offset_printed_page_start = min(observed_printed_pages)
                offset_printed_page_end = max(observed_printed_pages)
    resolved_pages: dict[int, int] = {}
    effective_printed_pages: dict[int, int | None] = {}
    mapping_methods: dict[int, str] = {}
    mapping_confidences: dict[int, float] = {}
    for index, node in enumerate(nodes):
        direct_match = direct_matches.get(index)
        mapped_from_printed_navigation = node.printed_page is not None and node.printed_page in printed_page_map and (printed_page_map[node.printed_page] == direct_match)
        effective_printed_pages[index] = node.printed_page
        proposed: int | None = None
        if page_offset is not None and node.printed_page is not None and (offset_printed_page_start is not None) and (offset_printed_page_end is not None) and (offset_printed_page_start <= node.printed_page <= offset_printed_page_end):
            proposed = node.printed_page + page_offset
            if not 1 <= proposed <= page_count:
                proposed = None
        if mapped_from_printed_navigation and direct_match is not None:
            resolved_pages[index] = direct_match
            mapping_methods[index] = 'native_printed_page_navigation'
            mapping_confidences[index] = max(0.94, node.confidence)
        elif direct_match is not None:
            resolved_pages[index] = direct_match
            if proposed == direct_match:
                mapping_methods[index] = 'heading_region_and_printed_offset'
                mapping_confidences[index] = max(0.9, node.confidence)
            elif page_offset is not None and 1 <= direct_match - page_offset <= page_count:
                effective_printed_pages[index] = direct_match - page_offset
                mapping_methods[index] = 'body_heading_corrected_printed_page'
                mapping_confidences[index] = max(0.9, node.confidence)
            else:
                mapping_methods[index] = 'body_heading_region_match'
                mapping_confidences[index] = max(0.82, node.confidence)
        elif proposed is not None:
            resolved_pages[index] = proposed
            mapping_methods[index] = 'verified_printed_to_physical_offset'
            mapping_confidences[index] = max(0.84, node.confidence)
    for index in range(1, len(nodes)):
        if index in resolved_pages:
            continue
        node = nodes[index]
        previous = nodes[index - 1]
        if node.level <= previous.level or node.printed_page is None or node.printed_page != previous.printed_page or (index - 1 not in resolved_pages):
            continue
        resolved_pages[index] = resolved_pages[index - 1]
        effective_printed_pages[index] = effective_printed_pages.get(index - 1)
        mapping_methods[index] = 'first_child_inherits_verified_parent_page'
        mapping_confidences[index] = max(0.84, node.confidence)
    candidates: list[DirectoryCandidate] = []
    for index, node in enumerate(nodes):
        mapped_page = resolved_pages.get(index)
        printed_page = effective_printed_pages.get(index)
        mapping_method = mapping_methods.get(index, 'unmapped')
        confidence = mapping_confidences.get(index, min(0.64, node.confidence))
        source_range = SourceRange(kind='pdf_pages', start=mapped_page, end=mapped_page, display_label=f'PDF p. {mapped_page}', metadata={'index_base': 1, 'physical_pages': True, 'printed_page': printed_page}) if mapped_page is not None else None
        candidates.append(DirectoryCandidate(local_key=f'pdf-toc-{index}', title=node.title, number=node.number, level=max(1, node.level), order_index=index, source_locator=f"pdf:toc:{node.toc_page}:printed:{printed_page or ''}", source_range=source_range, mapping_status='verified' if source_range is not None else 'unmapped', confidence=confidence if source_range is not None else min(0.64, node.confidence), evidence=(SourceCatalogEvidence(method=node.source, source_locator=f'pdf:toc:{node.toc_page}', page_start=node.toc_page, page_end=node.toc_page, excerpt=node.title, confidence=node.confidence, metadata={'printed_page': printed_page, 'ocr_printed_page': node.printed_page if printed_page != node.printed_page else None, 'physical_page': mapped_page, 'mapping_method': mapping_method, 'offset_support': offset_support, 'offset_printed_page_start': offset_printed_page_start, 'offset_printed_page_end': offset_printed_page_end}),), metadata={'printed_page': printed_page, 'ocr_printed_page': node.printed_page if printed_page != node.printed_page else None, 'toc_page': node.toc_page}))
    return candidates

def _best_heading_page(node: _PdfTocCandidate, heading_cache: dict[int, list[_PdfHeadingLine]]) -> int | None:
    target = _normalize_title(node.title)
    if not target:
        return None
    target_marker = parse_structural_heading(node.title)
    best_score = 0.0
    best_page: int | None = None
    for page_no in sorted(heading_cache):
        lines = heading_cache[page_no]
        combined_page_heading = _normalize_title(' '.join((line.text for line in lines)))
        if target and target in combined_page_heading and (len(target) >= max(4, round(len(combined_page_heading) * 0.28))):
            return page_no
        for line in lines:
            candidate = _normalize_title(line.text)
            if not candidate:
                continue
            shorter, longer = sorted((target, candidate), key=len)
            if shorter and shorter in longer and (len(shorter) >= max(4, round(len(longer) * 0.72))):
                return page_no
            candidate_marker = parse_structural_heading(line.text)
            if target_marker is None or candidate_marker is None or target_marker.kind != candidate_marker.kind or (_normalize_number(target_marker.number) != _normalize_number(candidate_marker.number)):
                continue
            score = _title_similarity(target, candidate)
            if score > best_score:
                best_score = score
                best_page = page_no
    return best_page if best_score >= 0.92 else None

def _directory_from_pdf_heading_regions(heading_cache: dict[int, list[_PdfHeadingLine]]) -> list[DirectoryCandidate]:
    occurrences = Counter((_normalize_title(line.text) for lines in heading_cache.values() for line in lines if _normalize_title(line.text)))
    repeated_limit = max(3, round(len(heading_cache) * 0.18))
    candidates: list[DirectoryCandidate] = []
    for page_no in sorted(heading_cache):
        lines = heading_cache[page_no]
        positive_sizes = [line.font_size for line in lines if line.font_size > 0]
        baseline = sorted(positive_sizes)[len(positive_sizes) // 2] if positive_sizes else 0.0
        for line in lines:
            title = _clean_title(line.text)
            normalized = _normalize_title(title)
            if not normalized or len(title) < 2 or len(title) > 180 or title.isdigit() or is_toc_heading(title) or (occurrences[normalized] >= repeated_limit):
                continue
            marker = parse_structural_heading(title)
            visually_prominent = line.font_size > 0 and line.font_size >= max(12.0, baseline * 1.16)
            near_top = line.y_ratio <= 0.16
            if marker is None and (not (visually_prominent and near_top)):
                continue
            number, inferred_level = _heading_number_and_level(title)
            level = marker.level if marker is not None else inferred_level
            confidence = 0.86 if marker is not None and line.source.endswith('text') else 0.72
            candidates.append(DirectoryCandidate(local_key=f'pdf-heading-{page_no}-{len(candidates)}', title=title, number=number, level=max(1, level), order_index=len(candidates), source_locator=f'pdf:heading-region:{page_no}', source_range=SourceRange(kind='pdf_pages', start=page_no, end=page_no, display_label=f'PDF p. {page_no}', metadata={'index_base': 1, 'physical_pages': True}), mapping_status='verified', confidence=confidence, evidence=(SourceCatalogEvidence(method=line.source, source_locator=f'pdf:heading-region:{page_no}', page_start=page_no, page_end=page_no, excerpt=title, confidence=confidence),), metadata={'codex_may_reject': True}))
    return candidates[:MAX_DIRECTORY_NODES]

def _extract_docx(path: Path, *, fallback_title: str) -> DirectoryExtraction:
    try:
        paragraphs = read_docx_paragraph_blocks(path, include_text=lambda style_name: re.search('(?:heading|标题|標題)\\s*(\\d+)', style_name, re.I) is not None)
    except OoxmlNavigationError as exc:
        raise DirectoryExtractionError(str(exc)) from exc
    candidates: list[DirectoryCandidate] = []
    paragraph_count = len(paragraphs)
    for paragraph in paragraphs:
        paragraph_index = paragraph.index
        style_name = paragraph.style_name
        match = re.search('(?:heading|标题|標題)\\s*(\\d+)', style_name, re.I)
        if match is None:
            continue
        title = _clean_title(paragraph.text)
        if not title:
            continue
        number, inferred_level = _heading_number_and_level(title)
        level = max(1, int(match.group(1)), inferred_level)
        candidates.append(DirectoryCandidate(local_key=f'docx-{paragraph_index}', title=title, number=number, level=level, order_index=len(candidates), source_locator=f'docx:paragraph:{paragraph_index}', source_range=SourceRange(kind='docx_paragraphs', start=paragraph_index, end=paragraph_index, display_label=f'Paragraph {paragraph_index + 1}', metadata={'index_base': 0}), mapping_status='verified', confidence=0.96, evidence=(SourceCatalogEvidence(method='docx_heading_style', source_locator=f'docx:paragraph:{paragraph_index}', excerpt=title, confidence=0.96, metadata={'style': style_name}),)))
    if not candidates and paragraph_count:
        candidates = [_whole_container_candidate(key='docx-document', title=fallback_title or path.stem, source_range=SourceRange(kind='docx_paragraphs', start=0, end=paragraph_count - 1, display_label='Whole document', metadata={'index_base': 0}), locator='docx:document', method='docx_document_boundary')]
    return DirectoryExtraction(candidates=tuple(_close_numeric_ranges(candidates, maximum=max(0, paragraph_count - 1), kind='docx_paragraphs')), metadata={'format': 'docx', 'paragraph_count': paragraph_count, 'paragraph_sequence': 'word_document_xml_v1', 'body_text_extracted': False})

class _NavigationHTMLParser(HTMLParser):

    def __init__(self, *, collect_links: bool=False) -> None:
        super().__init__(convert_charrefs=True)
        self.collect_links = collect_links
        self.headings: list[tuple[int, str, str]] = []
        self.links: list[tuple[str, str, int]] = []
        self._heading_level: int | None = None
        self._heading_id = ''
        self._heading_parts: list[str] = []
        self._link_href = ''
        self._link_parts: list[str] = []
        self._link_level = 1
        self._class_level: int | None = None
        self._class_level_tag = ''
        self._list_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.lower(): value or '' for name, value in attrs}
        match = re.fullmatch('h([1-6])', tag.lower())
        if match:
            self._heading_level = int(match.group(1))
            self._heading_id = attributes.get('id', '')
            self._heading_parts = []
        if tag.lower() in {'ol', 'ul'}:
            self._list_depth += 1
        class_level = _toc_class_level(attributes.get('class', ''))
        if class_level is not None and tag.lower() in {'div', 'li', 'p', 'span'}:
            self._class_level = class_level
            self._class_level_tag = tag.lower()
        if self.collect_links and tag.lower() == 'a' and attributes.get('href'):
            self._link_href = attributes['href']
            self._link_parts = []
            self._link_level = self._class_level or max(1, self._list_depth)

    def handle_endtag(self, tag: str) -> None:
        if re.fullmatch('h[1-6]', tag.lower()) and self._heading_level is not None:
            title = _clean_title(' '.join(self._heading_parts))
            if title:
                self.headings.append((self._heading_level, title, self._heading_id))
            self._heading_level = None
            self._heading_id = ''
            self._heading_parts = []
        if self.collect_links and tag.lower() == 'a' and self._link_href:
            label = _clean_title(' '.join(self._link_parts))
            if label:
                self.links.append((self._link_href, label, self._link_level))
            self._link_href = ''
            self._link_parts = []
            self._link_level = 1
        if tag.lower() in {'ol', 'ul'}:
            self._list_depth = max(0, self._list_depth - 1)
        if tag.lower() == self._class_level_tag:
            self._class_level = None
            self._class_level_tag = ''

    def handle_data(self, data: str) -> None:
        if self._heading_level is not None:
            self._heading_parts.append(data)
        if self._link_href:
            self._link_parts.append(data)

def _toc_class_level(value: str) -> int | None:
    for token in str(value or '').split():
        match = re.fullmatch('(?:toc|level|indent)[_-]?(\\d{1,2})', token, re.I)
        if match:
            return max(1, min(12, int(match.group(1))))
    return None

def _extract_html(path: Path, *, fallback_title: str) -> DirectoryExtraction:
    parser = _NavigationHTMLParser()
    parser.feed(path.read_text(encoding='utf-8', errors='replace'))
    candidates: list[DirectoryCandidate] = []
    for order_index, (level, title, anchor) in enumerate(parser.headings[:MAX_DIRECTORY_NODES]):
        number, inferred_level = _heading_number_and_level(title)
        next_boundary = next((index for index in range(order_index + 1, len(parser.headings)) if parser.headings[index][0] <= level), len(parser.headings))
        end_ordinal = max(order_index, next_boundary - 1)
        start_anchor = anchor
        end_anchor = parser.headings[next_boundary][2] if next_boundary < len(parser.headings) else ''
        candidates.append(DirectoryCandidate(local_key=f'html-{order_index}', title=title, number=number, level=max(level, inferred_level), order_index=order_index, source_locator=f'html:{path.name}#{start_anchor}' if start_anchor else f'html:{path.name}:heading:{order_index}', source_range=SourceRange(kind='dom_anchor', start=order_index, end=end_ordinal, container=path.name, start_anchor=start_anchor, end_anchor=end_anchor, display_label=f'#{start_anchor}' if start_anchor else f'Heading {order_index + 1}', metadata={'index_base': 0, 'heading_ordinal': order_index, 'end_heading_ordinal': next_boundary}), mapping_status='verified', confidence=0.94 if anchor else 0.84, evidence=(SourceCatalogEvidence(method='html_heading', source_locator=f'html:{path.name}#{start_anchor}' if start_anchor else f'html:{path.name}:heading:{order_index}', excerpt=title, confidence=0.94 if anchor else 0.84),)))
    if not candidates:
        candidates = [_whole_container_candidate(key='html-document', title=fallback_title or path.stem, source_range=SourceRange(kind='dom_anchor', container=path.name, display_label='Whole document', metadata={'root': True}), locator=f'html:{path.name}', method='html_document_boundary')]
    return DirectoryExtraction(candidates=tuple(candidates), metadata={'format': 'html', 'body_text_extracted': False})

def _extract_pptx(path: Path) -> DirectoryExtraction:
    candidates: list[DirectoryCandidate] = []
    with SafeSourceArchive(path) as archive:
        try:
            slide_names = ordered_pptx_slide_parts(archive)
        except OoxmlNavigationError as exc:
            raise DirectoryExtractionError(str(exc)) from exc
        for slide_no, name in enumerate(slide_names, start=1):
            root = parse_untrusted_xml(archive.read(name))
            title = next((_clean_title(str(node.text or '')) for node in root.iter() if node.tag.endswith('}t') and _clean_title(str(node.text or ''))), f'Slide {slide_no}')
            candidates.append(DirectoryCandidate(local_key=f'pptx-{slide_no}', title=title, level=1, order_index=len(candidates), source_locator=f'pptx:slide:{slide_no}', source_range=SourceRange(kind='ppt_slides', start=slide_no, end=slide_no, display_label=f'Slide {slide_no}', metadata={'index_base': 1}), mapping_status='verified', confidence=0.94, evidence=(SourceCatalogEvidence(method='pptx_slide_title', source_locator=f'pptx:slide:{slide_no}', excerpt=title, confidence=0.94),)))
    return DirectoryExtraction(candidates=tuple(candidates), page_count=len(candidates), inspected_page_count=len(candidates), metadata={'format': 'pptx', 'slide_count': len(candidates), 'slide_sequence': 'presentation_sldId_v1', 'body_text_extracted': False})

def _extract_xlsx(path: Path) -> DirectoryExtraction:
    candidates: list[DirectoryCandidate] = []
    with SafeSourceArchive(path) as archive:
        names = set(archive.namelist())
        sheets = _xlsx_workbook_sheets(archive, names)
        for sheet_index, (title, sheet_path) in enumerate(sheets):
            root = parse_untrusted_xml(archive.read(sheet_path))
            last_row = 1
            dimension = next((node for node in root.iter() if _xml_local_name(node.tag) == 'dimension'), None)
            if dimension is not None:
                reference = str(dimension.attrib.get('ref') or '')
                row_numbers = [int(value) for value in re.findall('\\d+', reference)]
                if row_numbers:
                    last_row = max(row_numbers)
            if last_row == 1:
                row_numbers = [int(node.attrib['r']) for node in root.iter() if _xml_local_name(node.tag) == 'row' and str(node.attrib.get('r') or '').isdigit()]
                if row_numbers:
                    last_row = max(row_numbers)
            candidates.append(DirectoryCandidate(local_key=f'xlsx-{sheet_index}', title=title, level=1, order_index=sheet_index, source_locator=f'xlsx:sheet:{sheet_index + 1}', source_range=SourceRange(kind='sheet_rows', start=1, end=max(1, last_row), container=title, display_label=f'{title}!1:{max(1, last_row)}', metadata={'index_base': 1, 'sheet_index': sheet_index, 'sheet_path': sheet_path}), mapping_status='verified', confidence=1.0, evidence=(SourceCatalogEvidence(method='xlsx_workbook_sheet', source_locator=f'xlsx:sheet:{sheet_index + 1}', excerpt=title, confidence=1.0),)))
    return DirectoryExtraction(candidates=tuple(candidates), metadata={'format': 'xlsx', 'sheet_count': len(candidates), 'body_text_extracted': False})

def _xlsx_workbook_sheets(archive: SafeSourceArchive, names: set[str]) -> list[tuple[str, str]]:
    workbook_name = 'xl/workbook.xml'
    relationships_name = 'xl/_rels/workbook.xml.rels'
    if workbook_name in names and relationships_name in names:
        workbook = parse_untrusted_xml(archive.read(workbook_name))
        relationships = parse_untrusted_xml(archive.read(relationships_name))
        targets = {str(node.attrib.get('Id') or ''): str(node.attrib.get('Target') or '') for node in relationships.iter() if _xml_local_name(node.tag) == 'Relationship' and str(node.attrib.get('TargetMode') or '').lower() != 'external'}
        sheets: list[tuple[str, str]] = []
        for node in workbook.iter():
            if _xml_local_name(node.tag) != 'sheet':
                continue
            title = str(node.attrib.get('name') or '').strip()
            relationship_id = next((str(value) for key, value in node.attrib.items() if key == 'r:id' or key.split('}')[-1] == 'id'), '')
            target = targets.get(relationship_id, '')
            if not title or not target:
                continue
            normalized_target = unquote(target).replace('\\', '/').lstrip('/')
            if not normalized_target.startswith('xl/'):
                normalized_target = posixpath.normpath(posixpath.join('xl', normalized_target))
            if normalized_target in names:
                sheets.append((title, normalized_target))
        if sheets:
            return sheets
    sheet_files = sorted((name for name in names if re.fullmatch('xl/worksheets/sheet\\d+\\.xml', name)), key=lambda name: int(re.search('(\\d+)', Path(name).stem).group(1)))
    return [(f'Sheet {index + 1}', name) for index, name in enumerate(sheet_files)]

def _xml_local_name(tag: object) -> str:
    return str(tag).split('}')[-1]

def _extract_csv(path: Path, *, fallback_title: str) -> DirectoryExtraction:
    row_count = 0
    section_rows: list[tuple[int, str, int, str]] = []
    with path.open('r', encoding='utf-8-sig', errors='replace', newline='') as handle:
        for row_no, row in enumerate(csv.reader(handle), start=1):
            row_count = row_no
            nonempty = [cell.strip() for cell in row if cell.strip()]
            if len(nonempty) != 1:
                continue
            marker = parse_structural_heading(nonempty[0])
            if marker is not None:
                section_rows.append((row_no, nonempty[0], marker.level, marker.number))
    candidates: list[DirectoryCandidate] = []
    if row_count:
        candidates.append(_whole_container_candidate(key='csv-table', title=fallback_title or path.stem, source_range=SourceRange(kind='sheet_rows', start=1, end=row_count, container=path.name, display_label=f'Rows 1-{row_count}', metadata={'index_base': 1}), locator=f'csv:{path.name}', method='csv_row_boundary'))
    for row_no, title, level, number in section_rows[:MAX_DIRECTORY_NODES - len(candidates)]:
        candidates.append(DirectoryCandidate(local_key=f'csv-{row_no}', title=title, number=number, level=max(2, level + 1 if candidates else level), order_index=len(candidates), source_locator=f'csv:row:{row_no}', source_range=SourceRange(kind='sheet_rows', start=row_no, end=row_no, container=path.name, display_label=f'Row {row_no}', metadata={'index_base': 1}), mapping_status='verified', confidence=0.9, evidence=(SourceCatalogEvidence(method='csv_structural_row', source_locator=f'csv:row:{row_no}', excerpt=title, confidence=0.9),)))
    return DirectoryExtraction(candidates=tuple(_close_numeric_ranges(candidates, maximum=max(1, row_count), kind='sheet_rows')), metadata={'format': 'csv', 'row_count': row_count, 'body_text_extracted': False})

def _extract_text(path: Path, *, fallback_title: str, markdown: bool) -> DirectoryExtraction:
    candidates: list[DirectoryCandidate] = []
    line_count = 0
    previous_line = ''
    with path.open('r', encoding='utf-8', errors='replace') as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            line_count = line_no
            line = raw_line.strip()
            title = ''
            level = 1
            if markdown:
                match = re.match('^(#{1,6})\\s+(.+?)\\s*#*\\s*$', line)
                if match:
                    level = len(match.group(1))
                    title = _clean_title(match.group(2))
                elif re.fullmatch('(?:=+|-+)', line) and previous_line:
                    title = _clean_title(previous_line)
                    level = 1 if line.startswith('=') else 2
            if not title:
                marker = parse_structural_heading(line)
                if marker is not None:
                    title = _clean_title(line)
                    level = marker.level
            if title:
                number, inferred_level = _heading_number_and_level(title)
                candidates.append(DirectoryCandidate(local_key=f'text-{line_no}', title=title, number=number, level=max(level, inferred_level), order_index=len(candidates), source_locator=f'text:line:{line_no}', source_range=SourceRange(kind='text_lines', start=line_no, end=line_no, display_label=f'Lines {line_no}', metadata={'index_base': 1}), mapping_status='verified', confidence=0.96 if markdown and raw_line.lstrip().startswith('#') else 0.86, evidence=(SourceCatalogEvidence(method='markdown_heading' if markdown else 'text_structural_heading', source_locator=f'text:line:{line_no}', excerpt=title, confidence=0.96 if markdown else 0.86),)))
            previous_line = line
    if not candidates and line_count:
        candidates = [_whole_container_candidate(key='text-document', title=fallback_title or path.stem, source_range=SourceRange(kind='text_lines', start=1, end=line_count, display_label=f'Lines 1-{line_count}', metadata={'index_base': 1}), locator='text:document', method='text_line_boundary')]
    return DirectoryExtraction(candidates=tuple(_close_numeric_ranges(candidates, maximum=max(1, line_count), kind='text_lines')), metadata={'format': 'markdown' if markdown else 'text', 'line_count': line_count, 'body_text_extracted': False})

def _extract_epub_native_navigation(path: Path) -> DirectoryExtraction:
    """EPUB toc.ncx / nav.xhtml → spine/XHTML anchors only (no spine-only fallback)."""
    candidates: list[DirectoryCandidate] = []
    warnings: list[str] = []
    with SafeSourceArchive(path) as archive:
        names = archive.namelist()
        name_set = set(names)
        spine = _epub_spine(archive)
        spine_index = {name: index for index, name in enumerate(spine)}
        nav_entries = _epub_nav_entries(archive, names)
        native_navigation_count = len(nav_entries)
        if native_navigation_count < 1:
            return DirectoryExtraction(candidates=(), warnings=('No EPUB toc.ncx / nav.xhtml body index was found.',), metadata={'format': 'epub', 'directory_source': 'native_navigation', 'spine_count': len(spine), 'body_text_extracted': False, 'native_navigation_count': 0, 'native_body_index': False})
        navigation_truncated = native_navigation_count > MAX_DIRECTORY_NODES
        if navigation_truncated:
            warnings.append('EPUB native navigation was truncated at the structural node limit; the published catalog is partial.')
        anchor_names_by_target: dict[str, set[str] | None] = {}
        navigation_resolution_limit = MAX_DIRECTORY_NODES + 1 if navigation_truncated else MAX_DIRECTORY_NODES
        for order_index, (target, fragment, title, level) in enumerate(nav_entries[:navigation_resolution_limit]):
            index = spine_index.get(target)
            decoded_fragment = unquote(fragment)
            target_exists = target in name_set
            anchor_verified = not decoded_fragment
            if decoded_fragment and target_exists:
                if target not in anchor_names_by_target:
                    anchor_names_by_target[target] = _epub_xhtml_anchor_names(archive, target)
                anchor_names = anchor_names_by_target[target]
                anchor_verified = anchor_names is not None and decoded_fragment in anchor_names
            range_verified = index is not None and target_exists and anchor_verified
            source_range = SourceRange(kind='epub_spine', start=index, end=index, container=target, start_anchor=decoded_fragment, display_label=f"{target}{('#' + decoded_fragment if decoded_fragment else '')}", metadata={'index_base': 0, 'href': target}) if range_verified else None
            number, _inferred_level = _heading_number_and_level(title)
            native_level = max(1, min(12, level))
            source_locator = f"epub:{target}{('#' + decoded_fragment if decoded_fragment else '')}"
            candidates.append(DirectoryCandidate(local_key=f'epub-{order_index}', title=title, number=number, level=native_level, order_index=order_index, source_locator=source_locator, source_range=source_range, mapping_status='verified' if source_range is not None else 'unmapped', confidence=0.98 if decoded_fragment and source_range is not None else 0.88 if source_range is not None else 0.5, evidence=(SourceCatalogEvidence(method='epub_navigation', source_locator=source_locator, excerpt=title, confidence=0.98 if decoded_fragment else 0.88),), metadata={'navigation_provenance': 'native', 'hierarchy_locked': True, 'native_level': native_level, 'fragment_validation': 'verified_id_or_name' if decoded_fragment and range_verified else 'missing_id_or_name' if decoded_fragment else 'not_required'}))
        closed_candidates = _close_epub_ranges(candidates, maximum=max(0, len(spine) - 1))
        if navigation_truncated:
            closed_candidates = _demote_epub_ranges_open_at_truncation(candidates, closed_candidates, published_count=MAX_DIRECTORY_NODES)
        return DirectoryExtraction(candidates=tuple(closed_candidates[:MAX_DIRECTORY_NODES]), warnings=tuple(warnings), metadata={'format': 'epub', 'directory_source': 'native_navigation', 'spine_count': len(spine), 'body_text_extracted': False, 'native_navigation_count': native_navigation_count, 'published_navigation_count': min(native_navigation_count, MAX_DIRECTORY_NODES), 'navigation_truncated': navigation_truncated, 'navigation_node_limit': MAX_DIRECTORY_NODES, 'anchor_validation': 'xhtml_id_name_attributes_only', 'anchor_document_count': len(anchor_names_by_target), 'native_body_index': True})

def _extract_epub(path: Path) -> DirectoryExtraction:
    native = _extract_epub_native_navigation(path)
    if native.candidates:
        return native
    candidates: list[DirectoryCandidate] = []
    warnings: list[str] = list(native.warnings)
    with SafeSourceArchive(path) as archive:
        spine = _epub_spine(archive)
        for index, name in enumerate(spine[:MAX_DIRECTORY_NODES]):
            title = _epub_spine_title(archive, name) or Path(name).stem
            candidates.append(_whole_container_candidate(key=f'epub-spine-{index}', title=title, source_range=SourceRange(kind='epub_spine', start=index, end=index, container=name, display_label=name, metadata={'index_base': 0, 'href': name}), locator=f'epub:{name}', method='epub_spine_item'))
        closed_candidates = _close_epub_ranges(candidates, maximum=max(0, len(spine) - 1))
        return DirectoryExtraction(candidates=tuple(closed_candidates[:MAX_DIRECTORY_NODES]), warnings=tuple(warnings), metadata={'format': 'epub', 'directory_source': 'spine_only', 'spine_count': len(spine), 'body_text_extracted': False, 'native_navigation_count': 0, 'published_navigation_count': 0, 'navigation_truncated': False, 'navigation_node_limit': MAX_DIRECTORY_NODES, 'anchor_validation': 'xhtml_id_name_attributes_only', 'anchor_document_count': 0, 'native_body_index': False})

class _EpubAnchorHTMLParser(HTMLParser):
    """Collect only XHTML tag anchors; discard all document text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchor_names: set[str] = set()

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name.lower() in {'id', 'name'} and value:
                self.anchor_names.add(value)

def _epub_xhtml_anchor_names(archive: SafeSourceArchive, target: str) -> set[str] | None:
    parser = _EpubAnchorHTMLParser()
    try:
        parser.feed(archive.read(target).decode('utf-8', errors='replace'))
    except Exception:
        return None
    return parser.anchor_names

def _epub_spine(archive: SafeSourceArchive) -> list[str]:
    try:
        container = parse_untrusted_xml(archive.read('META-INF/container.xml'))
    except Exception:
        return []
    rootfile = next((str(node.attrib.get('full-path') or '') for node in container.iter() if node.tag.split('}')[-1] == 'rootfile'), '')
    if not rootfile:
        return []
    try:
        package = parse_untrusted_xml(archive.read(rootfile))
    except Exception:
        return []
    base = posixpath.dirname(rootfile)
    manifest: dict[str, str] = {}
    order: list[str] = []
    for node in package.iter():
        local = node.tag.split('}')[-1]
        if local == 'item':
            item_id = str(node.attrib.get('id') or '')
            href = str(node.attrib.get('href') or '')
            if item_id and href:
                manifest[item_id] = posixpath.normpath(posixpath.join(base, href)).lstrip('/')
        elif local == 'itemref' and node.attrib.get('idref'):
            order.append(str(node.attrib['idref']))
    return [manifest[item_id] for item_id in order if item_id in manifest]

def _epub_nav_entries(archive: SafeSourceArchive, names: list[str]) -> list[tuple[str, str, str, int]]:
    entries: list[tuple[str, str, str, int]] = []
    nav_names = [name for name in names if name.lower().endswith(('.xhtml', '.html', '.htm')) and (Path(name).stem.casefold() in {'nav', 'toc'} or Path(name).stem.casefold().endswith(('_nav', '-nav', '_toc', '-toc')))]
    for name in nav_names:
        parser = _NavigationHTMLParser(collect_links=True)
        try:
            parser.feed(archive.read(name).decode('utf-8', errors='replace'))
        except Exception:
            continue
        base = posixpath.dirname(name)
        for href, label, level in parser.links:
            target, separator, fragment = html.unescape(href).partition('#')
            target = posixpath.normpath(posixpath.join(base, unquote(target))).lstrip('/')
            if target and (not target.startswith('../')):
                entries.append((target, fragment if separator else '', label, level))
    if entries:
        return _dedupe_epub_entries(entries)
    for name in [value for value in names if value.lower().endswith('.ncx')]:
        try:
            root = parse_untrusted_xml(archive.read(name))
        except Exception:
            continue
        base = posixpath.dirname(name)

        def visit(parent: object, level: int=1) -> None:
            for point in list(parent):
                if point.tag.split('}')[-1] != 'navPoint':
                    continue
                label = next((str(node.text or '') for node in point.iter() if node.tag.split('}')[-1] == 'text' and str(node.text or '').strip()), '')
                source = next((str(node.attrib.get('src') or '') for node in point.iter() if node.tag.split('}')[-1] == 'content'), '')
                target, separator, fragment = source.partition('#')
                target = posixpath.normpath(posixpath.join(base, unquote(target))).lstrip('/')
                if label and target and (not target.startswith('../')):
                    entries.append((target, fragment if separator else '', _clean_title(label), level))
                visit(point, level + 1)
        nav_map = next((node for node in root.iter() if node.tag.split('}')[-1] == 'navMap'), None)
        if nav_map is not None:
            visit(nav_map)
    return _dedupe_epub_entries(entries)

def _dedupe_epub_entries(entries: Iterable[tuple[str, str, str, int]]) -> list[tuple[str, str, str, int]]:
    result: list[tuple[str, str, str, int]] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in entries:
        key = (entry[0], entry[1], _normalize_title(entry[2]))
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return result

def _expand_numeric_parent_ranges(candidates: list[DirectoryCandidate], *, kind: str) -> list[DirectoryCandidate]:
    """Ensure every verified native parent contains its verified descendants."""
    expanded = list(candidates)
    for index in range(len(expanded) - 1, -1, -1):
        parent = expanded[index]
        parent_range = parent.source_range
        if parent.mapping_status != 'verified' or parent_range is None or parent_range.kind != kind or (not isinstance(parent_range.start, int)) or (not isinstance(parent_range.end, int)):
            continue
        descendants: list[SourceRange] = []
        for candidate in expanded[index + 1:]:
            if candidate.level <= parent.level:
                break
            source_range = candidate.source_range
            if candidate.mapping_status == 'verified' and source_range is not None and (source_range.kind == kind) and isinstance(source_range.start, int) and isinstance(source_range.end, int):
                descendants.append(source_range)
        if not descendants:
            continue
        start = min(parent_range.start, *(item.start for item in descendants))
        end = max(parent_range.end, *(item.end for item in descendants))
        if (start, end) == (parent_range.start, parent_range.end):
            continue
        range_for_label = parent_range.model_copy(update={'start': start})
        expanded[index] = replace(parent, source_range=parent_range.model_copy(update={'start': start, 'end': end, 'display_label': _range_display_label(range_for_label, end=end)}), metadata={**parent.metadata, 'range_expanded_for_descendants': True})
    return expanded

def _epub_spine_title(archive: SafeSourceArchive, name: str) -> str:
    try:
        parser = _NavigationHTMLParser()
        parser.feed(archive.read(name).decode('utf-8', errors='replace'))
    except Exception:
        return ''
    return parser.headings[0][1] if parser.headings else ''

def _extract_json(path: Path, *, fallback_title: str) -> DirectoryExtraction:
    try:
        value = json.loads(path.read_text(encoding='utf-8', errors='strict'))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise DirectoryExtractionError('JSON source is invalid.') from exc
    candidates: list[DirectoryCandidate] = []

    def visit(node: object, path_parts: list[str], level: int) -> None:
        if len(candidates) >= MAX_DIRECTORY_NODES or level > 12:
            return
        if isinstance(node, dict):
            items = list(node.items())
        elif isinstance(node, list):
            items = [(f'[{index}]', child) for index, child in enumerate(node)]
        else:
            return
        for key, child in items:
            if len(candidates) >= MAX_DIRECTORY_NODES:
                return
            child_path = [*path_parts, str(key)]
            locator = '/' + '/'.join((part.replace('~', '~0').replace('/', '~1') for part in child_path))
            candidates.append(DirectoryCandidate(local_key=f'json-{len(candidates)}', title=str(key), level=max(1, level), order_index=len(candidates), source_locator=f'json:{locator}', source_range=SourceRange(kind='structured_path', path=child_path, display_label=locator, metadata={'syntax': 'json_pointer'}), mapping_status='verified', confidence=1.0, evidence=(SourceCatalogEvidence(method='json_structure', source_locator=f'json:{locator}', excerpt=str(key), confidence=1.0),)))
            visit(child, child_path, level + 1)
    visit(value, [], 1)
    if not candidates:
        candidates = [_whole_container_candidate(key='json-document', title=fallback_title or path.stem, source_range=SourceRange(kind='structured_path', path=[], display_label='/', metadata={'syntax': 'json_pointer', 'root': True}), locator='json:/', method='json_root')]
    warnings = ('JSON directory was truncated at the structural node limit.',) if len(candidates) >= MAX_DIRECTORY_NODES else ()
    return DirectoryExtraction(candidates=tuple(candidates), warnings=warnings, metadata={'format': 'json', 'body_text_extracted': False})

def _extract_xml(path: Path, *, fallback_title: str) -> DirectoryExtraction:
    try:
        root = parse_untrusted_xml(path.read_bytes())
    except Exception as exc:
        raise DirectoryExtractionError('XML source is invalid or unsafe.') from exc
    candidates: list[DirectoryCandidate] = []

    def local_name(tag: str) -> str:
        return tag.split('}')[-1]

    def visit(node: object, path_parts: list[str], level: int) -> None:
        if len(candidates) >= MAX_DIRECTORY_NODES or level > 12:
            return
        sibling_counts: Counter[str] = Counter()
        for child in list(node):
            name = local_name(str(child.tag))
            sibling_counts[name] += 1
            indexed_name = f'{name}[{sibling_counts[name]}]'
            child_path = [*path_parts, indexed_name]
            qualifier = str(child.attrib.get('id') or child.attrib.get('name') or '').strip()
            title = f'{name} — {qualifier}' if qualifier else name
            xpath = '/' + '/'.join(child_path)
            candidates.append(DirectoryCandidate(local_key=f'xml-{len(candidates)}', title=title, level=max(1, level), order_index=len(candidates), source_locator=f'xml:{xpath}', source_range=SourceRange(kind='structured_path', path=child_path, display_label=xpath, metadata={'syntax': 'indexed_xml_path'}), mapping_status='verified', confidence=1.0, evidence=(SourceCatalogEvidence(method='xml_structure', source_locator=f'xml:{xpath}', excerpt=title, confidence=1.0),)))
            visit(child, child_path, level + 1)
            if len(candidates) >= MAX_DIRECTORY_NODES:
                return
    root_name = local_name(root.tag)
    candidates.append(_whole_container_candidate(key='xml-root', title=root_name or fallback_title or path.stem, source_range=SourceRange(kind='structured_path', path=[root_name] if root_name else [], display_label=f'/{root_name}' if root_name else '/', metadata={'syntax': 'indexed_xml_path'}), locator=f'xml:/{root_name}' if root_name else 'xml:/', method='xml_root'))
    visit(root, [root_name] if root_name else [], 2)
    warnings = ('XML directory was truncated at the structural node limit.',) if len(candidates) >= MAX_DIRECTORY_NODES else ()
    return DirectoryExtraction(candidates=tuple(candidates), warnings=warnings, metadata={'format': 'xml', 'body_text_extracted': False})

def _close_numeric_ranges(candidates: list[DirectoryCandidate], *, maximum: int, kind: str) -> list[DirectoryCandidate]:
    result: list[DirectoryCandidate] = []
    for index, candidate in enumerate(candidates):
        source_range = candidate.source_range
        if source_range is None or source_range.kind != kind or (not isinstance(source_range.start, int)):
            result.append(candidate)
            continue
        boundary = next((following for following in candidates[index + 1:] if following.level <= candidate.level), None)
        boundary_range = boundary.source_range if boundary is not None else None
        if boundary is not None and (boundary.mapping_status != 'verified' or boundary_range is None or boundary_range.kind != kind or (not isinstance(boundary_range.start, int))):
            result.append(_demote_unverified_range_boundary(candidate, boundary))
            continue
        if boundary_range is not None and isinstance(boundary_range.start, int) and (boundary_range.start < source_range.start):
            result.append(replace(candidate, source_range=source_range.model_copy(update={'end': source_range.start, 'display_label': _range_display_label(source_range, end=source_range.start)}), metadata={**candidate.metadata, 'range_boundary_status': 'single_page_nonmonotonic_successor', 'range_boundary_local_key': boundary.local_key if boundary else ''}))
            continue
        next_start = boundary_range.start if boundary_range is not None else None
        end = maximum if next_start is None else max(source_range.start, next_start - 1)
        if kind == 'epub_spine' and source_range.start_anchor:
            end = max(source_range.start, min(maximum, end))
        display_label = _range_display_label(source_range, end=end)
        result.append(replace(candidate, source_range=source_range.model_copy(update={'end': end, 'display_label': display_label})))
    return _expand_numeric_parent_ranges(result, kind=kind)

def _close_epub_ranges(candidates: list[DirectoryCandidate], *, maximum: int) -> list[DirectoryCandidate]:
    result: list[DirectoryCandidate] = []
    for index, candidate in enumerate(candidates):
        source_range = candidate.source_range
        if source_range is None or source_range.kind != 'epub_spine' or (not isinstance(source_range.start, int)):
            result.append(candidate)
            continue
        boundary = next((following for following in candidates[index + 1:] if following.level <= candidate.level), None)
        boundary_range = boundary.source_range if boundary is not None else None
        if boundary is not None and (boundary.mapping_status != 'verified' or boundary_range is None or boundary_range.kind != 'epub_spine' or (not isinstance(boundary_range.start, int)) or (boundary_range.start < source_range.start)):
            result.append(_demote_unverified_range_boundary(candidate, boundary))
            continue
        next_start = boundary_range.start if boundary_range is not None else None
        boundary_anchor = boundary_range.start_anchor if boundary_range is not None else ''
        end = maximum if not isinstance(next_start, int) else max(source_range.start, next_start if boundary_anchor else next_start - 1)
        end_anchor = boundary_anchor if boundary_anchor else ''
        result.append(replace(candidate, source_range=source_range.model_copy(update={'end': end, 'end_anchor': end_anchor, 'display_label': source_range.display_label if source_range.start == end else f'EPUB spine {source_range.start}-{end}'})))
    return result

def _demote_epub_ranges_open_at_truncation(original: list[DirectoryCandidate], closed: list[DirectoryCandidate], *, published_count: int) -> list[DirectoryCandidate]:
    """Keep truncated EPUB ranges citable only when the lookahead closes them."""
    result = list(closed)
    published_end = min(published_count, len(original), len(result))
    for index in range(published_end):
        if result[index].mapping_status != 'verified':
            continue
        boundary = next((following for following in original[index + 1:] if following.level <= original[index].level), None)
        if boundary is not None:
            continue
        result[index] = replace(result[index], mapping_status='partial', confidence=min(result[index].confidence, 0.64), metadata={**result[index].metadata, 'range_boundary_status': 'navigation_truncated'})
    return result

def _demote_unverified_range_boundary(candidate: DirectoryCandidate, boundary: DirectoryCandidate) -> DirectoryCandidate:
    return replace(candidate, mapping_status='partial', confidence=min(candidate.confidence, 0.64), metadata={**candidate.metadata, 'range_boundary_status': 'unverified_successor', 'range_boundary_local_key': boundary.local_key})

def _range_display_label(source_range: SourceRange, *, end: int) -> str:
    start = source_range.start
    if not isinstance(start, int):
        return source_range.display_label
    if source_range.kind == 'pdf_pages':
        return f'PDF p. {start}' if start == end else f'PDF pp. {start}-{end}'
    if source_range.kind == 'text_lines':
        return f'Line {start}' if start == end else f'Lines {start}-{end}'
    if source_range.kind == 'docx_paragraphs':
        return f'Paragraph {start + 1}' if start == end else f'Paragraphs {start + 1}-{end + 1}'
    return source_range.display_label

def _whole_container_candidate(*, key: str, title: str, source_range: SourceRange, locator: str, method: str) -> DirectoryCandidate:
    return DirectoryCandidate(local_key=key, title=_clean_title(title) or 'Document', level=1, order_index=0, source_locator=locator, source_range=source_range, mapping_status='verified', confidence=1.0, evidence=(SourceCatalogEvidence(method=method, source_locator=locator, excerpt=_clean_title(title), confidence=1.0),))

def _heading_number_and_level(title: str) -> tuple[str, int]:
    marker = parse_structural_heading(title)
    if marker is not None:
        return (marker.number, marker.level)
    chinese_item = re.match('^([一二三四五六七八九十百]+)[、.]', title)
    if chinese_item:
        return (chinese_item.group(1), 3)
    section_exercise = re.match('^(?:习题|练习|exercises?)\\s*([0-9一二三四五六七八九十百]+)\\s*[-－—.]\\s*([0-9一二三四五六七八九十百]+)', title, flags=re.I)
    if section_exercise:
        return (f'{section_exercise.group(1)}-{section_exercise.group(2)}', 3)
    if re.match('^(?:本章小结|章末小结|拓展阅读|测试题|chapter\\s+summary)', title, flags=re.I):
        return ('', 2)
    decimal = re.match('^(\\d+(?:\\.\\d+)*)', title)
    if decimal:
        number = decimal.group(1)
        return (number, max(1, len(number.split('.'))))
    return ('', 1)

def _normalize_number(value: str) -> str:
    return re.sub('\\s+', '', value or '').casefold().strip('.。')

def _clean_title(value: str) -> str:
    return re.sub('\\s+', ' ', html.unescape(str(value or ''))).strip()

def _normalize_title(value: str) -> str:
    normalized = normalize_toc_text(_clean_title(value)).casefold()
    return re.sub('[^0-9a-z\\u3400-\\u9fff]+', '', normalized)

def _title_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if min(len(left), len(right)) >= 4 and (left in right or right in left):
        return 0.93
    return difflib.SequenceMatcher(a=left, b=right).ratio()

def _has_meaningful_pdf_heading_lines(lines: Iterable[_PdfHeadingLine]) -> bool:
    """Reject sparse PDF text layers such as page numbers and watermarks.

    Scanned PDFs commonly retain a tiny hidden text layer even though the page
    contents still need OCR.  A real TOC marker or a numbered heading with an
    actual title is enough to trust the native layer.  Otherwise require either
    one normal-cased title-like line or several non-numeric lines before
    suppressing heading-region OCR.
    """
    materialized = list(lines)
    if _pdf_page_has_toc_heading(materialized):
        return True
    title_like_lines: list[str] = []
    for line in materialized:
        text = _clean_title(line.text)
        if not text or text.isdigit():
            continue
        letters = re.findall('[A-Za-z\\u3400-\\u9fff]', text)
        if parse_structural_heading(text) is not None and len(letters) >= 2:
            return True
        if len(letters) >= 8 and (not (text.isascii() and text.isupper())):
            return True
        if letters:
            title_like_lines.append(text)
    combined = ' '.join(title_like_lines)
    return len(title_like_lines) >= 2 and len(re.findall('[A-Za-z\\u3400-\\u9fff]', combined)) >= 12

def _report(callback: CatalogProgressCallback | None, phase: str, progress: int) -> None:
    if callback is not None:
        callback(phase, progress)
