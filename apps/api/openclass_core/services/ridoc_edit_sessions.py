"""Durable History editing previews and a single authoritative save on exit."""
from __future__ import annotations
import json
from typing import Any
from openclass_core.models import new_id, now_iso
from openclass_core.services.ridoc_edit_operations import merge_edit_plan
from openclass_core.services.ridoc_edit_confirmations import issue_token, removal_impact, removal_set_impact, removal_set_purpose, require_token, review_payload
from openclass_core.services.ridoc_edit_package_drafts import build_draft, change_summary, package_session, render_draft, session_record
from openclass_core.services.ridoc_edit_session_store import allow_history_commit, cleanup_snapshot, complete_submission, ensure_tables, get_submission, prepare_submission, save_base_snapshot, snapshot_path, submission_result
from openclass_core.services.ridoc_lock import ridoc_file_lock
from openclass_core.services.ridoc_range_request_cache import range_history_request
from openclass_core.services.ridoc_v3 import RidocArchiveV3, RidocV3ConflictError, RidocV3Error, content_hash, ridoc_revision_for_archive, write_ridoc_v3

def _checkpoint(phase: str) -> None:
    """Fault injection seam around the one authoritative save."""

def _repository_version(store: Any, owner: str) -> int | None:
    return store._repository_row(owner)['version'] if hasattr(store, 'repository_id') else None

def _runtime_blocker(store: Any, owner: str, package: str, lessons: list[str]) -> str | None:
    with store._connect() as connection:
        has_turns = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ridoc_runtime_turns'").fetchone()
        pending = has_turns and connection.execute("SELECT 1 FROM ridoc_runtime_turns WHERE owner_user_id=? AND package_id=? AND state='pending' LIMIT 1", (owner, store.deletion_scope_id(package))).fetchone()
    if pending:
        return 'A response is being generated. Wait until it finishes before editing history'
    for lesson in lessons:
        merge = store.repository_store.load_active_merge_session(store.repository_id, lesson) if hasattr(store, 'repository_id') else store.load_active_merge_session(owner, lesson)
        if merge and merge.status in {'draft', 'ai_running', 'ready'}:
            return 'Complete or discard the current merge draft before editing history'
    return None

def _record(store: Any, owner: str, package: str, lesson: str, session_id: str) -> dict[str, Any]:
    return session_record(store, owner, store.deletion_scope_id(package), package, session_id)

def _draft(store: Any, record: dict[str, Any]):
    return build_draft(store, record)

def _stale(store: Any, record: dict[str, Any], current: RidocArchiveV3) -> bool:
    return not current.revision or current.revision.value != record['base_revision'] or _repository_version(store, record['owner_id']) != record['base_repository_version']

def _editable(store: Any, record: dict[str, Any], current: RidocArchiveV3, version: int) -> None:
    if record['state'] != 'editing':
        raise RidocV3ConflictError('History editing has ended')
    if record['version'] != version:
        raise RidocV3ConflictError('The edit draft was updated on another page. Reload the draft')
    if _stale(store, record, current):
        raise RidocV3ConflictError('The course has changed. The edit draft has been preserved; review it, then discard the draft and start again')
    if (blocked := _runtime_blocker(store, record['owner_id'], record['package_id'], current.package.lesson_order)):
        raise RidocV3Error(blocked)

def _view(store: Any, record: dict[str, Any], current: RidocArchiveV3, lesson_id: str, *, prepared_draft: Any=None, compact: bool=False) -> dict[str, Any]:
    if compact and record['operations_json'] == '[]' and (not record.get('pending')) and (not _stale(store, record, current)):
        from openclass_core.services.ridoc_v3 import read_ridoc_v3
        from openclass_core.services.ridoc_edit_transport import clean_history_session
        from openclass_core.services.ridoc_learning_sequence import branch_sequence
        base = read_ridoc_v3(snapshot_path(store, record['owner_id'], record['scope_id'], record['session_id']))
        if not base.revision or base.revision.value != record['base_revision'] or base.package.id != record['package_id']:
            raise RidocV3Error("The History edit draft's base snapshot is corrupt")
        if lesson_id not in base.package.lessons:
            raise RidocV3Error('RIDOC lesson is unavailable')
        value = clean_history_session({**record, 'lesson_id': lesson_id})
        value['base_learning_node_ids'] = branch_sequence(base, lesson_id)
        value['blocked_reason'] = _runtime_blocker(store, record['owner_id'], record['package_id'], current.package.lesson_order)
        return value
    _base, (display, effective, removed, affected, changed) = prepared_draft if prepared_draft is not None else _draft(store, record)
    stale = _stale(store, record, current)
    blocked = 'The course has changed. The edit draft has been preserved; review it, then discard the draft and start again' if stale else _runtime_blocker(store, record['owner_id'], record['package_id'], current.package.lesson_order)
    return render_draft(record, lesson_id, display, effective, removed, affected, changed, stale=stale, blocked_reason=blocked)

