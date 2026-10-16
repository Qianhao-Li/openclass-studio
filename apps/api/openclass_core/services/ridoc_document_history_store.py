"""Exactly-once document cursor mutations, recoverable outside the RIDOC artifact."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from openclass_core.services.ridoc_deletion_store import advance_history_context
from openclass_core.services.ridoc_v3 import RidocArchiveV3, RidocV3Error

def _exists(connection: Any) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ridoc_document_history_steps'").fetchone() is not None

def get_document_step(store: Any, owner: str, scope: str, mutation: str) -> dict[str, Any] | None:
    with store._connect() as connection:
        if not _exists(connection):
            return None
        row = connection.execute('SELECT * FROM ridoc_document_history_steps WHERE owner_id=? AND scope_id=? AND mutation_id=?', (owner, scope, mutation)).fetchone()
    return dict(row) if row else None

def prepare_document_step(store: Any, *, owner: str, scope: str, lesson: str, mutation: str, request_hash: str, old: str, new: str) -> None:
    with store._connect() as connection:
        connection.execute('\n            CREATE TABLE IF NOT EXISTS ridoc_document_history_steps (\n                owner_id TEXT NOT NULL, scope_id TEXT NOT NULL, mutation_id TEXT NOT NULL,\n                lesson_id TEXT NOT NULL, request_hash TEXT NOT NULL,\n                old_revision TEXT NOT NULL, new_revision TEXT NOT NULL, state TEXT NOT NULL,\n                PRIMARY KEY(owner_id, scope_id, mutation_id)\n            )\n        ')
        connection.execute('INSERT INTO ridoc_document_history_steps VALUES (?,?,?,?,?,?,?,?)', (owner, scope, mutation, lesson, request_hash, old, new, 'prepared'))

def complete_document_step(store: Any, record: dict[str, Any]) -> None:
    with store._connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        changed = connection.execute("UPDATE ridoc_document_history_steps SET state='completed' WHERE owner_id=? AND scope_id=? AND mutation_id=? AND state='prepared'", (record['owner_id'], record['scope_id'], record['mutation_id'])).rowcount
        if changed:
            advance_history_context(connection, record['owner_id'], record['scope_id'], record['lesson_id'])

def recover_document_history_steps(store: Any, owner: str, path: Path, archive: RidocArchiveV3) -> None:
    scope = store.deletion_scope_id(archive.package.id)
    with store._connect() as connection:
        if not _exists(connection):
            return
        records = connection.execute("SELECT * FROM ridoc_document_history_steps WHERE owner_id=? AND scope_id=? AND state='prepared'", (owner, scope)).fetchall()
    actual = archive.revision.value if archive.revision else ''
    for value in records:
        record = dict(value)
        if actual == record['new_revision']:
            store._upsert_index(owner, path, archive)
            complete_document_step(store, record)
        elif actual == record['old_revision']:
            with store._connect() as connection:
                connection.execute("DELETE FROM ridoc_document_history_steps WHERE owner_id=? AND scope_id=? AND mutation_id=? AND state='prepared'", (owner, scope, record['mutation_id']))
        else:
            raise RidocV3Error('An interrupted document history step has an unexpected RIDOC revision')
