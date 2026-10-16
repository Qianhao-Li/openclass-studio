"""Durable deletion identities and recovery, outside the learning artifact.

Only identities, revisions and structural results are stored here, never Q&A
text. Callers hold the RIDOC file lock across prepare, replace and completion.
"""
from __future__ import annotations
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from openclass_core.models import now_iso
from openclass_core.services.ridoc_v3 import RidocV3ConflictError, RidocV3Error

class RidocDeletedTurnError(RidocV3Error):
    """A consumed input cannot recreate a deliberately deleted exchange."""

def advance_history_context(conn: sqlite3.Connection, owner: str, scope: str, lesson: str) -> None:
    """Invalidate runtime context in the caller's completed-edit transaction."""
    conn.execute('INSERT INTO ridoc_context_epochs VALUES (?,?,?,1) ON CONFLICT(owner_id,scope_id,lesson_id) DO UPDATE SET epoch=epoch+1', (owner, scope, lesson))
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ('ridoc_lesson_runtime', 'ridoc_runtime_turns'):
        if table in tables:
            conn.execute(f'DELETE FROM {table} WHERE owner_user_id=? AND package_id=? AND lesson_id=?', (owner, scope, lesson))

class RidocDeletionJournal:

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        with self.connect() as conn:
            conn.executescript('\n                CREATE TABLE IF NOT EXISTS ridoc_deletions (\n                    owner_id TEXT NOT NULL, scope_id TEXT NOT NULL,\n                    mutation_id TEXT NOT NULL, lesson_id TEXT NOT NULL,\n                    node_id TEXT NOT NULL, transaction_id TEXT NOT NULL,\n                    old_revision TEXT NOT NULL, new_revision TEXT NOT NULL,\n                    state TEXT NOT NULL, result_json TEXT NOT NULL,\n                    created_at TEXT NOT NULL,\n                    PRIMARY KEY(owner_id, scope_id, mutation_id)\n                );\n                CREATE TABLE IF NOT EXISTS ridoc_deleted_turns (\n                    owner_id TEXT NOT NULL, scope_id TEXT NOT NULL,\n                    lesson_id TEXT NOT NULL, transaction_id TEXT NOT NULL,\n                    PRIMARY KEY(owner_id, scope_id, lesson_id, transaction_id)\n                );\n                CREATE TABLE IF NOT EXISTS ridoc_turn_input_identities (\n                    owner_id TEXT NOT NULL, scope_id TEXT NOT NULL,\n                    lesson_id TEXT NOT NULL, transaction_id TEXT NOT NULL,\n                    session_id TEXT NOT NULL, input_event_id TEXT NOT NULL,\n                    PRIMARY KEY(owner_id, scope_id, lesson_id, transaction_id),\n                    UNIQUE(owner_id, scope_id, session_id, input_event_id)\n                );\n                CREATE TABLE IF NOT EXISTS ridoc_context_epochs (\n                    owner_id TEXT NOT NULL, scope_id TEXT NOT NULL,\n                    lesson_id TEXT NOT NULL, epoch INTEGER NOT NULL DEFAULT 0,\n                    PRIMARY KEY(owner_id, scope_id, lesson_id)\n                );\n            ')

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=FULL')
            with conn:
                yield conn
        finally:
            conn.close()

    def get(self, owner: str, scope: str, mutation: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM ridoc_deletions WHERE owner_id=? AND scope_id=? AND mutation_id=?', (owner, scope, mutation)).fetchone()
        return dict(row) if row else None

    def pending(self, owner: str, scope: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM ridoc_deletions WHERE owner_id=? AND scope_id=? AND state='prepared'", (owner, scope)).fetchall()
        return [dict(row) for row in rows]

    def prepare(self, owner: str, scope: str, mutation: str, lesson: str, node: str, transaction: str, old: str, new: str, result: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute('INSERT INTO ridoc_deletions VALUES (?,?,?,?,?,?,?,?,?,?,?)', (owner, scope, mutation, lesson, node, transaction, old, new, 'prepared', json.dumps(result, ensure_ascii=False), now_iso()))

    def abort(self, owner: str, scope: str, mutation: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM ridoc_deletions WHERE owner_id=? AND scope_id=? AND mutation_id=? AND state='prepared'", (owner, scope, mutation))

    def complete(self, record: dict[str, Any], *, runtime_package_id: str) -> None:
        owner, scope, lesson = (record['owner_id'], record['scope_id'], record['lesson_id'])
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            changed = conn.execute("UPDATE ridoc_deletions SET state='completed' WHERE owner_id=? AND scope_id=? AND mutation_id=? AND state='prepared'", (owner, scope, record['mutation_id'])).rowcount
            if not changed:
                return
            conn.execute('INSERT OR IGNORE INTO ridoc_deleted_turns VALUES (?,?,?,?)', (owner, scope, lesson, record['transaction_id']))
            advance_history_context(conn, owner, runtime_package_id, lesson)

    def assert_not_deleted(self, owner: str, scope: str, lesson: str, transaction: str, session_id: str='', input_event_id: str='') -> None:
        with self.connect() as conn:
            transactions = {transaction}
            if session_id and input_event_id:
                row = conn.execute('SELECT transaction_id FROM ridoc_turn_input_identities WHERE owner_id=? AND scope_id=? AND session_id=? AND input_event_id=?', (owner, scope, session_id, input_event_id)).fetchone()
                if row:
                    transactions.add(row[0])
            for identity in transactions:
                deleted = conn.execute('SELECT 1 FROM ridoc_deleted_turns WHERE owner_id=? AND scope_id=? AND lesson_id=? AND transaction_id=?', (owner, scope, lesson, identity)).fetchone()
                prepared = conn.execute("SELECT 1 FROM ridoc_deletions WHERE owner_id=? AND scope_id=? AND lesson_id=? AND transaction_id=? AND state='prepared'", (owner, scope, lesson, identity)).fetchone()
                batch_prepared = None
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ridoc_edit_deleted_items'").fetchone():
                    batch_prepared = conn.execute("SELECT 1 FROM ridoc_edit_deleted_items AS item JOIN ridoc_edit_submissions AS batch ON batch.owner_id=item.owner_id AND batch.scope_id=item.scope_id AND batch.mutation_id=item.mutation_id WHERE item.owner_id=? AND item.scope_id=? AND item.lesson_id=? AND item.transaction_id=? AND batch.state='prepared' AND item.kind='M'", (owner, scope, lesson, identity)).fetchone()
                if deleted or prepared or batch_prepared:
                    raise RidocDeletedTurnError('This conversation turn was deleted and cannot be replayed')

    def assert_action_not_deleted(self, owner: str, scope: str, lesson: str, transaction: str, action_id: str='') -> None:
        with self.connect() as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ridoc_deleted_actions'").fetchone():
                return
            deleted = conn.execute("SELECT 1 FROM ridoc_deleted_actions WHERE owner_id=? AND scope_id=? AND lesson_id=? AND (transaction_id=? OR (?!='' AND action_id=?))", (owner, scope, lesson, transaction, action_id, action_id)).fetchone()
            prepared = conn.execute("SELECT 1 FROM ridoc_edit_deleted_items AS item JOIN ridoc_edit_submissions AS batch ON batch.owner_id=item.owner_id AND batch.scope_id=item.scope_id AND batch.mutation_id=item.mutation_id WHERE item.owner_id=? AND item.scope_id=? AND item.lesson_id=? AND item.kind='C' AND batch.state='prepared' AND (item.transaction_id=? OR (?!='' AND item.action_id=?))", (owner, scope, lesson, transaction, action_id, action_id)).fetchone()
            if deleted or prepared:
                raise RidocDeletedTurnError('This document action was permanently deleted and cannot be replayed')

    def bind_input(self, owner: str, scope: str, lesson: str, transaction: str, session_id: str, input_event_id: str) -> None:
        self.assert_not_deleted(owner, scope, lesson, transaction, session_id, input_event_id)
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = conn.execute('SELECT * FROM ridoc_turn_input_identities WHERE owner_id=? AND scope_id=? AND ((session_id=? AND input_event_id=?) OR (lesson_id=? AND transaction_id=?))', (owner, scope, session_id, input_event_id, lesson, transaction)).fetchall()
            if any(((row['lesson_id'], row['transaction_id'], row['session_id'], row['input_event_id']) != (lesson, transaction, session_id, input_event_id) for row in rows)):
                raise RidocV3ConflictError('Chat input identity already belongs to another turn')
            conn.execute('INSERT OR IGNORE INTO ridoc_turn_input_identities VALUES (?,?,?,?,?,?)', (owner, scope, lesson, transaction, session_id, input_event_id))

    def epoch(self, owner: str, scope: str, lesson: str) -> int:
        with self.connect() as conn:
            row = conn.execute('SELECT epoch FROM ridoc_context_epochs WHERE owner_id=? AND scope_id=? AND lesson_id=?', (owner, scope, lesson)).fetchone()
        return int(row[0]) if row else 0