@range_history_request
def get_active_edit_session(store: Any, *, owner_user_id: str, package_id: str, lesson_id: str, compact: bool=False) -> dict[str, Any] | None:
    with store._lock, ridoc_file_lock(store.package_path(owner_user_id, package_id)):
        current = store.load_archive(owner_user_id, package_id=package_id)
        record = package_session(store, owner_user_id, store.deletion_scope_id(package_id), package_id)
        return _view(store, record, current, lesson_id, compact=compact) if record else None

@range_history_request
def begin_edit_session(store: Any, *, owner_user_id: str, package_id: str, lesson_id: str, expected_revision: str, expected_version: int | None=None, compact: bool=False) -> dict[str, Any]:
    with store._lock, ridoc_file_lock(store.package_path(owner_user_id, package_id)):
        current = store.load_archive(owner_user_id, package_id=package_id)
        if lesson_id not in current.package.lessons:
            raise RidocV3Error('RIDOC lesson is unavailable')
        scope = store.deletion_scope_id(package_id)
        if (existing := package_session(store, owner_user_id, scope, package_id)):
            with store._connect() as connection:
                connection.execute('UPDATE ridoc_edit_sessions SET editing_enabled=1 WHERE owner_id=? AND scope_id=? AND session_id=?', (owner_user_id, scope, existing['session_id']))
            return _view(store, {**existing, 'editing_enabled': 1}, current, lesson_id, compact=compact)
        version = _repository_version(store, owner_user_id)
        if compact and expected_version is None:
            expected_version = version
        if not current.revision or expected_revision != current.revision.value or version != expected_version:
            raise RidocV3ConflictError('The course has changed. Refresh before entering edit mode again')
        if (blocked := _runtime_blocker(store, owner_user_id, package_id, current.package.lesson_order)):
            raise RidocV3Error(blocked)
        session_id = new_id('history_edit')
        record = {'owner_id': owner_user_id, 'scope_id': scope, 'session_id': session_id, 'package_id': package_id, 'lesson_id': lesson_id, 'base_revision': expected_revision, 'base_repository_version': version, 'version': 1, 'state': 'editing', 'operations_json': '[]', 'created_at': now_iso(), 'editing_enabled': 1, 'draft_schema_version': 2, 'pending': 0, 'merged_into': ''}
        save_base_snapshot(store.package_path(owner_user_id, package_id), snapshot_path(store, owner_user_id, scope, session_id), expected_revision)
        try:
            with store._connect() as connection:
                ensure_tables(connection)
                connection.execute('INSERT INTO ridoc_edit_sessions(owner_id,scope_id,session_id,package_id,lesson_id,base_revision,base_repository_version,version,state,operations_json,created_at,editing_enabled,draft_schema_version,pending,merged_into) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', tuple(record.values()))
        except Exception:
            cleanup_snapshot(store, record)
            raise
        return _view(store, record, current, lesson_id, compact=compact)

