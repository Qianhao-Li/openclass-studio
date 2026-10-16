"""Keep only this turn's evidence cited by its answer or changed target document."""
from __future__ import annotations
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
import re
from markdown_it import MarkdownIt
from openclass_core.models import BoardDocument, RetrievalEvidence, SourceCitation
from openclass_core.services.rich_document import document_to_markdown

class TurnEvidenceCapture(dict[str, SourceCitation]):

    def __init__(self):
        super().__init__()
        self.aliases: dict[str, tuple[str, ...]] = {}
        self.reserved_numbers: set[int] = set()
        self.pdf_evidence_ids: set[str] = set()
        self.document_numbers: dict[int, tuple[str, ...]] | None = None
        self.read_evidence: dict[str, RetrievalEvidence] = {}
_current: ContextVar[TurnEvidenceCapture | None] = ContextVar('google_turn_evidence', default=None)
_MARKER = re.compile('\\[([^\\[\\]\\s]+)\\]')

def _as_evidence(item):
    return item if isinstance(item, RetrievalEvidence) else RetrievalEvidence.model_validate(item)

def to_citations(items) -> list[SourceCitation]:
    from openclass_core.services.source_retrieval_service import _citation_from_evidence
    result = []
    for item in items:
        if isinstance(item, SourceCitation):
            result.append(item)
            continue
        evidence = _as_evidence(item)
        if evidence.metadata.get('source_backend') not in {'google', 'local'}:
            continue
        if evidence.metadata.get('retrieval_mode') == 'frozen_source_range_batch_summary':
            continue
        from openclass_core.services.source_content_contract import verified_read_evidence
        if not verified_read_evidence(evidence):
            continue
        chunk_ids = evidence.chunk_ids or evidence.metadata.get('google_chunk_ids')
        if not evidence.chunk_ids and isinstance(chunk_ids, list) and all((isinstance(value, str) and value for value in chunk_ids)):
            evidence = evidence.model_copy(update={'chunk_ids': list(chunk_ids)})
        result.append(_citation_from_evidence(evidence))
    return result

def current_citation_binding():
    """Return only this turn's verified identities and pre-existing board labels."""
    capture = _current.get()
    if capture is None:
        return ([], set())
    return (list(capture.values()), {str(number) for number in capture.reserved_numbers})

def current_pdf_evidence_ids():
    capture = _current.get()
    return set(capture.pdf_evidence_ids) if capture is not None else set()

def original_ids_for_document_excerpt(markdown):
    """Resolve the current board's labels back to this turn's actual original units."""
    capture = _current.get()
    if capture is None:
        return set()
    identities = {marker for block in _visible_blocks(markdown) for marker in _MARKER.findall(block) if marker in capture.read_evidence}
    mapping = capture.document_numbers
    if mapping is None:
        mapping = {item.citation_number: (item.evidence_id,) for item in capture.values() if item.citation_number is not None}
    for number in footnote_numbers(markdown):
        identities.update(mapping.get(number, ()))
    return identities

def read_evidence_for_citation(citation):
    """Resolve only original evidence delivered and registered in this active turn."""
    from openclass_core.services.source_content_contract import verified_read_evidence
    capture = _current.get()
    item = capture.read_evidence.get(citation.evidence_id) if capture is not None else None
    if item is None or not verified_read_evidence(item):
        return None
    from openclass_core.services.source_retrieval_service import _citation_from_evidence
    expected = _citation_from_evidence(item)
    fields = ('source_id', 'source_content_hash', 'page_start', 'page_end', 'chunk_ids', 'block_ids', 'source_document_id')
    if any((getattr(citation, field) != getattr(expected, field) for field in fields)):
        return None
    return item

