"""Owner-scoped Codex account slots and revocable execution leases, outside course history."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import hashlib
import base64
import json
import os
from pathlib import Path
import secrets
import shutil
import threading
from openclass_core.services.external_connections import ExternalConnectionError, ExternalConnectionStore
from openclass_core.services.studio_ports import host_service
_current: ContextVar['CodexConnectionLease | None'] = ContextVar('codex_connection', default=None)
_migration_lock = threading.RLock()

def connection_store() -> ExternalConnectionStore:
    return host_service('connections')

def legacy_home(owner: str) -> Path:
    from openclass_core.services.codex_app_server import codex_home_root
    if not owner.strip():
        raise ExternalConnectionError('A signed-in user is required for Codex.', 401)
    return codex_home_root() / 'accounts' / hashlib.sha256(owner.strip().encode()).hexdigest()

def slot_home(owner: str, key: str) -> Path:
    if not key:
        raise ExternalConnectionError('The Codex connection has no isolated credential slot.', 409)
    return legacy_home(owner) / 'connections' / hashlib.sha256(key.encode()).hexdigest()

def _private_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=448)
    path.parent.chmod(448)
    temporary = path.with_name('.' + path.name + '-' + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 384)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, separators=(',', ':'))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

def _managed_auth(home: Path) -> dict:
    path = home / 'auth.json'
    if path.is_symlink():
        raise ExternalConnectionError('A linked credential file cannot be used for Codex.', 403)
    try:
        auth = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ExternalConnectionError('The managed Codex credential is unavailable. Reconnect the account.', 401) from exc
    tokens = auth.get('tokens', {}) if isinstance(auth, dict) else {}
    if not all((isinstance(tokens.get(key), str) and tokens[key] for key in ('access_token', 'refresh_token', 'account_id'))):
        raise ExternalConnectionError('A completed ChatGPT subscription login is required.', 401)
    return auth

def register_managed_login(owner: str, key: str, *, account, connection_id: str | None=None, authorization_versions=None, store=None) -> dict:
    store = store or connection_store()
    home = slot_home(owner, key)
    auth = _managed_auth(home)
    account_id = str(auth['tokens']['account_id'])
    identity = ''
    for token_name in ('id_token', 'access_token'):
        try:
            payload = str(auth['tokens'].get(token_name, '')).split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
            identity = str(claims.get('sub') or '')
            if identity:
                break
        except (IndexError, ValueError, TypeError):
            continue
    subject = hashlib.sha256((account_id + '\x00' + identity).encode()).hexdigest()
    canonical_key = 'account-' + subject
    existing = next((row for row in store.list_connections(owner, 'codex') if row['subject'] == subject), None)
    from openclass_core.services.codex_credentials import credential_file_lock
    with credential_file_lock(owner, connection_id or (existing['id'] if existing else canonical_key)):
        return _save_managed_login(store, owner, subject, canonical_key, account, auth, connection_id, authorization_versions)

def _save_managed_login(store, owner, subject, canonical_key, account, auth, connection_id, authorization_versions):
    saved = store.save_connection(owner, 'codex', 'https://chatgpt.com', subject, str(account.email or 'ChatGPT'), {'directory_key': canonical_key, 'managed_auth': auth}, {'models': {'granted': True, 'enabled': True, 'plan_type': account.plan_type}}, {'mode': 'models'}, connection_id=connection_id, authorization_versions=authorization_versions)
    try:
        _private_write(slot_home(owner, canonical_key) / 'auth.json', auth)
        _private_write(legacy_home(owner) / '.managed-connections.json', {'migrated': True})
    except OSError:
        store.update_connection(owner, saved['id'], saved['revision'], status='reauth_required')
        raise ExternalConnectionError('The account credential could not be saved. Reconnect the account.', 503) from None
    return saved

def codex_reauthorization_target(owner: str, connection_id: str, *, store=None) -> dict:
    store = store or connection_store()
    row = store.get_connection(owner, connection_id)
    if not row or row['provider'] != 'codex' or row['status'] == 'disconnected':
        raise ExternalConnectionError('Connected Codex account not found.', 404)
    return row

def migrate_legacy_connection(owner: str, *, store=None) -> dict | None:
    store = store or connection_store()
    with _migration_lock:
        if store.list_connections(owner, 'codex') or (legacy_home(owner) / '.managed-connections.json').exists():
            return None
        if not (legacy_home(owner) / 'auth.json').is_file() or store.cipher is None:
            return None
        from openclass_core.services.external_migration_guard import ExternalMigrationBlocked, require_verified_backup
        try:
            require_verified_backup()
        except ExternalMigrationBlocked as exc:
            raise ExternalConnectionError(str(exc), 503) from exc
        from openclass_core.services.codex_credentials import _legacy_codex_auth_candidate, credential_contents_checksum
        auth, proof = _legacy_codex_auth_candidate(owner)
        key = 'legacy-' + secrets.token_hex(16)
        from openclass_core.models import CodexAccountView
        account = CodexAccountView(type='chatgpt', email=None, plan_type=None)
        _private_write(slot_home(owner, key) / 'auth.json', auth)
        saved = register_managed_login(owner, key, account=account, store=store)
        written = _managed_auth(slot_home(owner, 'account-' + saved['subject']))
        if credential_contents_checksum(written) != proof['auth_sha256']:
            store.update_connection(owner, saved['id'], saved['revision'], status='reauth_required')
            raise ExternalConnectionError('The migrated credential failed verification. Legacy credentials were preserved.', 409)
        shutil.rmtree(slot_home(owner, key))
        (legacy_home(owner) / 'auth.json').unlink()
        from openclass_core.services.pi_agent_runtime import remove_pi_openai_codex_auth
        if not proof['retain_legacy_pi']:
            remove_pi_openai_codex_auth(owner)
        return saved

def list_codex_connections(owner: str, *, migrate: bool=True) -> list[dict]:
    store = connection_store()
    if migrate:
        migrate_legacy_connection(owner, store=store)
    return store.list_connections(owner, 'codex')

@dataclass(frozen=True)
class CodexConnectionLease:
    owner: str
    connection_id: str
    generation: int
    directory_key: str
    store: ExternalConnectionStore

    @property
    def home(self) -> Path:
        return slot_home(self.owner, self.directory_key)

    def check(self) -> None:
        record = self.store.get_connection(self.owner, self.connection_id)
        if not record or record['provider'] != 'codex' or record['status'] != 'connected' or (record['generation'] != self.generation) or (record.get('capabilities', {}).get('models', {}).get('enabled') is not True):
            from openclass_core.services.codex_app_server import CodexTurnCancelledError
            raise CodexTurnCancelledError('The selected Codex account was disconnected or changed during this task.')

def current_codex_lease(owner: str | None=None) -> CodexConnectionLease | None:
    lease = _current.get()
    return lease if lease is not None and (owner is None or lease.owner == owner) else None

def resolve_codex_lease(owner: str, connection_id: str | None=None, *, store=None) -> CodexConnectionLease | None:
    current = current_codex_lease(owner)
    if current and connection_id is not None and (current.connection_id != connection_id):
        raise ExternalConnectionError('A task cannot change its selected ChatGPT/Codex account.', 409)
    if current:
        current.check()
        return current
    store = store or connection_store()
    rows = store.list_connections(owner, 'codex')
    if not rows and (legacy_home(owner) / 'auth.json').is_file() and (store.cipher is not None):
        migrate_legacy_connection(owner, store=store)
        rows = store.list_connections(owner, 'codex')
    preferred = None
    if connection_id is None and getattr(store, 'auth_store', None) is not None:
        preferred = host_service('preferred_connection', store.auth_store, owner)
        connection_id = preferred
    if connection_id is None:
        active = [row for row in rows if row['status'] == 'connected' and isinstance(row.get('capabilities', {}).get('models'), dict) and (row['capabilities']['models'].get('granted') is True) and (row['capabilities']['models'].get('enabled') is True)]
        if len(active) > 1:
            raise ExternalConnectionError('Choose which ChatGPT/Codex account this task should use.', 409)
        if not active:
            if rows or (legacy_home(owner) / '.managed-connections.json').exists():
                raise ExternalConnectionError('Reconnect or select an available ChatGPT/Codex account.', 401)
            return None
        connection_id = active[0]['id']
    row = store.get_connection(owner, connection_id)
    if not row or row['provider'] != 'codex' or row['status'] != 'connected':
        if preferred:
            raise ExternalConnectionError('The default ChatGPT/Codex account is unavailable. Reconnect it or choose another default account.', 409)
        raise ExternalConnectionError('The selected ChatGPT/Codex account is unavailable.', 403)
    if preferred and row.get('capabilities', {}).get('models', {}).get('enabled') is not True:
        raise ExternalConnectionError('The default ChatGPT/Codex account cannot use models. Reconnect it or choose another default account.', 409)
    credentials = store.get_credentials(owner, connection_id, 'codex')
    lease = CodexConnectionLease(owner, connection_id, int(row['generation']), str(credentials.get('directory_key') or ''), store)
    lease.check()
    if lease.home.is_symlink() or lease.home.parent.is_symlink():
        raise ExternalConnectionError('The Codex credential directory is unsafe.', 403)
    if not (lease.home / 'auth.json').is_file():
        raise ExternalConnectionError('Reconnect the Codex account to restore its credential.', 401)
    return lease

@contextmanager
def bind_codex_lease(lease: CodexConnectionLease | None):
    if lease is not None:
        lease.check()
    token = _current.set(lease)
    try:
        yield lease
        if lease is not None:
            lease.check()
    finally:
        _current.reset(token)

def guarded_codex_call(method):

    @wraps(method)
    def call(self, *args, **kwargs):
        selection = getattr(self, 'model', None)
        provider = kwargs.get('provider', getattr(selection, 'provider', getattr(self, 'provider', 'openai_codex')))
        access = kwargs.get('access_method', getattr(selection, 'access_method', getattr(self, 'access_method', None)))
        if provider not in {'openai_codex', 'openai-codex'} or access == 'platform_credits':
            return method(self, *args, **kwargs)
        owner = kwargs.get('user_id', getattr(self, 'owner_user_id', getattr(self, 'user_id', '')))
        connection_id = kwargs.get('connection_id', getattr(selection, 'connection_id', getattr(self, 'connection_id', None)))
        lease = getattr(self, '_codex_lease', None) or resolve_codex_lease(owner, connection_id)
        if lease and hasattr(selection, 'model_copy'):
            self.model = selection.model_copy(update={'connection_id': lease.connection_id})
        with bind_codex_lease(lease):
            result = method(self, *args, **kwargs)
            if lease:
                lease.store.mark_access(lease.owner, lease.connection_id, lease.generation)
            return result
    return call

def codex_connection_revoked() -> bool:
    lease = current_codex_lease()
    if lease is None:
        return False
    try:
        lease.check()
        return False
    except RuntimeError:
        return True

def disconnect_codex_connection(owner_user_id: str, connection_id: str, expected_revision: int, *, store=None) -> dict:
    store = store or connection_store()
    row = store.get_connection(owner_user_id, connection_id)
    if not row or row['provider'] != 'codex':
        raise ExternalConnectionError('Connected Codex account not found.', 404)
    key = 'account-' + row['subject']
    result = store.disconnect(owner_user_id, connection_id, expected_revision)
    home = slot_home(owner_user_id, key)
    from openclass_core.services.codex_credentials import CodexAccountBusyError, credential_file_lock, remove_credential_directories
    try:
        with credential_file_lock(owner_user_id, connection_id):
            remove_credential_directories(owner_user_id, connection_id, home)
        result = {**result, 'credential_cleanup': 'succeeded'}
    except (CodexAccountBusyError, OSError):
        result = {**result, 'credential_cleanup': 'retry_pending'}
    from openclass_core.services.codex_app_server import _invalidate_status
    _invalidate_status(owner_user_id)
    return result

def remove_owner_codex_credentials(owner_user_id: str) -> None:
    """Remove only this OpenClass owner's local Codex and Pi agent directories."""
    if not owner_user_id.strip():
        raise ExternalConnectionError('A signed-in user is required for Codex cleanup.', 401)

    def remove(path: Path) -> None:
        if path.parent.is_symlink() or path.parent.parent.is_symlink():
            raise ExternalConnectionError('The Codex credential directory is unsafe.', 403)
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
    remove(legacy_home(owner_user_id))
    from openclass_core.services.pi_agent_runtime import pi_runtime_root
    owner_key = hashlib.sha256(owner_user_id.encode()).hexdigest()[:24]
    remove(pi_runtime_root() / 'agents' / owner_key)
    from openclass_core.services.codex_app_server import _invalidate_status
    _invalidate_status(owner_user_id)
