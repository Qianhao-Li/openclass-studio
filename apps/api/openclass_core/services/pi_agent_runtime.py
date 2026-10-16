from __future__ import annotations
import base64
import binascii
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, TypeVar
from pydantic import BaseModel
from openclass_core.services.codex_connections import current_codex_lease, resolve_codex_lease, guarded_codex_call, codex_connection_revoked
from openclass_core.services.codex_credentials import guarded_codex_process, guarded_codex_bridge, register_provider_process
from openclass_core.models import AgentActivityEvent, new_id, now_iso
from openclass_core.services.ai_logging import ai_usage_logger, current_ai_log_context, log_image_dispatch, log_image_dispatch_completed
from openclass_core.services.model_service_contracts import ModelBillingError as BillingError, ModelBillingPort as BillingService, PlatformModelAccessPort as OpenRouterProvisioningService
from openclass_core.services.studio_ports import host_service
from openclass_core.services.config import DATA_DIR, ROOT_DIR, load_root_dotenv
from openclass_core.services.codex_app_server import CodexTurnCancelledError
from openclass_core.services.pi_provider_catalog import pi_api_provider, validate_pi_provider_configuration
from openclass_core.services.structured_output import json_object, validation_issues, validation_repair_prompt
StructuredModel = TypeVar('StructuredModel', bound=BaseModel)
PiProcessRunner = Callable[..., subprocess.CompletedProcess[str]]
PI_REQUEST_TIMEOUT_SECONDS = 10 * 60
PI_TRANSIENT_RETRY_ATTEMPTS = 1
PI_TRANSIENT_ERROR_MARKERS = ('websocket error', 'connection reset', 'connection closed', 'connection lost', 'network error')
PI_OPENAI_CODEX_SERVICE_TIERS = {'priority'}
PI_ACTIVITY_EMIT_CHARACTER_INTERVAL = 120
PI_MAX_IMAGE_INPUT_BYTES = 20 * 1024 * 1024
PI_MAX_TOTAL_IMAGE_INPUT_BYTES = 40 * 1024 * 1024
PI_PROCESS_POLL_SECONDS = 0.1
PI_PROCESS_TERMINATE_GRACE_SECONDS = 1.0
PI_PROCESS_FINAL_EVENT_GRACE_SECONDS = 0.5
PI_OPENAI_CODEX_PROVIDER = 'openai-codex'
PI_CODEX_AUTH_FINGERPRINT_FILE = '.openclass-codex-auth.sha256'
_DATA_IMAGE_PATTERN = re.compile('^data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=\\r\\n]+)$', re.IGNORECASE)
_IMAGE_SUFFIXES = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp', 'image/gif': '.gif'}
logger = logging.getLogger(__name__)
_pi_auth_locks_guard = threading.Lock()
_pi_auth_locks: dict[str, threading.Lock] = {}

@dataclass(frozen=True)
class PiStructuredResponse:
    output_parsed: BaseModel
    activity: list[AgentActivityEvent]

@dataclass(frozen=True)
class PiTextResponse:
    output_text: str
    activity: list[AgentActivityEvent]

