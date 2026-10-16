from __future__ import annotations
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any
from openclass_core.services.image_ocr import OCRLineLayout, OCRPageLayout, extract_pdf_pages_layout
MAX_OCR_TOC_PAGES = 24
OCR_TOC_PROBE_BATCH_PAGES = 8
ROW_Y_TOLERANCE = 0.006
COLUMN_START_GAP = 0.2
COLUMN_GUTTER_PADDING = 0.015
PAGE_NUMBER_MIN_X_DELTA = 0.1
_CHINESE_ORDINAL = '0-9一二三四五六七八九十百千零〇两'
_TOC_HEADINGS = {'目录', '总目录', '章节目录', '目次', 'contents', 'tableofcontents', 'sommaire', 'inhalt'}

@dataclass(frozen=True)
class StructuralHeadingMarker:
    kind: str
    level: int
    number: str = ''
    marker: str = ''

@dataclass(frozen=True)
class PdfOutlineAnchor:
    title: str
    page_no: int
    level: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class PdfTocNode:
    title: str
    printed_page: int
    toc_page: int
    level: int = 1
    number: str = ''
    physical_page: int | None = None
    verified: bool = False
    confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class PdfTocExtraction:
    nodes: list[PdfTocNode] = field(default_factory=list)
    toc_page_start: int | None = None
    toc_page_end: int | None = None
    printed_page_offset: int | None = None
    mapping_support: int = 0
    warnings: list[str] = field(default_factory=list)

@dataclass
class _RawTocRow:
    title: str
    printed_page: int | None
    toc_page: int
    x: float
    height: float
    number: str = ''
    level: int = 1
    printed_page_inferred: bool = False

def normalize_toc_text(value: str) -> str:
    """Normalize PDF text-layer quirks without changing visible title content."""
    normalized = unicodedata.normalize('NFKC', value or '')
    result: list[str] = []
    index = 0
    while index < len(normalized):
        character = normalized[index]
        if unicodedata.category(character) != 'Co':
            result.append(character)
            index += 1
            continue
        end = index + 1
        while end < len(normalized) and normalized[end] == character:
            end += 1
        run_length = end - index
        result.append('…' * run_length if run_length >= 2 else character)
        index = end
    return ''.join(result)

def is_toc_heading(value: str) -> bool:
    normalized = re.sub('[^a-z\\u4e00-\\u9fff]+', '', normalize_toc_text(value).lower())
    return normalized in _TOC_HEADINGS

def parse_structural_heading(value: str) -> StructuralHeadingMarker | None:
    cleaned = _clean_title(normalize_toc_text(value))
    if not cleaned or is_toc_heading(cleaned):
        return None
    chinese = re.match(f'^(第\\s*([{_CHINESE_ORDINAL}]+)\\s*(章|篇|部|卷|单元|节))', cleaned)
    if chinese:
        unit = chinese.group(3)
        kind = 'section' if unit == '节' else 'chapter'
        return StructuralHeadingMarker(kind=kind, level=2 if kind == 'section' else 1, number=chinese.group(2), marker=chinese.group(1))
    english = re.match('^((chapter|part|book|unit|section|appendix)\\s+([0-9ivxlcdm]+(?:\\.[0-9]+)*|[a-z]))(?=\\s|[.:：、-]|$)', cleaned, flags=re.I)
    if english:
        token = english.group(2).lower()
        return StructuralHeadingMarker(kind='section' if token == 'section' else 'chapter', level=2 if token == 'section' else 1, number=english.group(3), marker=english.group(1))
    decimal = re.match('^(\\d+(?:\\.\\d+){0,8})(?=\\s|[.:：、-]|[\\u4e00-\\u9fff]|$)', cleaned)
    if decimal:
        number = decimal.group(1)
        return StructuralHeadingMarker(kind='numbered_heading', level=max(1, len(number.split('.'))), number=number, marker=number)
    appendix = re.match('^(附录)(?:\\s*([A-Z0-9一二三四五六七八九十]+))?', cleaned, flags=re.I)
    if appendix:
        return StructuralHeadingMarker(kind='appendix', level=1, number=appendix.group(2) or '', marker=appendix.group(0))
    return None