def restore_delivered_read(item, *, service=None):
    """Rehydrate an opaque native-tool record without granting a new read citation."""
    from openclass_core.services.source_content_contract import verified_read_receipt, verified_read_evidence
    from openclass_core.services.source_content_reader import read_source_content
    from openclass_core.services.source_content_scope import ensure_unit_authorized
    from .config import GoogleSourceError
    from .service import get_google_source_service
    item = _as_evidence(item)
    receipt = item.metadata.get('source_read_receipt') or {}
    if not verified_read_receipt(receipt):
        raise GoogleSourceError('The delivered source record has no authenticated read receipt.')
    if receipt.get('source_id') != item.source_ingestion_id or receipt.get('content_hash') != item.metadata.get('source_content_hash') or receipt.get('source_version') != item.metadata.get('source_version') or (item.id not in receipt.get('evidence_hashes', {})):
        raise GoogleSourceError('The delivered source record does not match its authenticated identity.')
    requested = dict(item.metadata.get('source_range') or {})
    scope = receipt.get('authorized_range', receipt['range'])
    if scope and scope.get('kind') != 'whole_source':
        ensure_unit_authorized(requested, scope)
    if requested.get('kind') == 'pdf_pages':
        original = receipt['range']
        requested.update({key: original[key] for key in ('start_anchor', 'end_anchor', 'metadata') if original.get(key)})
    service = service or get_google_source_service()
    result = read_source_content(service, owner_user_id=receipt['owner_user_id'], package_id=receipt['package_id'], source_id=receipt['source_id'], content_hash=receipt['content_hash'], source_range=requested, authorization=receipt['authorization'], grant_id=receipt['grant_id'], role=receipt['role'])
    matches = [candidate for candidate in result.evidence_items if candidate.id == item.id]
    if len(matches) != 1:
        raise GoogleSourceError('The delivered original unit could not be reproduced.')
    restored = item.model_copy(update={'excerpt': matches[0].excerpt, 'expanded_text': matches[0].expanded_text})
    if not verified_read_evidence(restored):
        raise GoogleSourceError('The reproduced original does not match the content actually delivered in this turn.')
    return restored

def remember(items):
    items = list(items)
    capture = _current.get()
    citations = to_citations(items)
    if capture is not None:
        numbered = []
        for citation in citations:
            previous = capture.get(citation.evidence_id)
            number = previous.citation_number if previous is not None else None
            if number is None:
                taken = capture.reserved_numbers | {item.citation_number for item in capture.values() if item.citation_number is not None}
                number = max(taken, default=0) + 1
            citation = citation.model_copy(update={'citation_number': number})
            capture[citation.evidence_id] = citation
            numbered.append(citation)
        citations = numbered
        for item in items:
            if isinstance(item, SourceCitation):
                continue
            evidence = _as_evidence(item)
            from openclass_core.services.source_content_contract import verified_read_evidence
            if verified_read_evidence(evidence):
                capture.read_evidence[evidence.id] = evidence.model_copy(deep=True)
            if evidence.metadata.get('page_coordinate_system') == 'physical_pdf_pages':
                capture.pdf_evidence_ids.add(evidence.id)
            if evidence.metadata.get('source_backend') not in {'google', 'local'} or evidence.metadata.get('retrieval_mode') != 'frozen_source_range_batch_summary':
                continue
            covered = evidence.metadata.get('covered_evidence_ids')
            if not isinstance(covered, list) or not covered:
                continue
            resolved = []
            for identity in covered:
                if identity in capture:
                    resolved.append(identity)
                elif identity in capture.aliases:
                    resolved.extend(capture.aliases[identity])
                else:
                    break
            else:
                capture.aliases[evidence.id] = tuple(dict.fromkeys(resolved))
    return citations

@contextmanager
def capture_turn(*, document_before=None):
    existing = _current.get()
    if existing is not None:
        yield existing
        return
    capture = TurnEvidenceCapture()
    if document_before is not None:
        markdown = document_to_markdown(document_before)
        capture.reserved_numbers = footnote_numbers(markdown)
        from openclass_core.services.board_citations import _collect_definitions
        definitions, _order, _body = _collect_definitions(markdown.splitlines())
        capture.reserved_numbers.update((int(label) for label in definitions if label.isdigit() and int(label) > 0))
    token = _current.set(capture)
    try:
        yield capture
    finally:
        _current.reset(token)

def _visible_blocks(markdown: str) -> list[str]:
    """Paragraph/heading/list text; code examples are not citation markers."""
    blocks = []
    markdown = '\n'.join((line for line in markdown.splitlines() if not re.match('^\\s*\\[\\^\\d+\\]:', line)))
    for token in MarkdownIt('commonmark').parse(markdown):
        if token.type != 'inline':
            continue
        text = ''.join((child.content if child.type == 'text' else '\n' if child.type in {'softbreak', 'hardbreak'} else '' for child in token.children or []))
        if text.strip():
            blocks.append(text.strip())
    return blocks

def _changed_document_markers(before: BoardDocument | None, after: BoardDocument | None) -> set[str]:
    if before is None or after is None:
        return set()
    before_blocks = Counter(_visible_blocks(document_to_markdown(before)))
    markers = set()
    for block in _visible_blocks(document_to_markdown(after)):
        if before_blocks[block]:
            before_blocks[block] -= 1
            continue
        markers.update(_MARKER.findall(block))
    return markers

def footnote_numbers(markdown: str) -> set[int]:
    body = '\n'.join((line for line in markdown.splitlines() if not re.match('^\\s*\\[\\^\\d+\\]:', line)))
    return {int(value) for block in _visible_blocks(body) for value in re.findall('\\[\\^(\\d+)\\]', block) if int(value) > 0}

