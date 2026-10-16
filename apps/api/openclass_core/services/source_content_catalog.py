"""Native non-PDF navigation, derived solely from original objects or snapshots."""
from __future__ import annotations
import csv
import hashlib
import io
import json
import re
import tempfile
from pathlib import Path
from openclass_core.models import SourceChapter, SourceRange, SourceStructure, SourceStructureQuality
from .source_archive import SafeSourceArchive
from .source_content_formats import SourceContentReadError, decode
from .source_content_office import _relationships, _sheet_drawings
from .source_content_web import _DOM, _nodes
from .source_content_media import _seconds
from .source_ooxml_navigation import ordered_pptx_slide_parts
from .source_range_reader import _epub_spine_items
from .source_xml import parse_untrusted_xml
from .google_sources.catalog_fallback import ensure_original_cache
from .google_sources.config import GoogleSourceError
from .google_sources.coverage import validate_output
from .google_sources.original_cache import original_cache_root
PIPELINE = 'source_original_catalog_v1'
MAX_NATIVE_NAV_ITEMS = 2000

def _candidate(title, kind, start=None, end=None, *, level=1, **range_values):
    return {'title': title, 'level': level, 'range': {'kind': kind, **({'start': start, 'end': end} if start is not None else {}), **range_values}}

def _local(tag):
    return str(tag).rsplit('}', 1)[-1]

def _text(node):
    return ''.join((child.text or '' for child in node.iter() if _local(child.tag) == 't')).strip()

def _heading_ranges(entries, count, kind):
    result = []
    for index, (start, title, level) in enumerate(entries):
        end = next((later_start - 1 for later_start, _, later_level in entries[index + 1:] if later_level <= level), count)
        result.append(_candidate(title, kind, start, end, level=level))
    return result

def _docx(path):
    with SafeSourceArchive(path) as archive:
        root = parse_untrusted_xml(archive.read('word/document.xml'))
        body = next((node for node in root if _local(node.tag) == 'body'), None)
        if body is None:
            raise SourceContentReadError('The original DOCX has no body.')
        paragraphs = [node for node in body.iter() if _local(node.tag) == 'p']
        styles = {}
        if 'word/styles.xml' in archive.namelist():
            for style in parse_untrusted_xml(archive.read('word/styles.xml')):
                style_id = next((value for key, value in style.attrib.items() if _local(key) == 'styleId'), '')
                outline = next((node for node in style.iter() if _local(node.tag) == 'outlineLvl'), None)
                if outline is not None:
                    value = next((value for key, value in outline.attrib.items() if _local(key) == 'val'), '9')
                    if value.isdigit() and 0 <= int(value) < 9:
                        styles[style_id] = int(value) + 1
        headings = []
        for index, paragraph in enumerate(paragraphs, 1):
            level = None
            for node in paragraph:
                if _local(node.tag) != 'pPr':
                    continue
                for property_node in node:
                    value = next((value for key, value in property_node.attrib.items() if _local(key) == 'val'), '')
                    if _local(property_node.tag) == 'outlineLvl' and value.isdigit() and (int(value) < 9):
                        level = int(value) + 1
                    elif _local(property_node.tag) == 'pStyle' and level is None:
                        level = styles.get(value)
            if level and _text(paragraph):
                headings.append((index, _text(paragraph), level))
        return (_heading_ranges(headings, len(paragraphs), 'docx_paragraphs') if headings else [_candidate('Document content', 'docx_paragraphs', 1, len(paragraphs))] if paragraphs else [], bool(headings))

def _office(path):
    if path.suffix.lower() == '.docx':
        return _docx(path)
    with SafeSourceArchive(path) as archive:
        if path.suffix.lower() == '.pptx':
            result = []
            for index, name in enumerate(ordered_pptx_slide_parts(archive), 1):
                root = parse_untrusted_xml(archive.read(name))
                title = ''
                for shape in root.iter():
                    if _local(shape.tag) == 'sp' and any((_local(node.tag) == 'ph' and node.attrib.get('type') in {'title', 'ctrTitle'} for node in shape.iter())):
                        title = _text(shape)
                        break
                result.append(_candidate(title or f'Slide {index}', 'ppt_slides', index, index))
            return (result, False)
        root = parse_untrusted_xml(archive.read('xl/workbook.xml'))
        rels = _relationships(archive, 'xl/workbook.xml')
        result = []
        for sheet in root.iter():
            if _local(sheet.tag) != 'sheet':
                continue
            relation = next((value for key, value in sheet.attrib.items() if 'relationships' in key), None)
            target, kind, external = rels.get(relation, ('', '', True))
            if external or kind != 'worksheet':
                raise SourceContentReadError('The workbook sheet relationship is invalid.')
            document = parse_untrusted_xml(archive.read(target))
            count = max((int(node.attrib['r']) for node in document.iter() if _local(node.tag) == 'row'), default=0)
            _, visual_extent = _sheet_drawings(archive, document, target)
            count = max(count, visual_extent)
            if count:
                result.append(_candidate(sheet.attrib['name'], 'sheet_rows', 1, count, container=sheet.attrib['name'], metadata={'whole_native_container': True}))
        return (result, False)

