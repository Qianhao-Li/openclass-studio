"""Recoverable History drafts and one atomic submission journal outside RIDOC."""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from openclass_core.services.ridoc_deletion_store import advance_history_context
from openclass_core.services.ridoc_v3 import RidocArchiveV3, RidocV3ConflictError, RidocV3Error, read_ridoc_v3
_COMMIT_SCOPE: ContextVar[tuple[str, str] | None] = ContextVar('history_edit_commit_scope', default=None)

@contextmanager
def allow_history_commit(store: Any, owner_user_id: str, package_id: str):
    token = _COMMIT_SCOPE.set((owner_user_id, store.deletion_scope_id(package_id)))
    try:
        yield
    finally:
        _COMMIT_SCOPE.reset(token)

def assert_no_pending_history(store: Any, owner_user_id: str, package_id: str) -> None:
    scope = store.deletion_scope_id(package_id)
    if _COMMIT_SCOPE.get() == (owner_user_id, scope):
        return
    with store._connect() as connection:
        if not exists(connection):
            return
        columns = {row[1] for row in connection.execute('PRAGMA table_info(ridoc_edit_sessions)')}
        predicate = "(pending=1 OR (draft_schema_version=1 AND operations_json!='[]'))" if 'pending' in columns else "operations_json!='[]'"
        pending = connection.execute(f"SELECT 1 FROM ridoc_edit_sessions WHERE owner_id=? AND scope_id=? AND state='editing' AND {predicate} LIMIT 1", (owner_user_id, scope)).fetchone()
    if pending:
        raise RidocV3ConflictError('This course has pending History changes. Review and keep or discard them before making further course changes')

def snapshot_path(store: Any, owner: str, scope: str, session_id: str) -> Path:
    digest = lambda value: hashlib.sha256(value.encode('utf-8')).hexdigest()
    return store.root / '.history-edit-sessions' / digest(owner) / digest(scope) / f'{digest(session_id)}.ridoc'

def cleanup_snapshot(store: Any, record: dict[str, Any]) -> None:
    path = snapshot_path(store, record['owner_id'], record['scope_id'], record['session_id'])
    for candidate in (path, path.with_name(f'{path.name}.lock')):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass

def save_base_snapshot(source: Path, target: Path, revision: str) -> None:
    """Preserve the exact verified base bytes, including older JSON encodings."""
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f'.{target.name}.', suffix='.tmp', dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w+b') as copied, source.open('rb') as original:
            shutil.copyfileobj(original, copied)
            copied.flush()
            os.fsync(copied.fileno())
        restored = read_ridoc_v3(temporary)
        if restored.revision is None or restored.revision.value != revision:
            raise RidocV3Error('History edit base snapshot verification failed')
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)

def exists(connection: Any) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ridoc_edit_sessions'").fetchone() is not None