class _PiActivityRecorder:
    """Convert Pi's JSON event stream into public, durable activity updates.

    Pi can emit private reasoning text token by token. OpenClass deliberately
    exposes truthful lifecycle and volume updates without persisting that raw
    private reasoning.
    """

    def __init__(self, *, turn_id: str, request_id: str, owner_user_id: str, lesson_id: str | None, provider: str, model: str, request_kind: str, callback: Callable[[AgentActivityEvent], None] | None, on_text_delta: Callable[[str], None] | None=None) -> None:
        self.turn_id = turn_id
        self.request_id = request_id
        self.owner_user_id = owner_user_id
        self.lesson_id = lesson_id
        self.provider = provider
        self.model = model
        self.request_kind = request_kind
        self.callback = callback
        self.on_text_delta = on_text_delta
        self._events: dict[str, AgentActivityEvent] = {}
        self._order: list[str] = []
        self._character_counts: dict[str, int] = {}
        self._last_emitted_counts: dict[str, int] = {}
        self.started_at = time.monotonic()
        self.first_event_at: float | None = None
        self.first_text_at: float | None = None
        self.upstream_cost_usd: Decimal | None = None
        self.usage: dict[str, object] = {}

    @property
    def events(self) -> list[AgentActivityEvent]:
        return [self._events[event_id] for event_id in self._order]

    def _event_id(self, kind: str, content_index: object=0) -> str:
        return f'{self.request_id}:{kind}:{content_index}'

    def _publish(self, *, event_id: str, label: str, status: str, kind: str, detail: str, force: bool=True) -> None:
        existing = self._events.get(event_id)
        event = AgentActivityEvent(id=event_id, turn_id=self.turn_id, stage='build_context' if kind == 'reasoning' else 'execute_role', label=label, status=status, role='OpenClass', metadata={'kind': kind, 'detail': detail, 'agent_backend': 'pi', 'provider': self.provider, 'model': self.model}, created_at=existing.created_at if existing is not None else now_iso())
        if existing is None:
            self._order.append(event_id)
        self._events[event_id] = event
        if self.callback is None:
            return
        character_count = self._character_counts.get(event_id, 0)
        last_emitted = self._last_emitted_counts.get(event_id, 0)
        if not force and character_count - last_emitted < PI_ACTIVITY_EMIT_CHARACTER_INTERVAL:
            return
        self._last_emitted_counts[event_id] = character_count
        try:
            self.callback(event)
        except Exception:
            logger.exception('Failed to publish live Pi activity')

    def observe_line(self, line: str) -> None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        if self.first_event_at is None:
            self.first_event_at = time.monotonic()
        event_type = str(payload.get('type') or '')
        runtime_id = self._event_id('runtime')
        if event_type == 'agent_start':
            ai_usage_logger.log_model_run_event('provider_started', run_id=self.request_id, provider=self.provider, model=self.model, status='running', user_id=self.owner_user_id, lesson_id=self.lesson_id, turn_id=self.turn_id, request_kind=self.request_kind)
            self._publish(event_id=runtime_id, label='OpenClass connected to the model', status='running', kind='model_runtime', detail=f'Using {self.provider} / {self.model} to process the current step.')
            return
        if event_type == 'agent_end':
            self._observe_usage(payload)
            ai_usage_logger.log_model_run_event('provider_completed', run_id=self.request_id, provider=self.provider, model=self.model, status='completed', user_id=self.owner_user_id, lesson_id=self.lesson_id, turn_id=self.turn_id, request_kind=self.request_kind, usage=self.usage)
            self._publish(event_id=runtime_id, label='OpenClass completed the model run', status='completed', kind='model_runtime', detail=f'{self.provider} / {self.model} returned the result for this step.')
            return
        if event_type != 'message_update':
            return
        update = payload.get('assistantMessageEvent')
        if not isinstance(update, dict):
            return
        update_type = str(update.get('type') or '')
        content_index = update.get('contentIndex', 0)
        if update_type.startswith('thinking_'):
            self._observe_content_update(update=update, update_type=update_type, event_id=self._event_id('reasoning', content_index), kind='reasoning', running_label='OpenClass is reasoning', completed_label='OpenClass completed reasoning', noun='reasoning')
        elif update_type.startswith('text_'):
            self._observe_content_update(update=update, update_type=update_type, event_id=self._event_id('output', content_index), kind='model_output', running_label='OpenClass is generating the result', completed_label='OpenClass generated the model result', noun='output')

    def _observe_usage(self, payload: dict[str, Any]) -> None:
        messages = payload.get('messages')
        if not isinstance(messages, list):
            return
        token_fields = {'input': 'input_tokens', 'output': 'output_tokens', 'cacheRead': 'cache_read_tokens', 'cacheWrite': 'cache_write_tokens', 'totalTokens': 'total_tokens'}
        token_totals = {target: 0 for target in token_fields.values()}
        total_cost = Decimal('0')
        found_cost = False
        for message in messages:
            if not isinstance(message, dict) or message.get('role') != 'assistant':
                continue
            usage = message.get('usage')
            if not isinstance(usage, dict):
                continue
            for source, target in token_fields.items():
                value = usage.get(source)
                if isinstance(value, int) and (not isinstance(value, bool)) and (value >= 0):
                    token_totals[target] += value
            cost = usage.get('cost')
            raw_total = cost.get('total') if isinstance(cost, dict) else None
            if raw_total is None:
                continue
            try:
                parsed_cost = Decimal(str(raw_total))
            except InvalidOperation:
                continue
            if parsed_cost < 0:
                continue
            total_cost += parsed_cost
            found_cost = True
        if not found_cost:
            return
        self.upstream_cost_usd = total_cost
        self.usage = {**token_totals, 'upstream_cost_usd': format(total_cost, 'f')}

    def _observe_content_update(self, *, update: dict[str, Any], update_type: str, event_id: str, kind: str, running_label: str, completed_label: str, noun: str) -> None:
        if update_type.endswith('_delta'):
            delta = update.get('delta')
            if isinstance(delta, str):
                self._character_counts[event_id] = self._character_counts.get(event_id, 0) + len(delta)
                ai_usage_logger.log_model_run_event('reasoning_progress' if kind == 'reasoning' else 'output_delta', run_id=self.request_id, provider=self.provider, model=self.model, status='running', user_id=self.owner_user_id, lesson_id=self.lesson_id, turn_id=self.turn_id, request_kind=self.request_kind, delta=delta if kind == 'model_output' and self.request_kind == 'text' else None, metadata={'content_index': update.get('contentIndex', 0), 'character_count': self._character_counts[event_id], 'private_reasoning_omitted': kind == 'reasoning'})
                if kind == 'model_output' and delta:
                    if self.first_text_at is None:
                        self.first_text_at = time.monotonic()
                    if self.on_text_delta is not None:
                        self.on_text_delta(delta)
        character_count = self._character_counts.get(event_id, 0)
        if update_type.endswith('_end'):
            detail = f'Model {noun} completed'
            if character_count:
                detail += f'; received {character_count} characters'
            self._publish(event_id=event_id, label=completed_label, status='completed', kind=kind, detail=f'{detail}.')
            return
        privacy_note = '; private chain-of-thought is not recorded in chat history' if kind == 'reasoning' else ''
        detail = f'The model is generating {noun}'
        if character_count:
            detail += f'; received {character_count} characters'
        self._publish(event_id=event_id, label=running_label, status='running', kind=kind, detail=f'{detail}{privacy_note}.', force=not update_type.endswith('_delta'))

    def fail(self, detail: str) -> None:
        self._publish(event_id=self._event_id('runtime'), label='OpenClass model run did not complete', status='failed', kind='model_runtime', detail=detail)

    def timing_payload(self) -> dict[str, int | None]:
        completed_at = time.monotonic()
        return {'first_event_ms': round((self.first_event_at - self.started_at) * 1000) if self.first_event_at is not None else None, 'first_text_delta_ms': round((self.first_text_at - self.started_at) * 1000) if self.first_text_at is not None else None, 'completed_ms': round((completed_at - self.started_at) * 1000)}

def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=PI_PROCESS_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()

def _valid_image_signature(mime_type: str, content: bytes) -> bool:
    if mime_type == 'image/png':
        return content.startswith(b'\x89PNG\r\n\x1a\n')
    if mime_type == 'image/jpeg':
        return content.startswith(b'\xff\xd8\xff')
    if mime_type == 'image/webp':
        return len(content) >= 12 and content[:4] == b'RIFF' and (content[8:12] == b'WEBP')
    if mime_type == 'image/gif':
        return content.startswith((b'GIF87a', b'GIF89a'))
    return False

