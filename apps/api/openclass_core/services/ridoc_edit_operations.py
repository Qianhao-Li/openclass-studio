"""Deterministic in-memory History edits over one immutable session base."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from openclass_core.services.ridoc_document_cursor import applied_branch_sequence, document_history_state, set_document_cursor
from openclass_core.services.ridoc_history_edit import delete_message_node
from openclass_core.services.ridoc_learning_sequence import branch_replay, branch_sequence, build_merge_plan, sequence_at_node, set_branch_sequence
from openclass_core.services.ridoc_v3 import RIDOC_V3_DOCUMENT_CURSOR_SPEC_VERSION, RidocArchiveV3, RidocBranch, RidocDocumentNode, RidocMergeEventNode, RidocMessageNode, RidocV3ConflictError, RidocV3Error, content_hash, validate_ridoc_v3
_FIELDS = {'reorder_messages': ({'branch_name', 'ordered_node_ids'}, set()), 'step_document': ({'branch_name', 'direction', 'target_node_id'}, set()), 'step_document_actions': ({'branch_name', 'direction', 'target_node_ids'}, set()), 'delete_message': ({'node_id'}, set()), 'create_branch': ({'name'}, {'from_node_id'}), 'checkout_branch': ({'name'}, set()), 'merge_branch': ({'source_branch_name', 'target_branch_name'}, {'ordered_node_ids'})}

def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RidocV3Error(f'The History edit is missing a valid {field}')
    return value.strip()

def _node_ids(value: Any) -> list[str]:
    if not isinstance(value, list) or any((not isinstance(item, str) or not item.strip() for item in value)):
        raise RidocV3Error('The node order must be a list of valid node IDs')
    return list(value)

def _branch(archive: RidocArchiveV3, lesson_id: str, name: str, *, active: bool=False):
    graph = archive.package.lessons[lesson_id]
    branch = graph.branches.get(name)
    if branch is None:
        raise RidocV3Error('RIDOC branch is unavailable')
    if active and graph.current_branch != name:
        raise RidocV3ConflictError('Switch to the branch you want to edit first')
    if active and branch.archived:
        raise RidocV3Error('History cannot be edited in an archived branch')
    return branch

def _reorder(archive: RidocArchiveV3, lesson_id: str, operation: dict[str, Any]) -> None:
    name = _text(operation['branch_name'], 'branch_name')
    _branch(archive, lesson_id, name, active=True)
    original = branch_sequence(archive, lesson_id, name)
    ordered = _node_ids(operation['ordered_node_ids'])
    if len(ordered) != len(set(ordered)) or set(ordered) != set(original):
        raise RidocV3Error('The order must preserve every node in the current branch exactly once')
    fixed = lambda ids: [identity for identity in ids if not isinstance(archive.nodes[f'{lesson_id}/{identity}'], RidocMessageNode)]
    first = archive.nodes.get(f'{lesson_id}/{ordered[0]}') if ordered else None
    if not isinstance(first, RidocDocumentNode) or not first.initial or fixed(ordered) != fixed(original):
        raise RidocV3Error('Only M conversations can be moved. C0 must remain first, and C document changes must keep their order')
    before = branch_replay(archive, lesson_id, name)['document_ref']
    if ordered != original:
        set_branch_sequence(archive, lesson_id, name, ordered)
        if branch_replay(archive, lesson_id, name)['document_ref'] != before:
            raise RidocV3Error('Reordering conversations cannot change the final board')

def _step(archive: RidocArchiveV3, lesson_id: str, operation: dict[str, Any]) -> None:
    name = _text(operation['branch_name'], 'branch_name')
    branch = _branch(archive, lesson_id, name, active=True)
    direction = _text(operation['direction'], 'direction')
    if direction not in {'undo', 'redo'}:
        raise RidocV3Error('The board step must be undo or redo')
    target = _text(operation['target_node_id'], 'target_node_id')
    state = document_history_state(archive, lesson_id, name)
    if target != state[f'{direction}_node_id']:
        raise RidocV3ConflictError('Only the latest active board change can be undone, and undone changes must be redone in order')
    changes = [identity for identity in branch_sequence(archive, lesson_id, name) if isinstance(archive.nodes[f'{lesson_id}/{identity}'], RidocDocumentNode)]
    cursor = changes[changes.index(target) - 1] if direction == 'undo' else target
    set_document_cursor(archive, lesson_id, name, cursor, changes[-1])
    if archive.manifest['spec_version'] not in {'3.4', '3.5'}:
        archive.manifest['spec_version'] = RIDOC_V3_DOCUMENT_CURSOR_SPEC_VERSION

def _create_branch(archive: RidocArchiveV3, lesson_id: str, operation: dict[str, Any]) -> None:
    graph = archive.package.lessons[lesson_id]
    name = _text(operation['name'], 'name')
    if name in graph.branches:
        raise RidocV3ConflictError('RIDOC branch already exists')
    current = graph.branches[graph.current_branch]
    source_branch = current
    source = operation.get('from_node_id')
    if source is not None:
        source = _text(source, 'from_node_id')
        if f'{lesson_id}/{source}' not in archive.nodes:
            raise RidocV3Error('RIDOC history node is unavailable')
        if archive.manifest.get('spec_version') in {'3.4', '3.5'}:
            original_branch = archive.nodes[f'{lesson_id}/{source}'].branch_name
            candidates = [name for name in graph.branches if source in branch_sequence(archive, lesson_id, name)]
            chosen = graph.current_branch if graph.current_branch in candidates else original_branch if original_branch in candidates else candidates[0] if candidates else None
            if chosen is None:
                raise RidocV3Error('The history node has no verifiable source branch state')
            source_branch = graph.branches[chosen]
            source_ids = branch_sequence(archive, lesson_id, chosen)
            inherited = source_ids[:source_ids.index(source) + 1]
        else:
            inherited = sequence_at_node(archive, lesson_id, source)
    else:
        if archive.manifest.get('spec_version') in {'3.4', '3.5'}:
            from openclass_core.services.ridoc_v34_states import continuation_node_id
            source = continuation_node_id(archive, lesson_id)
        else:
            source = current.head_node_id
        inherited = applied_branch_sequence(archive, lesson_id) if current.document_cursor_node_id is not None else branch_sequence(archive, lesson_id)
    graph.branches[name] = RidocBranch(name=name, head_node_id=source, base_node_id=source, learning_order_merged=source_branch.learning_order_merged, document_occurrences={key: value.model_copy(deep=True) for key, value in source_branch.document_occurrences.items()} if archive.manifest.get('spec_version') in {'3.4', '3.5'} else {})
    if archive.manifest.get('spec_version') == '3.5' and operation.get('from_node_id') is None:
        from openclass_core.services.ridoc_v34_states import archive_state
        actual = archive_state(archive, lesson_id, source_branch.name)
        graph.branches[name].current_state_ref = actual['state_ref']
        graph.branches[name].current_trace_ref = source_branch.current_trace_ref if not source_branch.document_cursor_node_id else None
        graph.branches[name].learning_node_ids = list(inherited)
    graph.current_branch = name
    if not any((isinstance(archive.nodes[f'{lesson_id}/{identity}'], RidocMergeEventNode) for identity in inherited)):
        set_branch_sequence(archive, lesson_id, name, inherited)

def merge_edit_plan(archive: RidocArchiveV3, lesson_id: str, source_branch_name: str) -> dict[str, Any]:
    """The selected draft branch is the target; blockers remain visible in the plan."""
    validate_ridoc_v3(archive)
    if lesson_id not in archive.package.lessons:
        raise RidocV3Error('RIDOC lesson is unavailable')
    graph = archive.package.lessons[lesson_id]
    _branch(archive, lesson_id, graph.current_branch, active=True)
    return build_merge_plan(archive, lesson_id, _text(source_branch_name, 'source_branch_name'))

def _merge(archive: RidocArchiveV3, lesson_id: str, operation: dict[str, Any], *, session_id: str, operation_id: str, created_at: str) -> None:
    graph = archive.package.lessons[lesson_id]
    target_name = _text(operation['target_branch_name'], 'target_branch_name')
    source_name = _text(operation['source_branch_name'], 'source_branch_name')
    target = _branch(archive, lesson_id, target_name, active=True)
    source = _branch(archive, lesson_id, source_name)
    ordered = operation.get('ordered_node_ids')
    if ordered is not None:
        ordered = _node_ids(ordered)
    plan = build_merge_plan(archive, lesson_id, source_name, ordered)
    if plan['blockers']:
        blocker = plan['blockers'][0]
        raise RidocV3ConflictError(f"Node {blocker['node_id']} cannot be merged: {blocker['reason']}")
    transaction = 'history-edit-merge:' + content_hash({'session_id': session_id, 'operation_id': operation_id})
    if any((record.get('transaction_id') == transaction for record in graph.merge_records)):
        raise RidocV3ConflictError('The merge operation identity has already been used')
    if archive.manifest.get('spec_version') in {'3.4', '3.5'}:
        from openclass_core.services.ridoc_v34_states import install_merge_plan
        install_merge_plan(archive, lesson_id, target_name, plan)
    else:
        set_branch_sequence(archive, lesson_id, target_name, plan['ordered_node_ids'])
    target.learning_order_merged = True
    graph.merge_records.append({'transaction_id': transaction, 'source_branch': source_name, 'target_branch': target_name, 'source_head_id': source.head_node_id, 'target_head_id': target.head_node_id, 'base_node_id': plan['base_node_id'], 'label': f'Merge {source_name} into {target_name}', 'created_at': created_at})

def _semantic_state(archive: RidocArchiveV3) -> dict[str, Any]:
    package = archive.package.model_dump(mode='json')
    for lesson_id, lesson in package['lessons'].items():
        lesson.pop('updated_at', None)
        for name, branch in lesson['branches'].items():
            branch['learning_node_ids'] = branch_sequence(archive, lesson_id, name)
            branch.pop('replay_document_refs', None)
    return {'manifest': {key: value for key, value in archive.manifest.items() if key != 'spec_version'}, 'package': package, 'nodes': {key: node.model_dump(mode='json') for key, node in archive.nodes.items()}, 'evidence': archive.evidence, 'assets': {key: content_hash(value) for key, value in archive.assets.items()}, 'document_states': {key: value.model_dump(mode='json') for key, value in archive.document_states.items()}, 'node_refs': {key: value.model_dump(mode='json') for key, value in archive.node_refs.items()}}

def apply_edit_operations(base_archive: RidocArchiveV3, lesson_id: str, operations: list[dict[str, Any]], *, session_id: str, created_at: str) -> tuple[RidocArchiveV3, list[dict[str, str]], bool]:
    """Apply the complete operation list atomically to a clone, without I/O."""
    validate_ridoc_v3(base_archive)
    if lesson_id not in base_archive.package.lessons:
        raise RidocV3Error('RIDOC lesson is unavailable')
    session_id = _text(session_id, 'session_id')
    created_at = _text(created_at, 'created_at')
    try:
        datetime.fromisoformat(created_at.replace('Z', '+00:00'))
    except ValueError as exc:
        raise RidocV3Error('The History edit timestamp must use ISO format') from exc
    if not isinstance(operations, list):
        raise RidocV3Error('History edit operations must be a list')
    edited = deepcopy(base_archive)
    deleted: list[dict[str, str]] = []
    identities: set[str] = set()
    for entry in operations:
        if not isinstance(entry, dict) or set(entry) != {'operation_id', 'operation'}:
            raise RidocV3Error('The History edit operation format is invalid')
        identity = _text(entry['operation_id'], 'operation_id')
        if identity in identities:
            raise RidocV3ConflictError('History edit operation_id values must be unique')
        identities.add(identity)
        operation = entry['operation']
        kind = operation.get('kind') if isinstance(operation, dict) else None
        if not isinstance(kind, str) or kind not in _FIELDS:
            raise RidocV3Error('Unsupported History edit operation')
        required, optional = _FIELDS[kind]
        if not required <= set(operation) or set(operation) - required - optional - {'kind'}:
            raise RidocV3Error('The History edit operation fields are invalid')
        if kind == 'reorder_messages':
            _reorder(edited, lesson_id, operation)
        elif kind == 'step_document':
            _step(edited, lesson_id, operation)
        elif kind == 'delete_message':
            result = delete_message_node(edited, lesson_id, _text(operation['node_id'], 'node_id'))
            deleted.append({'node_id': result['deleted_node_id'], 'transaction_id': result['deleted_transaction_id']})
        elif kind == 'create_branch':
            _create_branch(edited, lesson_id, operation)
        elif kind == 'checkout_branch':
            name = _text(operation['name'], 'name')
            _branch(edited, lesson_id, name)
            edited.package.lessons[lesson_id].current_branch = name
        else:
            _merge(edited, lesson_id, operation, session_id=session_id, operation_id=identity, created_at=created_at)
        validate_ridoc_v3(edited)
    if _semantic_state(edited) == _semantic_state(base_archive):
        return (base_archive, [], False)
    edited.package.lessons[lesson_id].updated_at = created_at
    validate_ridoc_v3(edited)
    return (edited, deleted, True)

@dataclass
class PendingEditCheckpoint:
    """Private, verified operation-prefix state; never part of a RIDOC."""
    display: RidocArchiveV3
    baseline_cursors: dict[tuple[str, str], str | None]
    identities: set[str]
    operations: list[dict[str, Any]]
    session_id: str
    created_at: str
    default_lesson_id: str

def pending_edit_draft(base_archive: RidocArchiveV3, default_lesson_id: str, operations: list[dict[str, Any]], *, session_id: str, created_at: str, checkpoint: PendingEditCheckpoint | None=None, save_checkpoint: Callable[[PendingEditCheckpoint], None] | None=None) -> tuple[RidocArchiveV3, RidocArchiveV3, list[dict[str, str]], list[str], bool]:
    """Keep gray rows in the display graph; remove them only in the effective clone.

    Older lesson-local drafts omit lesson_id. Their original lesson remains the
    explicit compatibility default; new operations always carry their lesson.
    """
    from openclass_core.services.ridoc_v34 import remove_nodes
    validate_ridoc_v3(base_archive)
    if checkpoint is not None and (checkpoint.session_id != session_id or checkpoint.created_at != created_at or checkpoint.default_lesson_id != default_lesson_id or (checkpoint.operations != operations[:len(checkpoint.operations)])):
        raise RidocV3Error('The History draft checkpoint does not match its operation prefix')
    display = deepcopy(checkpoint.display if checkpoint is not None else base_archive)
    pending: dict[str, set[str]] = getattr(display, '_range_pending', {}) if checkpoint else {}
    range_touched: set[tuple[str, str]] = getattr(display, '_range_touched', set()) if checkpoint else set()
    display._range_pending = pending
    display._range_touched = range_touched
    baseline_cursors = dict(checkpoint.baseline_cursors) if checkpoint else {(lesson, name): branch.document_cursor_node_id for lesson, graph in display.package.lessons.items() for name, branch in graph.branches.items()}
    identities: set[str] = set(checkpoint.identities) if checkpoint else set()
    fields = {**_FIELDS, 'restore_message': ({'node_id'}, set())}
    for entry in operations[len(checkpoint.operations) if checkpoint else 0:]:
        if not isinstance(entry, dict) or set(entry) - {'lesson_id', 'operation_id', 'operation'} or (not {'operation_id', 'operation'} <= set(entry)):
            raise RidocV3Error('The History edit operation format is invalid')
        identity = _text(entry['operation_id'], 'operation_id')
        if identity in identities:
            raise RidocV3ConflictError('History edit operation_id values must be unique')
        identities.add(identity)
        lesson_id = _text(entry.get('lesson_id', default_lesson_id), 'lesson_id')
        if lesson_id not in display.package.lessons:
            raise RidocV3Error('RIDOC lesson is unavailable')
        operation = entry['operation']
        kind = operation.get('kind') if isinstance(operation, dict) else None
        if not isinstance(kind, str) or kind not in fields:
            raise RidocV3Error('Unsupported History edit operation')
        required, optional = fields[kind]
        if not required <= set(operation) or set(operation) - required - optional - {'kind'}:
            raise RidocV3Error('The History edit operation fields are invalid')
        removals = pending.setdefault(lesson_id, set())
        if kind in {'delete_message', 'restore_message'}:
            node_id = _text(operation['node_id'], 'node_id')
            node = display.nodes.get(f'{lesson_id}/{node_id}')
            if not isinstance(node, RidocMessageNode):
                raise RidocV3Error('Only complete M conversations can be marked or restored')
            if kind == 'delete_message':
                removals.add(node_id)
            else:
                removals.discard(node_id)
        elif kind == 'step_document_actions' or (kind == 'step_document' and range_touched):
            from openclass_core.services.ridoc_range_edit import apply_range_selection
            range_operation = operation if kind == 'step_document_actions' else {'branch_name': operation['branch_name'], 'direction': operation['direction'], 'target_node_ids': [operation['target_node_id']]}
            targets = range_operation['target_node_ids']
            if range_operation['direction'] == 'redo' and len(targets) == 1 and (targets[0] not in removals):
                _step(display, lesson_id, {'branch_name': range_operation['branch_name'], 'direction': 'redo', 'target_node_id': targets[0]})
                branch_name = range_operation['branch_name']
                baseline_cursors[lesson_id, branch_name] = display.package.lessons[lesson_id].branches[branch_name].document_cursor_node_id
            else:
                apply_range_selection(display, lesson_id, range_operation, pending, range_touched)
        elif kind == 'step_document':
            _step(display, lesson_id, operation)
            node_id = operation['target_node_id']
            if operation['direction'] == 'undo':
                removals.add(node_id)
            else:
                if node_id not in removals:
                    branch_name = operation['branch_name']
                    baseline_cursors[lesson_id, branch_name] = display.package.lessons[lesson_id].branches[branch_name].document_cursor_node_id
                removals.discard(node_id)
        elif kind == 'reorder_messages':
            _reorder(display, lesson_id, operation)
        elif kind == 'create_branch':
            if range_touched and any(pending.values()) and (operation.get('from_node_id') is None):
                from openclass_core.services.ridoc_range_edit import materialize_range_draft
                effective_branch_source, _ = materialize_range_draft(display, pending, range_touched)
                _create_branch(effective_branch_source, lesson_id, operation)
                branch = effective_branch_source.package.lessons[lesson_id].branches[operation['name']]
                from openclass_core.services.ridoc_v35 import prepare_range_archive
                prepare_range_archive(display)
                for field in ('documents', 'document_states', 'range_proofs', 'range_traces'):
                    getattr(display, field).update(deepcopy(getattr(effective_branch_source, field)))
                for key, value in effective_branch_source.node_refs.items():
                    if key not in display.node_refs:
                        display.node_refs[key] = value.model_copy(deep=True)
                for key, node in display.nodes.items():
                    if key in display.node_refs:
                        display.node_refs[key].status = 'live'
                display.package.lessons[lesson_id].branches[operation['name']] = branch.model_copy(deep=True)
                display.package.lessons[lesson_id].current_branch = operation['name']
                from openclass_core.services.ridoc_v34_references import prune_objects
                prune_objects(display)
            else:
                if operation.get('from_node_id') in pending.get(lesson_id, set()):
                    raise RidocV3Error('Restore the source node marked for deletion before branching from it')
                _create_branch(display, lesson_id, operation)
        elif kind == 'checkout_branch':
            name = _text(operation['name'], 'name')
            _branch(display, lesson_id, name)
            display.package.lessons[lesson_id].current_branch = name
        else:
            if range_touched and any(pending.get(lesson_id, set())):
                raise RidocV3Error('Restore or save document changes marked for deletion before merging branches')
            _merge(display, lesson_id, operation, session_id=session_id, operation_id=identity, created_at=created_at)
        for lid, graph in display.package.lessons.items():
            for name, branch in graph.branches.items():
                key = (lid, name)
                baseline_cursors.setdefault(key, branch.document_cursor_node_id)
                if range_touched:
                    continue
                changes = [value for value in branch_sequence(display, lid, name) if isinstance(display.nodes[f'{lid}/{value}'], RidocDocumentNode)]
                baseline = baseline_cursors[key]
                position = changes.index(baseline) if baseline is not None else len(changes) - 1
                while position > 0 and changes[position] in pending.get(lid, set()):
                    position -= 1
                branch.document_cursor_node_id = changes[position] if position < len(changes) - 1 else None
        validate_ridoc_v3(display)
    verified_checkpoint = PendingEditCheckpoint(deepcopy(display), dict(baseline_cursors), set(identities), deepcopy(operations), session_id, created_at, default_lesson_id) if save_checkpoint is not None else None
    ordered = {lesson: [identity for identity in display.package.lessons[lesson].node_order if identity in ids] for lesson, ids in pending.items() if ids}
    if range_touched:
        from openclass_core.services.ridoc_range_edit import materialize_range_draft
        effective, removed = materialize_range_draft(display, pending, range_touched)
    else:
        effective = deepcopy(display)
        removed = remove_nodes(effective, ordered) if ordered else []
    changed = _semantic_state(effective) != _semantic_state(base_archive)
    if not changed:
        if verified_checkpoint is not None:
            save_checkpoint(verified_checkpoint)
        return (base_archive, base_archive, [], [], False)
    comparable_base = base_archive
    if effective.manifest.get('spec_version') == '3.5' and base_archive.manifest.get('spec_version') != '3.5':
        from openclass_core.services.ridoc_v35 import prepare_range_archive
        comparable_base = deepcopy(base_archive)
        prepare_range_archive(comparable_base)
    elif effective.manifest.get('spec_version') == '3.4' and base_archive.manifest.get('spec_version') not in {'3.4', '3.5'}:
        from openclass_core.services.ridoc_v34 import upgrade_archive
        comparable_base = deepcopy(base_archive)
        upgrade_archive(comparable_base)
    before, after = (_semantic_state(comparable_base), _semantic_state(effective))
    affected = [lesson for lesson in base_archive.package.lesson_order if before['package']['lessons'][lesson] != after['package']['lessons'][lesson] or {key: value for key, value in before['nodes'].items() if key.startswith(f'{lesson}/')} != {key: value for key, value in after['nodes'].items() if key.startswith(f'{lesson}/')}]
    for lesson in affected:
        display.package.lessons[lesson].updated_at = created_at
        effective.package.lessons[lesson].updated_at = created_at
    validate_ridoc_v3(effective)
    if verified_checkpoint is not None:
        save_checkpoint(verified_checkpoint)
    return (display, effective, removed, affected, True)
