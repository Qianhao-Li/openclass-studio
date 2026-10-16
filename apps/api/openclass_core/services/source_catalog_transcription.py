"""Recoverable original-directory transcription, separate from body mapping."""
from __future__ import annotations
import hashlib
import json
from collections import Counter
from pydantic import BaseModel, ConfigDict, Field
from openclass_core.models import SourceRange
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_catalog_refinement_evidence import RefinementNode, read_units, range_was_read

class TranscribedUnit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_range: SourceRange
    nodes: list[RefinementNode] = Field(default_factory=list)
    read_ids: list[str] = Field(default_factory=list)
    complete: bool = False
    reviewed_for_request_id: str = ''

class DirectoryTranscription(BaseModel):
    model_config = ConfigDict(extra='forbid')
    units: list[TranscribedUnit] = Field(default_factory=list)
    discovery_complete: bool = False
    review_request_id: str = ''

    @property
    def complete(self):
        return self.discovery_complete and bool(self.units) and all((unit.complete for unit in self.units))

    @property
    def review_complete(self):
        return not self.review_request_id or all((unit.reviewed_for_request_id == self.review_request_id for unit in self.units))

def unit_key(value):
    value = SourceRange.model_validate(value)
    coordinates = value.model_dump(mode='json', exclude={'metadata'})
    if value.metadata.get('basic_unit_id') is not None:
        coordinates['basic_unit_id'] = value.metadata['basic_unit_id']
    return json.dumps(coordinates, sort_keys=True, ensure_ascii=False)

def state(task):
    return DirectoryTranscription.model_validate(task.get('directory_transcription') or {})

def plan_units(task, ranges, *, discovery_complete=False):
    directory = state(task)
    previous = {unit_key(unit.source_range): unit for unit in directory.units}
    review_id = directory.review_request_id
    if task.get('directory_review_requested') and directory.complete and directory.review_complete and (task.get('review_completed_units') or (task.get('report') or {}).get('directory_complete')):
        review_id = task['request_id']
    units, seen = ([], set())
    for raw in ranges:
        value = SourceRange.model_validate(raw)
        if value.kind == 'pdf_pages' and (type(value.start) is not int or value.start < 1 or value.start != value.end):
            raise GoogleSourceError('Plan one physical directory page per transcription unit.')
        key = unit_key(value)
        if key in seen:
            raise GoogleSourceError('Directory transcription units must not repeat.')
        seen.add(key)
        units.append(previous.get(key) or TranscribedUnit(source_range=value))
    if discovery_complete and (not units):
        raise GoogleSourceError('An authored directory needs at least one original unit.')
    task['directory_transcription'] = DirectoryTranscription(units=units, discovery_complete=discovery_complete, review_request_id=review_id).model_dump(mode='json')
    if not task.get('directory_clue_archive'):
        from openclass_core.services.source_catalog_reconciliation import snapshot_clues
        task['directory_clue_archive'] = snapshot_clues(task)
    return transcription_view(task)

def _review_identities(directory, index, observed):
    ancestors = []
    for previous in directory.units[:index]:
        for node in previous.nodes:
            while ancestors and ancestors[-1].level >= node.level:
                ancestors.pop()
            ancestors.append(node)

    def coordinates(nodes):
        stack, result = (list(ancestors), [])
        for node in nodes:
            while stack and stack[-1].level >= node.level:
                stack.pop()
            result.append((tuple(((parent.title, parent.level) for parent in stack)), node.title, node.level, node.printed_page_label))
            stack.append(node)
        return result
    unit = directory.units[index]
    previous, current = (coordinates(unit.nodes), coordinates(observed))
    previous_counts, current_counts = (Counter(previous), Counter(current))
    unique = {position: node.key for node, position in zip(unit.nodes, previous, strict=True) if previous_counts[position] == current_counts[position] == 1}
    old_keys = {node.key for node in unit.nodes}
    result = []
    for order, (node, position) in enumerate(zip(observed, current, strict=True)):
        key = unique.get(position, node.key)
        if position not in unique and key in old_keys:
            digest = hashlib.sha256(json.dumps([unit_key(unit.source_range), position, order], ensure_ascii=False).encode()).hexdigest()
            key = 'review_' + digest[:20]
        result.append(node.model_copy(update={'key': key}))
    return result

def transcribe_unit(task, source_range, nodes, read_ids, *, complete, reconciliation_complete=False, _verified_fragment_page=False):
    if _verified_fragment_page:
        from openclass_core.services.source_catalog_fragments import enabled
        if not complete or not enabled(task):
            raise GoogleSourceError('A verified fragment commit requires a complete fragment protocol page.')
    if complete:
        from openclass_core.services.source_catalog_fragments import require_verified_page
        require_verified_page(task, source_range, nodes)
    directory = state(task)
    key = unit_key(source_range)
    index = next((i for i, unit in enumerate(directory.units) if unit_key(unit.source_range) == key), None)
    if index is None:
        raise GoogleSourceError('Plan this directory unit before transcribing it.')
    from openclass_core.services.source_catalog_reconciliation import record_changes, validate_reconciliation
    if reconciliation_complete:
        if not complete:
            raise GoogleSourceError('An unfinished unit cannot finish its reconciliation.')
        validate_reconciliation(task, source_range, read_ids)
    unit = directory.units[index]
    if not range_was_read(unit.source_range, read_units(task, read_ids)):
        raise GoogleSourceError('Transcription requires an actual read of this original directory unit.')
    if directory.review_request_id:
        current_ids = [identity for identity in read_ids if task.get('reads', {}).get(identity, {}).get('request_id') == task['request_id']]
        if not range_was_read(unit.source_range, read_units(task, current_ids)):
            raise GoogleSourceError("A directory review requires this request's actual read of the original unit.")
    observed = [RefinementNode.model_validate(raw).model_copy(update={'directory_read_ids': list(read_ids)}) for raw in nodes]
    if not observed:
        raise GoogleSourceError('An empty transcription cannot finish a planned original directory unit. Read its original rows, or revise the plan if this is not a TOC page.')
    if directory.review_request_id:
        observed = _review_identities(directory, index, observed)
    keys = [node.key for node in observed]
    other_keys = {node.key for i, previous in enumerate(directory.units) if i != index for node in previous.nodes}
    if len(set(keys)) != len(keys) or other_keys.intersection(keys):
        raise GoogleSourceError('Each authored directory row needs a distinct key; continuation context is not a new row.')
    if _verified_fragment_page:
        task['directory_reconciliation'] = {identity: pending for identity, pending in task.get('directory_reconciliation', {}).items() if identity != key}
    else:
        record_changes(task, source_range, unit.nodes, observed, read_ids, complete=complete, reconciled=reconciliation_complete)
    directory.units[index] = unit.model_copy(update={'nodes': observed, 'read_ids': list(read_ids), 'complete': complete, 'reviewed_for_request_id': directory.review_request_id if complete else ''})
    task['directory_transcription'] = directory.model_dump(mode='json')
    return transcription_view(task)