def _stage_image_inputs(image_inputs: list[str], *, workspace: Path, preserve_image_bytes: bool=False, provider: str='', model: str='') -> list[Path]:
    from openclass_core.services.model_image_limits import image_transfer_limits
    limits = image_transfer_limits(provider, model)
    staged: list[Path] = []
    total_bytes = 0
    for index, image_input in enumerate(image_inputs, start=1):
        match = _DATA_IMAGE_PATTERN.fullmatch(image_input.strip())
        if match is None:
            raise RuntimeError('Pi image inputs must be bounded base64 data URLs')
        mime_type = match.group(1).lower()
        try:
            content = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError('Pi image input contains invalid base64 data') from exc
        if not content or len(content) > limits.per_image:
            raise RuntimeError('Pi image input exceeds the configured size limit')
        total_bytes += len(content)
        if total_bytes > limits.total_images:
            raise RuntimeError('Pi image inputs exceed the configured total size limit')
        if not _valid_image_signature(mime_type, content):
            raise RuntimeError('Pi image input does not match its declared MIME type')
        if preserve_image_bytes:
            from PIL import Image
            try:
                with Image.open(io.BytesIO(content)) as page_image:
                    expected_format = {'image/png': 'PNG', 'image/jpeg': 'JPEG', 'image/webp': 'WEBP', 'image/gif': 'GIF'}[mime_type]
                    if page_image.format != expected_format or (mime_type == 'image/png' and page_image.is_animated):
                        raise ValueError('Image format does not match the preserved input')
                    page_image.verify()
            except Exception:
                message = 'Preserved PDF page inputs must be valid static PNG images' if mime_type == 'image/png' else 'Preserved image inputs must be valid images matching their declared MIME type'
                raise RuntimeError(message) from None
        image_path = workspace / f'input-{index}{_IMAGE_SUFFIXES[mime_type]}'
        image_path.write_bytes(content)
        image_path.chmod(384)
        staged.append(image_path)
    return staged

def _run_streaming_pi_process(command: list[str], *, input_text: str, cwd: Path, env: dict[str, str], timeout: int, recorder: _PiActivityRecorder, is_cancelled: Callable[[], bool] | None) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, cwd=cwd, env=env)
    register_provider_process(process)
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    terminal_assistant_event = threading.Event()

    def read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            stdout_lines.append(line)
            recorder.observe_line(line)
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = event.get('message') if event.get('type') == 'message_end' else None
            if isinstance(message, dict) and message.get('role') == 'assistant':
                terminal_assistant_event.set()

    def read_stderr() -> None:
        assert process.stderr is not None
        stderr_lines.extend(process.stderr)
    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    assert process.stdin is not None
    try:
        process.stdin.write(input_text)
        process.stdin.close()
    except BrokenPipeError:
        pass
    deadline = time.monotonic() + timeout
    terminal_received_at: float | None = None
    completed_from_terminal_event = False
    while process.poll() is None:
        if codex_connection_revoked() or (is_cancelled is not None and is_cancelled()):
            _terminate_process(process)
            stdout_thread.join()
            stderr_thread.join()
            recorder.fail('The user stopped the current model request.')
            raise CodexTurnCancelledError('Pi turn was cancelled')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process(process)
            stdout_thread.join()
            stderr_thread.join()
            recorder.fail(f'The model request timed out after {timeout} seconds.')
            raise subprocess.TimeoutExpired(command, timeout, output=''.join(stdout_lines), stderr=''.join(stderr_lines))
        if terminal_assistant_event.is_set():
            if terminal_received_at is None:
                terminal_received_at = time.monotonic()
            elif time.monotonic() - terminal_received_at >= PI_PROCESS_FINAL_EVENT_GRACE_SECONDS:
                process.kill()
                process.wait()
                completed_from_terminal_event = True
                break
        try:
            process.wait(timeout=min(PI_PROCESS_POLL_SECONDS, remaining))
        except subprocess.TimeoutExpired:
            continue
    returncode = 0 if completed_from_terminal_event else int(process.returncode or 0)
    stdout_thread.join()
    stderr_thread.join()
    return subprocess.CompletedProcess(command, returncode, ''.join(stdout_lines), ''.join(stderr_lines))

def pi_runtime_root() -> Path:
    configured = (os.getenv('OPENCLASS_PI_RUNTIME_ROOT') or '').strip()
    if configured:
        return Path(configured).expanduser().resolve()
    configured_database = (os.getenv('OPENCLASS_DATABASE_PATH') or '').strip()
    if configured_database:
        database_path = Path(configured_database).expanduser()
        if not database_path.is_absolute():
            database_path = ROOT_DIR / database_path
        return (database_path.parent / 'pi-runtime').resolve()
    return (DATA_DIR / 'pi-runtime').resolve()

def pi_binary_path() -> str | None:
    configured = (os.getenv('OPENCLASS_PI_BINARY') or '').strip()
    if configured:
        candidate = Path(configured).expanduser().resolve()
        return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
    bundled = ROOT_DIR / 'node_modules' / '.bin' / 'pi'
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return str(bundled)
    return shutil.which('pi')

def pi_runtime_available() -> bool:
    return pi_binary_path() is not None

def _pi_auth_lock(agent_dir: Path) -> threading.Lock:
    key = str(agent_dir)
    with _pi_auth_locks_guard:
        return _pi_auth_locks.setdefault(key, threading.Lock())

def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}

def _write_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=448)
    try:
        path.parent.chmod(448)
    except OSError:
        pass
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f'.{path.name}-', suffix='.tmp', mode='w', encoding='utf-8', delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
        temporary_path.chmod(384)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()

def _jwt_expiry_milliseconds(access_token: str) -> int | None:
    parts = access_token.split('.')
    if len(parts) < 2:
        return None
    payload = parts[1]
    payload += '=' * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode('ascii')))
    except (UnicodeEncodeError, ValueError, binascii.Error, json.JSONDecodeError):
        return None
    expires = claims.get('exp') if isinstance(claims, dict) else None
    if not isinstance(expires, (int, float)) or isinstance(expires, bool):
        return None
    return int(expires * 1000)

def _pi_credential_from_codex_auth(auth: dict[str, Any]) -> dict[str, Any] | None:
    tokens = auth.get('tokens')
    if not isinstance(tokens, dict):
        return None
    access = tokens.get('access_token')
    refresh = tokens.get('refresh_token')
    account_id = tokens.get('account_id')
    if not all((isinstance(value, str) and value.strip() for value in (access, refresh, account_id))):
        return None
    expires = _jwt_expiry_milliseconds(access)
    if expires is None:
        return None
    return {'type': 'oauth', 'access': access, 'refresh': refresh, 'expires': expires, 'accountId': account_id}

