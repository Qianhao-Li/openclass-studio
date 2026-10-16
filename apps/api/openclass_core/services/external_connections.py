"""Owner-scoped external accounts and authorization attempts outside RIDOC."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Any, Protocol
from cryptography.fernet import Fernet, InvalidToken

class ConnectionDatabase(Protocol):

    def connection(self):
        ...

    def transaction(self):
        ...

class ExternalConnectionError(RuntimeError):

    def __init__(self, message: str, status_code: int=400):
        super().__init__(message)
        self.status_code = status_code

def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def _object(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ExternalConnectionError('Invalid stored connection metadata.', 503)
    return parsed

def _return_path(value: str) -> str:
    if not value.startswith('/') or value.startswith('//') or '\\' in value or any((ord(char) < 32 for char in value)):
        raise ExternalConnectionError('Invalid authorization return path.', 400)
    return value

def _resource_scope(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get('mode') not in {'selected', 'all', 'provider', 'models'}:
        raise ExternalConnectionError('Choose a valid external resource scope.', 422)
    if set(value) - {'mode', 'resource_ids', 'folder_ids'}:
        raise ExternalConnectionError('This resource scope contains unsupported settings.', 422)
    for key in ('resource_ids', 'folder_ids'):
        selected = value.get(key, [])
        if not isinstance(selected, list) or len(selected) > 1000 or any((not isinstance(item, str) or not item or len(item) > 2048 for item in selected)) or (len(set(selected)) != len(selected)):
            raise ExternalConnectionError('Choose valid external resources.', 422)
    return value

def capability_resource_scope(connection: dict, capability: str) -> dict:
    grant = connection.get('capabilities', {}).get(capability, {})
    if isinstance(grant, dict) and isinstance(grant.get('resource_scope'), dict):
        return grant['resource_scope']
    if len(connection.get('capabilities', {})) == 1:
        return connection.get('resource_scope', {'mode': 'selected', 'resource_ids': []})
    return {'mode': 'selected', 'resource_ids': []}

class ExternalConnectionStore:

    def __init__(self, auth_store: ConnectionDatabase, token_key: bytes | None=None):
        self.auth_store = auth_store
        try:
            self.cipher = Fernet(token_key) if token_key else None
        except (ValueError, TypeError) as exc:
            raise ExternalConnectionError('External account encryption is not configured.', 503) from exc
        with self.auth_store.transaction() as db:
            db.executescript("\n                CREATE TABLE IF NOT EXISTS external_connections (\n                    id TEXT PRIMARY KEY,\n                    owner_user_id TEXT NOT NULL,\n                    provider TEXT NOT NULL,\n                    issuer TEXT NOT NULL,\n                    subject TEXT NOT NULL,\n                    tenant TEXT NOT NULL DEFAULT '',\n                    display_name TEXT NOT NULL,\n                    credentials TEXT NOT NULL,\n                    capabilities_json TEXT NOT NULL,\n                    resource_scope_json TEXT NOT NULL,\n                    status TEXT NOT NULL CHECK(status IN ('connected','paused','reauth_required','disconnected')),\n                    revision INTEGER NOT NULL DEFAULT 1,\n                    generation INTEGER NOT NULL DEFAULT 1,\n                    last_access_at REAL,\n                    created_at REAL NOT NULL,\n                    updated_at REAL NOT NULL,\n                    UNIQUE(owner_user_id,provider,issuer,subject,tenant)\n                );\n                CREATE INDEX IF NOT EXISTS external_connections_owner\n                    ON external_connections(owner_user_id,provider,status);\n                CREATE TABLE IF NOT EXISTS external_auth_attempts (\n                    state_hash TEXT PRIMARY KEY,\n                    owner_user_id TEXT NOT NULL,\n                    session_hash TEXT NOT NULL,\n                    provider TEXT NOT NULL,\n                    purpose TEXT NOT NULL,\n                    payload_encrypted TEXT NOT NULL,\n                    return_path TEXT NOT NULL,\n                    expires_at REAL NOT NULL\n                );\n                CREATE INDEX IF NOT EXISTS external_auth_attempts_owner\n                    ON external_auth_attempts(owner_user_id,provider,expires_at);\n                CREATE TABLE IF NOT EXISTS external_source_scopes (\n                    owner_user_id TEXT NOT NULL,\n                    package_id TEXT NOT NULL,\n                    scope_json TEXT NOT NULL,\n                    revision INTEGER NOT NULL DEFAULT 1,\n                    updated_at REAL NOT NULL,\n                    PRIMARY KEY(owner_user_id,package_id)\n                );\n            ")

    @classmethod
    def from_env(cls, auth_store: ConnectionDatabase) -> ExternalConnectionStore:
        key_file = os.getenv('OPENCLASS_EXTERNAL_CONNECTION_TOKEN_KEY_FILE') or os.getenv('OPENCLASS_GOOGLE_DRIVE_TOKEN_KEY_FILE')
        if not key_file:
            return cls(auth_store)
        try:
            key = Path(key_file).expanduser().read_bytes().strip()
        except OSError as exc:
            raise ExternalConnectionError('External account encryption is not configured.', 503) from exc
        return cls(auth_store, key)

    def _encrypt(self, value: dict[str, Any]) -> str:
        if self.cipher is None:
            raise ExternalConnectionError('External account encryption is not configured.', 503)
        return self.cipher.encrypt(_json(value).encode('utf-8')).decode('ascii')

    def _decrypt(self, encrypted: str) -> dict[str, Any]:
        if self.cipher is None:
            raise ExternalConnectionError('External account encryption is not configured.', 503)
        try:
            return _object(self.cipher.decrypt(encrypted.encode('ascii')).decode('utf-8'))
        except (InvalidToken, UnicodeError, ValueError, TypeError) as exc:
            raise ExternalConnectionError('External account credentials cannot be read; reconnect the account.', 503) from exc

    @staticmethod
    def _view(row: sqlite3.Row) -> dict[str, Any]:
        return {'id': row['id'], 'owner_user_id': row['owner_user_id'], 'provider': row['provider'], 'issuer': row['issuer'], 'subject': row['subject'], 'tenant': row['tenant'], 'display_name': row['display_name'], 'capabilities': _object(row['capabilities_json']), 'resource_scope': _object(row['resource_scope_json']), 'status': row['status'], 'revision': row['revision'], 'generation': row['generation'], 'last_access_at': row['last_access_at'], 'created_at': row['created_at'], 'updated_at': row['updated_at']}

    def list_connections(self, owner_user_id: str, provider: str | None=None) -> list[dict[str, Any]]:
        with self.auth_store.connection() as db:
            if provider is None:
                rows = db.execute("SELECT * FROM external_connections WHERE owner_user_id=? AND status!='disconnected' ORDER BY updated_at DESC", (owner_user_id,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM external_connections WHERE owner_user_id=? AND provider=? AND status!='disconnected' ORDER BY updated_at DESC", (owner_user_id, provider)).fetchall()
            return [self._view(row) for row in rows]

    def get_connection(self, owner_user_id: str, connection_id: str) -> dict[str, Any] | None:
        with self.auth_store.connection() as db:
            row = db.execute('SELECT * FROM external_connections WHERE owner_user_id=? AND id=?', (owner_user_id, connection_id)).fetchone()
            return self._view(row) if row else None

    def get_credentials(self, owner_user_id: str, connection_id: str, provider: str) -> dict[str, Any]:
        with self.auth_store.connection() as db:
            row = db.execute("SELECT credentials FROM external_connections WHERE owner_user_id=? AND id=? AND provider=? AND status='connected'", (owner_user_id, connection_id, provider)).fetchone()
            if row is None:
                raise ExternalConnectionError('This connected account is unavailable.', 403)
            return self._decrypt(row['credentials'])

    def credentials_for_reauthorization(self, owner_user_id: str, connection_id: str, provider: str) -> dict[str, Any]:
        with self.auth_store.connection() as db:
            row = db.execute("SELECT credentials FROM external_connections WHERE owner_user_id=? AND id=? AND provider=? AND status!='disconnected'", (owner_user_id, connection_id, provider)).fetchone()
            if row is None:
                raise ExternalConnectionError('This connected account is unavailable.', 403)
            return self._decrypt(row['credentials'])

    def authorization_versions(self, owner_user_id: str, provider: str) -> dict[str, dict[str, int]]:
        with self.auth_store.connection() as db:
            rows = db.execute('SELECT id,revision,generation FROM external_connections WHERE owner_user_id=? AND provider=?', (owner_user_id, provider)).fetchall()
        return {row['id']: {'revision': row['revision'], 'generation': row['generation']} for row in rows}

    def save_connection(self, owner_user_id: str, provider: str, issuer: str, subject: str, display_name: str, credentials: dict[str, Any], capabilities: dict[str, Any], resource_scope: dict[str, Any], tenant: str='', connection_id: str | None=None, authorization_versions: dict[str, dict[str, int]] | None=None, merge_grants: bool=False) -> dict[str, Any]:
        if not all((owner_user_id, provider, issuer, subject)):
            raise ExternalConnectionError('The external account identity is incomplete.', 422)
        resource_scope = _resource_scope(resource_scope)
        now = time.time()
        with self.auth_store.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM external_connections WHERE owner_user_id=? AND provider=? AND issuer=? AND subject=? AND tenant=?', (owner_user_id, provider, issuer, subject, tenant)).fetchone()
            if connection_id is not None:
                target = db.execute('SELECT * FROM external_connections WHERE owner_user_id=? AND id=?', (owner_user_id, connection_id)).fetchone()
                if target is None or (target['provider'], target['issuer'], target['subject'], target['tenant']) != (provider, issuer, subject, tenant):
                    raise ExternalConnectionError('The reauthorized account does not match this connection.', 409)
                if existing is not None and existing['id'] != connection_id:
                    raise ExternalConnectionError('This external account is already connected.', 409)
            row = existing if connection_id is None else target
            if authorization_versions is not None and row is not None:
                expected = authorization_versions.get(row['id'])
                if expected != {'revision': row['revision'], 'generation': row['generation']}:
                    raise ExternalConnectionError('Account access changed after authorization started. Start a new authorization.', 409)
            if authorization_versions is not None and connection_id is not None and (row is not None) and (row['status'] == 'disconnected'):
                raise ExternalConnectionError('This account was disconnected. Start a new authorization.', 409)
            if merge_grants and row is not None and (row['status'] != 'disconnected'):
                previous_capabilities = _object(row['capabilities_json'])
                previous_scope = _object(row['resource_scope_json'])
                previous = {'capabilities': previous_capabilities, 'resource_scope': previous_scope}
                previous_capabilities = {name: {**grant, 'resource_scope': capability_resource_scope(previous, name)} if isinstance(grant, dict) else grant for name, grant in previous_capabilities.items()}
                for name, grant in capabilities.items():
                    if isinstance(grant, dict) and name in previous_capabilities:
                        grant = {**grant, 'resource_scope': capability_resource_scope(previous, name)}
                        capabilities = {**capabilities, name: grant}
                capabilities = {**previous_capabilities, **capabilities}
                resource_scope = previous_scope
                if 'grants' in credentials and row['credentials']:
                    previous_credentials = self._decrypt(row['credentials'])
                    credentials = {**previous_credentials, **credentials, 'grants': {**previous_credentials.get('grants', {}), **credentials['grants']}}
            encrypted = self._encrypt(credentials)
            identifier = row['id'] if row else 'ext_' + secrets.token_hex(16)
            if row:
                db.execute("UPDATE external_connections SET display_name=?,credentials=?,capabilities_json=?,resource_scope_json=?,status='connected',revision=revision+1,generation=generation+1,updated_at=? WHERE id=? AND owner_user_id=?", (display_name, encrypted, _json(capabilities), _json(resource_scope), now, identifier, owner_user_id))
            else:
                db.execute("INSERT INTO external_connections(id,owner_user_id,provider,issuer,subject,tenant,display_name,credentials,capabilities_json,resource_scope_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'connected',?,?)", (identifier, owner_user_id, provider, issuer, subject, tenant, display_name, encrypted, _json(capabilities), _json(resource_scope), now, now))
            result = db.execute('SELECT * FROM external_connections WHERE id=? AND owner_user_id=?', (identifier, owner_user_id)).fetchone()
            return self._view(result)

    def update_connection(self, owner_user_id: str, connection_id: str, expected_revision: int, *, capabilities: dict[str, Any] | None=None, resource_scope: dict[str, Any] | None=None, status: str | None=None, capability_scopes: dict[str, dict] | None=None) -> dict[str, Any]:
        if status is not None and status not in {'connected', 'paused', 'reauth_required'}:
            raise ExternalConnectionError('Invalid account status.', 422)
        if resource_scope is not None:
            resource_scope = _resource_scope(resource_scope)
        now = time.time()
        with self.auth_store.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM external_connections WHERE id=? AND owner_user_id=? AND status!='disconnected'", (connection_id, owner_user_id)).fetchone()
            if row is None:
                raise ExternalConnectionError('Connected account not found.', 404)
            if row['revision'] != expected_revision:
                raise ExternalConnectionError('Connected account changed in another window.', 409)
            if status == 'connected' and row['status'] != 'paused':
                raise ExternalConnectionError('Reauthorize this account before resuming access.', 409)
            current_capabilities = capabilities if capabilities is not None else _object(row['capabilities_json'])
            scopes = dict(capability_scopes or {})
            if resource_scope is not None:
                if len(current_capabilities) != 1:
                    raise ExternalConnectionError('Choose a resource scope for each account capability.', 422)
                scopes.setdefault(next(iter(current_capabilities)), resource_scope)
            if not set(scopes).issubset(current_capabilities):
                raise ExternalConnectionError('Choose scopes for capabilities this account granted.', 422)
            for name, scope in scopes.items():
                if not isinstance(current_capabilities[name], dict):
                    grant = {'granted': bool(current_capabilities[name]), 'enabled': bool(current_capabilities[name])}
                else:
                    grant = current_capabilities[name]
                current_capabilities[name] = {**grant, 'resource_scope': _resource_scope(scope)}
            if scopes and len(current_capabilities) == 1:
                resource_scope = next(iter(current_capabilities.values()))['resource_scope']
            db.execute('UPDATE external_connections SET capabilities_json=?,resource_scope_json=?,status=?,revision=revision+1,generation=generation+1,updated_at=? WHERE id=? AND owner_user_id=?', (_json(current_capabilities), _json(resource_scope if resource_scope is not None else _object(row['resource_scope_json'])), status or row['status'], now, connection_id, owner_user_id))
            changed = db.execute('SELECT * FROM external_connections WHERE id=?', (connection_id,)).fetchone()
            return self._view(changed)

    def update_credentials(self, owner_user_id: str, connection_id: str, expected_generation: int, credentials: dict[str, Any]) -> dict[str, Any]:
        encrypted = self._encrypt(credentials)
        with self.auth_store.transaction() as db:
            changed = db.execute("UPDATE external_connections SET credentials=?,generation=generation+1,revision=revision+1,updated_at=? WHERE owner_user_id=? AND id=? AND status='connected' AND generation=?", (encrypted, time.time(), owner_user_id, connection_id, expected_generation))
            if changed.rowcount != 1:
                raise ExternalConnectionError('Account access changed during credential refresh.', 409)
            row = db.execute('SELECT * FROM external_connections WHERE id=? AND owner_user_id=?', (connection_id, owner_user_id)).fetchone()
            return self._view(row)

    def invalidate_provider(self, provider: str, subject: str | None=None, resource_id: str | None=None) -> int:
        if not provider or (subject is None and resource_id is None):
            raise ExternalConnectionError('A provider identity or resource is required for revocation.', 422)
        with self.auth_store.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT * FROM external_connections WHERE provider=? AND status='connected'", (provider,)).fetchall()
            changed = 0
            for row in rows:
                if subject is not None and row['subject'] != subject:
                    continue
                scope = _object(row['resource_scope_json'])
                if resource_id is not None and scope.get('mode') not in {'all', 'provider'} and (resource_id not in scope.get('resource_ids', [])):
                    continue
                db.execute("UPDATE external_connections SET status='reauth_required',credentials='',generation=generation+1,revision=revision+1,updated_at=? WHERE id=?", (time.time(), row['id']))
                changed += 1
            return changed

    def disconnect(self, owner_user_id: str, connection_id: str, expected_revision: int) -> dict[str, Any]:
        now = time.time()
        with self.auth_store.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM external_connections WHERE id=? AND owner_user_id=? AND status!='disconnected'", (connection_id, owner_user_id)).fetchone()
            if row is None:
                raise ExternalConnectionError('Connected account not found.', 404)
            if row['revision'] != expected_revision:
                raise ExternalConnectionError('Connected account changed in another window.', 409)
            db.execute("UPDATE external_connections SET status='disconnected',credentials='',revision=revision+1,generation=generation+1,updated_at=? WHERE id=? AND owner_user_id=?", (now, connection_id, owner_user_id))
            changed = db.execute('SELECT * FROM external_connections WHERE id=?', (connection_id,)).fetchone()
            return self._view(changed)

    def mark_access(self, owner_user_id: str, connection_id: str, generation: int) -> None:
        with self.auth_store.transaction() as db:
            changed = db.execute("UPDATE external_connections SET last_access_at=? WHERE owner_user_id=? AND id=? AND generation=? AND status='connected'", (time.time(), owner_user_id, connection_id, generation))
            if changed.rowcount != 1:
                raise ExternalConnectionError('Account access changed during this request.', 409)

    def create_attempt(self, owner_user_id: str, provider: str, session_token: str, purpose: str, payload: dict[str, Any], return_path: str='/settings', ttl_seconds: int=600, *, state: str | None=None) -> str:
        if not session_token or not owner_user_id:
            raise ExternalConnectionError('Sign in again before connecting an account.', 401)
        state = state or secrets.token_urlsafe(32)
        if len(state) > 256:
            raise ExternalConnectionError('Invalid authorization state.', 422)
        with self.auth_store.transaction() as db:
            db.execute('DELETE FROM external_auth_attempts WHERE expires_at<=?', (time.time(),))
            db.execute('INSERT INTO external_auth_attempts(state_hash,owner_user_id,session_hash,provider,purpose,payload_encrypted,return_path,expires_at) VALUES(?,?,?,?,?,?,?,?)', (hashlib.sha256(state.encode()).hexdigest(), owner_user_id, hashlib.sha256(session_token.encode()).hexdigest(), provider, purpose, self._encrypt(payload), _return_path(return_path), time.time() + ttl_seconds))
        return state

    def consume_attempt(self, owner_user_id: str, provider: str, session_token: str, state: str) -> dict[str, Any]:
        with self.auth_store.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM external_auth_attempts WHERE state_hash=? AND owner_user_id=? AND provider=?', (hashlib.sha256(state.encode()).hexdigest(), owner_user_id, provider)).fetchone()
            if row is None or row['expires_at'] <= time.time() or row['session_hash'] != hashlib.sha256(session_token.encode()).hexdigest():
                raise ExternalConnectionError('Account authorization expired or belongs to another session.', 409)
            db.execute('DELETE FROM external_auth_attempts WHERE state_hash=?', (row['state_hash'],))
            return {'purpose': row['purpose'], 'payload': self._decrypt(row['payload_encrypted']), 'return_path': row['return_path']}

    def source_scope(self, owner_user_id: str, package_id: str) -> dict[str, Any]:
        with self.auth_store.connection() as db:
            row = db.execute('SELECT scope_json,revision FROM external_source_scopes WHERE owner_user_id=? AND package_id=?', (owner_user_id, package_id)).fetchone()
            return {'scope': _object(row['scope_json']), 'revision': row['revision']} if row else {'scope': {'connection_ids': []}, 'revision': 0}

    def update_source_scope(self, owner_user_id: str, package_id: str, scope: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        connection_ids = scope.get('connection_ids')
        if not isinstance(connection_ids, list) or len(connection_ids) > 100 or any((not isinstance(value, str) for value in connection_ids)) or (len(set(connection_ids)) != len(connection_ids)):
            raise ExternalConnectionError('Select valid connected accounts.', 422)
        with self.auth_store.transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT revision FROM external_source_scopes WHERE owner_user_id=? AND package_id=?', (owner_user_id, package_id)).fetchone()
            revision = current['revision'] if current else 0
            if revision != expected_revision:
                raise ExternalConnectionError('Course account scope changed in another window.', 409)
            for connection_id in connection_ids:
                available = db.execute("SELECT provider FROM external_connections WHERE id=? AND owner_user_id=? AND status='connected'", (connection_id, owner_user_id)).fetchone()
                if available is None or available['provider'] not in {'google', 'microsoft', 'github', 'gitlab', 'notion', 'zotero'}:
                    raise ExternalConnectionError('A selected source account is unavailable.', 403)
            db.execute('INSERT INTO external_source_scopes(owner_user_id,package_id,scope_json,revision,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(owner_user_id,package_id) DO UPDATE SET scope_json=excluded.scope_json,revision=excluded.revision,updated_at=excluded.updated_at', (owner_user_id, package_id, _json({'connection_ids': connection_ids}), revision + 1, time.time()))
            return {'scope': {'connection_ids': connection_ids}, 'revision': revision + 1}