def extract_pdf_toc(path: Path, *, outline: list[PdfOutlineAnchor], page_count: int) -> PdfTocExtraction:
    toc_range = _toc_page_range(outline, page_count=page_count)
    if toc_range is None:
        return PdfTocExtraction()
    toc_page_start, toc_page_end = toc_range
    layouts = extract_pdf_pages_layout(path, page_start=toc_page_start, page_end=toc_page_end, max_pages=MAX_OCR_TOC_PAGES, trailing_column_pass=True)
    if not layouts:
        return PdfTocExtraction(toc_page_start=toc_page_start, toc_page_end=toc_page_end, warnings=['扫描目录页 OCR 未返回可用布局，已保留 PDF 原生书签。'])
    rows = _toc_rows(layouts)
    if not rows:
        return PdfTocExtraction(toc_page_start=toc_page_start, toc_page_end=toc_page_end, warnings=['扫描目录页未识别出标题与印刷页码对应关系，已保留 PDF 原生书签。'])
    _fill_missing_printed_pages(rows, outline)
    rows = [row for row in rows if row.printed_page is not None]
    _assign_levels(rows)
    offset, support = _printed_page_mapping(rows, outline)
    mapping_is_verified = offset is not None and support >= 2
    outline_by_title = {_normalized_title(anchor.title): anchor for anchor in outline if _normalized_title(anchor.title)}
    nodes: list[PdfTocNode] = []
    for row in rows:
        normalized_title = _normalized_title(row.title)
        matching_outline = outline_by_title.get(normalized_title)
        structural_marker = parse_structural_heading(row.title)
        physical_page: int | None = None
        verified = False
        confidence = 0.64
        verification = 'toc_candidate'
        if matching_outline is not None:
            physical_page = matching_outline.page_no
            verified = True
            confidence = 0.94
            verification = 'native_outline_anchor'
        elif mapping_is_verified and offset is not None and (row.printed_page is not None):
            mapped_page = row.printed_page + offset
            if 1 <= mapped_page <= page_count:
                physical_page = mapped_page
                verified = True
                confidence = 0.81 if row.printed_page_inferred else 0.87
                verification = 'verified_printed_page_mapping_inferred' if row.printed_page_inferred else 'verified_printed_page_mapping'
        nodes.append(PdfTocNode(title=matching_outline.title if matching_outline else row.title, number=row.number, level=row.level, printed_page=row.printed_page, toc_page=row.toc_page, physical_page=physical_page, verified=verified, confidence=confidence, metadata={'source': 'pdf_toc_ocr', 'printed_page': row.printed_page, 'toc_page': row.toc_page, 'verification': verification, 'printed_page_offset': offset, 'mapping_support': support, 'printed_page_inferred': row.printed_page_inferred, 'ocr_x': round(row.x, 4), 'outline_title': matching_outline.title if matching_outline else '', 'outline_page': matching_outline.page_no if matching_outline else None, 'structure_kind': structural_marker.kind if structural_marker else '', 'structure_marker': structural_marker.marker if structural_marker else ''}))
    warnings: list[str] = []
    if not mapping_is_verified:
        warnings.append('已识别完整目录候选，但印刷页与物理页映射不足；未绑定正文的节点不可直接引用。')
    return PdfTocExtraction(nodes=nodes, toc_page_start=toc_page_start, toc_page_end=toc_page_end, printed_page_offset=offset, mapping_support=support, warnings=warnings)