def _remove_pi_provider_auth(agent_dir: Path) -> None:
    auth_path = agent_dir / 'auth.json'
    fingerprint_path = agent_dir / PI_CODEX_AUTH_FINGERPRINT_FILE
    with _pi_auth_lock(agent_dir):
        auth = _read_json_object(auth_path)
        if PI_OPENAI_CODEX_PROVIDER in auth:
            del auth[PI_OPENAI_CODEX_PROVIDER]
            _write_private_text(auth_path, json.dumps(auth, indent=2) + '\n')
        try:
            fingerprint_path.unlink(missing_ok=True)
        except OSError:
            pass

def remove_pi_openai_codex_auth(owner_user_id: str, *, runtime_root: Path | None=None) -> None:
    if (os.getenv('OPENCLASS_PI_AGENT_DIR') or '').strip():
        return
    root = runtime_root or pi_runtime_root()
    owner_key = hashlib.sha256(owner_user_id.encode('utf-8')).hexdigest()[:24]
    _remove_pi_provider_auth(root / 'agents' / owner_key)

@guarded_codex_bridge
def ensure_pi_openai_codex_auth(*, owner_user_id: str, runtime_root: Path | None=None, connection_id: str | None=None) -> bool:
    """Bridge one OpenClass user's Codex OAuth credential into their Pi directory."""
    lease = resolve_codex_lease(owner_user_id, connection_id)
    configured_agent_dir = (os.getenv('OPENCLASS_PI_AGENT_DIR') or '').strip() if lease is None else ''
    if configured_agent_dir:
        auth = _read_json_object(Path(configured_agent_dir).expanduser().resolve() / 'auth.json')
        return isinstance(auth.get(PI_OPENAI_CODEX_PROVIDER), dict)
    from openclass_core.services.codex_app_server import codex_home_path
    root = runtime_root or pi_runtime_root()
    selected_id = lease.connection_id if lease else None
    agent_dir = pi_agent_directory(owner_user_id=owner_user_id, runtime_root=root, **{'connection_id': selected_id} if selected_id else {})
    source_path = codex_home_path(owner_user_id, **{'connection_id': selected_id} if selected_id else {}) / 'auth.json'
    try:
        source_bytes = source_path.read_bytes()
        source_auth = json.loads(source_bytes)
    except (OSError, json.JSONDecodeError):
        _remove_pi_provider_auth(agent_dir)
        return False
    if not isinstance(source_auth, dict):
        _remove_pi_provider_auth(agent_dir)
        return False
    credential = _pi_credential_from_codex_auth(source_auth)
    if credential is None:
        _remove_pi_provider_auth(agent_dir)
        return False
    fingerprint = hashlib.sha256(source_bytes).hexdigest()
    auth_path = agent_dir / 'auth.json'
    fingerprint_path = agent_dir / PI_CODEX_AUTH_FINGERPRINT_FILE
    with _pi_auth_lock(agent_dir):
        try:
            current_fingerprint = fingerprint_path.read_text(encoding='ascii').strip()
        except OSError:
            current_fingerprint = ''
        auth = _read_json_object(auth_path)
        if current_fingerprint == fingerprint and isinstance(auth.get(PI_OPENAI_CODEX_PROVIDER), dict):
            return True
        auth[PI_OPENAI_CODEX_PROVIDER] = credential
        _write_private_text(auth_path, json.dumps(auth, indent=2) + '\n')
        _write_private_text(fingerprint_path, fingerprint + '\n')
    return True

def pi_credentials_available(*, owner_user_id: str, runtime_root: Path | None=None, connection_id: str | None=None) -> bool:
    """Report whether the selected Pi account directory has usable auth state."""
    try:
        lease = resolve_codex_lease(owner_user_id, connection_id)
        if lease is not None:
            return _pi_credential_from_codex_auth(_read_json_object(lease.home / 'auth.json')) is not None
        return ensure_pi_openai_codex_auth(owner_user_id=owner_user_id, runtime_root=runtime_root, **{'connection_id': connection_id} if connection_id else {})
    except (OSError, RuntimeError):
        return False

def _pi_user_agent_directory(*, owner_user_id: str, runtime_root: Path) -> Path:
    owner_key = hashlib.sha256(owner_user_id.encode('utf-8')).hexdigest()[:24]
    agent_dir = runtime_root / 'agents' / owner_key
    agent_dir.mkdir(parents=True, exist_ok=True, mode=448)
    try:
        agent_dir.chmod(448)
    except OSError:
        pass
    return agent_dir

def pi_agent_directory(*, owner_user_id: str, runtime_root: Path, connection_id: str | None=None) -> Path:
    lease = resolve_codex_lease(owner_user_id, connection_id) if connection_id else current_codex_lease(owner_user_id)
    if lease:
        lease.check()
        owner_key = hashlib.sha256(owner_user_id.encode()).hexdigest()[:24]
        agent_dir = runtime_root / 'agents' / owner_key / 'connections' / hashlib.sha256(lease.connection_id.encode()).hexdigest()
        if agent_dir.is_symlink():
            raise RuntimeError('The Pi credential directory is unsafe')
        agent_dir.mkdir(parents=True, exist_ok=True, mode=448)
        agent_dir.chmod(448)
        return agent_dir
    configured_agent_dir = (os.getenv('OPENCLASS_PI_AGENT_DIR') or '').strip()
    if configured_agent_dir:
        agent_dir = Path(configured_agent_dir).expanduser().resolve()
        if not agent_dir.is_dir():
            raise RuntimeError('The configured Pi agent directory does not exist')
        return agent_dir
    return _pi_user_agent_directory(owner_user_id=owner_user_id, runtime_root=runtime_root)

def _pi_provider(provider: str) -> str:
    return 'openai-codex' if provider == 'openai_codex' else provider.replace('_', '-')

