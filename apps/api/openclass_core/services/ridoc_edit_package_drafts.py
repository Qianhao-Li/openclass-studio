"""Package-wide draft migration, immutable-base rendering, and change summaries."""
from __future__ import annotations
import json
from typing import Any
from openclass_core.services.ridoc_edit_operations import merge_edit_plan, pending_edit_draft
from openclass_core.services.ridoc_edit_session_store import ensure_tables, exists, get_session, snapshot_path
from openclass_core.services.ridoc_history import history_page
from openclass_core.services.ridoc_learning_sequence import branch_sequence
from openclass_core.services.ridoc_v3 import RidocV3ConflictError, RidocV3Error, read_ridoc_v3
from openclass_core.services.ridoc_workspace_store import materialize_course_package

def build_draft(store: Any, record: dict[str, Any]):
    from openclass_core.services.ridoc_edit_draft_cache import verified_draft
    path = snapshot_path(store, record['owner_id'], record['scope_id'], record['session_id'])

    def build(checkpoint, save_checkpoint):
        base = read_ridoc_v3(path)
        if not base.revision or base.revision.value != record['base_revision'] or base.package.id != record['package_id']:
            raise RidocV3Error("The History edit draft's base snapshot is corrupt")
        result = pending_edit_draft(base, record['lesson_id'], json.loads(record['operations_json']), session_id=record['session_id'], created_at=record['created_at'], checkpoint=checkpoint, save_checkpoint=save_checkpoint)
        return (base, result)
    return verified_draft(store, record, path, build)

def _restore_gray_positions(ordered: list[str], previous: list[str], allowed: set[str]) -> list[str]:
    missing = set(previous) - set(ordered)
    if not missing <= allowed or set(ordered) - set(previous):
        raise RidocV3ConflictError('A legacy reorder draft is missing nodes that cannot be recovered. All drafts have been preserved')
    result = list(ordered)
    for position in range(len(previous) - 1, -1, -1):
        identity = previous[position]
        if identity not in missing:
            continue
        following = next((candidate for candidate in previous[position + 1:] if candidate in result), None)
        result.insert(result.index(following) if following else len(result), identity)
    return result

def package_session(store: Any, owner: str, scope: str, package_id: str) -> dict[str, Any] | None:
    with store._connect() as connection:
        if not exists(connection):
            return None
        ensure_tables(connection)
        rows = [dict(row) for row in connection.execute("SELECT * FROM ridoc_edit_sessions WHERE owner_id=? AND scope_id=? AND package_id=? AND state='editing' ORDER BY created_at,session_id", (owner, scope, package_id))]
    if not rows:
        return None
    if len({(row['base_revision'], row['base_repository_version']) for row in rows}) > 1:
        raise RidocV3ConflictError('Legacy lesson drafts have different base versions. All drafts have been preserved; handle them separately first: ' + ', '.join((row['session_id'] for row in rows)))
    primary = rows[0]
    if len(rows) == 1 and primary['draft_schema_version'] == 2:
        return primary
    combined, identities = ([], set())
    primary_base = read_ridoc_v3(snapshot_path(store, owner, scope, primary['session_id']))
    for row in rows:
        source = read_ridoc_v3(snapshot_path(store, owner, scope, row['session_id']))
        if source.revision is None or source.revision.value != primary['base_revision']:
            raise RidocV3Error("The legacy draft's base snapshot is corrupt. The draft has been preserved")
        for item in json.loads(row['operations_json']):
            identity = item['operation_id']
            if identity in identities:
                identity = f"{row['session_id']}:{identity}"
            identities.add(identity)
            lesson = item.get('lesson_id', row['lesson_id'])
            operation = dict(item['operation'])
            if row['draft_schema_version'] == 1 and operation.get('ordered_node_ids') is not None:
                display, _effective, removed, _affected, _changed = pending_edit_draft(primary_base, primary['lesson_id'], combined, session_id=primary['session_id'], created_at=primary['created_at'])
                pending_messages = {entry['node_id'] for entry in removed if entry['lesson_id'] == lesson and entry['kind'] == 'M'}
                previous = merge_edit_plan(display, lesson, operation['source_branch_name'])['default_node_ids'] if operation['kind'] == 'merge_branch' else branch_sequence(display, lesson, operation['branch_name'])
                operation['ordered_node_ids'] = _restore_gray_positions(operation['ordered_node_ids'], previous, pending_messages)
            combined.append({**item, 'operation': operation, 'operation_id': identity, 'lesson_id': lesson})
    updated = {**primary, 'version': max((row['version'] for row in rows)) + 1, 'operations_json': json.dumps(combined, ensure_ascii=False), 'draft_schema_version': 2}
    _base, (_display, _effective, _removed, _affected, changed) = build_draft(store, updated)
    updated['pending'] = int(changed)
    with store._connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('UPDATE ridoc_edit_sessions SET version=?,operations_json=?,draft_schema_version=2,pending=? WHERE owner_id=? AND scope_id=? AND session_id=?', (updated['version'], updated['operations_json'], int(changed), owner, scope, primary['session_id']))
        for row in rows[1:]:
            connection.execute("UPDATE ridoc_edit_sessions SET state='merged',merged_into=?,pending=0 WHERE owner_id=? AND scope_id=? AND session_id=?", (primary['session_id'], owner, scope, row['session_id']))
    return updated

