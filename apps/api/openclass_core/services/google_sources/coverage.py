"""Separate verified uploaded pages from pages with retrievable text or annotations."""
from __future__ import annotations
import hashlib
import io
from .config import GoogleSourceError

def validate_inputs(raw, adapted):
    if not adapted.parts:
        raise GoogleSourceError('The source has no content that can be submitted to Layout Parser.')
    from pypdf import PdfReader
    for part in adapted.parts:
        if part.mime_type == 'application/pdf':
            try:
                count = len(PdfReader(io.BytesIO(part.content)).pages)
            except Exception as exc:
                raise GoogleSourceError('The PDF input for Layout Parser is corrupt.') from exc
            if count != part.page_count or count < 1:
                raise GoogleSourceError('The Layout Parser input page count does not match the source segment.')
    if adapted.format == 'pdf':
        expected = len(PdfReader(io.BytesIO(raw)).pages)
        next_page = 0
        for part in adapted.parts:
            if part.mime_type != 'application/pdf' or part.page_offset != next_page:
                raise GoogleSourceError('Source segments contain missing or overlapping pages. An incomplete original cannot be submitted.')
            next_page += part.page_count
        if next_page != expected:
            raise GoogleSourceError('Source segments do not cover all original pages. An incomplete original cannot be submitted.')
        return expected
    if adapted.format == 'image':
        return sum((part.page_count for part in adapted.parts))
    return None

def chunk_page_coverage(part, chunks, *, normalize=False):
    readable = set()
    for chunk in chunks:
        span = chunk.get('pageSpan', {})
        start, end = (span.get('pageStart'), span.get('pageEnd'))
        if start is None and end is None:
            continue
        lower = 1 if normalize else part.page_offset + 1
        upper = lower + part.page_count - 1
        if type(start) is not int or type(end) is not int or start < lower or (end < start) or (part.page_count and end > upper):
            raise GoogleSourceError('Layout Parser returned page numbers outside the corresponding input range.')
        if normalize:
            start, end = (start + part.page_offset, end + part.page_offset)
            span.update(pageStart=start, pageEnd=end)
        if str(chunk.get('content') or '').strip():
            readable.update(range(start, end + 1))
    pages = set(range(part.page_offset + 1, part.page_offset + part.page_count + 1))
    return {'page_count': part.page_count, 'retrievable_pages': sorted(readable), 'unmapped_pages': sorted(pages - readable)}

def validate_output(part, output):
    if output.get('input', {}).get('sha256') != hashlib.sha256(part.content).hexdigest() or output.get('page_offset') != part.page_offset or output.get('page_count') != part.page_count or (not isinstance(output.get('parsed'), dict)) or (not isinstance(output.get('chunks'), list)) or (part.layout_required and (not output.get('layout_verified'))):
        raise GoogleSourceError('The Layout Parser output does not match the complete input segments.')
    return chunk_page_coverage(part, output['chunks'])
