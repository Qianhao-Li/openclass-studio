"""Range History's draft adapter. Only the owning edit service writes courses."""
from __future__ import annotations
from copy import deepcopy
from typing import Any
from openclass_core.models import BoardDocument
from openclass_core.range_history_models import ContentActionView
from openclass_core.services.ridoc_learning_sequence import branch_sequence
from openclass_core.services.ridoc_display_messages import public_range_reason
from openclass_core.services.ridoc_v3 import RidocDocumentNode, RidocV3Error, content_hash

def source_actions(archive, lesson_id: str, branch_name: str):
    from openclass_core.services.ridoc_range_compilation_cache import verified_source_actions
    return verified_source_actions(archive, lesson_id, branch_name, lambda: _source_actions(archive, lesson_id, branch_name))

def _source_actions(archive, lesson_id: str, branch_name: str):
    if archive.manifest.get('spec_version') not in {'3.4', '3.5'}:
        from openclass_core.services.ridoc_v34 import upgrade_archive
        archive = deepcopy(archive)
        upgrade_archive(archive)
    branch = archive.package.lessons[lesson_id].branches[branch_name]
    ids = branch_sequence(archive, lesson_id, branch_name)
    changes = [archive.nodes[f'{lesson_id}/{identity}'] for identity in ids if isinstance(archive.nodes[f'{lesson_id}/{identity}'], RidocDocumentNode)]
    if not changes or not changes[0].initial:
        raise RidocV3Error('Document range history is missing C0')
    initial = BoardDocument.model_validate(archive.documents[changes[0].after_document_ref])
    actions = []
    for node in changes[1:]:
        occurrence = branch.document_occurrences.get(node.id)
        before_ref, after_ref = (node.before_document_ref, node.after_document_ref)
        if occurrence is not None:
            if not occurrence.before_state_ref:
                raise RidocV3Error('The document change is missing a verified input state')
            before_ref = archive.document_states[occurrence.before_state_ref].document_ref
            after_ref = archive.document_states[occurrence.after_state_ref].document_ref
        actions.append({'node_id': node.id, 'before': BoardDocument.model_validate(archive.documents[before_ref]), 'after': BoardDocument.model_validate(archive.documents[after_ref])})
    return (changes[0].id, initial, actions)

def baseline_withdrawn(archive, lesson_id: str, branch_name: str) -> set[str]:
    from openclass_core.services.ridoc_document_cursor import document_history_state
    return set(document_history_state(archive, lesson_id, branch_name)['withdrawn_node_ids'])

def range_result(archive, lesson_id: str, branch_name: str, excluded=()):
    from openclass_core.services.ridoc_range_history import build_range_history
    from openclass_core.services.ridoc_range_request_cache import replay_result
    _initial_id, initial, actions = source_actions(archive, lesson_id, branch_name)
    allowed = {action['node_id'] for action in actions}
    selected = set(excluded) & allowed
    key = (content_hash(initial), tuple(((item['node_id'], content_hash(item['before']), content_hash(item['after'])) for item in actions)), tuple(sorted(selected)))
    return replay_result(key, lambda: build_range_history(initial, actions, excluded_node_ids=selected))

