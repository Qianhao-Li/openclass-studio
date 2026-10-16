"""Reject explicit page claims that contradict the original evidence of a label.

This checks declared coordinates only. It neither infers page locations from
prose nor repairs a citation by assigning it to another piece of evidence.
"""
from __future__ import annotations
import re
from openclass_core.services.board_citations import _collect_definitions
from openclass_core.services.rich_document import document_to_markdown
from .config import GoogleSourceError
_NUMBER = '[1-9][0-9]{0,8}'
_RANGE = '(?:[-–—~～]|至|到|\\bto\\b|\\bthrough\\b)'
_INTERVAL = f'{_NUMBER}(?:\\s*页?\\s*{_RANGE}\\s*第?\\s*{_NUMBER})?'
_NUMBERS = f'{_INTERVAL}(?:\\s*(?:[,，、&]|\\band\\b)\\s*{_INTERVAL})*'
_DECLARATION = re.compile(f'(?P<pdf>(?<![A-Za-z])PDF(?![A-Za-z]))\\s*(?:(?:physical\\s+)?(?:pages?\\b|pp?\\.)\\s*|(?:第|页码?|物理页)\\s*)?(?P<pdf_numbers>{_NUMBERS})(?![0-9A-Za-z章节])(?:\\s*页)?|(?P<label>\\bpages?\\b|\\bpp?\\.|物理页(?:码)?|页码?)\\s*(?P<numbers>{_NUMBERS})(?![0-9A-Za-z])(?:\\s*页)?|(?P<chinese>第)\\s*(?P<chinese_numbers>{_NUMBERS})\\s*页', re.IGNORECASE)
_INTERVAL_RE = re.compile(f'({_NUMBER})(?:\\s*页?\\s*{_RANGE}\\s*第?\\s*({_NUMBER}))?', re.IGNORECASE)
_PRINTED = re.compile('(?:printed|book)\\s*(?:pages?|pp?\\.)?|(?:书(?:本|中)?|印刷|正文)\\s*(?:页码?|第)?', re.IGNORECASE)
_PHYSICAL = re.compile('(?<![A-Za-z])PDF(?![A-Za-z])|physical|物理', re.IGNORECASE)
_CLAUSE_BOUNDARY = re.compile('[()（）;；\\n]')

def declared_pdf_ranges(definition):
    """Read explicit page notation, keeping printed and physical coordinates apart."""
    result = []
    for match in _DECLARATION.finditer(definition):
        if not match.group('pdf'):
            before = definition[:match.start()]
            boundaries = list(_CLAUSE_BOUNDARY.finditer(before))
            context = before[boundaries[-1].end():] if boundaries else before
            printed = list(_PRINTED.finditer(context))
            physical = list(_PHYSICAL.finditer(context))
            if printed and (not physical or printed[-1].start() > physical[-1].start()):
                continue
        for interval in _INTERVAL_RE.finditer(match.group('pdf_numbers') or match.group('numbers') or match.group('chinese_numbers')):
            start, end = (int(interval[1]), int(interval[2] or interval[1]))
            if end < start:
                raise GoogleSourceError('A document reference declares a reversed PDF page range.')
            result.append((start, end))
    return result

def _covered(declared, available):
    cursor, end = declared
    for low, high in sorted(available):
        if high < cursor:
            continue
        if low > cursor:
            return False
        cursor = max(cursor, high + 1)
        if cursor > end:
            return True
    return False

def validate_document_citation_locations(document, delivered_citations):
    """delivered_citations contains (citation, authenticated original-read evidence)."""
    if document is None:
        return
    definitions = {}
    for markdown in (document.content_text, document_to_markdown(document)):
        entries, _order, _body = _collect_definitions(markdown.splitlines())
        for label, definition in entries.items():
            definitions.setdefault(label, set()).add(definition)
    by_label = {}
    for citation, evidence in delivered_citations:
        if evidence is None or citation.citation_number is None:
            continue
        coordinate = evidence.metadata.get('source_range') or {}
        if coordinate.get('kind') != 'pdf_pages':
            continue
        start, end = (coordinate.get('start'), coordinate.get('end'))
        if type(start) is int and type(end) is int and (1 <= start <= end):
            by_label.setdefault(str(citation.citation_number), []).append((start, end))
    for label, available in by_label.items():
        for definition in definitions.get(label, ()):
            for declared in declared_pdf_ranges(definition):
                if not _covered(declared, available):
                    raise GoogleSourceError(f'Document reference [^{label}] declares PDF pages {declared[0]}-{declared[1]} outside its delivered original evidence. The citation must be corrected before saving.')