def _validated_personal_api_provider(provider: str) -> str:
    normalized = _pi_provider(provider)
    if pi_api_provider(normalized) is None:
        raise RuntimeError(f'Personal API credentials are not supported for {provider}')
    return normalized

def pi_personal_api_configured(*, owner_user_id: str, provider: str, runtime_root: Path | None=None) -> bool:
    normalized_provider = _validated_personal_api_provider(provider)
    agent_dir = _pi_user_agent_directory(owner_user_id=owner_user_id, runtime_root=runtime_root or pi_runtime_root())
    credential = _read_json_object(agent_dir / 'auth.json').get(normalized_provider)
    configured = bool(isinstance(credential, dict) and credential.get('type') == 'api_key' and isinstance(credential.get('key'), str) and credential['key'].strip())
    if configured:
        try:
            validate_pi_provider_configuration(normalized_provider, credential.get('env', {}))
        except (ValueError, TypeError, AttributeError):
            return False
    return configured

def save_pi_personal_api_key(*, owner_user_id: str, provider: str, api_key: str, provider_config: dict[str, str] | None=None, runtime_root: Path | None=None) -> None:
    normalized_provider = _validated_personal_api_provider(provider)
    normalized_key = api_key.strip()
    if not normalized_key:
        raise ValueError('API key cannot be empty.')
    if len(normalized_key) > 16384:
        raise ValueError('API key is too long.')
    if any((character.isspace() for character in normalized_key)):
        raise ValueError('API key cannot contain whitespace.')
    if normalized_key.startswith(('!', '$')):
        raise ValueError('API key cannot start with ! or $.')
    agent_dir = _pi_user_agent_directory(owner_user_id=owner_user_id, runtime_root=runtime_root or pi_runtime_root())
    auth_path = agent_dir / 'auth.json'
    with _pi_auth_lock(agent_dir):
        auth = _read_json_object(auth_path)
        existing = auth.get(normalized_provider)
        previous_config = existing.get('env', {}) if isinstance(existing, dict) else {}
        config = validate_pi_provider_configuration(normalized_provider, {**previous_config, **(provider_config or {})})
        auth[normalized_provider] = {'type': 'api_key', 'key': normalized_key, **({'env': config} if config else {})}
        _write_private_text(auth_path, json.dumps(auth, indent=2) + '\n')

def remove_pi_personal_api_key(*, owner_user_id: str, provider: str, runtime_root: Path | None=None) -> None:
    normalized_provider = _validated_personal_api_provider(provider)
    agent_dir = _pi_user_agent_directory(owner_user_id=owner_user_id, runtime_root=runtime_root or pi_runtime_root())
    auth_path = agent_dir / 'auth.json'
    with _pi_auth_lock(agent_dir):
        auth = _read_json_object(auth_path)
        if normalized_provider not in auth:
            return
        del auth[normalized_provider]
        _write_private_text(auth_path, json.dumps(auth, indent=2) + '\n')

def _runtime_settings_extension_path() -> Path:
    return Path(__file__).with_name('pi_runtime_settings_extension.ts').resolve()

def _validated_service_tier(provider: str, service_tier: str | None) -> str | None:
    normalized = str(service_tier or '').strip()
    if not normalized:
        return None
    if _pi_provider(provider) != 'openai-codex' or normalized not in PI_OPENAI_CODEX_SERVICE_TIERS:
        raise RuntimeError('The selected Pi model does not support this service tier')
    return normalized

def _pi_request_timeout_seconds() -> int:
    raw = (os.getenv('OPENCLASS_PI_REQUEST_TIMEOUT_SECONDS') or '').strip()
    if not raw:
        return PI_REQUEST_TIMEOUT_SECONDS
    try:
        timeout = int(raw)
    except ValueError as exc:
        raise RuntimeError('OPENCLASS_PI_REQUEST_TIMEOUT_SECONDS must be an integer') from exc
    if not 30 <= timeout <= 30 * 60:
        raise RuntimeError('OPENCLASS_PI_REQUEST_TIMEOUT_SECONDS must be between 30 and 1800 seconds')
    return timeout

def _assistant_text(stdout: str) -> str:
    final_text = ''
    last_error = ''
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get('type') != 'message_end':
            continue
        message = event.get('message')
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            continue
        error_message = message.get('errorMessage')
        if isinstance(error_message, str) and error_message.strip():
            last_error = error_message.strip()
            continue
        content = message.get('content')
        if isinstance(content, str):
            final_text = content.strip()
            if final_text:
                last_error = ''
            continue
        if not isinstance(content, list):
            continue
        text_parts = [item.get('text', '') for item in content if isinstance(item, dict) and item.get('type') == 'text' and isinstance(item.get('text'), str)]
        if text_parts:
            final_text = ''.join(text_parts).strip()
            if final_text:
                last_error = ''
    if not final_text:
        if last_error:
            raise RuntimeError(f'Pi model request failed: {last_error}')
        raise RuntimeError('Pi completed without an assistant response')
    return final_text

def _is_transient_pi_error(error: RuntimeError) -> bool:
    message = str(error).casefold()
    return any((marker in message for marker in PI_TRANSIENT_ERROR_MARKERS))

def _platform_credit_billing_enabled() -> bool:
    return host_service('credit_billing_enabled')

