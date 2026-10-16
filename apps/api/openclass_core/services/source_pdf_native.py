"""Verified visible PDF text, derived only from immutable original bytes.

This is a disposable process cache, never a source-body database or OCR engine.
Pages with images, invisible glyphs or ambiguous drawing coverage stay visual.
"""
from __future__ import annotations
import hashlib
import math
import unicodedata
from functools import lru_cache
from typing import NamedTuple
from openclass_core.services.google_sources.config import GoogleSourceError

def normalized(value):
    return ''.join((c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum()))

class NativePdfPage(NamedTuple):
    text: str | None
    visual: bool

def _page_text(page, clip=None):
    """Return text only when the native carrier describes visible text alone."""
    from openclass_core.services import pdf_backend as fitz
    if page.rotation or page.first_annot:
        return None
    logs = page.get_bboxlog()
    traces = page.get_texttrace()
    if not traces:
        return None
    bounds = page.rect
    region = fitz.Rect(clip) if clip is not None else bounds
    for span in traces:
        relevant = [char for char in span.get('chars', ()) if region.contains(fitz.Rect(char[3]))]
        if not relevant:
            continue
        if span.get('type') not in (0, 1) or span.get('opacity', 1) < 0.99:
            return None
        covers = [fitz.Rect(box) for kind, box in logs[span['seqno'] + 1:] if kind in {'fill-image', 'fill-path', 'stroke-path', 'fill-shade'}]
        for code, glyph, _origin, bbox in relevant:
            if code == 65533 or code < 0 or glyph < 0:
                return None
            rect = fitz.Rect(bbox)
            if not all((math.isfinite(v) for v in bbox)) or rect.is_empty or (not bounds.contains(rect)):
                return None
            for cover in covers:
                if rect.intersects(cover):
                    return None
    text = page.get_text('text', sort=True, clip=region)
    visible = ''.join((chr(c[0]) for span in traces for c in span.get('chars', ()) if region.contains(fitz.Rect(c[3]))))
    if not text.strip() or normalized(text) != normalized(visible):
        from collections import Counter
        if not text.strip() or Counter(normalized(text)) - Counter(normalized(visible)):
            return None
    if '�' in text or '\x00' in text:
        return None
    return text

@lru_cache(maxsize=12)
def _snapshot(path, digest, identity):
    from openclass_core.services import pdf_backend as fitz
    with fitz.open(path) as document:
        if document.is_encrypted:
            raise GoogleSourceError('Decrypt the PDF before reading it.')
        pages = tuple((NativePdfPage(_page_text(page), any((kind in {'fill-image', 'fill-shade', 'stroke-path', 'fill-path'} for kind, _box in page.get_bboxlog()))) for page in document))
        return pages

def native_pages(path, source):
    digest = source.metadata['content_hash']
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise GoogleSourceError('The original PDF hash changed before native reading.')
    identity = (source.owner_user_id, source.package_id, source.id, source.metadata['source_version'])
    return _snapshot(str(path), digest, identity)

def native_pdf_parts(path, source, requested, offset, max_units, max_chars):
    """Return a complete bounded native batch or None for visual fallback."""
    region = (requested or {}).get('metadata', {}).get('native_text_region')
    if region is not None:
        from openclass_core.services import pdf_backend as fitz
        if not isinstance(region, list) or len(region) != 4 or any((type(value) not in (int, float) or not math.isfinite(value) for value in region)) or (not 0 <= region[0] < region[2] <= 1) or (not 0 <= region[1] < region[3] <= 1) or (requested.get('kind') != 'pdf_pages') or (type(requested.get('start')) is not int) or (requested['start'] != requested.get('end')) or (offset != 0):
            raise GoogleSourceError('The native PDF text region is invalid or exceeds its finite scope.')
        if hashlib.sha256(path.read_bytes()).hexdigest() != source.metadata['content_hash']:
            raise GoogleSourceError('The original changed before native region reading.')
        from openclass_core.services.google_sources.catalog_native_pdf import native_range_clips
        boundary_clips = native_range_clips(path, source, requested) or {}
        if str(requested['start']) in boundary_clips:
            authorized = boundary_clips[str(requested['start'])]
            if not authorized[0] <= region[0] < region[2] <= authorized[2] or not authorized[1] <= region[1] < region[3] <= authorized[3]:
                raise GoogleSourceError('The native text region expands its verified chapter boundary.')
        with fitz.open(path) as document:
            page_no = requested['start']
            if not 1 <= page_no <= len(document):
                raise GoogleSourceError('The native PDF text region is outside the original.')
            page = document[page_no - 1]
            rect = page.rect
            text = _page_text(page, fitz.Rect(region[0] * rect.width, region[1] * rect.height, region[2] * rect.width, region[3] * rect.height))
        if text is None or len(text) > max_chars:
            raise GoogleSourceError('The selected native text region has no faithful visible carrier within this read budget.')
        return {'parts': [{'kind': 'text', 'text': text, 'locator': requested, 'provenance': {'representation': 'original_pdf_visible_text', 'input_method': 'original_native_object', 'content_scope': 'native_text_region', 'original_sha256': source.metadata['content_hash'], 'visibility_verification': 'native_glyph_trace_v1'}}], 'range': requested, 'next_cursor': None, 'complete': True, 'missing': [], 'unit_count': 1}
    pages = native_pages(path, source)
    requested = requested or {'kind': 'pdf_pages', 'start': 1, 'end': len(pages)}
    start, end = (requested.get('start'), requested.get('end'))
    if requested.get('kind') != 'pdf_pages' or type(start) is not int or type(end) is not int or (not 1 <= start <= end <= len(pages)) or (not 0 <= offset <= end - start + 1):
        raise GoogleSourceError('The native PDF requires a valid physical page range and cursor.')
    if requested.get('start_anchor') or requested.get('end_anchor') or requested.get('metadata', {}).get('page_clips'):
        return None
    from openclass_core.services.google_sources.catalog_native_pdf import native_range_clips
    clips = native_range_clips(path, source, requested) or {}
    parts, size = ([], 0)
    for number in range(start + offset, min(end + 1, start + offset + max_units)):
        page = pages[number - 1]
        text = page.text
        if text is None or page.visual:
            if not parts:
                return None
            break
        if str(number) in clips:
            from openclass_core.services import pdf_backend as fitz
            with fitz.open(path) as document:
                rect = document[number - 1].rect
                clip = clips[str(number)]
                text = document[number - 1].get_text('text', sort=True, clip=fitz.Rect(clip[0] * rect.width, clip[1] * rect.height, clip[2] * rect.width, clip[3] * rect.height))
        if len(text) > max_chars:
            if not parts:
                return None
            break
        if size + len(text) > max_chars:
            break
        parts.append({'kind': 'text', 'text': text, 'locator': {'kind': 'pdf_pages', 'start': number, 'end': number, **({'metadata': requested['metadata']} if requested.get('metadata', {}).get('boundary_precision') == 'verified_native_region' else {})}, 'provenance': {'representation': 'original_pdf_visible_text', 'visibility_verification': 'native_glyph_trace_v1', 'original_sha256': source.metadata['content_hash']}})
        size += len(text)
    next_offset = offset + len(parts)
    return {'parts': parts, 'range': requested, 'next_cursor': next_offset if start + next_offset <= end else None, 'complete': start + next_offset > end, 'missing': [], 'unit_count': len(parts)}
