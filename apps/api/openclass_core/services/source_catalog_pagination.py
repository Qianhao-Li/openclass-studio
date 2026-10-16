"""Bind the existing exact-P calibration to this task's original-read receipts."""
from __future__ import annotations
from types import SimpleNamespace
from pydantic import Field
from openclass_core.models import SourceRange
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_codex_catalog import AgentCatalogPaginationRegime, SourcePdfPageOffsetAnchor, _validate_agent_pagination_regimes
from openclass_core.services.source_content_contract import content_digest
from openclass_core.services.source_catalog_page_mapping_proof import PAGE_MAPPING_POLICY_VERSION
PAGINATION_RANGE_CONTRACT_VERSION = 2

class ObservedPaginationAnchor(SourcePdfPageOffsetAnchor):
    node_key: str
    observed_label: str = Field(min_length=1)
    starts_at_page_top: bool = Field(strict=True)
    read_ids: list[str] = Field(min_length=1)

class ObservedPaginationRegime(AgentCatalogPaginationRegime):
    anchors: list[ObservedPaginationAnchor] = Field(min_length=3, max_length=16)

def _normal(value):
    return ''.join(value.split()).casefold()

def observed_heading(task, nodes, raw, *, directory_pages):
    from openclass_core.services.source_catalog_refinement_evidence import read_units, range_was_read
    anchor = ObservedPaginationAnchor.model_validate(raw)
    node = next((node for node in nodes if node.key == anchor.node_key), None)
    if not node or node.printed_page_label.strip() != str(anchor.printed_page):
        raise GoogleSourceError('A body anchor must match its actual directory row and printed page.')
    if _normal(node.title) not in _normal(anchor.observed_label):
        raise GoogleSourceError('The observed body heading does not identify this directory row.')
    if anchor.pdf_file_page in directory_pages:
        raise GoogleSourceError('A TOC page cannot establish a body-page offset.')
    value = SourceRange(kind='pdf_pages', start=anchor.pdf_file_page, end=anchor.pdf_file_page)
    if not range_was_read(value, read_units(task, anchor.read_ids)):
        raise GoogleSourceError('The body anchor needs an actual read of that original page.')
    return anchor

def register_regime(task, nodes, raw, *, page_count, directory_pages):
    regime = ObservedPaginationRegime.model_validate(raw)
    for anchor in regime.anchors:
        observed_heading(task, nodes, anchor.model_dump(), directory_pages=directory_pages)
    previous = task.get('pdf_pagination', {})
    regimes = {item['id']: ObservedPaginationRegime.model_validate(item) for item in previous.get('regimes', [])}
    regimes[regime.id] = regime
    try:
        _validate_agent_pagination_regimes(SimpleNamespace(pagination_regimes=list(regimes.values())), page_count=page_count)
    except RuntimeError as exc:
        raise GoogleSourceError(str(exc)) from exc
    headings = dict(previous.get('headings', {}))
    for anchor in regime.anchors:
        headings[anchor.node_key] = anchor.model_dump(mode='json')
    task['pdf_pagination'] = {'regimes': [item.model_dump(mode='json') for item in regimes.values()], 'headings': headings}

def register_heading(task, nodes, raw, *, directory_pages):
    anchor = observed_heading(task, nodes, raw, directory_pages=directory_pages)
    regimes = [ObservedPaginationRegime.model_validate(item) for item in task.get('pdf_pagination', {}).get('regimes', [])]
    if not any((regime.printed_page_start <= anchor.printed_page <= regime.printed_page_end and anchor.pdf_file_page - anchor.printed_page + 1 == regime.page_offset_p for regime in regimes)):
        raise GoogleSourceError('This body heading does not belong to a proven pagination regime.')
    task['pdf_pagination']['headings'][anchor.node_key] = anchor.model_dump(mode='json')