def transcription_view(task):
    from openclass_core.services.source_catalog_directory_gaps import pending_gap
    from openclass_core.services.source_catalog_reconciliation import pending_reconciliation
    from openclass_core.services.source_catalog_fragments import fragments_complete
    directory = state(task)
    reconciliation = pending_reconciliation(task)
    next_unit = next((unit for unit in directory.units if not unit.complete or (directory.review_request_id and unit.reviewed_for_request_id != directory.review_request_id)), None)
    ancestors = []
    focus = next((unit for unit in directory.units if reconciliation and unit_key(unit.source_range) == unit_key(reconciliation['source_range'])), next_unit)
    for unit in directory.units:
        if unit is focus:
            break
        for node in unit.nodes:
            while ancestors and ancestors[-1].level >= node.level:
                ancestors.pop()
            ancestors.append(node)
    gap = pending_gap(task)
    return {'discovery_complete': directory.discovery_complete, 'complete': directory.complete and directory.review_complete and (gap is None) and (reconciliation is None) and fragments_complete(task), 'pending_gap': gap, 'pending_reconciliation': reconciliation, 'units': [{'source_range': unit.source_range.model_dump(mode='json'), 'complete': unit.complete, 'row_count': len(unit.nodes), 'read_ids': unit.read_ids} for unit in directory.units], 'next_unit': focus.source_range.model_dump(mode='json') if focus else None, 'reviewing_saved_unit': bool(next_unit and next_unit.complete and (reconciliation is None)), 'current_rows': reconciliation['current_rows'] if reconciliation else [], 'open_ancestors': [{'key': node.key, 'title': node.title, 'level': node.level} for node in ancestors] if next_unit else []}

def completed_nodes(task):
    from openclass_core.services.source_catalog_directory_gaps import pending_gap
    from openclass_core.services.source_catalog_fragments import fragments_complete
    directory = state(task)
    return [node for unit in directory.units for node in unit.nodes] if directory.complete and pending_gap(task) is None and fragments_complete(task) else None

def restored_page_labels(task):
    """Keep numeric pagination based on the transcribed authored directory."""
    authored = {node.key: node for node in completed_nodes(task) or []}
    restored = []
    for raw in (task.get('report') or {}).get('nodes', []):
        node = RefinementNode.model_validate(raw)
        original = authored.get(node.key)
        if original and original.title == node.title and (original.level == node.level) and original.printed_page_label.strip().isdigit() and (original.printed_page_label != node.printed_page_label) and read_units(task, original.directory_read_ids):
            restored.append(node.model_copy(update={'printed_page_label': original.printed_page_label, 'directory_printed_page_label': original.printed_page_label, 'page_label_reviewed': False, 'directory_read_ids': original.directory_read_ids, 'source_range': None, 'range_complete': False, 'read_ids': []}).model_dump(mode='json'))
    return restored

def progress_key(task):
    """Read receipt IDs, wording of plans and repeated reads are not progress."""

    def rows(nodes):
        return [{key: node.get(key) for key in ('key', 'title', 'level', 'printed_page_label', 'source_range', 'range_complete')} for node in nodes]
    directory = state(task)
    from openclass_core.services.source_catalog_fragments import fragment_progress
    from openclass_core.services.source_catalog_scope_integration import progress as scope_progress
    from openclass_core.services.source_catalog_heading_lines import progress as line_progress
    coverage = {unit_key(unit['locator']) for unit in read_units(task, list(task.get('reads', {})))}
    return json.dumps({'nodes': rows((task.get('report') or {}).get('nodes', [])), 'discovery_complete': directory.discovery_complete, 'review_request_id': directory.review_request_id, 'pdf_pagination': task.get('pdf_pagination', {}), 'directory_reconciliation': {key: {field: value.get(field) for field in ('source_range', 'displaced_rows')} for key, value in task.get('directory_reconciliation', {}).items()}, 'transcription': [{'unit': unit_key(unit.source_range), 'complete': unit.complete, 'reviewed_for_request_id': unit.reviewed_for_request_id, 'nodes': rows([node.model_dump(mode='json') for node in unit.nodes])} for unit in directory.units], 'directory_fragments': fragment_progress(task), 'chapter_scope': scope_progress(task), 'heading_lines': line_progress(task), 'original_units': sorted(coverage)}, sort_keys=True, ensure_ascii=False)
