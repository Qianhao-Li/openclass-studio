"""Bind citations inside authorized operation fragments, without editing the board.

Callers must supply only this turn's verified citations. This pure formatter does
not retrieve evidence, grant source access, or prove that a sentence is supported.
"""
from __future__ import annotations
import re
from collections.abc import Iterable, Sequence
from markdown_it import MarkdownIt
from openclass_core.models import SourceCitation

class CitationLabelError(ValueError):
    pass
_DEFINITION = re.compile('^ {0,3}\\[\\^([^\\]\\s]+)\\]:[ \\t]*(.*?)(?:\\r?\\n)?$')
_MARKER = re.compile('\\[(\\^?[^\\[\\]\\s]+)\\]')
_CODE_SPAN = re.compile('(?<!`)(`+)(?!`)([\\s\\S]*?)(?<!`)\\1(?!`)')

def _code_ranges(markdown: str) -> list[tuple[int, int]]:
    lines = markdown.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return [(offsets[token.map[0]], offsets[token.map[1]]) for token in MarkdownIt('commonmark').parse(markdown) if token.type in {'fence', 'code_block'} and token.map is not None]

def _escaped(text: str, offset: int) -> bool:
    start = offset
    while start and text[start - 1] == '\\':
        start -= 1
    return (offset - start) % 2 == 1

def _plain_label(text: str) -> str:
    text = ' '.join(str(text or '').split())
    return re.sub('([\\\\`*_{}\\[\\]()<>#+.!|])', '\\\\\\1', text)

def _definition(citation: SourceCitation, pdf_evidence_ids: set[str]) -> str:
    if not citation.source_id or not re.fullmatch('[a-fA-F0-9]{64}', citation.source_content_hash):
        raise CitationLabelError('A citation in this turn is missing a verified source identity or file hash.')
    start, end = (citation.page_start, citation.page_end)
    if (start is None) != (end is None) or (start is not None and (type(start) is not int or type(end) is not int or (not 1 <= start <= end))):
        raise CitationLabelError('A citation in this turn has invalid page numbers and cannot produce a footnote.')
    title = _plain_label(citation.source_title) or 'Uploaded source'
    if start is None:
        return title
    span = str(start) if start == end else f'{start}–{end}'
    pdf = citation.evidence_id in pdf_evidence_ids
    return f"{title}; {('PDF pages' if pdf else 'source pages')} {span}."

def _strip_definitions(markdown: str, labels: dict[str, SourceCitation], reserved: set[str]) -> str:
    lines = markdown.splitlines(keepends=True)
    protected = _code_ranges(markdown)
    result, offset, index = ([], 0, 0)
    while index < len(lines):
        line = lines[index]
        match = _DEFINITION.match(line) if not any((start <= offset < end for start, end in protected)) else None
        if match is None:
            result.append(line)
            offset += len(line)
            index += 1
            continue
        label = match.group(1)
        if label in reserved:
            raise CitationLabelError('This operation cannot overwrite preserved footnote definitions in the existing document.')
        if label not in labels:
            raise CitationLabelError('The footnote definition does not belong to the verified source evidence for this turn.')
        offset += len(line)
        index += 1
        while index < len(lines):
            continuation = lines[index]
            if continuation.startswith(('    ', '\t')):
                offset += len(continuation)
                index += 1
            elif not continuation.strip() and index + 1 < len(lines) and lines[index + 1].startswith(('    ', '\t')):
                offset += len(continuation)
                index += 1
            else:
                break
    return ''.join(result)

def bind_operation_citations(markdown: str, citations: Sequence[SourceCitation], *, reserved_labels: Iterable[str | int] | None=None, pdf_evidence_ids: Iterable[str] | None=None) -> str:
    return bind_operation_list_citations([markdown], citations, reserved_labels=reserved_labels, pdf_evidence_ids=pdf_evidence_ids)[0]

def bind_operation_list_citations(fragments: Sequence[str], citations: Sequence[SourceCitation], *, reserved_labels: Iterable[str | int] | None=None, pdf_evidence_ids: Iterable[str] | None=None) -> list[str]:
    """Replace this turn's ID markers and definitions; preserve other body text.

    Old reserved markers stay intact. Their definitions cannot be supplied or
    changed here. Definitions for new citations appear once, in the operation
    containing their first use. Unknown citations fail before anything persists.
    PDF identities must come from verified page-coordinate metadata, not titles.
    """
    reserved = {str(label) for label in reserved_labels or ()}
    pdf_ids = set(pdf_evidence_ids or ())
    by_id, labels = ({}, {})
    for citation in citations:
        if not isinstance(citation, SourceCitation) or not citation.evidence_id:
            raise CitationLabelError('Footnotes must come from a verified SourceCitation for this turn.')
        previous = by_id.get(citation.evidence_id)
        if previous is not None and previous != citation:
            raise CitationLabelError('The citation identity for this evidence conflicts with another citation in this turn.')
        by_id[citation.evidence_id] = citation
        if citation.citation_number is None:
            continue
        if type(citation.citation_number) is not int or citation.citation_number < 1:
            raise CitationLabelError("The footnote number for this turn's source evidence is invalid.")
        label = str(citation.citation_number)
        if label in reserved or (label in labels and labels[label].evidence_id != citation.evidence_id):
            raise CitationLabelError('A citation number in this turn conflicts with an existing number.')
        labels[label] = citation
    defined, results = (set(), [])
    for fragment in fragments:
        body = _strip_definitions(fragment, labels, reserved)
        protected = [*_code_ranges(body), *(match.span() for match in _CODE_SPAN.finditer(body))]
        used = []

        def replace(match):
            if _escaped(body, match.start()) or any((start <= match.start() < end for start, end in protected)):
                return match.group(0)
            marker = match.group(1)
            if marker.startswith('^'):
                label = marker[1:]
                if label in reserved:
                    return match.group(0)
                citation = labels.get(label)
                if citation is None:
                    raise CitationLabelError("The footnote number belongs to neither this turn's evidence nor a preserved document footnote.")
            else:
                citation = by_id.get(marker)
                if citation is None:
                    if marker.startswith('evidence_'):
                        raise CitationLabelError("The source evidence marker does not belong to this turn's verified evidence.")
                    return match.group(0)
                if citation.citation_number is None:
                    raise CitationLabelError("This turn's source evidence has no assigned footnote number.")
                label = str(citation.citation_number)
            if body[match.end():].startswith('('):
                raise CitationLabelError('A source evidence marker cannot also be used as an external link.')
            _definition(citation, pdf_ids)
            if label not in used:
                used.append(label)
            return f'[^{label}]'
        rewritten = _MARKER.sub(replace, body)
        definitions = [f'[^{label}]: {_definition(labels[label], pdf_ids)}' for label in used if label not in defined]
        if definitions:
            rewritten += ('' if rewritten.endswith('\n\n') else '\n' if rewritten.endswith('\n') else '\n\n') + '\n'.join(definitions)
        if fragment.strip() and (not rewritten.strip()):
            raise CitationLabelError('Footnote definitions alone do not constitute a document change.')
        defined.update(used)
        results.append(rewritten)
    return results