def extract_pdf_toc_from_range(path: Path, *, page_start: int, page_end: int) -> PdfTocExtraction:
    """Read a printed TOC when the PDF has no native outline anchors."""
    layouts = extract_pdf_pages_layout(path, page_start=page_start, page_end=page_end, max_pages=MAX_OCR_TOC_PAGES, trailing_column_pass=True)
    if not layouts:
        return PdfTocExtraction(toc_page_start=page_start, toc_page_end=page_end, warnings=['目录页版面 OCR 未返回可用结果，已回退到 PDF 文字层。'])
    rows = _toc_rows(layouts)
    _assign_levels(rows)
    nodes: list[PdfTocNode] = []
    for row in rows:
        structural_marker = parse_structural_heading(row.title)
        if structural_marker is None and _explicit_level(row.title, row.number) is None:
            continue
        nodes.append(PdfTocNode(title=row.title, number=row.number, level=row.level, printed_page=row.printed_page or 0, toc_page=row.toc_page, confidence=0.72, metadata={'source': 'pdf_toc_layout_ocr', 'printed_page': row.printed_page, 'toc_page': row.toc_page, 'verification': 'toc_candidate', 'ocr_x': round(row.x, 4), 'structure_kind': structural_marker.kind if structural_marker else '', 'structure_marker': structural_marker.marker if structural_marker else ''}))
    return PdfTocExtraction(nodes=nodes, toc_page_start=page_start, toc_page_end=page_end)

def probe_pdf_toc_from_leading_pages(path: Path, *, page_count: int, max_probe_pages: int) -> PdfTocExtraction:
    """Locate a scanned TOC by its repeated title-to-page layout.

    This is used only when native outlines, text-layer TOC headings, and the
    lightweight heading-region OCR probe found nothing. Requiring multiple
    structural rows with printed page numbers keeps ordinary body pages out of
    the directory path without relying on a language- or subject-specific title.
    """
    probe_end = min(page_count, max(1, max_probe_pages))
    for batch_start in range(1, probe_end + 1, OCR_TOC_PROBE_BATCH_PAGES):
        batch_end = min(probe_end, batch_start + OCR_TOC_PROBE_BATCH_PAGES - 1)
        extraction = extract_pdf_toc_from_range(path, page_start=batch_start, page_end=batch_end)
        candidate_pages = _structural_toc_candidate_pages(extraction)
        if not candidate_pages:
            continue
        start_page = candidate_pages[0]
        expanded_end = min(page_count, start_page + MAX_OCR_TOC_PAGES - 1)
        if batch_start != start_page or batch_end != expanded_end:
            extraction = extract_pdf_toc_from_range(path, page_start=start_page, page_end=expanded_end)
            candidate_pages = _structural_toc_candidate_pages(extraction)
            if not candidate_pages:
                continue
            start_page = candidate_pages[0]
        retained_pages = [start_page]
        for page_no in candidate_pages[1:]:
            if page_no > retained_pages[-1] + 1:
                break
            retained_pages.append(page_no)
        retained = [node for node in extraction.nodes if node.toc_page in retained_pages]
        return PdfTocExtraction(nodes=retained, toc_page_start=start_page, toc_page_end=retained_pages[-1], warnings=list(extraction.warnings))
    return PdfTocExtraction()

def _structural_toc_candidate_pages(extraction: PdfTocExtraction) -> list[int]:
    nodes_by_page: dict[int, list[PdfTocNode]] = {}
    for node in extraction.nodes:
        if node.printed_page < 1:
            continue
        nodes_by_page.setdefault(node.toc_page, []).append(node)
    return sorted((page_no for page_no, nodes in nodes_by_page.items() if len(nodes) >= 2 and sum((parse_structural_heading(node.title) is not None for node in nodes)) >= 2))

def _toc_page_range(outline: list[PdfOutlineAnchor], *, page_count: int) -> tuple[int, int] | None:
    ordered = sorted((anchor for anchor in outline if anchor.page_no > 0), key=lambda anchor: anchor.page_no)
    for index, anchor in enumerate(ordered):
        if not _is_toc_heading(anchor.title):
            continue
        next_page = next((candidate.page_no for candidate in ordered[index + 1:] if candidate.page_no > anchor.page_no), min(page_count + 1, anchor.page_no + MAX_OCR_TOC_PAGES))
        end_page = min(page_count, next_page - 1, anchor.page_no + MAX_OCR_TOC_PAGES - 1)
        return (anchor.page_no, max(anchor.page_no, end_page))
    return None