def ensure_tables(connection: Any) -> None:
    connection.executescript("\n        CREATE TABLE IF NOT EXISTS ridoc_edit_sessions (\n            owner_id TEXT NOT NULL, scope_id TEXT NOT NULL, session_id TEXT NOT NULL,\n            package_id TEXT NOT NULL, lesson_id TEXT NOT NULL, base_revision TEXT NOT NULL,\n            base_repository_version INTEGER, version INTEGER NOT NULL,\n            state TEXT NOT NULL, operations_json TEXT NOT NULL, created_at TEXT NOT NULL,\n            PRIMARY KEY(owner_id, scope_id, session_id)\n        );\n        CREATE UNIQUE INDEX IF NOT EXISTS ridoc_edit_sessions_active\n            ON ridoc_edit_sessions(owner_id, scope_id, lesson_id) WHERE state='editing';\n        CREATE TABLE IF NOT EXISTS ridoc_edit_submissions (\n            owner_id TEXT NOT NULL, scope_id TEXT NOT NULL, mutation_id TEXT NOT NULL,\n            session_id TEXT NOT NULL, lesson_id TEXT NOT NULL, draft_version INTEGER NOT NULL,\n            request_hash TEXT NOT NULL, old_revision TEXT NOT NULL, new_revision TEXT NOT NULL,\n            state TEXT NOT NULL, changed INTEGER NOT NULL,\n            PRIMARY KEY(owner_id, scope_id, mutation_id)\n        );\n        CREATE TABLE IF NOT EXISTS ridoc_edit_deleted_items (\n            owner_id TEXT NOT NULL, scope_id TEXT NOT NULL, mutation_id TEXT NOT NULL,\n            lesson_id TEXT NOT NULL, node_id TEXT NOT NULL, transaction_id TEXT NOT NULL,\n            PRIMARY KEY(owner_id, scope_id, mutation_id, lesson_id, node_id)\n        );\n        CREATE TABLE IF NOT EXISTS ridoc_edit_merged_items (\n            owner_id TEXT NOT NULL, scope_id TEXT NOT NULL, mutation_id TEXT NOT NULL,\n            lesson_id TEXT NOT NULL, transaction_id TEXT NOT NULL,\n            PRIMARY KEY(owner_id, scope_id, mutation_id, lesson_id, transaction_id)\n        );\n        CREATE TABLE IF NOT EXISTS ridoc_deleted_actions (\n            owner_id TEXT NOT NULL, scope_id TEXT NOT NULL, lesson_id TEXT NOT NULL,\n            transaction_id TEXT NOT NULL, action_id TEXT NOT NULL,\n            PRIMARY KEY(owner_id, scope_id, lesson_id, transaction_id, action_id)\n        );\n        CREATE TABLE IF NOT EXISTS ridoc_edit_confirmations (\n            token_hash TEXT PRIMARY KEY, owner_id TEXT NOT NULL, scope_id TEXT NOT NULL,\n            session_id TEXT NOT NULL, draft_version INTEGER NOT NULL, purpose TEXT NOT NULL,\n            payload_hash TEXT NOT NULL\n        );\n    ")
    additions = {'ridoc_edit_sessions': {'editing_enabled': 'INTEGER NOT NULL DEFAULT 1', 'draft_schema_version': 'INTEGER NOT NULL DEFAULT 1', 'pending': 'INTEGER NOT NULL DEFAULT 0', 'merged_into': "TEXT NOT NULL DEFAULT ''"}, 'ridoc_edit_submissions': {'affected_lesson_ids_json': "TEXT NOT NULL DEFAULT '[]'"}, 'ridoc_edit_deleted_items': {'kind': "TEXT NOT NULL DEFAULT 'M'", 'action_id': "TEXT NOT NULL DEFAULT ''"}}
    for table, fields in additions.items():
        columns = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
        for name, declaration in fields.items():
            if name not in columns:
                connection.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
    for table, last in (('ridoc_edit_deleted_items', 'node_id'), ('ridoc_edit_merged_items', 'transaction_id')):
        info = list(connection.execute(f'PRAGMA table_info({table})'))
        primary = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
        expected = ['owner_id', 'scope_id', 'mutation_id', 'lesson_id', last]
        if primary == expected:
            continue
        names = [row[1] for row in info]
        columns = ','.join(names)
        declaration = ','.join((f'{name} TEXT NOT NULL' for name in names))
        connection.execute('SAVEPOINT history_edit_items_schema')
        try:
            connection.execute(f"CREATE TABLE {table}_v2 ({declaration}, PRIMARY KEY({','.join(expected)}))")
            connection.execute(f'INSERT INTO {table}_v2({columns}) SELECT {columns} FROM {table}')
            connection.execute(f'DROP TABLE {table}')
            connection.execute(f'ALTER TABLE {table}_v2 RENAME TO {table}')
            connection.execute('RELEASE history_edit_items_schema')
        except Exception:
            connection.execute('ROLLBACK TO history_edit_items_schema')
            connection.execute('RELEASE history_edit_items_schema')
            raise

def get_session(store: Any, owner: str, scope: str, session_id: str) -> dict[str, Any] | None:
    with store._connect() as connection:
        if not exists(connection):
            return None
        row = connection.execute('SELECT * FROM ridoc_edit_sessions WHERE owner_id=? AND scope_id=? AND session_id=?', (owner, scope, session_id)).fetchone()
    return dict(row) if row else None

def active_session(store: Any, owner: str, scope: str, lesson_id: str) -> dict[str, Any] | None:
    with store._connect() as connection:
        if not exists(connection):
            return None
        row = connection.execute("SELECT * FROM ridoc_edit_sessions WHERE owner_id=? AND scope_id=? AND state='editing' ORDER BY created_at,session_id LIMIT 1", (owner, scope)).fetchone()
    return dict(row) if row else None

def get_submission(store: Any, owner: str, scope: str, mutation_id: str) -> dict[str, Any] | None:
    with store._connect() as connection:
        if not exists(connection):
            return None
        row = connection.execute('SELECT * FROM ridoc_edit_submissions WHERE owner_id=? AND scope_id=? AND mutation_id=?', (owner, scope, mutation_id)).fetchone()
    return dict(row) if row else None

