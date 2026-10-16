"""Explicit current-branch reading order; original course facts remain intact."""
from __future__ import annotations
from typing import Any
from openclass_core.models import RidocHistoryOrderRequest, now_iso
from openclass_core.services.ridoc_history_order_store import complete_order_operation, get_order_operation, prepare_order_operation
from openclass_core.services.ridoc_learning_sequence import branch_sequence, replay_sequence, require_replay, set_branch_sequence
from openclass_core.services.ridoc_lock import ridoc_file_lock
from openclass_core.services.ridoc_v3 import RidocArchiveV3, RidocDocumentNode, RidocMessageNode, RidocV3ConflictError, RidocV3Error, content_hash, ridoc_revision_for_archive, write_ridoc_v3

def _runtime_blocker(store: Any, owner: str, package_id: str, lesson_id: str) -> str | None:
    with store._connect() as connection:
        pending = connection.execute("SELECT 1 FROM ridoc_runtime_turns WHERE owner_user_id=? AND package_id=? AND lesson_id=? AND state='pending' LIMIT 1", (owner, store.deletion_scope_id(package_id), lesson_id)).fetchone()
    if pending:
        return 'A response is being generated. Wait until it finishes before reordering'
    session = store.repository_store.load_active_merge_session(store.repository_id, lesson_id) if hasattr(store, 'repository_id') else store.load_active_merge_session(owner, lesson_id)
    if session and session.status in {'draft', 'ai_running', 'ready'}:
        return 'Complete or discard the current merge draft before reordering conversations'
    return None

def history_order_view(archive: RidocArchiveV3, lesson_id: str, *, store: Any=None, owner_user_id: str='') -> dict[str, Any]:
    graph = archive.package.lessons[lesson_id]
    branch = graph.branches[graph.current_branch]
    ordered = branch_sequence(archive, lesson_id)
    blocked = 'Conversations cannot be reordered in an archived branch' if branch.archived else None
    try:
        require_replay(replay_sequence(archive, lesson_id, ordered))
    except RidocV3Error as exc:
        blocked = str(exc)
    if not blocked and (not any((isinstance(archive.nodes[f'{lesson_id}/{value}'], RidocMessageNode) for value in ordered))):
        blocked = 'The current branch has no conversation nodes to reorder'
    if not blocked and store is not None:
        blocked = _runtime_blocker(store, owner_user_id, archive.package.id, lesson_id)
    return {'lesson_id': lesson_id, 'branch_name': graph.current_branch, 'ordered_node_ids': ordered, 'ridoc_revision': archive.revision.value if archive.revision else '', 'blocked_reason': blocked}

def _checkpoint(phase: str) -> None:
    """Fault injection seam; no production side effects."""

def update_history_order(store: Any, *, owner_user_id: str, package_id: str, lesson_id: str, request: RidocHistoryOrderRequest) -> dict[str, Any]:
    path = store.package_path(owner_user_id, package_id)
    scope = store.deletion_scope_id(package_id)
    digest = content_hash({'lesson_id': lesson_id, **request.model_dump(mode='json')})
    with store._lock, ridoc_file_lock(path):
        archive = store.load_archive(owner_user_id, package_id=package_id)
        actual = archive.revision.value if archive.revision else ''
        previous_operation = get_order_operation(store, owner_user_id, scope, request.mutation_id)
        if previous_operation:
            if previous_operation['request_hash'] != digest:
                raise RidocV3ConflictError('The reorder operation identity has already been used by a different request')
            return {'ridoc_revision': actual, 'changed': bool(previous_operation['changed']), 'mutation_id': request.mutation_id}
        from openclass_core.services.ridoc_edit_session_store import assert_no_pending_history
        assert_no_pending_history(store, owner_user_id, package_id)
        if hasattr(store, 'repository_id'):
            if request.expected_version is None:
                raise RidocV3Error('Repository version is required')
            if store._repository_row(owner_user_id)['version'] != request.expected_version:
                raise RidocV3ConflictError('The repository has changed. Refresh before reordering again')
        if actual != request.expected_ridoc_revision:
            raise RidocV3ConflictError('The course has changed. Refresh before reordering again')
        graph = archive.package.lessons[lesson_id]
        if graph.current_branch != request.branch_name:
            raise RidocV3ConflictError('The current branch has changed. Refresh before reordering again')
        view = history_order_view(archive, lesson_id, store=store, owner_user_id=owner_user_id)
        if view['blocked_reason']:
            raise RidocV3Error(view['blocked_reason'])
        original, ordered = (view['ordered_node_ids'], request.ordered_node_ids)
        if len(ordered) != len(set(ordered)) or set(ordered) != set(original):
            raise RidocV3Error('The order must preserve every node in the current branch exactly once')
        fixed = lambda ids: [value for value in ids if not isinstance(archive.nodes[f'{lesson_id}/{value}'], RidocMessageNode)]
        first = archive.nodes[f'{lesson_id}/{ordered[0]}'] if ordered else None
        if not isinstance(first, RidocDocumentNode) or not first.initial or fixed(ordered) != fixed(original):
            raise RidocV3Error('Only M conversations can be moved. C0 must remain first, and C document changes must keep their order')
        changed = ordered != original
        previous = store._existing_index(owner_user_id, package_id)
        if changed:
            before_ref = require_replay(replay_sequence(archive, lesson_id, original))['document_ref']
            set_branch_sequence(archive, lesson_id, request.branch_name, ordered)
            after_ref = require_replay(replay_sequence(archive, lesson_id, ordered))['document_ref']
            if after_ref != before_ref:
                raise RidocV3Error('Reordering conversations cannot change the final board')
            graph.updated_at = now_iso()
        new_revision = ridoc_revision_for_archive(archive) if changed else actual
        _checkpoint('before_prepare')
        prepare_order_operation(store, owner=owner_user_id, scope=scope, lesson=lesson_id, mutation=request.mutation_id, request_hash=digest, old=actual, new=new_revision, changed=changed)
        if changed:
            _checkpoint('prepared')
            archive.revision = write_ridoc_v3(archive, path, expected_revision=actual)
            _checkpoint('replaced')
            store._upsert_index(owner_user_id, path, archive, previous=previous)
            _checkpoint('indexed')
            record = get_order_operation(store, owner_user_id, scope, request.mutation_id)
            assert record is not None
            complete_order_operation(store, record)
            _checkpoint('completed')
        return {'ridoc_revision': new_revision, 'changed': changed, 'mutation_id': request.mutation_id}