def _toc_rows(layouts: list[OCRPageLayout]) -> list[_RawTocRow]:
    rows: list[_RawTocRow] = []
    seen: set[tuple[str, int, str, int]] = set()
    for page in layouts:
        for grouped_lines in _group_page_rows(page.lines):
            row = _parse_layout_row(grouped_lines, toc_page=page.page_no)
            if row is None:
                continue
            structural_marker = parse_structural_heading(row.title)
            key = (_normalized_title(row.title), row.printed_page or -1, structural_marker.kind if structural_marker else '', structural_marker.level if structural_marker else row.level)
            if not key[0] or key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return rows

def _group_page_rows(lines: list[OCRLineLayout]) -> list[list[OCRLineLayout]]:
    columns = _split_page_columns(lines)
    return [group for column in columns for group in _group_column_rows(column)]

def _split_page_columns(lines: list[OCRLineLayout]) -> list[list[OCRLineLayout]]:
    structural_starts = sorted((line.x for line in lines if _printed_page_number(line.text) is None if parse_structural_heading(_clean_title_part(line.text)) is not None))
    if len(structural_starts) < 4:
        return [lines]
    clusters: list[list[float]] = []
    for start in structural_starts:
        if not clusters or start - clusters[-1][-1] >= COLUMN_START_GAP:
            clusters.append([start])
        else:
            clusters[-1].append(start)
    clusters = [cluster for cluster in clusters if len(cluster) >= 2]
    if len(clusters) < 2:
        return [lines]
    column_starts = [median(cluster) for cluster in clusters]
    boundaries = [max(column_starts[index], min(clusters[index + 1]) - COLUMN_GUTTER_PADDING) for index in range(len(clusters) - 1)]
    columns: list[list[OCRLineLayout]] = [[] for _ in clusters]
    for line in lines:
        column_index = next((index for index, boundary in enumerate(boundaries) if line.x < boundary), len(clusters) - 1)
        columns[column_index].append(line)
    return [column for column in columns if column]

def _group_column_rows(lines: list[OCRLineLayout]) -> list[list[OCRLineLayout]]:
    ordered = sorted(lines, key=lambda line: (-line.y, line.x))
    groups: list[list[OCRLineLayout]] = []
    group_y: list[float] = []
    for line in ordered:
        if not groups or abs(line.y - group_y[-1]) > ROW_Y_TOLERANCE:
            groups.append([line])
            group_y.append(line.y)
            continue
        groups[-1].append(line)
        group_y[-1] = sum((item.y for item in groups[-1])) / len(groups[-1])
    return [sorted(group, key=lambda line: line.x) for group in groups]

def _parse_layout_row(lines: list[OCRLineLayout], *, toc_page: int) -> _RawTocRow | None:
    visible = sorted((line for line in lines if line.text.strip()), key=lambda line: line.x)
    if not visible:
        return None
    title_anchor_x = min((line.x for line in visible if _printed_page_number(line.text) is None and (not _is_leader(_clean_title_part(line.text)))), default=min((line.x for line in visible)))
    page_line: OCRLineLayout | None = None
    printed_page: int | None = None
    for line in reversed(visible):
        parsed_page = _printed_page_number(line.text)
        if parsed_page is not None and line.x >= title_anchor_x + PAGE_NUMBER_MIN_X_DELTA:
            page_line = line
            printed_page = parsed_page
            break
    page_x = page_line.x if page_line is not None else max((line.x + line.width for line in visible)) + 0.001
    title_lines = [line for line in visible if line is not page_line and line.x < page_x and (_printed_page_number(line.text) is None)]
    title_lines = _dedupe_overlapping_title_lines(title_lines)
    title_parts: list[str] = []
    retained_title_lines: list[OCRLineLayout] = []
    for line in title_lines:
        part = _clean_title_part(line.text)
        if not part or _is_leader(part):
            continue
        title_parts.append(part)
        retained_title_lines.append(line)
    if not title_parts:
        return None
    title = _clean_title(' '.join(title_parts))
    if len(title) < 2 or len(title) > 180 or _printed_page_number(title) is not None:
        return None
    if printed_page is None:
        title, printed_page = _split_embedded_printed_page(title)
    if printed_page is None and parse_structural_heading(title) is None:
        return None
    x = min((line.x for line in retained_title_lines), default=0.0)
    height = max((line.height for line in retained_title_lines), default=0.0)
    return _RawTocRow(title=title, printed_page=printed_page, toc_page=toc_page, x=x, height=height, number=_chapter_number(title))