def prepare_submission(store: Any, record: dict[str, Any], *, mutation_id: str, request_hash: str, new_revision: str, changed: bool, deleted_items: list[dict[str, str]], merge_transaction_ids: list[dict[str, str]], affected_lesson_ids: list[str]) -> None:
    with store._connect() as connection:
        ensure_tables(connection)
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('INSERT INTO ridoc_edit_submissions(owner_id,scope_id,mutation_id,session_id,lesson_id,draft_version,request_hash,old_revision,new_revision,state,changed,affected_lesson_ids_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (record['owner_id'], record['scope_id'], mutation_id, record['session_id'], record['lesson_id'], record['version'], request_hash, record['base_revision'], new_revision, 'prepared', int(changed), json.dumps(affected_lesson_ids)))
        connection.executemany('INSERT INTO ridoc_edit_deleted_items(owner_id,scope_id,mutation_id,lesson_id,node_id,transaction_id,kind,action_id) VALUES (?,?,?,?,?,?,?,?)', [(record['owner_id'], record['scope_id'], mutation_id, item.get('lesson_id', record['lesson_id']), item['node_id'], item['transaction_id'], item.get('kind', 'M'), item.get('action_id', '')) for item in deleted_items])
        connection.executemany('INSERT INTO ridoc_edit_merged_items VALUES (?,?,?,?,?)', [(record['owner_id'], record['scope_id'], mutation_id, item['lesson_id'], item['transaction_id']) for item in merge_transaction_ids])

def complete_submission(store: Any, record: dict[str, Any]) -> None:
    with store._connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        completed = connection.execute("UPDATE ridoc_edit_submissions SET state='completed' WHERE owner_id=? AND scope_id=? AND mutation_id=? AND state='prepared'", (record['owner_id'], record['scope_id'], record['mutation_id'])).rowcount
        if completed:
            connection.execute("INSERT OR IGNORE INTO ridoc_deleted_turns(owner_id,scope_id,lesson_id,transaction_id) SELECT owner_id,scope_id,lesson_id,transaction_id FROM ridoc_edit_deleted_items WHERE owner_id=? AND scope_id=? AND mutation_id=? AND kind='M'", (record['owner_id'], record['scope_id'], record['mutation_id']))
            connection.execute("INSERT OR IGNORE INTO ridoc_deleted_actions(owner_id,scope_id,lesson_id,transaction_id,action_id) SELECT owner_id,scope_id,lesson_id,transaction_id,action_id FROM ridoc_edit_deleted_items WHERE owner_id=? AND scope_id=? AND mutation_id=? AND kind='C'", (record['owner_id'], record['scope_id'], record['mutation_id']))
            if record['changed']:
                lessons = json.loads(record.get('affected_lesson_ids_json', '[]')) or [record['lesson_id']]
                for lesson in set(lessons):
                    advance_history_context(connection, record['owner_id'], record['scope_id'], lesson)
            connection.execute("UPDATE ridoc_edit_sessions SET state='committed', operations_json='[]',pending=0,editing_enabled=0 WHERE owner_id=? AND scope_id=? AND session_id=?", (record['owner_id'], record['scope_id'], record['session_id']))
    cleanup_snapshot(store, record)

def submission_result(store: Any, record: dict[str, Any], revision: str, lesson_id: str | None=None) -> dict[str, Any]:
    with store._connect() as connection:
        removed = [dict(row) for row in connection.execute('SELECT lesson_id,node_id,kind FROM ridoc_edit_deleted_items WHERE owner_id=? AND scope_id=? AND mutation_id=? ORDER BY lesson_id,node_id', (record['owner_id'], record['scope_id'], record['mutation_id']))]
    return {'session_id': record['session_id'], 'ridoc_revision': revision, 'changed': bool(record['changed']), 'removed_nodes': removed, 'deleted_node_ids': [item['node_id'] for item in removed if item['lesson_id'] == (lesson_id or record['lesson_id'])], 'mutation_id': record['mutation_id']}

def recover_edit_sessions(store: Any, owner: str, path: Path, archive: RidocArchiveV3) -> None:
    scope = store.deletion_scope_id(archive.package.id)
    with store._connect() as connection:
        if not exists(connection):
            return
        ensure_tables(connection)
        pending = connection.execute("SELECT * FROM ridoc_edit_submissions WHERE owner_id=? AND scope_id=? AND state='prepared'", (owner, scope)).fetchall()
    actual = archive.revision.value if archive.revision else ''
    for row in pending:
        record = dict(row)
        if actual == record['new_revision']:
            if record['changed']:
                store._upsert_index(owner, path, archive)
            complete_submission(store, record)
        elif actual == record['old_revision']:
            with store._connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                for table in ('ridoc_edit_deleted_items', 'ridoc_edit_merged_items', 'ridoc_edit_submissions'):
                    connection.execute(f'DELETE FROM {table} WHERE owner_id=? AND scope_id=? AND mutation_id=?', (owner, scope, record['mutation_id']))
        else:
            raise RidocV3Error('An interrupted History edit has an unexpected RIDOC revision')
    with store._connect() as connection:
        terminal = connection.execute("SELECT owner_id,scope_id,session_id FROM ridoc_edit_sessions WHERE owner_id=? AND scope_id=? AND state!='editing'", (owner, scope)).fetchall()
    for record in terminal:
        cleanup_snapshot(store, dict(record))

def completed_batch_merges(store: Any, owner: str, scope: str, lesson: str) -> set[str]:
    with store._connect() as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ridoc_edit_merged_items'").fetchone():
            return set()
        return {row[0] for row in connection.execute("SELECT item.transaction_id FROM ridoc_edit_merged_items AS item JOIN ridoc_edit_submissions AS batch ON batch.owner_id=item.owner_id AND batch.scope_id=item.scope_id AND batch.mutation_id=item.mutation_id WHERE item.owner_id=? AND item.scope_id=? AND item.lesson_id=? AND batch.state='completed'", (owner, scope, lesson))}
