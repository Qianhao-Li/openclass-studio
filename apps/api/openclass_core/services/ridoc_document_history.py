"""Native sequential C undo/redo. Original course facts are never rewritten."""
from __future__ import annotations
from typing import Any
from openclass_core.models import RidocDocumentHistoryStepRequest, now_iso
from openclass_core.services.ridoc_document_cursor import document_history_state, set_document_cursor
from openclass_core.services.ridoc_document_history_store import complete_document_step, get_document_step, prepare_document_step
from openclass_core.services.ridoc_learning_sequence import branch_sequence
from openclass_core.services.ridoc_lock import ridoc_file_lock
from openclass_core.services.ridoc_v3 import RIDOC_V3_DOCUMENT_CURSOR_SPEC_VERSION, RidocArchiveV3, RidocDocumentNode, RidocV3ConflictError, RidocV3Error, content_hash, ridoc_revision_for_archive, write_ridoc_v3

def _runtime_blocker(store: Any, owner: str, package_id: str, lesson_id: str) -> str | None:
    with store._connect() as connection:
        pending = connection.execute("SELECT 1 FROM ridoc_runtime_turns WHERE owner_user_id=? AND package_id=? AND lesson_id=? AND state='pending' LIMIT 1", (owner, store.deletion_scope_id(package_id), lesson_id)).fetchone()
    if pending:
        return 'A response is being generated. Wait until it finishes before undoing or redoing board changes'
    session = store.repository_store.load_active_merge_session(store.repository_id, lesson_id) if hasattr(store, 'repository_id') else store.load_active_merge_session(owner, lesson_id)
    if session and session.status in {'draft', 'ai_running', 'ready'}:
        return 'Complete or discard the current merge draft before undoing or redoing board changes'
    return None

def document_history_view(archive: RidocArchiveV3, lesson_id: str, *, store: Any=None, owner_user_id: str='', branch_name: str | None=None) -> dict[str, Any]:
    graph = archive.package.lessons[lesson_id]
    branch = graph.branches[branch_name or graph.current_branch]
    try:
        state = document_history_state(archive, lesson_id, branch.name)
    except RidocV3Error as exc:
        return {'current_document_node_id': '', 'undo_node_id': None, 'redo_node_id': None, 'withdrawn_node_ids': [], 'blocked_reason': str(exc)}
    blocked = 'Board changes cannot be undone or redone in an archived branch' if branch.archived else None
    if branch.name != graph.current_branch:
        blocked = 'Switch to this branch before undoing or redoing board changes'
    if not blocked and store is not None:
        blocked = _runtime_blocker(store, owner_user_id, archive.package.id, lesson_id)
    from openclass_core.services.ridoc_range_edit import range_actions_view
    actions = range_actions_view(archive, lesson_id, branch.name, blocked_reason=blocked)
    from openclass_core.services.document_range_projection import attach_range_projections
    actions = attach_range_projections(archive, lesson_id, branch.name, actions)
    if getattr(archive, '_range_touched', set()):
        active = [action['id'] for action in actions if action['status'] == 'active']
        state = {'current_document_node_id': active[-1] if active else '', 'undo_node_id': next((action['id'] for action in reversed(actions) if action['can_undo']), None), 'redo_node_id': next((action['id'] for action in actions if action['can_redo']), None), 'withdrawn_node_ids': [action['id'] for action in actions if action['status'] == 'withdrawn']}
    return {**state, 'blocked_reason': blocked, 'actions': actions}

def _checkpoint(phase: str) -> None:
    """Fault injection seam; no production side effects."""

def step_document_history(store: Any, *, owner_user_id: str, package_id: str, lesson_id: str, request: RidocDocumentHistoryStepRequest) -> dict[str, Any]:
    path = store.package_path(owner_user_id, package_id)
    scope = store.deletion_scope_id(package_id)
    digest = content_hash({'lesson_id': lesson_id, **request.model_dump(mode='json')})
    with store._lock, ridoc_file_lock(path):
        archive = store.load_archive(owner_user_id, package_id=package_id)
        actual = archive.revision.value if archive.revision else ''
        operation = get_document_step(store, owner_user_id, scope, request.mutation_id)
        if operation:
            if operation['request_hash'] != digest:
                raise RidocV3ConflictError('The undo or redo operation identity has already been used by a different request')
            return {'ridoc_revision': actual, 'mutation_id': request.mutation_id, 'document_history': document_history_view(archive, lesson_id, store=store, owner_user_id=owner_user_id)}
        from openclass_core.services.ridoc_edit_session_store import assert_no_pending_history
        assert_no_pending_history(store, owner_user_id, package_id)
        if hasattr(store, 'repository_id'):
            if request.expected_version is None:
                raise RidocV3Error('Repository version is required')
            if store._repository_row(owner_user_id)['version'] != request.expected_version:
                raise RidocV3ConflictError('The repository has changed. Refresh before trying again')
        if actual != request.expected_ridoc_revision:
            raise RidocV3ConflictError('The course has changed. Refresh before undoing or redoing again')
        graph = archive.package.lessons[lesson_id]
        if graph.current_branch != request.expected_branch_name:
            raise RidocV3ConflictError('The current branch has changed. Refresh before trying again')
        state = document_history_view(archive, lesson_id, store=store, owner_user_id=owner_user_id)
        if state['blocked_reason']:
            raise RidocV3Error(state['blocked_reason'])
        if request.target_node_id != state[f'{request.direction}_node_id']:
            raise RidocV3ConflictError('Only the latest active board change can be undone, and undone changes must be redone in order')
        changes = [identity for identity in branch_sequence(archive, lesson_id) if isinstance(archive.nodes[f'{lesson_id}/{identity}'], RidocDocumentNode)]
        position = changes.index(request.target_node_id)
        cursor = changes[position - 1] if request.direction == 'undo' else request.target_node_id
        previous = store._existing_index(owner_user_id, package_id)
        set_document_cursor(archive, lesson_id, graph.current_branch, cursor, changes[-1])
        if archive.manifest.get('spec_version') not in {'3.4', '3.5'}:
            archive.manifest['spec_version'] = RIDOC_V3_DOCUMENT_CURSOR_SPEC_VERSION
        graph.updated_at = now_iso()
        new_revision = ridoc_revision_for_archive(archive)
        prepare_document_step(store, owner=owner_user_id, scope=scope, lesson=lesson_id, mutation=request.mutation_id, request_hash=digest, old=actual, new=new_revision)
        _checkpoint('prepared')
        archive.revision = write_ridoc_v3(archive, path, expected_revision=actual)
        _checkpoint('replaced')
        store._upsert_index(owner_user_id, path, archive, previous=previous)
        _checkpoint('indexed')
        record = get_document_step(store, owner_user_id, scope, request.mutation_id)
        assert record is not None
        complete_document_step(store, record)
        _checkpoint('completed')
        return {'ridoc_revision': archive.revision.value, 'mutation_id': request.mutation_id, 'document_history': document_history_view(archive, lesson_id, store=store, owner_user_id=owner_user_id)}
