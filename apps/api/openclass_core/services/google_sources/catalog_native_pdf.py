"""Deterministic original-bookmark mapping; ambiguous entries never disappear."""
from __future__ import annotations
import hashlib
import unicodedata
from openclass_core.models import SourceChapter, SourceRange, SourceStructure, SourceStructureQuality
from openclass_core.services.source_pdf_native import _page_text, native_pages
from .config import GoogleSourceError
PIPELINE = 'source_original_catalog_v1'

def heading_text(value):
    return ''.join((char for char in unicodedata.normalize('NFKC', value).casefold() if not char.isspace()))

def heading_point(page, title, *, verify_visible=True):
    """Require one exact visible line/window at the authored destination."""
    from openclass_core.services import pdf_backend as fitz
    raw = page.get_text('dict', flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)
    lines = [line for block in raw['blocks'] for line in block.get('lines', []) if ''.join((span['text'] for span in line['spans'])).strip()]
    matches = {}
    for i in range(len(lines)):
        for length in range(1, 4):
            window = lines[i:i + length]
            if len(window) != length:
                continue
            text = ''.join((span['text'] for line in window for span in line['spans']))
            if heading_text(text) != heading_text(title):
                continue
            box = fitz.Rect(window[0]['bbox'])
            for line in window[1:]:
                box |= fitz.Rect(line['bbox'])
            if length > 1 and box.height > sum((fitz.Rect(line['bbox']).height for line in window)) + 50:
                continue
            if verify_visible and heading_text(_page_text(page, box) or '') != heading_text(title):
                continue
            key = tuple((round(value, 5) for value in box))
            matches[key] = box
    if len(matches) != 1:
        return None
    box = next(iter(matches.values()))
    return {'physical_page': page.number + 1, 'title': title, 'bbox': [box.x0 / page.rect.width, box.y0 / page.rect.height, box.x1 / page.rect.width, box.y1 / page.rect.height], 'heading_sha256': hashlib.sha256(heading_text(title).encode()).hexdigest()}

def native_range_clips(path, source, requested):
    """Reproduce native boundary proofs before text extraction or image cropping."""
    from openclass_core.services import pdf_backend as fitz
    metadata = requested.get('metadata', {})
    if metadata.get('boundary_precision') != 'verified_native_region':
        return None
    if metadata.get('source_content_hash') != source.metadata['content_hash'] or metadata.get('source_version') != source.metadata['source_version']:
        raise GoogleSourceError('The native PDF boundary belongs to another original version.')
    if hashlib.sha256(path.read_bytes()).hexdigest() != source.metadata['content_hash']:
        raise GoogleSourceError('The original PDF changed before native boundary validation.')
    points = metadata.get('native_boundary_points', {})
    if not isinstance(points, dict) or 'start' not in points or (not {'start', 'end'} >= set(points)):
        raise GoogleSourceError('The native chapter has no complete boundary proof.')
    original_range = metadata.get('native_boundary_range', {})
    if not (type(original_range.get('start')) is int and type(original_range.get('end')) is int and (original_range['start'] <= requested['start'] <= requested['end'] <= original_range['end'])):
        raise GoogleSourceError('The native read exceeds its verified original chapter range.')
    clips = {}
    with fitz.open(path) as document:
        for edge, point in points.items():
            number = original_range['start'] if edge == 'start' else original_range['end'] + int(bool(metadata.get('native_end_exclusive_page')))
            if point.get('physical_page') != number or not 1 <= number <= len(document):
                raise GoogleSourceError('The native chapter boundary is outside its original pages.')
            if point.get('proof_kind') == 'verified_basic_native_line':
                from openclass_core.services.source_basic_pdf_boundaries import verify_point
                if verify_point(document[number - 1], point) != point:
                    raise GoogleSourceError('The native cached chapter boundary changed before original reading.')
            elif point.get('proof_kind') in {'verified_original_page_role', 'verified_original_visual_heading'}:
                from openclass_core.services.pdf_page_visual_input import PdfPageVisualReader
                with PdfPageVisualReader(path, expected_sha256=source.metadata['content_hash']) as reader:
                    actual_image = reader.render_pages([number])[0]
                if point.get('verification_passes') != 2 or not isinstance(point.get('bbox'), list) or len(point['bbox']) != 4 or (point['bbox'][0] != 0.0) or (point['bbox'][2:] != [1.0, 1.0]) or (not 0 <= point['bbox'][1] < 1) or (actual_image.sha256 != point.get('page_image_sha256')):
                    raise GoogleSourceError('The original page-role boundary has no matching visual proof.')
            else:
                actual = heading_point(document[number - 1], point.get('title', ''))
                if actual != point:
                    raise GoogleSourceError('The native chapter boundary no longer matches the visible original heading.')
            if not requested['start'] <= number <= requested['end']:
                continue
            clip = clips.setdefault(str(number), [0.0, 0.0, 1.0, 1.0])
            clip[1 if edge == 'start' else 3] = point['bbox'][1]
    if any((clip[1] >= clip[3] for clip in clips.values())):
        raise GoogleSourceError('The native chapter boundaries overlap or are reversed.')
    return clips