class PiTextClient:
    """Tool-free Pi runtime used behind OpenClass workflow validation."""

    def __init__(self, *, owner_user_id: str, provider: str, model: str, access_method: str | None=None, connection_id: str | None=None, reasoning_effort: str | None=None, service_tier: str | None=None, binary: str | None=None, runtime_root: Path | None=None, process_runner: PiProcessRunner | None=None, billing_service: BillingService | None=None, openrouter_service: OpenRouterProvisioningService | None=None) -> None:
        resolved_binary = binary or pi_binary_path()
        if not resolved_binary:
            raise RuntimeError('Pi is not installed on this server')
        self.owner_user_id = owner_user_id
        self.connection_id = connection_id
        self.provider = provider
        self.model = model
        if provider == 'deepseek':
            from openclass_core.services.deepseek_api import canonical_deepseek_model
            self.model = canonical_deepseek_model(model)
        self.access_method = access_method or ('chatgpt_subscription' if provider == 'openai_codex' else 'platform_credits')
        self.reasoning_effort = reasoning_effort
        self.service_tier = _validated_service_tier(provider, service_tier)
        self.binary = resolved_binary
        self.runtime_root = runtime_root or pi_runtime_root()
        self._process_runner = process_runner
        self._billing_service = billing_service
        self._openrouter_service = openrouter_service
        if self.access_method == 'platform_credits':
            service = self._openrouter()
            if service.config.provisioning_enabled:
                self.provider = 'openrouter'
                self.model = service.config.resolve_model(provider, self.model)

    def _billing(self) -> BillingService:
        if self._billing_service is None:
            self._billing_service = host_service('billing')
        return self._billing_service

    def _openrouter(self) -> OpenRouterProvisioningService:
        if self._openrouter_service is None:
            self._openrouter_service = host_service('openrouter')
        return self._openrouter_service

    def _command(self, *, system_prompt: str, image_paths: list[Path] | None=None, preserve_image_bytes: bool=False) -> list[str]:
        command = [self.binary, '--provider', _pi_provider(self.provider), '--model', self.model, '--mode', 'json', '--no-session', '--no-tools', '--no-extensions', '--no-skills', '--no-prompt-templates', '--no-themes', '--no-context-files', '--approve' if preserve_image_bytes else '--no-approve', '--system-prompt', system_prompt]
        if self.reasoning_effort:
            command.extend(['--thinking', 'off' if self.reasoning_effort == 'none' else self.reasoning_effort])
        needs_runtime_settings_extension = bool(self.service_tier) or (bool(self.reasoning_effort) and self.provider in {'deepseek', 'openrouter'})
        if needs_runtime_settings_extension:
            command.extend(['--extension', str(_runtime_settings_extension_path())])
        command.extend((f'@{path.name}' for path in image_paths or []))
        return command

    @guarded_codex_process
    def _run_once(self, *, system_prompt: str, user_prompt: str, turn_id: str, request_id: str, on_activity: Callable[[AgentActivityEvent], None] | None, on_text_delta: Callable[[str], None] | None, image_inputs: list[str] | None, is_cancelled: Callable[[], bool] | None, preserve_image_bytes: bool=False) -> str:
        load_root_dotenv()
        if preserve_image_bytes and (not image_inputs):
            raise RuntimeError('Original PDF page reading requires image attachments')
        if self.access_method == 'personal_api':
            if not pi_personal_api_configured(owner_user_id=self.owner_user_id, provider=self.provider, runtime_root=self.runtime_root):
                raise RuntimeError('The OpenClass user has not connected this provider API Key')
            persistent_agent_dir = _pi_user_agent_directory(owner_user_id=self.owner_user_id, runtime_root=self.runtime_root)
        elif self.access_method == 'platform_credits':
            persistent_agent_dir = None
        else:
            persistent_agent_dir = pi_agent_directory(owner_user_id=self.owner_user_id, runtime_root=self.runtime_root, connection_id=self.connection_id)
        if self.access_method == 'chatgpt_subscription' and _pi_provider(self.provider) == PI_OPENAI_CODEX_PROVIDER:
            if not ensure_pi_openai_codex_auth(owner_user_id=self.owner_user_id, runtime_root=self.runtime_root, connection_id=self.connection_id):
                raise RuntimeError('The OpenClass user has not connected a ChatGPT account')
        workspace_root = self.runtime_root / 'workspaces'
        workspace_root.mkdir(parents=True, exist_ok=True, mode=448)
        environment = os.environ.copy()
        if self.access_method == 'chatgpt_subscription':
            for name in ('OPENAI_API_KEY', 'OPENAI_AUTH_TOKEN', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN', 'CODEX_ID_TOKEN', 'OPENAI_BASE_URL'):
                environment.pop(name, None)
        environment.pop('OPENROUTER_MANAGEMENT_API_KEY', None)
        environment.pop('OPENROUTER_API_KEY', None)
        environment.update({'PI_OFFLINE': '1', 'PI_SKIP_VERSION_CHECK': '1', 'PI_TELEMETRY': '0'})
        if self.service_tier:
            environment['OPENCLASS_PI_SERVICE_TIER'] = self.service_tier
        if self.reasoning_effort and self.provider in {'deepseek', 'openrouter'}:
            environment['OPENCLASS_PI_REASONING_EFFORT'] = self.reasoning_effort
        if self.provider == 'openrouter' and self.access_method == 'platform_credits':
            environment['OPENROUTER_API_KEY'] = self._openrouter().api_key_for_user(self.owner_user_id)
        with tempfile.TemporaryDirectory(prefix='turn-', dir=workspace_root) as temporary:
            temporary_path = Path(temporary)
            agent_dir = persistent_agent_dir or temporary_path / '.pi-platform-agent'
            agent_dir.mkdir(parents=True, exist_ok=True, mode=448)
            environment['PI_CODING_AGENT_DIR'] = str(agent_dir)
            from openclass_core.services.original_source_delivery import current_original_source_batch
            if current_original_source_batch() is not None:
                preserve_image_bytes = True
            image_paths = _stage_image_inputs(image_inputs or [], workspace=temporary_path, preserve_image_bytes=preserve_image_bytes, provider=self.provider, model=self.model)
            if preserve_image_bytes:
                _write_private_text(temporary_path / '.pi' / 'settings.json', json.dumps({'images': {'autoResize': False, 'blockImages': False}}))
            timeout_seconds = _pi_request_timeout_seconds()
            recorder = _PiActivityRecorder(turn_id=turn_id, request_id=request_id, owner_user_id=self.owner_user_id, lesson_id=str(current_ai_log_context().get('lesson_id') or '') or None, provider=self.provider, model=self.model, request_kind=request_id.rsplit(':', 2)[-2], callback=on_activity, on_text_delta=on_text_delta)
            billing: BillingService | None = None
            reservation_active = False
            if self.access_method == 'platform_credits' and _platform_credit_billing_enabled():
                billing = self._billing()
                try:
                    billing.reserve_model_call(user_id=self.owner_user_id, request_id=request_id, provider=self.provider, model=self.model)
                except BillingError as error:
                    raise RuntimeError(error.detail) from error
                reservation_active = True
            try:
                command = self._command(system_prompt=system_prompt, image_paths=image_paths, preserve_image_bytes=preserve_image_bytes)
                ai_usage_logger.log_model_run_event('started', run_id=request_id, provider=self.provider, model=self.model, status='running', user_id=self.owner_user_id, turn_id=turn_id, request_kind=recorder.request_kind, input_data={'system_prompt_character_count': len(system_prompt), 'system_prompt_sha256': hashlib.sha256(system_prompt.encode()).hexdigest(), 'user_prompt_character_count': len(user_prompt), 'user_prompt_sha256': hashlib.sha256(user_prompt.encode()).hexdigest(), 'image_count': len(image_paths)}, metadata={'agent_backend': 'pi', 'access_method': self.access_method, 'connection_id': current_codex_lease().connection_id if current_codex_lease() else None, 'connection_generation': current_codex_lease().generation if current_codex_lease() else None, 'reasoning_effort': self.reasoning_effort, 'service_tier': self.service_tier, 'attempt': int(request_id.rsplit(':', 1)[-1])})
                image_dispatch = log_image_dispatch(transport='pi_cli_staged_images', provider=self.provider, model=self.model, run_id=request_id, turn_id=turn_id, user_id=self.owner_user_id, preserve_image_bytes=preserve_image_bytes, image_contents=[path.read_bytes() for path in image_paths])
                from openclass_core.services.original_source_wire import validate_staged_original_images
                validate_staged_original_images(system_prompt=system_prompt, user_prompt=user_prompt, image_paths=image_paths)
                if self._process_runner is None:
                    result = _run_streaming_pi_process(command, input_text=user_prompt, cwd=temporary_path, env=environment, timeout=timeout_seconds, recorder=recorder, is_cancelled=is_cancelled)
                else:
                    result = self._process_runner(command, input=user_prompt, text=True, capture_output=True, cwd=temporary_path, env=environment, timeout=timeout_seconds, check=False)
                    for line in result.stdout.splitlines():
                        recorder.observe_line(line)
                if result.returncode == 0:
                    log_image_dispatch_completed(image_dispatch, run_id=request_id, turn_id=turn_id)
            except subprocess.TimeoutExpired as exc:
                ai_usage_logger.log_model_run_event('failed', run_id=request_id, provider=self.provider, model=self.model, status='failed', user_id=self.owner_user_id, turn_id=turn_id, request_kind=recorder.request_kind, error=f'Pi model request timed out after {timeout_seconds} seconds')
                raise RuntimeError(f'Pi model request timed out after {timeout_seconds} seconds') from exc
            except Exception as exc:
                ai_usage_logger.log_model_run_event('failed', run_id=request_id, provider=self.provider, model=self.model, status='failed', user_id=self.owner_user_id, turn_id=turn_id, request_kind=recorder.request_kind, error=str(exc))
                raise
            finally:
                ai_usage_logger.log_event('pi_request_timing', provider=self.provider, model=self.model, request_id=request_id, **recorder.timing_payload())
                if billing is not None and reservation_active:
                    if recorder.upstream_cost_usd is None:
                        billing.release_model_call(request_id=request_id)
                        ai_usage_logger.log_event('pi_usage_unavailable', provider=self.provider, model=self.model, request_id=request_id)
                    else:
                        billing.settle_model_call(request_id=request_id, upstream_cost_usd=recorder.upstream_cost_usd, usage=recorder.usage)
                elif self.provider == 'openrouter' and self.access_method == 'platform_credits' and (recorder.upstream_cost_usd is not None):
                    self._billing().record_openrouter_usage(user_id=self.owner_user_id, upstream_cost_usd=recorder.upstream_cost_usd)
        if result.returncode != 0:
            detail = (result.stderr or '').strip()[-600:]
            recorder.fail('The model process returned a failure status.')
            ai_usage_logger.log_model_run_event('failed', run_id=request_id, provider=self.provider, model=self.model, status='failed', user_id=self.owner_user_id, turn_id=turn_id, request_kind=recorder.request_kind, error=detail or f'exit code {result.returncode}', metadata={'returncode': result.returncode})
            raise RuntimeError('Pi model request failed' + (f': {detail}' if detail else f' with exit code {result.returncode}'))
        try:
            output_text = _assistant_text(result.stdout)
        except RuntimeError as exc:
            recorder.fail('The model did not return a usable assistant result.')
            ai_usage_logger.log_model_run_event('failed', run_id=request_id, provider=self.provider, model=self.model, status='failed', user_id=self.owner_user_id, turn_id=turn_id, request_kind=recorder.request_kind, error=str(exc))
            raise
        ai_usage_logger.log_model_run_event('completed', run_id=request_id, provider=self.provider, model=self.model, status='completed', user_id=self.owner_user_id, turn_id=turn_id, request_kind=recorder.request_kind, output_data={'character_count': len(output_text), 'sha256': hashlib.sha256(output_text.encode()).hexdigest()} if recorder.request_kind != 'text' else {'text': output_text}, usage=recorder.usage, metadata=recorder.timing_payload())
        return output_text

    def _run(self, *, system_prompt: str, user_prompt: str, turn_id: str, request_kind: str, on_activity: Callable[[AgentActivityEvent], None] | None, on_text_delta: Callable[[str], None] | None=None, image_inputs: list[str] | None=None, is_cancelled: Callable[[], bool] | None=None, preserve_image_bytes: bool=False) -> str:
        for attempt in range(PI_TRANSIENT_RETRY_ATTEMPTS + 1):
            attempt_emitted_text = False

            def publish_text_delta(delta: str) -> None:
                nonlocal attempt_emitted_text
                attempt_emitted_text = True
                if on_text_delta is not None:
                    on_text_delta(delta)
            try:
                return self._run_once(system_prompt=system_prompt, user_prompt=user_prompt, turn_id=turn_id, request_id=f'{turn_id}:{request_kind}:{attempt + 1}', on_activity=on_activity, on_text_delta=publish_text_delta if on_text_delta is not None else None, image_inputs=image_inputs, is_cancelled=is_cancelled, preserve_image_bytes=preserve_image_bytes)
            except RuntimeError as error:
                if attempt_emitted_text or attempt >= PI_TRANSIENT_RETRY_ATTEMPTS or (not (_is_transient_pi_error(error) or (preserve_image_bytes and 'fetch failed' in str(error).casefold()))):
                    raise
                if on_activity is not None:
                    on_activity(AgentActivityEvent(turn_id=turn_id, stage='execute_role', label='Model connection interrupted; retrying', status='running', role='OpenClass', metadata={'kind': 'model_retry', 'detail': f'Starting model request attempt {attempt + 2}.', 'agent_backend': 'pi', 'provider': self.provider, 'model': self.model}))
                ai_usage_logger.log_event('pi_transient_request_retry', provider=self.provider, model=self.model, attempt=attempt + 1, error=str(error))
        raise RuntimeError('Pi model request failed after a transient retry')

    @guarded_codex_call
    def parse(self, *, system_prompt: str, user_prompt: str, schema: type[StructuredModel], image_inputs: list[str] | None=None, on_activity: Callable[[AgentActivityEvent], None] | None=None, is_cancelled: Callable[[], bool] | None=None, preserve_image_bytes: bool=False) -> PiStructuredResponse:
        turn_id = new_id('piturn')
        activity_by_id: dict[str, AgentActivityEvent] = {}
        activity_order: list[str] = []

        def record_activity(event: AgentActivityEvent) -> None:
            if event.id not in activity_by_id:
                activity_order.append(event.id)
            activity_by_id[event.id] = event
            if on_activity is not None:
                on_activity(event)

        def current_activity() -> list[AgentActivityEvent]:
            return [activity_by_id[event_id] for event_id in activity_order]
        schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False, separators=(',', ':'))
        structured_system_prompt = f'{system_prompt}\n\nReturn one valid JSON object matching this JSON schema. Do not wrap it in Markdown and do not add prose outside the JSON.\nJSON schema: {schema_text}'
        output_text = self._run(system_prompt=structured_system_prompt, user_prompt=user_prompt, turn_id=turn_id, request_kind='primary', on_activity=record_activity, image_inputs=image_inputs, is_cancelled=is_cancelled, preserve_image_bytes=preserve_image_bytes)
        validation_event = AgentActivityEvent(turn_id=turn_id, stage='verify', label='OpenClass is validating the model result', status='running', role='OpenClass', metadata={'kind': 'structured_validation', 'detail': f'Checking the result against the {schema.__name__} schema.', 'agent_backend': 'pi', 'provider': self.provider, 'model': self.model})
        record_activity(validation_event)
        try:
            parsed = schema.model_validate(json_object(output_text))
        except Exception as first_error:
            from openclass_core.services.structured_output_repair import StructuredOutputValidationError, structured_output_repair_allowed
            if not structured_output_repair_allowed():
                raise StructuredOutputValidationError.from_validation(first_error, schema) from None
            record_activity(validation_event.model_copy(update={'label': 'Model result needs structural repair', 'status': 'blocked', 'metadata': {**validation_event.metadata, 'detail': 'The first result did not match the required structure; requesting a repaired result.'}}))
            initial_issues = validation_issues(first_error)
            repaired_text = self._run(system_prompt=structured_system_prompt, user_prompt=f'{user_prompt}\n\nPrevious response:\n{output_text}\n\n{validation_repair_prompt(first_error)}', turn_id=turn_id, request_kind='repair', on_activity=record_activity, image_inputs=image_inputs, is_cancelled=is_cancelled, preserve_image_bytes=preserve_image_bytes)
            try:
                parsed = schema.model_validate(json_object(repaired_text))
            except Exception as repair_error:
                ai_usage_logger.log_event('pi_structured_response_failed', provider=self.provider, model=self.model, turn_id=turn_id, initial_validation_issues=initial_issues, repair_validation_issues=validation_issues(repair_error))
                raise RuntimeError('Pi returned an invalid structured response') from repair_error
            output_text = repaired_text
            ai_usage_logger.log_event('pi_structured_response_repaired', provider=self.provider, model=self.model, turn_id=turn_id, initial_validation_issues=initial_issues)
        record_activity(validation_event.model_copy(update={'label': 'OpenClass validated the model result', 'status': 'completed', 'metadata': {**validation_event.metadata, 'detail': 'The model result passed schema validation.'}}))
        ai_usage_logger.log_event('pi_request_completed', provider=self.provider, model=self.model, turn_id=turn_id, output_character_count=len(output_text))
        return PiStructuredResponse(output_parsed=parsed, activity=current_activity())

    @guarded_codex_call
    def complete_text(self, *, system_prompt: str, user_prompt: str, image_inputs: list[str] | None=None, on_activity: Callable[[AgentActivityEvent], None] | None=None, on_text_delta: Callable[[str], None] | None=None, is_cancelled: Callable[[], bool] | None=None, preserve_image_bytes: bool=False) -> PiTextResponse:
        turn_id = new_id('piturn')
        activity_by_id: dict[str, AgentActivityEvent] = {}
        activity_order: list[str] = []

        def record_activity(event: AgentActivityEvent) -> None:
            if event.id not in activity_by_id:
                activity_order.append(event.id)
            activity_by_id[event.id] = event
            if on_activity is not None:
                on_activity(event)
        output_text = self._run(system_prompt=system_prompt, user_prompt=user_prompt, turn_id=turn_id, request_kind='text', on_activity=record_activity, on_text_delta=on_text_delta, image_inputs=image_inputs, is_cancelled=is_cancelled, preserve_image_bytes=preserve_image_bytes)
        ai_usage_logger.log_event('pi_text_request_completed', provider=self.provider, model=self.model, turn_id=turn_id, output_character_count=len(output_text))
        return PiTextResponse(output_text=output_text, activity=[activity_by_id[event_id] for event_id in activity_order])