def session_record(store: Any, owner: str, scope: str, package: str, session_id: str) -> dict[str, Any]:
    seen = set()
    while session_id not in seen:
        seen.add(session_id)
        record = get_session(store, owner, scope, session_id)
        if record is None or record['package_id'] != package:
            raise KeyError(session_id)
        if record.get('merged_into'):
            session_id = record['merged_into']
            continue
        return record
    raise RidocV3Error('The History draft reference is corrupt')

def change_summary(base, display, removed: list[dict[str, str]]) -> list[dict[str, Any]]:
    changes = []
    for lesson_id in base.package.lesson_order:
        before, after = (base.package.lessons[lesson_id], display.package.lessons[lesson_id])
        counts = {'delete_message': sum((item['lesson_id'] == lesson_id and item['kind'] == 'M' for item in removed)), 'remove_document': sum((item['lesson_id'] == lesson_id and item['kind'] == 'C' for item in removed)), 'create_branch': len(set(after.branches) - set(before.branches)), 'checkout_branch': int(before.current_branch != after.current_branch), 'merge_branch': len(after.merge_records) - len(before.merge_records), 'reorder_messages': sum((branch_sequence(base, lesson_id, name) != branch_sequence(display, lesson_id, name) for name in set(before.branches) & set(after.branches)))}
        for kind, count in counts.items():
            if count:
                changes.append({'lesson_id': lesson_id, 'kind': kind, 'count': count})
    return changes

def render_draft(record, requested_lesson_id, display, effective, removed, affected, changed, *, stale: bool, blocked_reason: str | None) -> dict[str, Any]:
    if requested_lesson_id not in display.package.lessons:
        raise RidocV3Error('RIDOC lesson is unavailable')
    pending = {(item['lesson_id'], item['node_id']) for item in removed}
    package = materialize_course_package(display)
    effective_package = materialize_course_package(effective)
    effective_lessons = {lesson.id: lesson for lesson in effective_package.lessons}
    for lesson in package.lessons:
        lesson.board_document = effective_lessons[lesson.id].board_document
        lesson.teaching_guide = effective_lessons[lesson.id].teaching_guide
        for commit in lesson.history_graph.commits:
            commit.metadata['pending_deletion'] = (lesson.id, commit.id) in pending
    history = history_page(display, requested_lesson_id, scope='all', limit=len(display.package.lessons[requested_lesson_id].node_order)).model_dump(mode='json')
    history['learning_node_ids'] = branch_sequence(display, requested_lesson_id)
    history['expected_version'] = record['base_repository_version']
    for node in history['nodes']:
        node['pending_deletion'] = (requested_lesson_id, node['id']) in pending
    return {'id': record['session_id'], 'package_id': record['package_id'], 'lesson_id': requested_lesson_id, 'base_ridoc_revision': record['base_revision'], 'base_repository_version': record['base_repository_version'], 'version': record['version'], 'state': record['state'], 'dirty': changed, 'pending': changed, 'editing_enabled': bool(record.get('editing_enabled', True)), 'stale': stale, 'blocked_reason': blocked_reason, 'operation_count': len(json.loads(record['operations_json'])), 'deleted_node_ids': [item['node_id'] for item in removed if item['lesson_id'] == requested_lesson_id], 'pending_removals': [{key: item[key] for key in ('lesson_id', 'node_id', 'kind')} for item in removed], 'affected_lesson_ids': affected, 'draft_course_package': package, 'draft_history': history}