def build_native_pdf_catalog(source, path, *, allow_visual_pages=False, verify_missing=None):
    """Return all embedded entries only when every range can be reproduced."""
    from openclass_core.services import pdf_backend as fitz
    pages = native_pages(path, source)
    if not allow_visual_pages and any((page.text is None or page.visual for page in pages)):
        return None
    with fitz.open(path) as document:
        rows = document.get_toc(simple=True)
        if rows and all((row[0] == 1 for row in rows)):
            from .catalog_structure import heading_number
            numbered = {}
            restored = []
            for native_level, title, number in rows:
                ordinal = heading_number(title)
                parent_number = ordinal.rsplit('.', 1)[0] if ordinal and '.' in ordinal else None
                level = numbered[parent_number] + 1 if parent_number in numbered else native_level
                restored.append([level, title, number])
                if ordinal:
                    numbered[ordinal] = level
            rows = restored
        if not rows or len(rows) > 10000:
            return None
        points, stack, missing = ([], [], [])
        previous = (0, 0.0)
        for level, title, number in rows:
            if type(level) is not int or not title.strip() or (not 1 <= number <= len(document)) or (level > len(stack) + 1):
                return None
            while stack and stack[-1] >= level:
                stack.pop()
            point = heading_point(document[number - 1], title)
            if point is None:
                hint = heading_point(document[number - 1], title, verify_visible=False)
                missing.append({'key': len(points), 'title': title, 'physical_page': number, **({'native_heading_hint': hint} if hint else {})})
            elif (number, point['bbox'][1]) < previous:
                return None
            previous = (number, point['bbox'][1] if point else 0.0)
            points.append(point)
            stack.append(level)
        if missing:
            verified = verify_missing(missing) if verify_missing else None
            if not verified or set(verified) != {item['key'] for item in missing}:
                return None
            for index, point in verified.items():
                points[index] = point
    version, digest = (source.metadata.get('catalog_version', 1), source.metadata['content_hash'])
    chapters, parents = ([], [])
    for index, (level, title, number) in enumerate(rows):
        while parents and parents[-1].level >= level:
            parents.pop()
        successor = next((j for j in range(index + 1, len(rows)) if rows[j][0] <= level), None)
        end = rows[successor][2] if successor is not None else len(pages)
        whole_page = points[index].get('proof_kind') == 'verified_original_page_role'
        if whole_page:
            end = number
            successor = None
        exclusive_page = successor is not None and points[successor]['bbox'][1] == 0.0
        if exclusive_page:
            end -= 1
        if end < number:
            return None
        proof = {'start': points[index], **({'end': points[successor]} if successor is not None else {})}
        chapter = SourceChapter(id='sourcechapter_' + hashlib.sha256(f'{source.id}:{digest}:{version}:native:{index}'.encode()).hexdigest()[:24], owner_user_id=source.owner_user_id, package_id=source.package_id, source_ingestion_id=source.id, title=title, level=level, parent_id=parents[-1].id if parents else None, path=[*(parent.title for parent in parents), title], order_index=index, catalog_version=version, source_content_hash=digest, mapping_status='verified', anchor_status='verified', range=SourceRange(kind='pdf_pages', start=number, end=end, metadata={'source_pipeline': PIPELINE, 'source_version': source.metadata['source_version'], 'source_content_hash': digest, 'boundary_precision': 'verified_native_region', 'native_boundary_points': proof, 'native_boundary_range': {'start': number, 'end': end}, 'native_end_exclusive_page': exclusive_page}), metadata={'origin': 'original_native_outline', 'source_pipeline': PIPELINE, 'locator_source': 'native_navigation'})
        try:
            native_range_clips(path, source, chapter.range.model_dump(mode='json'))
        except GoogleSourceError:
            return None
        chapters.append(chapter)
        parents.append(chapter)
    count = len(chapters)
    structure = SourceStructure(owner_user_id=source.owner_user_id, package_id=source.package_id, source_ingestion_id=source.id, status='ready', strategy='native_directory_v1', has_verified_toc=True, catalog_version=version, source_content_hash=digest, chapter_count=count, catalog_schema_version=PIPELINE, catalog_model='native_original_reader', quality=SourceStructureQuality(level='fully_verified', total_chapter_count=count, verified_chapter_count=count, verified_ratio=1), metadata={'source_backend': source.metadata.get('source_backend'), 'catalog_pipeline': PIPELINE, 'catalog_status': 'ready', 'directory_status': 'complete', 'catalog_label': 'Verified original bookmarks', 'model_call_count': 2 * ((len(missing) + 5) // 6), 'native_catalog_complete': True})
    return (structure, chapters)