@range_history_request
def update_edit_session(store: Any, *, owner_user_id: str, package_id: str, lesson_id: str, session_id: str, expected_draft_version: int, operation_id: str, operation: dict[str, Any], impact_token: str | None=None, compact: bool=False) -> dict[str, Any]:
    if not operation_id.strip():
        raise RidocV3Error('The edit operation requires a stable identity')
    with store._lock, ridoc_file_lock(store.package_path(owner_user_id, package_id)):
        current = store.load_archive(owner_user_id, package_id=package_id)
        record = _record(store, owner_user_id, package_id, lesson_id, session_id)
        operations = json.loads(record['operations_json'])
        duplicate = next((item for item in operations if item['operation_id'] == operation_id), None)
        if duplicate:
            if duplicate['operation'] != operation or duplicate.get('lesson_id', record['lesson_id']) != lesson_id:
                raise RidocV3ConflictError('The edit operation identity has already been used for different content')
            return _view(store, record, current, lesson_id, compact=compact)
        _editable(store, record, current, expected_draft_version)
        if not record.get('editing_enabled', True):
            raise RidocV3Error('Enter edit mode first')
        target = operation.get('node_id') if operation.get('kind') == 'delete_message' else operation.get('target_node_id') if operation.get('kind') == 'step_document' and operation.get('direction') == 'undo' else None
        if target:
            _base, (display, _effective, _removed, _affected, _changed) = _draft(store, record)
            impact = removal_impact(display, lesson_id, target)
            if impact['requires_confirmation']:
                require_token(store, record, f'impact:{lesson_id}:{target}', impact, impact_token)
        if operation.get('kind') == 'step_document_actions' and operation.get('direction') == 'undo':
            targets = operation['target_node_ids']
            _base, (display, _effective, _removed, _affected, _changed) = _draft(store, record)
            impact = removal_set_impact(display, lesson_id, targets)
            if impact['requires_confirmation']:
                require_token(store, record, removal_set_purpose(lesson_id, targets), impact, impact_token)
        updated = {**record, 'version': record['version'] + 1, 'operations_json': json.dumps([*operations, {'lesson_id': lesson_id, 'operation_id': operation_id, 'operation': operation}], ensure_ascii=False)}
        try:
            prepared_draft = _draft(store, updated)
            _base, (_display, _effective, _removed, _affected, pending) = prepared_draft
            updated['pending'] = int(pending)
        except RidocV3ConflictError as exc:
            raise RidocV3Error(str(exc)) from exc
        with store._connect() as connection:
            changed = connection.execute("UPDATE ridoc_edit_sessions SET version=?, operations_json=?,pending=? WHERE owner_id=? AND scope_id=? AND session_id=? AND state='editing' AND version=?", (updated['version'], updated['operations_json'], updated['pending'], owner_user_id, record['scope_id'], record['session_id'], expected_draft_version)).rowcount
        if not changed:
            raise RidocV3ConflictError('The edit draft was updated on another page. Reload the draft')
        return _view(store, updated, current, lesson_id, prepared_draft=prepared_draft, compact=compact)

@range_history_request
def commit_edit_session(store: Any, *, owner_user_id: str, package_id: str, lesson_id: str, session_id: str, expected_draft_version: int, mutation_id: str, confirmation_token: str | None=None) -> dict[str, Any]:
    if not mutation_id.strip():
        raise RidocV3Error('Saving edits requires a stable mutation identity')
    request_hash = content_hash({'session_id': session_id, 'version': expected_draft_version, 'confirmation_token_hash': content_hash((confirmation_token or '').encode())})
    path = store.package_path(owner_user_id, package_id)
    scope = store.deletion_scope_id(package_id)
    with store._lock, ridoc_file_lock(path), allow_history_commit(store, owner_user_id, package_id):
        current = store.load_archive(owner_user_id, package_id=package_id)
        if (submission := get_submission(store, owner_user_id, scope, mutation_id)):
            if submission['request_hash'] != request_hash:
                raise RidocV3ConflictError('The save operation identity has already been used by a different request')
            return submission_result(store, submission, current.revision.value, lesson_id)
        record = _record(store, owner_user_id, package_id, lesson_id, session_id)
        _editable(store, record, current, expected_draft_version)
        base, (display, edited, deleted, affected, changed) = _draft(store, record)
        review = review_payload(record, display, deleted, affected, change_summary(base, display, deleted), changed)
        require_token(store, record, 'review', review, confirmation_token)
        new_revision = ridoc_revision_for_archive(edited) if changed else record['base_revision']
        previous = store._existing_index(owner_user_id, package_id)
        _checkpoint('before_prepare')
        old_merges = {(lid, item['transaction_id']) for lid, graph in current.package.lessons.items() for item in graph.merge_records}
        prepare_submission(store, record, mutation_id=mutation_id, request_hash=request_hash, new_revision=new_revision, changed=changed, deleted_items=deleted, merge_transaction_ids=[{'lesson_id': lid, 'transaction_id': item['transaction_id']} for lid, graph in edited.package.lessons.items() for item in graph.merge_records if (lid, item['transaction_id']) not in old_merges], affected_lesson_ids=affected)
        _checkpoint('prepared')
        if changed:
            edited.revision = write_ridoc_v3(edited, path, expected_revision=record['base_revision'])
            _checkpoint('replaced')
            store._upsert_index(owner_user_id, path, edited, previous=previous)
            _checkpoint('indexed')
        submission = get_submission(store, owner_user_id, scope, mutation_id)
        assert submission is not None
        complete_submission(store, submission)
        _checkpoint('completed')
        return submission_result(store, submission, new_revision, lesson_id)