def _dom_catalog(text):
    parsed = _DOM()
    parsed.feed(text)
    candidates = []
    identifiers = [node['attributes'].get('id') for node in parsed.nodes]
    for node in parsed.nodes:
        anchor = node['attributes'].get('id')
        if node['tag'] not in {'section', 'article'} or not anchor or identifiers.count(anchor) != 1:
            continue
        heading = next((child for child in _nodes(node) if re.fullmatch('h[1-6]', child['tag'])), None)
        if heading:
            title = ''.join((child for descendant in _nodes(heading) for child in descendant['children'] if isinstance(child, str))).strip()
            if title:
                candidates.append(_candidate(title, 'dom_anchor', start_anchor=anchor))
    return (candidates or [_candidate('Document content', 'dom_anchor')], bool(candidates))

def _transcript_catalog(raw):
    result = []
    for line in decode(raw).splitlines():
        match = re.match('^\\[([^\\]]+?)\\s*(?:-->|–)\\s*([^\\]]+)\\]\\s*(.*)$', line)
        if not match:
            continue
        start, end = (_seconds(match[1]), _seconds(match[2]))
        if end <= start:
            raise SourceContentReadError('The transcript has invalid native time boundaries.')
        result.append(_candidate(f'{start:g}–{end:g} s', 'media_time', str(start), str(end)))
    return (result, False)

def _candidates(source, path, adapted):
    suffix = path.suffix.lower()
    if adapted.format == 'audio':
        if not adapted.parts:
            return ([], False)
        return _transcript_catalog(adapted.parts[0].content)
    if source.metadata.get('media_kind') == 'caption_transcript' or source.metadata.get('origin', {}).get('snapshot_kind') == 'caption_transcript':
        return _transcript_catalog(path.read_bytes())
    if suffix in {'.docx', '.pptx', '.xlsx', '.xlsm'}:
        return _office(path)
    if suffix == '.epub':
        with SafeSourceArchive(path) as archive:
            spine = _epub_spine_items(archive)
            result = []
            for index, name in enumerate(spine, 1):
                dom = _DOM()
                dom.feed(decode(archive.read(name)))
                title = next((''.join((child for child in node['children'] if isinstance(child, str))).strip() for node in dom.nodes if node['tag'] == 'title'), '')
                result.append(_candidate(title or f'Section {index}', 'epub_spine', index, index, container=name))
            return (result, False)
    if source.file_name.endswith('.webbundle.zip'):
        with SafeSourceArchive(path) as archive:
            return _dom_catalog(decode(archive.read('content.html')))
    if suffix in {'.html', '.htm'}:
        return _dom_catalog(decode(path.read_bytes()))
    if suffix in {'.csv', '.tsv'}:
        rows = list(csv.reader(io.StringIO(decode(path.read_bytes())), delimiter='\t' if suffix == '.tsv' else ','))
        return ([_candidate('Sheet1', 'sheet_rows', 1, len(rows), container='Sheet1')] if rows else [], False)
    if suffix == '.ipynb':
        cells = json.loads(path.read_bytes()).get('cells', [])
        return ([_candidate(f'Cell {index}', 'notebook_cells', index, index) for index in range(1, len(cells) + 1)], False)
    if adapted.format == 'image':
        count = sum((part.page_count for part in adapted.parts))
        return ([_candidate(f'Frame {index}', 'image_frames', index, index) for index in range(1, count + 1)], False)
    raw = decode(path.read_bytes())
    if suffix in {'.json', '.xml'}:
        if suffix == '.json':
            json.loads(raw)
        else:
            parse_untrusted_xml(raw.encode())
        return ([_candidate('Document content', 'structured_path')], False)
    lines = raw.splitlines()
    headings = []
    if suffix in {'.md', '.markdown'}:
        fenced = False
        for index, line in enumerate(lines, 1):
            if re.match('^\\s*(```|~~~)', line):
                fenced = not fenced
            match = re.match('^(#{1,6})\\s+(.+?)\\s*#*\\s*$', line) if not fenced else None
            if match:
                headings.append((index, match[2], len(match[1])))
    return (_heading_ranges(headings, len(lines), 'text_lines') if headings else [_candidate('Document content', 'text_lines', 1, len(lines))] if lines else [], bool(headings))

