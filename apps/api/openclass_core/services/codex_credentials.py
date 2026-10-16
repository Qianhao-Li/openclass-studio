"""Serialize credential-changing provider sessions for one connected account."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
from functools import wraps
import hashlib
import json
import os
import signal
import shutil
import subprocess
import threading
from openclass_core.services.external_connections import ExternalConnectionError
from openclass_core.services.codex_legacy_credentials import CodexLegacyCredentialError, _coordinated_managed_auth, _legacy_auth_candidate, credential_contents_checksum
_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}
_held_locks: ContextVar[frozenset[tuple[str, int, int]]] = ContextVar('codex_credential_locks', default=frozenset())
_provider_processes: ContextVar[list | None] = ContextVar('codex_provider_processes', default=None)

class CodexAccountBusyError(ExternalConnectionError):
    code = 'account_busy'

    def __init__(self):
        super().__init__('This ChatGPT account is busy. Retry when its current model request finishes.', 409)

class LegacyCodexCredentialConflict(ExternalConnectionError):

    def __init__(self, reason):
        self.code = reason
        super().__init__('Legacy Codex and Pi credentials could not be reconciled safely. Both copies were preserved; verify or reconnect this account before migration.', 409)

def _legacy_codex_auth_candidate(owner):
    from openclass_core.services.codex_connections import legacy_home
    from openclass_core.services.pi_agent_runtime import pi_runtime_root
    if (os.getenv('OPENCLASS_PI_AGENT_DIR') or '').strip():
        raise LegacyCodexCredentialConflict('shared_pi_directory')
    pi_home = pi_runtime_root() / 'agents' / hashlib.sha256(owner.encode()).hexdigest()[:24]
    try:
        return _legacy_auth_candidate(legacy_home(owner) / 'auth.json', pi_home)
    except CodexLegacyCredentialError as exc:
        raise LegacyCodexCredentialConflict(exc.reason) from None

def legacy_codex_migration_proof(owner) -> dict:
    """Read-only proof of the exact coordinated auth the migration must preserve."""
    return _legacy_codex_auth_candidate(owner)[1]

def inspect_legacy_codex_credentials(owner) -> dict:
    """Metadata-only preflight: never return tokens, paths, identities or token hashes."""
    try:
        proof = legacy_codex_migration_proof(owner)
        return {key: proof[key] for key in ('status', 'reason', 'pi_refresh_preserved', 'retain_legacy_pi')}
    except ExternalConnectionError as exc:
        return {'status': 'blocked', 'reason': getattr(exc, 'code', 'managed_credential_unavailable')}

@contextmanager
def credential_file_lock(owner: str, connection_id: str, *, bridge: bool=False):
    from openclass_core.services.codex_app_server import codex_home_root
    key = hashlib.sha256((owner + '\x00' + connection_id).encode()).hexdigest()
    held_key = (key, os.getpid(), threading.get_ident())
    if bridge and held_key in _held_locks.get():
        yield
        return
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=False):
        raise CodexAccountBusyError()
    descriptor = None
    try:
        root = codex_home_root() / 'credential-locks'
        root.mkdir(parents=True, exist_ok=True, mode=448)
        root.chmod(448)
        descriptor = os.open(root / (key + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 384)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CodexAccountBusyError() from None
        token = _held_locks.set(_held_locks.get() | {held_key})
        try:
            yield
        finally:
            _held_locks.reset(token)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        lock.release()

def synchronize_refreshed_pi_auth(lease, *, runtime_root=None) -> bool:
    """Caller holds the account lock; never restore deleted or replaced source auth."""
    from openclass_core.services.codex_connections import _private_write
    from openclass_core.services.pi_agent_runtime import pi_runtime_root, PI_CODEX_AUTH_FINGERPRINT_FILE
    try:
        lease.check()
        owner_key = hashlib.sha256(lease.owner.encode()).hexdigest()[:24]
        pi_home = (runtime_root or pi_runtime_root()) / 'agents' / owner_key / 'connections' / hashlib.sha256(lease.connection_id.encode()).hexdigest()
        source = lease.home / 'auth.json'
        fingerprint_path = pi_home / PI_CODEX_AUTH_FINGERPRINT_FILE
        if any((path.is_symlink() for path in (pi_home, pi_home / 'auth.json', source, fingerprint_path))):
            return False
        source_bytes = source.read_bytes()
        if fingerprint_path.read_text().strip() != hashlib.sha256(source_bytes).hexdigest():
            return False
        managed = json.loads(source_bytes)
        refreshed = json.loads((pi_home / 'auth.json').read_text()).get('openai-codex', {})
        candidate = _coordinated_managed_auth(managed, refreshed)
        if candidate == managed:
            return False
        lease.check()
        _private_write(source, candidate)
        from openclass_core.services.pi_agent_runtime import _write_private_text
        _write_private_text(fingerprint_path, hashlib.sha256(source.read_bytes()).hexdigest() + '\n')
        return True
    except (OSError, ValueError, RuntimeError, TypeError):
        return False

def remove_credential_directories(owner, connection_id, home):
    from openclass_core.services.pi_agent_runtime import pi_runtime_root
    owner_key = hashlib.sha256(owner.encode()).hexdigest()[:24]
    pi_home = pi_runtime_root() / 'agents' / owner_key / 'connections' / hashlib.sha256(connection_id.encode()).hexdigest()
    for path in (home, pi_home):
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)

def register_provider_process(process, *, process_group=False):
    processes = _provider_processes.get()
    if processes is not None:
        processes.append((process, process_group))

def _close_provider_processes(processes):
    for process, process_group in processes:
        if process.poll() is None:
            try:
                if process_group:
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass

@contextmanager
def codex_credential_session(*, pi=False, runtime_root=None):
    from openclass_core.services.codex_connections import current_codex_lease
    lease = current_codex_lease()
    if lease is None:
        yield
        return
    with credential_file_lock(lease.owner, lease.connection_id):
        lease.check()
        synchronize_refreshed_pi_auth(lease, runtime_root=runtime_root)
        if pi:
            from openclass_core.services.pi_agent_runtime import ensure_pi_openai_codex_auth
            if not ensure_pi_openai_codex_auth(owner_user_id=lease.owner, connection_id=lease.connection_id, runtime_root=runtime_root):
                raise ExternalConnectionError('Reconnect this ChatGPT account before using models.', 401)
        processes = []
        process_token = _provider_processes.set(processes)
        try:
            yield
        finally:
            _close_provider_processes(processes)
            _provider_processes.reset(process_token)
            row = lease.store.get_connection(lease.owner, lease.connection_id)
            if not row or row['status'] == 'disconnected':
                remove_credential_directories(lease.owner, lease.connection_id, lease.home)
            else:
                synchronize_refreshed_pi_auth(lease, runtime_root=runtime_root)

def guarded_codex_process(method):

    @wraps(method)
    def run(*args, **kwargs):
        with codex_credential_session(pi=True, runtime_root=getattr(args[0], 'runtime_root', None) if args else None):
            return method(*args, **kwargs)
    return run

def guarded_codex_bridge(method):

    @wraps(method)
    def run(*args, **kwargs):
        from openclass_core.services.codex_connections import resolve_codex_lease
        lease = resolve_codex_lease(kwargs['owner_user_id'], kwargs.get('connection_id'))
        if lease is None:
            return method(*args, **kwargs)
        with credential_file_lock(lease.owner, lease.connection_id, bridge=True):
            lease.check()
            return method(*args, **kwargs)
    return run
