"""Local settings and connection storage; no user or platform account tables."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from openclass_core.services.studio_identity import _private_create

class StudioPreferences(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preferredTextModel: str = Field(default='auto', min_length=1, max_length=160)
    preferredRealtimeModel: str = Field(default='auto', min_length=1, max_length=160)
    preferred_codex_connection_id: str | None = Field(default=None, min_length=1, max_length=128)
    nightMode: bool = False

class StudioStateStore:

    def __init__(self, root: Path):
        if root.is_symlink():
            raise ValueError('The Studio state directory cannot be a symlink.')
        root.mkdir(parents=True, exist_ok=True, mode=448)
        self.path = root / 'studio-state.sqlite3'
        self.key_file = root / 'connections.key'
        if self.key_file.is_symlink():
            raise ValueError('The Studio connection key cannot be a symlink.')
        try:
            _private_create(self.key_file, Fernet.generate_key())
        except FileExistsError:
            pass
        if self.key_file.stat().st_mode & 63:
            raise ValueError('The Studio connection key must be private.')
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS studio_preferences (principal_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, settings_json TEXT NOT NULL)')
        self.path.chmod(384)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            with db:
                yield db

    def preferences(self, principal_id: str):
        with self.connection() as db:
            row = db.execute('SELECT revision,settings_json FROM studio_preferences WHERE principal_id=?', (principal_id,)).fetchone()
        return {'principal_id': principal_id, 'revision': row['revision'] if row else 0, 'settings': StudioPreferences.model_validate(json.loads(row['settings_json']) if row else {}).model_dump()}

    def save_preferences(self, principal_id: str, settings: StudioPreferences, expected_revision: int):
        with self.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT revision FROM studio_preferences WHERE principal_id=?', (principal_id,)).fetchone()
            revision = row[0] if row else 0
            if revision != expected_revision:
                raise HTTPException(409, 'Studio settings changed. Reload before saving.')
            db.execute('INSERT INTO studio_preferences VALUES(?,?,?) ON CONFLICT(principal_id) DO UPDATE SET revision=excluded.revision,settings_json=excluded.settings_json', (principal_id, revision + 1, settings.model_dump_json()))
        return self.preferences(principal_id)

    def connections(self):
        from openclass_core.services.external_connections import ExternalConnectionStore
        return ExternalConnectionStore(self, self.key_file.read_bytes().strip())