def discard_edit_session(store: Any, *, owner_user_id: str, package_id: str, lesson_id: str, session_id: str, expected_draft_version: int) -> dict[str, str]:
    with store._lock, ridoc_file_lock(store.package_path(owner_user_id, package_id)):
        store.load_archive(owner_user_id, package_id=package_id)
        record = _record(store, owner_user_id, package_id, lesson_id, session_id)
        if record['version'] != expected_draft_version or record['state'] == 'committed':
            raise RidocV3ConflictError('The edit draft has changed. Reload it')
        with store._connect() as connection:
            connection.execute("UPDATE ridoc_edit_sessions SET state='discarded', operations_json='[]',pending=0,editing_enabled=0 WHERE owner_id=? AND scope_id=? AND session_id=?", (owner_user_id, record['scope_id'], record['session_id']))
        cleanup_snapshot(store, record)
        return {'session_id': session_id, 'state': 'discarded'}

@range_history_request
def get_edit_merge_plan(store: Any, *, owner_user_id: str, package_id: str, lesson_id: str, session_id: str, source_branch_name: str) -> dict[str, Any]:
    with store._lock, ridoc_file_lock(store.package_path(owner_user_id, package_id)):
        current = store.load_archive(owner_user_id, package_id=package_id)
        record = _record(store, owner_user_id, package_id, lesson_id, session_id)
        _editable(store, record, current, record['version'])
        _base, (display, _effective, _removed, _affected, _changed) = _draft(store, record)
        return {**merge_edit_plan(display, lesson_id, source_branch_name), 'ridoc_revision': record['base_revision']}

@range_history_request
def pause_edit_session(store: Any, *, owner_user_id: str, package_id: str, lesson_id: str, session_id: str, expected_draft_version: int, compact: bool=False) -> dict[str, Any]:
    with store._lock, ridoc_file_lock(store.package_path(owner_user_id, package_id)):
        current = store.load_archive(owner_user_id, package_id=package_id)
        record = _record(store, owner_user_id, package_id, lesson_id, session_id)
        if record['state'] != 'editing' or record['version'] != expected_draft_version:
            raise RidocV3ConflictError('The edit draft has changed. Reload it')
        with store._connect() as connection:
            connection.execute('UPDATE ridoc_edit_sessions SET editing_enabled=0 WHERE owner_id=? AND scope_id=? AND session_id=?', (owner_user_id, record['scope_id'], record['session_id']))
        return _view(store, {**record, 'editing_enabled': 0}, current, lesson_id, compact=compact)

@range_history_request
def preview_edit_removal(store: Any, *, owner_user_id: str, package_id: str, lesson_id: str, session_id: str, expected_draft_version: int, node_id: str | None=None, node_ids: list[str] | None=None) -> dict[str, Any]:
    with store._lock, ridoc_file_lock(store.package_path(owner_user_id, package_id)):
        current = store.load_archive(owner_user_id, package_id=package_id)
        record = _record(store, owner_user_id, package_id, lesson_id, session_id)
        _editable(store, record, current, expected_draft_version)
        _base, (display, _effective, _removed, _affected, _changed) = _draft(store, record)
        if node_ids is not None:
            impact = removal_set_impact(display, lesson_id, node_ids)
            return {**impact, 'impact_token': issue_token(store, record, removal_set_purpose(lesson_id, node_ids), impact)}
        if node_id is None:
            raise RidocV3Error('Select a history node to inspect')
        impact = removal_impact(display, lesson_id, node_id)
        return {**impact, 'impact_token': issue_token(store, record, f'impact:{lesson_id}:{node_id}', impact)}

@range_history_request
def review_edit_session(store: Any, *, owner_user_id: str, package_id: str, lesson_id: str, session_id: str, expected_draft_version: int) -> dict[str, Any]:
    with store._lock, ridoc_file_lock(store.package_path(owner_user_id, package_id)):
        current = store.load_archive(owner_user_id, package_id=package_id)
        record = _record(store, owner_user_id, package_id, lesson_id, session_id)
        _editable(store, record, current, expected_draft_version)
        base, (display, _effective, removed, affected, changed) = _draft(store, record)
        review = review_payload(record, display, removed, affected, change_summary(base, display, removed), changed)
        return {**review, 'confirmation_token': issue_token(store, record, 'review', review)}