def _positions(task, nodes, *, page_count, directory_pages):
    state = task.get('pdf_pagination', {})
    regimes = []
    for item in state.get('regimes', []):
        try:
            regime = ObservedPaginationRegime.model_validate(item)
            for anchor in regime.anchors:
                observed_heading(task, nodes, anchor.model_dump(), directory_pages=directory_pages)
            _validate_agent_pagination_regimes(SimpleNamespace(pagination_regimes=[regime]), page_count=page_count)
            regimes.append(regime)
        except (GoogleSourceError, ValueError, RuntimeError):
            continue
    starts, proofs = ({}, {})
    for node in nodes:
        label = node.printed_page_label.strip()
        if not label.isdigit():
            continue
        printed = int(label)
        regime = next((r for r in regimes if len(regimes) == 1 or r.printed_page_start <= printed <= r.printed_page_end), None)
        if not regime:
            continue
        physical = printed + regime.page_offset_p - 1
        starts[node.key] = physical
        proofs[node.key] = regime
    for index in range(len(nodes) - 2, -1, -1):
        node, first_child = (nodes[index], nodes[index + 1])
        if not node.printed_page_label.strip() and first_child.level > node.level and (first_child.key in starts):
            starts[node.key] = starts[first_child.key]
            proofs[node.key] = proofs[first_child.key]
    return (starts, proofs, regimes)

def mapped_positions(task, nodes, *, page_count, directory_pages=frozenset()):
    starts, _, _ = _positions(task, nodes, page_count=page_count, directory_pages=directory_pages)
    return starts

def _boundary_context(task, nodes, *, page_count, directory_pages):
    starts, proofs, _ = _positions(task, nodes, page_count=page_count, directory_pages=directory_pages)
    observed = {}
    for node in nodes:
        raw = task.get('pdf_pagination', {}).get('headings', {}).get(node.key)
        if not isinstance(raw, dict):
            continue
        try:
            anchor = observed_heading(task, nodes, raw, directory_pages=directory_pages)
        except (GoogleSourceError, ValueError):
            continue
        if anchor.node_key == node.key and anchor.pdf_file_page == starts.get(node.key) and (1 <= anchor.pdf_file_page <= page_count):
            observed[node.key] = anchor
    return (starts, proofs, observed)

def _following(nodes, index):
    return next((node for node in nodes[index + 1:] if node.level <= nodes[index].level), None)

def _page_end(start, following, starts, observed, page_count, directory_pages):
    if following is None:
        return (page_count, 'file_end', None)
    end = starts.get(following.key)
    if type(end) is not int or end < start or end in directory_pages:
        return (start, 'start_page_only', None)
    if end > page_count:
        return (page_count, 'file_end', None)
    after = observed.get(following.key)
    if end > start and after is not None and (after.starts_at_page_top is True):
        return (end - 1, 'before_observed_page_top', after)
    return (end, 'next_heading_page', None)

def mapped_range_limits(task, nodes, *, page_count, directory_pages=frozenset()):
    starts, _, observed = _boundary_context(task, nodes, page_count=page_count, directory_pages=directory_pages)
    return {node.key: 'next_heading_unlocated' for index, node in enumerate(nodes) if node.key in starts and 1 <= starts[node.key] <= page_count and (_page_end(starts[node.key], _following(nodes, index), starts, observed, page_count, directory_pages)[1] == 'start_page_only')}

def mapped_ranges(task, nodes, *, page_count, directory_pages=frozenset()):
    """Map authored positions to ordinary page scopes using approved calibration."""
    starts, proofs, observed = _boundary_context(task, nodes, page_count=page_count, directory_pages=directory_pages)
    result = {}
    for index, node in enumerate(nodes):
        start = starts.get(node.key)
        if start is None or not 1 <= start <= page_count or start in directory_pages:
            continue
        following = _following(nodes, index)
        end, rule, after = _page_end(start, following, starts, observed, page_count, directory_pages)
        regime = proofs[node.key]
        used = {regime.id: regime}
        if following is not None and following.key in proofs:
            used[proofs[following.key].id] = proofs[following.key]
        anchors = [anchor for item in used.values() for anchor in item.anchors] + ([after] if after else [])
        read_ids = list(dict.fromkeys((identity for anchor in anchors for identity in anchor.read_ids)))
        proof = {'policy_version': PAGE_MAPPING_POLICY_VERSION, 'content_hash': task['content_hash'], 'source_version': task['source_version'], 'page_count': page_count, 'node_key': node.key, 'node_fingerprint': content_digest({key: getattr(node, key) for key in ('key', 'title', 'level', 'printed_page_label')}), 'calibration_digest': content_digest([item.model_dump(mode='json') for item in used.values()]), 'start': start, 'end': end, 'end_rule': rule, 'read_ids': read_ids}
        value = SourceRange(kind='pdf_pages', start=start, end=end, metadata={'citation_precision': 'page', 'page_mapping_proof': proof})
        result[node.key] = (value, read_ids, regime.id)
    return result