def range_actions_view(archive, lesson_id: str, branch_name: str, *, blocked_reason=None):
    """The complete graph supplies permission; the visible window never does."""
    branch = archive.package.lessons[lesson_id].branches[branch_name]
    pending = getattr(archive, '_range_pending', {}).get(lesson_id, set())
    touched = getattr(archive, '_range_touched', set())
    excluded = baseline_withdrawn(archive, lesson_id, branch_name)
    legacy_excluded = set(excluded)
    from openclass_core.services.ridoc_document_cursor import document_history_state
    legacy_redo = document_history_state(archive, lesson_id, branch_name)['redo_node_id']
    if (lesson_id, branch_name) in touched:
        excluded |= pending
    c0, _initial, actions = source_actions(archive, lesson_id, branch_name)
    labels = {c0: 'C0', **{item['node_id']: f'C{index}' for index, item in enumerate(actions, 1)}}
    initial_entry = ContentActionView(id=c0, label='C0', sequence=0, initial=True, spans=[{'start': 0, 'end': 1}], blocked_reason='The initial document C0 cannot be undone.')
    try:
        result = range_result(archive, lesson_id, branch_name, excluded)
    except RidocV3Error as exc:
        return [initial_entry.model_dump(), *[ContentActionView(id=item['node_id'], label=labels[item['node_id']], sequence=index, status='withdrawn' if item['node_id'] in pending | excluded else 'active', scope='document', spans=[{'start': 0, 'end': 1}], blocked_reason=public_range_reason(str(exc))).model_dump() for index, item in enumerate(actions, 1)]]
    projected = [initial_entry.model_dump()]
    for index, entry in enumerate(result['entries'], 1):
        identity = entry.get('id', entry.get('node_id'))
        withdrawn = identity in pending | excluded
        blockers = entry.get('blockers', entry.get('blocking_node_ids', []))
        reason = blocked_reason or entry.get('blocked_reason')
        can_redo = bool(entry.get('can_redo'))
        if identity in legacy_excluded and identity not in pending:
            can_redo = can_redo and identity == legacy_redo
            if identity != legacy_redo:
                reason = 'The legacy sequential undo state requires redoing the previous change first.'
        if blockers and (not withdrawn):
            reason = 'First undo changes covering this range: ' + ', '.join((labels.get(value, value) for value in blockers)) + '.'
        elif blockers and withdrawn:
            reason = 'First restore the required change ranges: ' + ', '.join((labels.get(value, value) for value in blockers)) + '.'
        precision = list(dict.fromkeys([public_range_reason(value) for value in [entry.get('scope_reason'), *[item.get('reason') for item in entry.get('ambiguities', [])]] if value]))
        projected.append(ContentActionView(id=identity, label=labels[identity], sequence=index, rank=entry.get('rank', index), spans=entry.get('spans', []), dependencies=entry.get('dependencies', []), blockers=blockers, can_undo=bool(entry.get('can_undo')) and (not withdrawn) and (not blocked_reason), can_redo=can_redo and withdrawn and (not blocked_reason), status='withdrawn' if withdrawn else 'active', scope=entry.get('scope', 'ranges'), summary='; '.join(precision) if precision else 'Dependencies are determined only by the actual changed ranges, independently of meaning or references.', blocked_reason=public_range_reason(reason), changes=entry.get('changes', [])).model_dump())
        projected[-1]['locate_spans'] = entry.get('locate_spans', [])
    return projected

def apply_range_selection(display, lesson_id, operation, pending, touched):
    graph = display.package.lessons[lesson_id]
    name = operation['branch_name']
    if name != graph.current_branch or name not in graph.branches or graph.branches[name].archived:
        raise RidocV3Error('Switch to an editable target branch first')
    direction, targets = (operation['direction'], operation['target_node_ids'])
    if direction not in {'undo', 'redo'} or not targets or len(targets) != len(set(targets)):
        raise RidocV3Error('The document change direction or target set is invalid')
    rows = {row['id']: row for row in range_actions_view(display, lesson_id, name)}
    for identity in targets:
        row = rows.get(identity)
        if row is None or row['initial'] or (not row[f'can_{direction}']):
            raise RidocV3Error((row or {}).get('blocked_reason') or 'The change status has changed. Refresh and select it again')
    candidate = deepcopy(pending)
    selected = candidate.setdefault(lesson_id, set())
    if direction == 'undo':
        selected.update(targets)
    else:
        selected.difference_update(targets)
    candidate_touched = touched | {(lesson_id, name)}
    for lid, branch in candidate_touched:
        excluded = baseline_withdrawn(display, lid, branch) | candidate.get(lid, set())
        range_result(display, lid, branch, excluded)
    pending.clear()
    pending.update(candidate)
    touched.clear()
    touched.update(candidate_touched)

def materialize_range_draft(display, pending, touched):
    from openclass_core.services.ridoc_v35 import install_range_replay
    from openclass_core.services.ridoc_v34 import remove_nodes
    effective = deepcopy(display)
    for lesson_id, branch_name in touched:
        _c0, _initial, actions = source_actions(display, lesson_id, branch_name)
        selected = pending.get(lesson_id, set()) & {item['node_id'] for item in actions}
        if not selected:
            continue
        excluded = baseline_withdrawn(display, lesson_id, branch_name) | selected
        result = range_result(display, lesson_id, branch_name, excluded)
        result['preserve_withdrawn_node_ids'] = baseline_withdrawn(display, lesson_id, branch_name) - selected
        install_range_replay(effective, lesson_id, branch_name, result)
    ordered = {lesson: [identity for identity in display.package.lessons[lesson].node_order if identity in ids] for lesson, ids in pending.items() if ids}
    removed = remove_nodes(effective, ordered) if ordered else []
    return (effective, removed)