def bind_citation_numbers(items):
    """Annotate verified writer inputs with deterministic reader-facing numbers."""
    items = list(items)
    ordinary = []
    for item in items:
        if item.metadata.get('retrieval_mode') != 'frozen_source_range_batch_summary':
            restored = item.model_copy(update={'chunk_ids': item.chunk_ids or list(item.metadata.get('google_chunk_ids') or [])})
            ordinary.append(restored)
    registered = remember(ordinary)
    remember((item for item in items if item.metadata.get('retrieval_mode') == 'frozen_source_range_batch_summary'))
    capture = _current.get()
    if capture is not None:
        capture.pdf_evidence_ids.update((item.id for item in items if item.metadata.get('page_coordinate_system') == 'physical_pdf_pages'))
    known = capture if capture is not None else {item.evidence_id: item.model_copy(update={'citation_number': index + 1}) for index, item in enumerate(registered)}
    bound = []
    for item in items:
        identities = capture.aliases.get(item.id, (item.id,)) if capture is not None else (item.id,)
        numbers = [known[identity].citation_number for identity in identities if identity in known and known[identity].citation_number is not None]
        bound.append(item.model_copy(update={'metadata': {**item.metadata, 'citation_numbers': list(dict.fromkeys(numbers)), 'citation_number': numbers[0] if len(numbers) == 1 else None}}))
    return bound

def citation_map(items):
    return [{'citation_numbers': item.metadata.get('citation_numbers', []), 'evidence_id': item.id, 'source_title': item.source_title, 'page_range': item.page_range, 'page_coordinate_system': item.metadata.get('page_coordinate_system', ''), 'page_label': item.metadata.get('page_label', '')} for item in items]

def document_citation_map(markdown, candidates, *, normalize=True):
    from openclass_core.services.board_citations import normalize_board_citations
    mapping = normalize_board_citations(markdown).label_map if normalize else {str(number): str(number) for number in footnote_numbers(markdown)}
    result = {}
    for candidate in candidates:
        item = candidate if isinstance(candidate, SourceCitation) else SourceCitation.model_validate(candidate)
        label = mapping.get(str(item.citation_number))
        if label is not None and label.isdigit():
            result.setdefault(int(label), []).append(item.evidence_id)
    return {number: tuple(ids) for number, ids in result.items()}

def remember_document_numbering(markdown, *, normalize=True):
    capture = _current.get()
    if capture is not None:
        capture.document_numbers = document_citation_map(markdown, capture.values(), normalize=normalize)

def attach_cited(response, candidates, *, document_before: BoardDocument | None=None, document_after: BoardDocument | None=None, document_number_map=None):
    known = {item.evidence_id: item for item in response.source_citations}
    raw_candidates = list(candidates)
    candidates = [item if isinstance(item, SourceCitation) else SourceCitation.model_validate(item) for item in raw_candidates]
    chat_numbers = footnote_numbers(response.chatbot_message)
    board_numbers = set()
    if document_before is not None and document_after is not None:
        old_blocks = Counter(_visible_blocks(document_to_markdown(document_before)))
        for block in _visible_blocks(document_to_markdown(document_after)):
            if old_blocks[block]:
                old_blocks[block] -= 1
            elif not re.match('^\\[\\^\\d+\\]:', block):
                board_numbers.update((int(value) for value in re.findall('\\[\\^(\\d+)\\]', block) if int(value) > 0))
    cited = {marker for block in _visible_blocks(response.chatbot_message) for marker in _MARKER.findall(block)}
    cited.update(_changed_document_markers(document_before, document_after))
    capture = _current.get()
    if capture is not None:
        cited.update((identity for alias, identities in capture.aliases.items() if alias in cited for identity in identities))
    mapped = document_number_map if document_number_map is not None else capture.document_numbers if capture is not None else {}
    board_id_numbers = {identity: int(number) for number, identities in (mapped or {}).items() if int(number) in board_numbers for identity in identities}
    for raw, item in zip(raw_candidates, candidates, strict=True):
        citation = item if isinstance(item, SourceCitation) else SourceCitation.model_validate(item)
        if citation.evidence_id in cited or citation.citation_number in chat_numbers or citation.evidence_id in board_id_numbers:
            if isinstance(raw, dict) and raw.get('source_read_evidence'):
                restored = restore_delivered_read(raw['source_read_evidence'])
                remember([restored])
                citation = citation.model_copy(update={'excerpt': restored.excerpt})
            if citation.evidence_id in board_id_numbers:
                citation = citation.model_copy(update={'citation_number': board_id_numbers[citation.evidence_id]})
            known[citation.evidence_id] = citation
    response.source_citations = list(known.values())
    return response
