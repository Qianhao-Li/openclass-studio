from __future__ import annotations
import json
import base64
import binascii
import hashlib
import os
import re
import threading
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator
from pydantic import BaseModel
from openclass_core.models import new_id, now_iso
from openclass_core.services.config import DATA_DIR
_ai_log_context: ContextVar[dict[str, Any] | None] = ContextVar('ai_log_context', default=None)
_SENSITIVE_KEY_PATTERN = re.compile('(?:^|_)(?:api_key|authorization|client_secret|cookie|credential|password|refresh_token|access_token|secret)(?:$|_)', re.IGNORECASE)
_REDACTED = '[REDACTED]'

def _json_safe(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode='json')
    if hasattr(value, 'model_dump'):
        try:
            return value.model_dump(mode='json')
        except TypeError:
            return value.model_dump()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)

def _redact_sensitive(value: Any, *, key: str='') -> Any:
    if key and _SENSITIVE_KEY_PATTERN.search(key.replace('-', '_')):
        return _REDACTED
    if isinstance(value, dict):
        return {str(item_key): _redact_sensitive(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value

def _contains_scope(value: Any, *, key: str, expected: str) -> bool:
    if isinstance(value, dict):
        for item_key, item in value.items():
            if str(item_key) == key and str(item) == expected:
                return True
            if _contains_scope(item, key=key, expected=expected):
                return True
    elif isinstance(value, list):
        return any((_contains_scope(item, key=key, expected=expected) for item in value))
    return False

def current_ai_log_context() -> dict[str, Any]:
    return dict(_ai_log_context.get() or {})

def _image_audit_token(value):
    value = str(value or '')
    return value if re.fullmatch('[A-Za-z0-9_.:-]{0,180}', value) else 'sha256:' + hashlib.sha256(value.encode()).hexdigest()

def _image_audit_event(event_type, payload):
    context = current_ai_log_context()
    safe = {key: _image_audit_token(context[key]) for key in ('trace_id', 'user_id', 'lesson_id', 'turn_id', 'logical_role', 'logical_role_run_id', 'workflow_run_id') if context.get(key) is not None}
    event = {'id': new_id('imageaudit'), 'occurred_at': now_iso(), 'event_type': event_type, 'context': safe, 'payload': payload}
    ai_usage_logger._append(event)
    return event

def log_image_dispatch(*, transport, provider, model, run_id, preserve_image_bytes, image_inputs=None, image_contents=None, user_id=None, lesson_id=None, turn_id=None, role_hint=None):
    """Fingerprint the ordered image bytes at the actual transport boundary."""
    if image_inputs is not None and image_contents is not None:
        raise ValueError('Provide either inline images or dispatched image bytes')
    contents = list(image_contents or [])
    for image in image_inputs or []:
        try:
            header, encoded = str(image).split(',', 1)
            content = base64.b64decode(encoded, validate=True) if header.lower().startswith('data:image/') and header.lower().endswith(';base64') else None
        except (ValueError, binascii.Error):
            content = None
        contents.append(content)
    if not contents:
        return None
    context = current_ai_log_context()
    identity = new_id('imagedispatch')
    _image_audit_event('model_image_dispatch', {'dispatch_id': identity, 'transport': _image_audit_token(transport), 'provider': _image_audit_token(provider), 'model': _image_audit_token(model), 'run_id': _image_audit_token(run_id), 'user_id': _image_audit_token(user_id or context.get('user_id')), 'lesson_id': _image_audit_token(lesson_id or context.get('lesson_id')), 'turn_id': _image_audit_token(turn_id or context.get('turn_id')), 'logical_role': _image_audit_token(context.get('logical_role') or role_hint), 'logical_role_run_id': _image_audit_token(context.get('logical_role_run_id') or run_id), 'image_count': len(contents), 'image_sha256': [hashlib.sha256(value).hexdigest() if isinstance(value, bytes) and value else None for value in contents], 'image_bytes': [len(value) if isinstance(value, bytes) else None for value in contents], 'preserve_image_bytes': bool(preserve_image_bytes)})
    return identity

def log_image_dispatch_completed(dispatch_id, *, run_id='', turn_id=''):
    if dispatch_id:
        _image_audit_event('model_image_dispatch_completed', {'dispatch_id': _image_audit_token(dispatch_id), 'run_id': _image_audit_token(run_id), 'turn_id': _image_audit_token(turn_id), 'status': 'completed'})

@contextmanager
def ai_log_context(**context: Any) -> Iterator[dict[str, Any]]:
    next_context = current_ai_log_context()
    next_context.update({key: _json_safe(value) for key, value in context.items() if value is not None})
    token = _ai_log_context.set(next_context)
    try:
        yield next_context
    finally:
        _ai_log_context.reset(token)

def new_trace_id(prefix: str='trace') -> str:
    return new_id(prefix)

class AIUsageLogger:

    def __init__(self, path: Path | None=None) -> None:
        configured_path = os.getenv('AI_USAGE_LOG_PATH')
        self.path = path or (Path(configured_path) if configured_path else DATA_DIR / 'logs' / 'ai-usage.jsonl')
        self._lock = threading.Lock()
        self._run_sequences: dict[str, int] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log_event(self, event_type: str, **payload: Any) -> dict[str, Any]:
        event = {'id': new_id('ai_log'), 'occurred_at': now_iso(), 'event_type': event_type, 'context': _redact_sensitive(_json_safe(current_ai_log_context())), 'payload': _redact_sensitive(_json_safe(payload))}
        self._append(event)
        return event

    def _append(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a', encoding='utf-8') as output:
                output.write(f'{line}\n')

    def log_model_run_event(self, event: str, *, run_id: str, provider: str, model: str, status: str, user_id: str | None=None, lesson_id: str | None=None, turn_id: str | None=None, parent_run_id: str | None=None, request_kind: str | None=None, input_data: Any=None, output_data: Any=None, delta: str | None=None, usage: Any=None, error: str | None=None, metadata: dict[str, Any] | None=None) -> dict[str, Any]:
        """Append one provider-neutral, ordered model-run audit event."""
        with self._lock:
            sequence_no = self._run_sequences.get(run_id, 0) + 1
            self._run_sequences[run_id] = sequence_no
            payload = {'run_id': run_id, 'sequence_no': sequence_no, 'event': event, 'status': status, 'provider': provider, 'model': model, 'user_id': user_id, 'lesson_id': lesson_id, 'turn_id': turn_id, 'parent_run_id': parent_run_id, 'request_kind': request_kind, 'input': input_data, 'output': output_data, 'delta': delta, 'usage': usage, 'error': error, 'metadata': metadata or {}}
            event_payload = {'id': new_id('ai_log'), 'occurred_at': now_iso(), 'event_type': 'model_run_event', 'context': _redact_sensitive(_json_safe(current_ai_log_context())), 'payload': _redact_sensitive(_json_safe(payload))}
            line = json.dumps(event_payload, ensure_ascii=False)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a', encoding='utf-8') as output:
                output.write(f'{line}\n')
        return event_payload

    def read_lesson_events(self, *, lesson_id: str, limit: int=500, after_event_id: str | None=None) -> dict[str, Any]:
        """Read a lesson's append-only AI events without exposing other lessons."""
        bounded_limit = max(1, min(limit, 2000))
        if not self.path.is_file():
            return {'events': [], 'next_cursor': after_event_id, 'truncated': False}
        if after_event_id:
            matched: list[dict[str, Any]] = []
            cursor_seen = False
            truncated = False
            with self.path.open('r', encoding='utf-8') as source:
                for line in source:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if not cursor_seen:
                        cursor_seen = event.get('id') == after_event_id
                        continue
                    if not _contains_scope(event, key='lesson_id', expected=lesson_id):
                        continue
                    if len(matched) >= bounded_limit:
                        truncated = True
                        break
                    matched.append(_redact_sensitive(event))
            return {'events': matched, 'next_cursor': matched[-1]['id'] if matched else after_event_id, 'truncated': truncated, 'cursor_found': cursor_seen}
        latest: deque[dict[str, Any]] = deque(maxlen=bounded_limit)
        matched_count = 0
        with self.path.open('r', encoding='utf-8') as source:
            for line in source:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if not _contains_scope(event, key='lesson_id', expected=lesson_id):
                    continue
                matched_count += 1
                latest.append(_redact_sensitive(event))
        events = list(latest)
        return {'events': events, 'next_cursor': events[-1]['id'] if events else None, 'truncated': matched_count > len(events), 'cursor_found': True}
ai_usage_logger = AIUsageLogger()

def log_ai_interaction_message(*, channel: str, direction: str, role: str, transport: str, content: str, metadata: dict[str, Any] | None=None) -> dict[str, Any] | None:
    normalized = content.strip()
    if not normalized:
        return None
    payload: dict[str, Any] = {'message_id': new_id('ai_message'), 'channel': channel, 'direction': direction, 'role': role, 'transport': transport, 'content': normalized, 'content_length': len(normalized)}
    if metadata:
        payload['metadata'] = _json_safe(metadata)
    return ai_usage_logger.log_event('ai_interaction_message', **payload)
