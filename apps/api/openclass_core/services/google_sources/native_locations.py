"""Fast disposable native-text locations; original reading grants citation authority."""
from __future__ import annotations
import hashlib
import math
import re
from collections import Counter
from functools import lru_cache
from openclass_core.services.source_content_contract import seal_locator
from openclass_core.services.source_pdf_native import _page_text, normalized
from .config import GoogleSourceError
from .original_cache import original_path

@lru_cache(maxsize=12)
def _index(path, digest, identity):
    from openclass_core.services import pdf_backend as fitz
    blocks = []
    visual = False
    with fitz.open(path) as document:
        for page in document:
            visual |= any((kind in {'fill-image', 'fill-shade', 'stroke-path', 'fill-path'} for kind, _ in page.get_bboxlog()))
            if page.rotation or page.first_annot:
                continue
            for block in page.get_text('dict', flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)['blocks']:
                text = '\n'.join((''.join((span['text'] for span in line['spans'])) for line in block.get('lines', []))).strip()
                x0, y0, x1, y1 = block['bbox']
                box = (x0 / page.rect.width, y0 / page.rect.height, x1 / page.rect.width, y1 / page.rect.height)
                if text and 0 <= box[0] < box[2] <= 1 and (0 <= box[1] < box[3] <= 1):
                    blocks.append((page.number + 1, box, text))
        return (tuple(blocks), len(document), visual)

def native_index(source):
    path = original_path(source)
    identity = (source.owner_user_id, source.package_id, source.id, source.metadata['source_version'])
    return _index(str(path), source.metadata['content_hash'], identity)

def native_locations(service, source, query, *, scopes=(), limit=8):
    from openclass_core.services import pdf_backend as fitz
    from .catalog_native_pdf import native_range_clips
    blocks, count, _visual = native_index(source)
    q = normalized(query)
    terms = set(re.findall('[a-z0-9_]{2,}', query.casefold()))
    chinese = ''.join(re.findall('[\\u3400-\\u9fff]', query))
    terms.update((chinese[i:i + 2] for i in range(len(chinese) - 1)))
    if len(q) >= 2:
        terms.add(q)
    if not terms:
        return []
    document_frequency = Counter((term for _page, _box, text in blocks for term in terms if term in normalized(text)))
    candidates = []
    for page, box, text in blocks:
        if scopes and (not any((scope is None or (scope.kind == 'pdf_pages' and scope.start <= page <= scope.end) for scope in scopes))):
            continue
        body = normalized(text)
        score = sum((math.log((len(blocks) + 1) / (document_frequency[term] + 1) + 1) * min(body.count(term), 3) for term in terms if term in body))
        if score:
            candidates.append((score / math.sqrt(max(len(body), 80)), page, box))
    candidates.sort(key=lambda item: -item[0])
    locations = []
    path = original_path(source)
    with fitz.open(path) as document:
        for score, page, original_box in candidates[:max(32, limit * 4)]:
            service._check(source)
            allowed = [scope for scope in scopes if scope is None or (scope.kind == 'pdf_pages' and scope.start <= page <= scope.end)] or [None]
            for scope in allowed:
                box = list(original_box)
                if scope is not None:
                    scoped = native_range_clips(path, source, scope.model_dump(mode='json')) or {}
                    clip = scoped.get(str(page))
                    if clip:
                        box = [max(box[0], clip[0]), max(box[1], clip[1]), min(box[2], clip[2]), min(box[3], clip[3])]
                    if scope.start_anchor or scope.end_anchor:
                        continue
                if not box[0] < box[2] or not box[1] < box[3]:
                    continue
                rect = document[page - 1].rect
                if not _page_text(document[page - 1], fitz.Rect(box[0] * rect.width, box[1] * rect.height, box[2] * rect.width, box[3] * rect.height)):
                    continue
                requested = {'kind': 'pdf_pages', 'start': page, 'end': page, 'metadata': {**(scope.metadata if scope is not None else {}), 'native_text_region': box, 'locator_method': 'original_native_keyword'}}
                locations.append(seal_locator(source_id=source.id, source_version=source.metadata['source_version'], content_hash=source.metadata['content_hash'], source_title=source.title, source_range=requested, coordinate_type='pdf_pages', rank=len(locations) + 1, authorized_range=scope.model_dump(mode='json') if scope is not None else None))
                break
            if len(locations) >= limit:
                break
    return locations

def prepare_native_source(service, source):
    """Publish fast text access separately; only a fully mapped catalog is formal."""
    if source.mime_type != 'application/pdf' and (not source.file_name.lower().endswith('.pdf')):
        return source
    if source.metadata.get('native_text_ready') or not source.metadata.get('original_cache') or source.metadata.get('external_turn_temporary'):
        return source
    service._check(source)
    service._verify_blob_binding(source, source.metadata.get('google_original', source.metadata.get('local_original')), original=True)
    blocks, count, visual = native_index(source)
    if not blocks:
        return source
    from openclass_core.services import pdf_backend as fitz
    path = original_path(source)
    with fitz.open(path) as document:
        usable = any((_page_text(document[page - 1], fitz.Rect(box[0] * document[page - 1].rect.width, box[1] * document[page - 1].rect.height, box[2] * document[page - 1].rect.width, box[3] * document[page - 1].rect.height)) for page, box, _ in blocks[:16]))
    if not usable:
        return source
    source = service.state.update(source, status='ready', metadata={'native_text_ready': True, 'native_search_ready': True, 'native_pdf_reading': True, 'original_read_ready': True, 'page_count': count, 'update_status': 'queued', 'capabilities': ['download', 'locate_native', 'read_native_text']})
    if not visual:
        from .catalog_native_pdf import build_native_pdf_catalog
        from .catalog_publication import catalog_display_metadata
        native = build_native_pdf_catalog(source, path)
        if native:
            structure, chapters = native
            source = service.state.publish(source, structure_store=service.structure_store, structure=structure, chapters=chapters, metadata={**catalog_display_metadata(structure), 'native_catalog_complete': True, 'catalog_status': 'ready', 'catalog_ready': True, 'original_read_ready': True, 'native_pdf_reading': True, 'native_search_ready': True, 'update_status': 'queued', 'catalog_mode': 'native'})
    return service.state.update(source, metadata={'update_status': 'queued'}) if source.metadata.get('native_catalog_complete') else source
