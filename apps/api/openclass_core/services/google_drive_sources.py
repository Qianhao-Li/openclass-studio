"""Private Google Drive connection state; never part of a course RIDOC."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import urlsplit
from cryptography.fernet import Fernet, InvalidToken

class DriveSourceError(RuntimeError):

    def __init__(self, message: str, status_code: int=400):
        super().__init__(message)
        self.status_code = status_code

@dataclass(frozen=True)
class DriveConfig:
    client_id: str = ''
    client_secret: str = ''
    token_key: bytes = b''
    origin: str = ''
    picker_api_key: str = ''
    project_number: str = ''
    sync_seconds: int = 300
    max_file_bytes: int = 209715200
    callback_origin: str = ''

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.token_key and self.origin)

    @property
    def redirect_uri(self) -> str:
        return (self.callback_origin or self.origin) + '/api/integrations/google-drive/callback'

    @classmethod
    def from_env(cls) -> DriveConfig:
        client_id = os.getenv('OPENCLASS_GOOGLE_DRIVE_CLIENT_ID', '').strip()
        secret_path = os.getenv('OPENCLASS_GOOGLE_DRIVE_CLIENT_SECRET_FILE', '').strip()
        key_path = os.getenv('OPENCLASS_GOOGLE_DRIVE_TOKEN_KEY_FILE', '').strip()
        origin = os.getenv('OPENCLASS_PUBLIC_ORIGIN', '').rstrip('/')
        callback_origin = os.getenv('OPENCLASS_GOOGLE_DRIVE_CALLBACK_ORIGIN', origin).rstrip('/')
        if not all((client_id, secret_path, key_path, origin)):
            return cls()
        parsed = urlsplit(origin)
        if parsed.scheme != 'https' and (not (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1'})) or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise DriveSourceError('Google Drive requires a configured HTTPS public origin.', 503)
        callback = urlsplit(callback_origin)
        if callback.scheme != 'https' and (not (callback.scheme == 'http' and callback.hostname in {'localhost', '127.0.0.1'})) or callback.path or callback.query or callback.fragment or callback.username:
            raise DriveSourceError('Google Drive callback origin is invalid.', 503)
        try:
            secret = Path(secret_path).read_text().strip()
            key = Path(key_path).read_bytes().strip()
            Fernet(key)
        except (OSError, ValueError) as exc:
            raise DriveSourceError('Google Drive credentials are not configured correctly.', 503) from exc
        return cls(client_id, secret, key, origin, os.getenv('OPENCLASS_GOOGLE_DRIVE_PICKER_API_KEY', ''), os.getenv('OPENCLASS_GOOGLE_PROJECT_NUMBER', ''), max(60, int(os.getenv('OPENCLASS_GOOGLE_DRIVE_SYNC_SECONDS', '300'))), int(os.getenv('OPENCLASS_GOOGLE_MAX_UPLOAD_BYTES', '209715200')), callback_origin=callback_origin)

class DriveSourceStore:

    def __init__(self, path: Path, token_key: bytes):
        self.path = Path(path)
        self.cipher = Fernet(token_key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_file():
            with sqlite3.connect(f'{self.path.resolve().as_uri()}?mode=ro', uri=True) as probe:
                tables = {row[0] for row in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'google_drive_connections' in tables:
                    connection_columns = {row[1] for row in probe.execute('PRAGMA table_info(google_drive_connections)')}
                    state_columns = {row[1] for row in probe.execute('PRAGMA table_info(google_drive_oauth_states)')}
                    link_columns = {row[1] for row in probe.execute('PRAGMA table_info(google_drive_source_links)')}
                    single_owner = any((index[2] and [row[2] for row in probe.execute(f'PRAGMA index_info("{index[1]}")')] == ['owner_user_id'] for index in probe.execute('PRAGMA index_list(google_drive_connections)').fetchall()))
                    if single_owner or 'granted_scopes' not in connection_columns or 'retention' not in link_columns or ('return_path' not in state_columns) or ('access_mode' not in state_columns):
                        from openclass_core.services.external_migration_guard import ExternalMigrationBlocked, require_verified_backup
                        try:
                            require_verified_backup()
                        except ExternalMigrationBlocked as exc:
                            raise DriveSourceError(str(exc), 503) from exc
        with self.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            schema = "\n                CREATE TABLE IF NOT EXISTS google_drive_oauth_states (\n                    state_hash TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL,\n                    redirect_uri TEXT NOT NULL, expires_at REAL NOT NULL, return_path TEXT NOT NULL DEFAULT '/studio'\n                );\n                CREATE TABLE IF NOT EXISTS google_drive_connections (\n                    id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL,\n                    google_subject TEXT NOT NULL, email TEXT NOT NULL,\n                    credentials TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'connected',\n                    cursor TEXT NOT NULL DEFAULT '', synced_at REAL NOT NULL DEFAULT 0,\n                    last_error TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,\n                    granted_scopes TEXT NOT NULL DEFAULT 'https://www.googleapis.com/auth/drive.file',\n                    UNIQUE(owner_user_id, google_subject)\n                );\n                CREATE TABLE IF NOT EXISTS google_drive_source_links (\n                    connection_id TEXT NOT NULL, owner_user_id TEXT NOT NULL,\n                    package_id TEXT NOT NULL, file_id TEXT NOT NULL,\n                    source_id TEXT NOT NULL, drive_version TEXT NOT NULL,\n                    status TEXT NOT NULL DEFAULT 'active', last_error TEXT NOT NULL DEFAULT '',\n                    updated_at REAL NOT NULL, retention TEXT NOT NULL DEFAULT 'explicit_copy',\n                    PRIMARY KEY(owner_user_id, connection_id, package_id, file_id)\n                );\n                CREATE INDEX IF NOT EXISTS google_drive_link_source\n                    ON google_drive_source_links(owner_user_id, package_id, source_id);\n                CREATE TABLE IF NOT EXISTS google_drive_revocations (\n                    owner_user_id TEXT NOT NULL, package_id TEXT NOT NULL, source_id TEXT NOT NULL,\n                    created_at REAL NOT NULL, PRIMARY KEY(owner_user_id, package_id, source_id)\n                );\n            "
            for statement in schema.split(';'):
                if statement.strip():
                    db.execute(statement)
            self._migrate_connections(db)
            if 'return_path' not in {row[1] for row in db.execute('PRAGMA table_info(google_drive_oauth_states)')}:
                db.execute("ALTER TABLE google_drive_oauth_states ADD COLUMN return_path TEXT NOT NULL DEFAULT '/studio'")
            if 'access_mode' not in {row[1] for row in db.execute('PRAGMA table_info(google_drive_oauth_states)')}:
                db.execute("ALTER TABLE google_drive_oauth_states ADD COLUMN access_mode TEXT NOT NULL DEFAULT 'selected_files'")

    @staticmethod
    def _migrate_connections(db):
        single_owner = any((index[2] and [row[2] for row in db.execute(f'PRAGMA index_info("{index[1]}")')] == ['owner_user_id'] for index in db.execute('PRAGMA index_list(google_drive_connections)').fetchall()))
        if single_owner:
            db.execute("CREATE TABLE google_drive_connections_multi (\n                id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL, google_subject TEXT NOT NULL,\n                email TEXT NOT NULL, credentials TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'connected',\n                cursor TEXT NOT NULL DEFAULT '', synced_at REAL NOT NULL DEFAULT 0,\n                last_error TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,\n                UNIQUE(owner_user_id, google_subject))")
            db.execute('INSERT INTO google_drive_connections_multi SELECT id,owner_user_id,google_subject,email,credentials,status,cursor,synced_at,last_error,updated_at FROM google_drive_connections')
            db.execute('DROP TABLE google_drive_connections')
            db.execute('ALTER TABLE google_drive_connections_multi RENAME TO google_drive_connections')
        if 'granted_scopes' not in {row[1] for row in db.execute('PRAGMA table_info(google_drive_connections)')}:
            db.execute("ALTER TABLE google_drive_connections ADD COLUMN granted_scopes TEXT NOT NULL DEFAULT 'https://www.googleapis.com/auth/drive.file'")
        if 'retention' not in {row[1] for row in db.execute('PRAGMA table_info(google_drive_source_links)')}:
            db.execute("CREATE TABLE google_drive_source_links_multi (\n                connection_id TEXT NOT NULL, owner_user_id TEXT NOT NULL, package_id TEXT NOT NULL,\n                file_id TEXT NOT NULL, source_id TEXT NOT NULL, drive_version TEXT NOT NULL,\n                status TEXT NOT NULL DEFAULT 'active', last_error TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,\n                retention TEXT NOT NULL DEFAULT 'explicit_copy',\n                PRIMARY KEY(owner_user_id, connection_id, package_id, file_id))")
            db.execute("INSERT INTO google_drive_source_links_multi\n                SELECT connection_id,owner_user_id,package_id,file_id,source_id,drive_version,\n                CASE WHEN status='revoked' THEN 'detached' ELSE status END,last_error,updated_at,'explicit_copy'\n                FROM google_drive_source_links")
            db.execute('DROP TABLE google_drive_source_links')
            db.execute('ALTER TABLE google_drive_source_links_multi RENAME TO google_drive_source_links')
            db.execute('CREATE INDEX google_drive_link_source ON google_drive_source_links(owner_user_id,package_id,source_id)')

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=30000')
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_state(self, owner_user_id: str, redirect_uri: str, *, expires_at: float | None=None, return_path: str='/studio', access_mode: str='selected_files') -> str:
        if access_mode not in {'selected_files', 'readonly'}:
            raise DriveSourceError('Unknown Drive access mode.')
        if not return_path.startswith('/') or return_path.startswith('//') or any((char in return_path for char in ('\\', '\r', '\n'))) or (len(return_path) > 2048):
            raise DriveSourceError('Invalid return path.')
        state = secrets.token_urlsafe(32)
        with self.transaction() as db:
            db.execute('DELETE FROM google_drive_oauth_states WHERE expires_at < ?', (time.time(),))
            db.execute('INSERT INTO google_drive_oauth_states(state_hash,owner_user_id,redirect_uri,expires_at,return_path,access_mode) VALUES(?,?,?,?,?,?)', (hashlib.sha256(state.encode()).hexdigest(), owner_user_id, redirect_uri, expires_at if expires_at is not None else time.time() + 600, return_path, access_mode))
        return state

    def consume_state(self, state: str, owner_user_id: str) -> dict:
        digest = hashlib.sha256(state.encode()).hexdigest()
        with self.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM google_drive_oauth_states WHERE state_hash=? AND owner_user_id=? AND expires_at>?', (digest, owner_user_id, time.time())).fetchone()
            if row is None:
                raise DriveSourceError('Google Drive authorization expired or belongs to another session.', 403)
            db.execute('DELETE FROM google_drive_oauth_states WHERE state_hash=?', (digest,))
            return dict(row)

    @staticmethod
    def _public(row) -> dict | None:
        if row is None:
            return None
        return {key: row[key] for key in ('id', 'owner_user_id', 'google_subject', 'email', 'status', 'cursor', 'synced_at', 'last_error', 'updated_at', 'granted_scopes')}

    def connection_for_owner(self, owner_user_id: str, connection_id: str | None=None) -> dict | None:
        with self.transaction() as db:
            rows = db.execute('SELECT * FROM google_drive_connections WHERE owner_user_id=?' + (' AND id=?' if connection_id else ''), (owner_user_id, connection_id) if connection_id else (owner_user_id,)).fetchall()
            if len(rows) > 1:
                raise DriveSourceError('Choose a Google Drive account using connection_id.', 409)
            return self._public(rows[0]) if rows else None

    @staticmethod
    def _unified_google_account(db, owner_user_id: str, subject: str) -> bool:
        table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_connections'").fetchone()
        if table is None:
            return False
        return db.execute("SELECT 1 FROM external_connections WHERE owner_user_id=? AND provider='google' AND issuer='https://accounts.google.com' AND subject=? LIMIT 1", (owner_user_id, subject)).fetchone() is not None

    def has_unified_google_account(self, owner_user_id: str) -> bool:
        with self.transaction() as db:
            table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_connections'").fetchone()
            return bool(table and db.execute("SELECT 1 FROM external_connections WHERE owner_user_id=? AND provider='google' LIMIT 1", (owner_user_id,)).fetchone())

    def unified_account_matches(self, owner_user_id: str, subject: str) -> bool:
        with self.transaction() as db:
            return self._unified_google_account(db, owner_user_id, subject)

    def require_legacy_access(self, owner_user_id: str, connection_id: str) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT google_subject FROM google_drive_connections WHERE id=? AND owner_user_id=? AND status='connected'", (connection_id, owner_user_id)).fetchone()
            if row is None:
                raise DriveSourceError('Reconnect Google Drive before accessing this source.', 403)
            if self._unified_google_account(db, owner_user_id, row['google_subject']):
                raise DriveSourceError('This Google account is managed in Settings. Use its connected account there; legacy Drive access is paused.', 409)

    def connections_for_owner(self, owner_user_id: str) -> list[dict]:
        with self.transaction() as db:
            return [self._public(row) for row in db.execute('SELECT * FROM google_drive_connections WHERE owner_user_id=? ORDER BY updated_at,id', (owner_user_id,))]

    def connections(self) -> list[dict]:
        with self.transaction() as db:
            return [self._public(row) for row in db.execute("SELECT * FROM google_drive_connections WHERE status='connected'") if not self._unified_google_account(db, row['owner_user_id'], row['google_subject'])]

    def credentials(self, connection_id: str) -> dict:
        with self.transaction() as db:
            row = db.execute("SELECT credentials,owner_user_id,google_subject FROM google_drive_connections WHERE id=? AND status='connected'", (connection_id,)).fetchone()
            if row is not None and self._unified_google_account(db, row['owner_user_id'], row['google_subject']):
                raise DriveSourceError('This Google account is managed in Settings. Use its connected account there; legacy Drive access is paused.', 409)
        if row is None:
            raise DriveSourceError('Reconnect Google Drive before accessing this source.', 403)
        try:
            return json.loads(self.cipher.decrypt(row[0].encode()))
        except (InvalidToken, ValueError) as exc:
            raise DriveSourceError('Google Drive credentials cannot be decrypted; reconnect the account.', 503) from exc

    def save_connection(self, owner_user_id: str, subject: str, email: str, credentials: dict, *, granted_scopes: str='https://www.googleapis.com/auth/drive.file') -> dict:
        if not owner_user_id or not subject:
            raise DriveSourceError('A verified Google account identity is required.', 403)
        connection_id = 'gdrive_' + secrets.token_hex(12)
        encrypted = self.cipher.encrypt(json.dumps(credentials).encode()).decode()
        with self.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            if self._unified_google_account(db, owner_user_id, subject):
                raise DriveSourceError('This Google account is managed in Settings. Connect Drive there.', 409)
            db.execute("INSERT INTO google_drive_connections\n                (id,owner_user_id,google_subject,email,credentials,status,updated_at,granted_scopes) VALUES(?,?,?,?,?,'connected',?,?)\n                ON CONFLICT(owner_user_id,google_subject) DO UPDATE SET\n                email=excluded.email,credentials=excluded.credentials,status='connected',last_error='',\n                granted_scopes=excluded.granted_scopes,updated_at=excluded.updated_at", (connection_id, owner_user_id, subject, email, encrypted, time.time(), granted_scopes))
            return self._public(db.execute('SELECT * FROM google_drive_connections WHERE owner_user_id=? AND google_subject=?', (owner_user_id, subject)).fetchone())

    def save_link(self, connection_id: str, owner_user_id: str, package_id: str, file_id: str, source_id: str, version: str, *, retention: str='explicit_copy'):
        if retention not in {'explicit_copy', 'temporary'}:
            raise DriveSourceError('Unknown source retention policy.')
        with self.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM google_drive_connections WHERE id=? AND owner_user_id=? AND status='connected'", (connection_id, owner_user_id)).fetchone() is None:
                raise DriveSourceError('Google Drive was disconnected during import.', 403)
            subject = db.execute('SELECT google_subject FROM google_drive_connections WHERE id=?', (connection_id,)).fetchone()[0]
            if self._unified_google_account(db, owner_user_id, subject):
                raise DriveSourceError('This Google account is managed in Settings. Use its connected account there; legacy Drive access is paused.', 409)
            db.execute("INSERT INTO google_drive_source_links\n                (connection_id,owner_user_id,package_id,file_id,source_id,drive_version,updated_at,retention) VALUES(?,?,?,?,?,?,?,?)\n                ON CONFLICT(owner_user_id,connection_id,package_id,file_id) DO UPDATE SET\n                source_id=excluded.source_id,drive_version=excluded.drive_version,status='active',last_error='',updated_at=excluded.updated_at,\n                retention=CASE WHEN google_drive_source_links.retention='explicit_copy' THEN 'explicit_copy' ELSE excluded.retention END", (connection_id, owner_user_id, package_id, file_id, source_id, version, time.time(), retention))

    def links(self, owner_user_id: str, package_id: str | None=None, *, connection_id: str | None=None) -> list[dict]:
        with self.transaction() as db:
            sql = 'SELECT * FROM google_drive_source_links WHERE owner_user_id=?'
            args = [owner_user_id]
            if package_id is not None:
                sql += ' AND package_id=?'
                args.append(package_id)
            if connection_id is not None:
                sql += ' AND connection_id=?'
                args.append(connection_id)
            return [dict(row) for row in db.execute(sql, args)]

    def active_link(self, owner_user_id: str, package_id: str, source_id: str) -> dict | None:
        with self.transaction() as db:
            row = db.execute("SELECT l.* FROM google_drive_source_links l\n                JOIN google_drive_connections c ON c.id=l.connection_id AND c.owner_user_id=l.owner_user_id\n                WHERE l.owner_user_id=? AND l.package_id=? AND l.source_id=? AND l.status='active' AND c.status='connected'", (owner_user_id, package_id, source_id)).fetchone()
            if row and self._unified_google_account(db, owner_user_id, db.execute('SELECT google_subject FROM google_drive_connections WHERE id=?', (row['connection_id'],)).fetchone()[0]):
                return None
            return dict(row) if row else None

    def _revoke(self, db, owner_user_id: str, connection_id: str, file_id: str | None=None):
        where = 'owner_user_id=? AND connection_id=?' + (' AND file_id=?' if file_id else '')
        args = (owner_user_id, connection_id, file_id) if file_id else (owner_user_id, connection_id)
        db.execute("INSERT OR IGNORE INTO google_drive_revocations SELECT owner_user_id,package_id,source_id,? FROM google_drive_source_links WHERE retention='temporary' AND " + where, (time.time(), *args))
        db.execute("UPDATE google_drive_source_links SET status=CASE WHEN retention='explicit_copy' THEN 'detached' ELSE 'revoked' END,updated_at=? WHERE " + where, (time.time(), *args))

    def revoke_file(self, owner_user_id: str, file_id: str, *, connection_id: str | None=None):
        row = self.connection_for_owner(owner_user_id, connection_id)
        if row is None:
            raise DriveSourceError('Unknown Google Drive connection.', 404)
        with self.transaction() as db:
            self._revoke(db, owner_user_id, row['id'], file_id)

    def disconnect(self, owner_user_id: str, *, connection_id: str | None=None, reason: str=''):
        row = self.connection_for_owner(owner_user_id, connection_id)
        if row is None:
            raise DriveSourceError('Unknown Google Drive connection.', 404)
        with self.transaction() as db:
            self._revoke(db, owner_user_id, row['id'])
            db.execute("UPDATE google_drive_connections SET status='disconnected',credentials='',last_error=?,updated_at=? WHERE owner_user_id=? AND id=?", (reason, time.time(), owner_user_id, row['id']))

    def update_sync(self, connection_id: str, *, cursor: str | None=None, error: str=''):
        with self.transaction() as db:
            db.execute('UPDATE google_drive_connections SET cursor=COALESCE(?,cursor),last_error=?,synced_at=?,updated_at=? WHERE id=?', (cursor, error, time.time(), time.time(), connection_id))

    def pending_revocations(self) -> list[dict]:
        with self.transaction() as db:
            return [dict(row) for row in db.execute("SELECT r.* FROM google_drive_revocations r WHERE NOT EXISTS (\n                SELECT 1 FROM google_drive_source_links l WHERE l.owner_user_id=r.owner_user_id\n                AND l.package_id=r.package_id AND l.source_id=r.source_id AND l.retention='explicit_copy')\n                ORDER BY r.created_at")]

    def complete_revocation(self, owner_user_id: str, package_id: str, source_id: str):
        with self.transaction() as db:
            db.execute('DELETE FROM google_drive_revocations WHERE owner_user_id=? AND package_id=? AND source_id=?', (owner_user_id, package_id, source_id))

def is_imported_drive_copy(*, database_path: Path, owner_user_id: str, package_id: str, source_id: str) -> bool:
    """Imported bytes remain user-owned Sources after the remote account disconnects."""
    if not Path(database_path).is_file():
        return False
    with sqlite3.connect(f'{Path(database_path).resolve().as_uri()}?mode=ro', uri=True) as db:
        columns = {row[1] for row in db.execute('PRAGMA table_info(google_drive_source_links)')}
        if not columns:
            return False
        retained = " AND retention='explicit_copy'" if 'retention' in columns else ''
        return db.execute('SELECT 1 FROM google_drive_source_links WHERE owner_user_id=? AND package_id=? AND source_id=?' + retained, (owner_user_id, package_id, source_id)).fetchone() is not None

def ensure_drive_source_access(source, database_path: Path) -> None:
    """Revocation is enforced before model context, independently of remote index deletion."""
    origin = source.metadata.get('origin')
    if isinstance(origin, dict):
        origin = origin.get('origin') or origin.get('provider')
    if origin != 'google_drive':
        return
    if is_imported_drive_copy(database_path=database_path, owner_user_id=source.owner_user_id, package_id=source.package_id, source_id=source.id):
        return
    config = DriveConfig.from_env()
    if not config.configured:
        raise DriveSourceError('Reconnect Google Drive before reading this linked source.', 403)
    store = DriveSourceStore(database_path, config.token_key)
    if store.active_link(source.owner_user_id, source.package_id, source.id) is None:
        raise DriveSourceError('Google Drive access to this source has been revoked.', 403)