def _dedupe_overlapping_title_lines(lines: list[OCRLineLayout]) -> list[OCRLineLayout]:
    retained: list[OCRLineLayout] = []
    for line in sorted(lines, key=lambda item: item.width, reverse=True):
        key = _normalized_title(line.text) or re.sub('[^0-9a-z\\u4e00-\\u9fff]+', '', _clean_title(line.text).lower())
        if not key:
            continue
        line_end = line.x + line.width
        duplicate = False
        for existing in retained:
            existing_key = _normalized_title(existing.text) or re.sub('[^0-9a-z\\u4e00-\\u9fff]+', '', _clean_title(existing.text).lower())
            overlap = max(0.0, min(line_end, existing.x + existing.width) - max(line.x, existing.x))
            shorter_width = max(0.001, min(line.width, existing.width))
            contained_overlap = overlap / shorter_width >= 0.85
            text_overlap = key in existing_key or existing_key in key
            if contained_overlap and (text_overlap or existing.width >= line.width * 1.35):
                duplicate = True
                break
        if not duplicate:
            retained.append(line)
    return sorted(retained, key=lambda line: line.x)

def _assign_levels(rows: list[_RawTocRow]) -> None:
    rows_by_page: dict[int, list[_RawTocRow]] = {}
    for row in rows:
        rows_by_page.setdefault(row.toc_page, []).append(row)
    page_indents: dict[int, tuple[float, float]] = {}
    for page_no, page_rows in rows_by_page.items():
        explicit_root_x = [row.x for row in page_rows if _explicit_level(row.title, row.number) == 1]
        explicit_child_x = [row.x for row in page_rows if (_explicit_level(row.title, row.number) or 0) > 1]
        child_x = median(explicit_child_x) if explicit_child_x else 0.0
        root_x = median(explicit_root_x) if explicit_root_x else max(0.0, child_x - 0.016)
        if not explicit_child_x:
            child_x = root_x + 0.016
        page_indents[page_no] = (root_x, child_x)
    current_root = False
    for row in rows:
        root_x, child_x = page_indents[row.toc_page]
        explicit_level = _explicit_level(row.title, row.number)
        if explicit_level is not None:
            row.level = explicit_level
        elif current_root:
            row.level = 1 if abs(row.x - root_x) <= abs(row.x - child_x) else 2
        else:
            row.level = 1
        if row.level == 1:
            current_root = True

def _printed_page_mapping(rows: list[_RawTocRow], outline: list[PdfOutlineAnchor]) -> tuple[int | None, int]:
    outline_by_title = {_normalized_title(anchor.title): anchor for anchor in outline if _normalized_title(anchor.title)}
    offsets: list[int] = []
    for row in rows:
        if row.printed_page is None:
            continue
        anchor = outline_by_title.get(_normalized_title(row.title))
        if anchor is not None:
            offsets.append(anchor.page_no - row.printed_page)
    if not offsets:
        return (None, 0)
    offset, support = Counter(offsets).most_common(1)[0]
    return (offset, support)

