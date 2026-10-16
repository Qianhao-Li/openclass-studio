"""Recover atomic sequence merges across file replacement and index updates.

Only transaction identities and revisions live in this runtime journal. Learning
content and the completed merge record remain authoritative in the RIDOC file.
"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Callable
from openclass_core.services.ridoc_lock import ridoc_file_lock
from openclass_core.services.ridoc_v3 import RidocArchiveV3, RidocV3ConflictError, RidocV3Error, ridoc_revision_for_archive, write_ridoc_v3

def _ensure_table(connection: Any) -> None:
    connection.execute('\n        CREATE TABLE IF NOT EXISTS ridoc_sequence_mutations (\n            owner_id TEXT NOT NULL, scope_id TEXT NOT NULL,\n            transaction_id TEXT NOT NULL, old_revision TEXT NOT NULL,\n            new_revision TEXT NOT NULL,\n            PRIMARY KEY(owner_id, scope_id, transaction_id)\n        )\n    ')

def _clear(store: Any, owner_id: str, scope_id: str, transaction_id: str) -> None:
    with store._connect() as connection:
        connection.execute('DELETE FROM ridoc_sequence_mutations WHERE owner_id=? AND scope_id=? AND transaction_id=?', (owner_id, scope_id, transaction_id))

def recover_sequence_mutations(store: Any, owner_id: str, path: Path, archive: RidocArchiveV3) -> None:
    scope = store.deletion_scope_id(archive.package.id)
    with store._connect() as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ridoc_sequence_mutations'").fetchone():
            return
        pending = connection.execute('SELECT * FROM ridoc_sequence_mutations WHERE owner_id=? AND scope_id=?', (owner_id, scope)).fetchall()
    actual = archive.revision.value if archive.revision else ''
    for record in pending:
        if actual == record['new_revision']:
            store._upsert_index(owner_id, path, archive)
        elif actual != record['old_revision']:
            raise RidocV3Error('An interrupted sequence merge has an unexpected RIDOC revision')
        _clear(store, owner_id, scope, record['transaction_id'])

def commit_sequence_mutation(store: Any, owner_id: str, package_id: str, expected_revision: str, transaction_id: str, callback: Callable[[RidocArchiveV3], None]) -> Any:
    path = store.package_path(owner_id, package_id)
    scope = store.deletion_scope_id(package_id)
    with store._lock, ridoc_file_lock(path):
        archive = store.load_archive(owner_id, package_id=package_id)
        from openclass_core.services.ridoc_edit_session_store import assert_no_pending_history
        assert_no_pending_history(store, owner_id, package_id)
        if archive.revision is None or archive.revision.value != expected_revision:
            raise RidocV3ConflictError('RIDOC revision changed; review the merge again')
        previous = store._existing_index(owner_id, package_id)
        callback(archive)
        new_revision = ridoc_revision_for_archive(archive)
        with store._connect() as connection:
            _ensure_table(connection)
            connection.execute('INSERT INTO ridoc_sequence_mutations VALUES (?,?,?,?,?)', (owner_id, scope, transaction_id, expected_revision, new_revision))
        archive.revision = write_ridoc_v3(archive, path, expected_revision=expected_revision)
        result = store._upsert_index(owner_id, path, archive, previous=previous)
        _clear(store, owner_id, scope, transaction_id)
        return result
