"""Retrieval preferences live outside RIDOC and are scoped to an authenticated identity."""
from __future__ import annotations
from functools import lru_cache
from pathlib import Path
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from openclass_core.services.external_connections import ConnectionDatabase
from openclass_core.services.studio_ports import host_service

class SourcePreferenceView(BaseModel):
    enabled: bool = False
    revision: int = 0

class SourcePreferenceUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool
    expected_revision: int = Field(ge=0)

class SourceRetrievalPreferences:

    def __init__(self, store: ConnectionDatabase):
        self.store = store
        with store.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS guest_source_preferences (owner_user_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, revision INTEGER NOT NULL)')

    def _registered(self, owner: str) -> bool:
        return host_service('source_registered', self.store, owner)

    def get(self, owner: str) -> SourcePreferenceView:
        if self._registered(owner):
            return host_service('source_preference', self.store, owner, None)
        with self.store.connection() as db:
            row = db.execute('SELECT enabled,revision FROM guest_source_preferences WHERE owner_user_id=?', (owner,)).fetchone()
        return SourcePreferenceView(enabled=bool(row[0]), revision=row[1]) if row else SourcePreferenceView()

    def update(self, owner: str, payload: SourcePreferenceUpdate) -> SourcePreferenceView:
        if self._registered(owner):
            return host_service('source_preference', self.store, owner, payload)
        with self.store.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT revision FROM guest_source_preferences WHERE owner_user_id=?', (owner,)).fetchone()
            revision = row[0] if row else 0
            if revision != payload.expected_revision:
                raise HTTPException(status_code=409, detail='Source preference changed in another window. Refresh and try again.')
            db.execute('INSERT INTO guest_source_preferences VALUES(?,?,?) ON CONFLICT(owner_user_id) DO UPDATE SET enabled=excluded.enabled,revision=excluded.revision', (owner, int(payload.enabled), revision + 1))
        return SourcePreferenceView(enabled=payload.enabled, revision=revision + 1)

@lru_cache(maxsize=8)
def _preferences(path: str) -> SourceRetrievalPreferences:
    return SourceRetrievalPreferences(host_service('source_preferences_store', Path(path)))

def auto_source_retrieval_enabled(owner_user_id: str, database_path: Path | None=None) -> bool:
    if database_path is None:
        from openclass_core.services.workspace_state import DATABASE_PATH
        database_path = DATABASE_PATH
    return _preferences(str(database_path)).get(owner_user_id).enabled