def _fill_missing_printed_pages(rows: list[_RawTocRow], outline: list[PdfOutlineAnchor]) -> None:
    offset, _support = _printed_page_mapping(rows, outline)
    outline_by_title = {_normalized_title(anchor.title): anchor for anchor in outline if _normalized_title(anchor.title)}
    for row in rows:
        if row.printed_page is not None or offset is None:
            continue
        anchor = outline_by_title.get(_normalized_title(row.title))
        if anchor is not None:
            row.printed_page = max(1, anchor.page_no - offset)
            row.printed_page_inferred = True
    for index, row in enumerate(rows):
        if row.printed_page is not None:
            continue
        previous_page = next((candidate.printed_page for candidate in reversed(rows[:index]) if candidate.printed_page is not None), None)
        next_page = next((candidate.printed_page for candidate in rows[index + 1:] if candidate.printed_page is not None), None)
        if previous_page is not None and next_page is not None and (0 <= next_page - previous_page <= 2):
            row.printed_page = previous_page
            row.printed_page_inferred = True

def _explicit_level(title: str, number: str) -> int | None:
    structural_marker = parse_structural_heading(title)
    if structural_marker is not None:
        return structural_marker.level
    if re.match('^[A-Z](?:\\s+|(?=[\\u4e00-\\u9fff]))', title):
        return 2
    if number:
        return max(1, len(number.split('.')))
    return None

def _chapter_number(title: str) -> str:
    cleaned = _clean_title(title)
    structural_marker = parse_structural_heading(cleaned)
    if structural_marker is not None:
        return structural_marker.number
    decimal = re.match('^(\\d+(?:\\.\\d+){0,8})(?=\\s|[.:：、-]|[\\u4e00-\\u9fff]|$)', cleaned)
    if decimal:
        return decimal.group(1)
    arabic_chapter = re.match('^第\\s*(\\d+)\\s*[章节篇部]', cleaned)
    if arabic_chapter:
        return arabic_chapter.group(1)
    letter = re.match('^([A-Z])(?=\\s|[.:：、-]|[\\u4e00-\\u9fff])', cleaned)
    return letter.group(1) if letter else ''

def _printed_page_number(value: str) -> int | None:
    cleaned = normalize_toc_text(value).strip()
    match = re.fullmatch('[.．·•⋯…:：\\s]*[（(\\[]?\\s*([0-9]{1,4})\\s*[）)\\]]?[.．·•⋯…\\s]*', cleaned)
    if not match:
        return None
    return int(match.group(1))

def _split_embedded_printed_page(value: str) -> tuple[str, int | None]:
    cleaned = normalize_toc_text(value).strip()
    match = re.match('^(?P<title>.*\\D)(?:\\s*[/\\\\|·•⋯…:：\'\\"‘’“”_-]+\\s*|\\s{2,})(?P<page>[0-9]{1,4})\\s*$', cleaned)
    if match is None:
        return (value, None)
    title = _clean_title(match.group('title'))
    if len(title) < 2:
        return (value, None)
    return (title, int(match.group('page')))

def _clean_title_part(value: str) -> str:
    normalized = normalize_toc_text(value)
    return re.sub('^[.．。·•⋯…\\s]+|[.．。·•⋯…\\s]+$', '', normalized).strip()

def _clean_title(value: str) -> str:
    normalized = normalize_toc_text(value)
    cleaned = re.sub('\\s*[=+•\\-]*\\s*[（(][A-Z0-9]{1,6}[）)]\\s*$', '', normalized, flags=re.I)
    cleaned = re.sub('[.．。·•⋯…！!+=\\-\\s]+$', '', cleaned)
    return re.sub('\\s+', ' ', cleaned).strip()

def _is_leader(value: str) -> bool:
    return bool(re.fullmatch('[.．。·•⋯…\\s]+', normalize_toc_text(value)))

def _normalized_title(value: str) -> str:
    cleaned = _clean_title(value).lower()
    cleaned = re.sub(f'^第\\s*[{_CHINESE_ORDINAL}]+\\s*[章节篇部卷单元]\\s*', '', cleaned)
    cleaned = re.sub('^(?:chapter|part|book|unit|section|appendix)\\s+[0-9a-zivxlcdm.]+\\s*', '', cleaned)
    cleaned = re.sub('^\\d+(?:\\.\\d+)*\\s*', '', cleaned)
    return re.sub('[^0-9a-z\\u4e00-\\u9fff]+', '', cleaned)

def _is_toc_heading(value: str) -> bool:
    return is_toc_heading(value)