def build_native_content_catalog(service, source, adapted, outputs):
    """Create native navigation without using parsed blocks or RAG body text."""
    if adapted.format == 'pdf':
        raise GoogleSourceError('PDF catalogs require original-page inspection.')
    requested = {key: source.metadata[key] for key in ('catalog_version', 'catalog_model_selection') if key in source.metadata}
    service._check_cloud(source)
    source, path = ensure_original_cache(service, source)
    source = source.model_copy(update={'metadata': {**source.metadata, **requested}})
    original_path = path
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != source.metadata['content_hash'] or len(outputs) != len(adapted.parts):
        raise GoogleSourceError('The native catalog input does not match this source version.')
    for part, output in zip(adapted.parts, outputs, strict=True):
        validate_output(part, output)
        if not output.get('processing_complete'):
            raise GoogleSourceError('Native source catalog publication requires completed cloud processing.')
    provenance = {}
    with tempfile.TemporaryDirectory(prefix='native-catalog-', dir=original_cache_root()) as directory:
        legacy = Path(source.file_name).suffix.lower() in {'.doc', '.ppt', '.xls', '.odt', '.odp', '.ods', '.rtf'}
        if legacy:
            matching = [part for part in adapted.parts if part.locator.get('original_sha256') == digest and part.locator.get('conversion') == 'libreoffice' and (Path(part.name).suffix.lower() in {'.docx', '.pptx', '.xlsx'})]
            if len(matching) != 1:
                raise GoogleSourceError('The source has no unique native conversion snapshot.')
            snapshot = matching[0]
            path = Path(directory) / snapshot.name
            path.write_bytes(snapshot.content)
            provenance = {'conversion': 'libreoffice', 'snapshot_sha256': hashlib.sha256(snapshot.content).hexdigest()}
        candidates, authored = _candidates(source, path, adapted)
    service._check_cloud(source)
    if hashlib.sha256(original_path.read_bytes()).hexdigest() != digest:
        raise GoogleSourceError('The original changed during native catalog reading.')
    complete = len(candidates) <= MAX_NATIVE_NAV_ITEMS and bool(candidates)
    candidates = candidates[:MAX_NATIVE_NAV_ITEMS]
    version = int(source.metadata.get('catalog_version', 1))
    chapters, stack = ([], [])
    for index, item in enumerate(candidates):
        while stack and stack[-1][0] >= item['level']:
            stack.pop()
        identity = 'sourcechapter_' + hashlib.sha256(f'{source.id}:{digest}:{version}:{index}'.encode()).hexdigest()[:24]
        native_range = SourceRange.model_validate({**item['range'], 'metadata': {**item['range'].get('metadata', {}), 'source_pipeline': PIPELINE, 'source_version': source.metadata['source_version'], 'source_content_hash': digest, 'index_base': 1, 'evidence_method': 'original_native_structure', **provenance}})
        chapter = SourceChapter(id=identity, owner_user_id=source.owner_user_id, package_id=source.package_id, source_ingestion_id=source.id, title=item['title'], parent_id=stack[-1][1].id if stack else None, level=len(stack) + 1, path=[*(entry[1].title for entry in stack), item['title']], order_index=index, range=native_range, mapping_status='verified', anchor_status='verified', source_content_hash=digest, catalog_version=version, metadata={'origin': 'original_native_heading' if authored else 'original_content_structure', 'source_pipeline': PIPELINE, 'source_version': source.metadata['source_version'], **provenance})
        chapters.append(chapter)
        stack.append((item['level'], chapter))
    status = 'ready' if complete else 'partial' if chapters else 'unavailable'
    structure = SourceStructure(owner_user_id=source.owner_user_id, package_id=source.package_id, source_ingestion_id=source.id, status='ready' if chapters else 'linear_only', strategy='native_directory_v1', has_verified_toc=authored and complete, source_content_hash=digest, catalog_version=version, chapter_count=len(chapters), catalog_schema_version=PIPELINE, catalog_model='native_original_reader', quality=SourceStructureQuality(level='fully_verified' if complete else 'search_only', total_chapter_count=len(chapters), verified_chapter_count=len(chapters), verified_ratio=1.0 if chapters else 0.0), metadata={'source_backend': source.metadata.get('source_backend'), 'source_pipeline': PIPELINE, 'source_version': source.metadata['source_version'], 'catalog_mode': 'ordinary' if source.metadata.get('source_backend') == 'local' else 'cloud', 'catalog_label': 'Native headings' if authored else 'Content structure', 'catalog_status': status, 'directory_status': 'complete' if complete else 'incomplete', 'search_ready': True, 'catalog_warning': 'The native navigation limit was reached.' if not complete and chapters else ''})
    return (structure, chapters)
