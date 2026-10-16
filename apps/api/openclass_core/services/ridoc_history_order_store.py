"""Recoverable order-edit identities outside the learning artifact.

Only request hashes and revisions are retained. A retry acknowledges the edit;
it never re-applies an old node list after a later deletion or another edit.
"""
from __future__ import annotations
from pathlib import Path
from typing import Any
from openclass_core.services.ridoc_deletion_store import advance_history_context
from openclass_core.services.ridoc_v3 import RidocArchiveV3, RidocV3Error

def _exists(connection: Any) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ridoc_history_orders'").fetchone() is not None

def get_order_operation(store: Any, owner: str, scope: str, mutation: str) -> dict[str, Any] | None:
    with store._connect() as connection:
        if not _exists(connection):
            return None
        row = connection.execute('SELECT * FROM ridoc_history_orders WHERE owner_id=? AND scope_id=? AND mutation_id=?', (owner, scope, mutation)).fetchone()
    return dict(row) if row else None

def prepare_order_operation(store: Any, *, owner: str, scope: str, lesson: str, mutation: str, request_hash: str, old: str, new: str, changed: bool) -> None:
    with store._connect() as connection:
        connection.execute('\n            CREATE TABLE IF NOT EXISTS ridoc_history_orders (\n                owner_id TEXT NOT NULL, scope_id TEXT NOT NULL,\n                mutation_id TEXT NOT NULL, lesson_id TEXT NOT NULL,\n                request_hash TEXT NOT NULL, old_revision TEXT NOT NULL,\n                new_revision TEXT NOT NULL, state TEXT NOT NULL,\n                changed INTEGER NOT NULL,\n                PRIMARY KEY(owner_id, scope_id, mutation_id)\n            )\n        ')
        connection.execute('INSERT INTO ridoc_history_orders VALUES (?,?,?,?,?,?,?,?,?)', (owner, scope, mutation, lesson, request_hash, old, new, 'prepared' if changed else 'completed', int(changed)))

def complete_order_operation(store: Any, record: dict[str, Any]) -> None:
    with store._connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        changed = connection.execute("UPDATE ridoc_history_orders SET state='completed' WHERE owner_id=? AND scope_id=? AND mutation_id=? AND state='prepared'", (record['owner_id'], record['scope_id'], record['mutation_id'])).rowcount
        if changed and record['changed']:
            advance_history_context(connection, record['owner_id'], record['scope_id'], record['lesson_id'])

def recover_history_orders(store: Any, owner: str, path: Path, archive: RidocArchiveV3) -> None:
    scope = store.deletion_scope_id(archive.package.id)
    with store._connect() as connection:
        if not _exists(connection):
            return
        records = connection.execute("SELECT * FROM ridoc_history_orders WHERE owner_id=? AND scope_id=? AND state='prepared'", (owner, scope)).fetchall()
    actual = archive.revision.value if archive.revision else ''
    for value in records:
        record = dict(value)
        if actual == record['new_revision']:
            store._upsert_index(owner, path, archive)
            complete_order_operation(store, record)
        elif actual == record['old_revision']:
            with store._connect() as connection:
                connection.execute("DELETE FROM ridoc_history_orders WHERE owner_id=? AND scope_id=? AND mutation_id=? AND state='prepared'", (owner, scope, record['mutation_id']))
        else:
            raise RidocV3Error('An interrupted history order edit has an unexpected RIDOC revision')
