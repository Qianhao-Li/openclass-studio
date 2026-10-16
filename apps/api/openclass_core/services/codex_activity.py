from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Any, Callable
from openclass_core.models import AgentActivityEvent
PUBLIC_AGENT_ROLE = 'OpenClass'
PUBLIC_TOOL_ROLE = 'OpenClass tool'

def _activity_status(value: object, *, default: str='completed') -> str:
    normalized = str(value or '').strip().lower()
    if normalized in {'inprogress', 'in_progress', 'pending', 'running'}:
        return 'running'
    if normalized in {'failed', 'error'}:
        return 'failed'
    if normalized in {'declined', 'blocked'}:
        return 'blocked'
    if normalized in {'skipped', 'cancelled', 'canceled'}:
        return 'skipped'
    return default

def _activity_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ''
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)

class CodexActivityRecorder:
    """Normalize public app-server progress into durable learner-visible events."""

    def __init__(self, callback: Callable[[AgentActivityEvent], None] | None=None, *, capture_agent_output: bool=False) -> None:
        self.callback = callback
        self.capture_agent_output = capture_agent_output
        self._events: dict[str, AgentActivityEvent] = {}
        self._order: list[str] = []
        self._item_phases: dict[str, str] = {}

    @property
    def events(self) -> list[AgentActivityEvent]:
        return [self._events[event_id] for event_id in self._order]

    def _publish(self, *, item_id: str, turn_id: str, stage: str, label: str, status: str, role: str, metadata: dict[str, Any]) -> None:
        if not item_id:
            return
        existing = self._events.get(item_id)
        event = AgentActivityEvent(id=item_id, turn_id=turn_id or (existing.turn_id if existing else ''), stage=stage, label=label, status=status, role=role, metadata=metadata, created_at=existing.created_at if existing is not None else datetime.now(timezone.utc).isoformat())
        if existing is None:
            self._order.append(item_id)
        self._events[item_id] = event
        if self.callback is not None:
            self.callback(event)

    def _append_detail(self, *, item_id: str, turn_id: str, delta: str, stage: str, label: str, role: str, kind: str, text_field: str='detail') -> None:
        if not delta:
            return
        existing = self._events.get(item_id)
        metadata = dict(existing.metadata) if existing is not None else {'kind': kind}
        metadata[text_field] = f'{_activity_text(metadata.get(text_field))}{delta}'
        self._publish(item_id=item_id, turn_id=turn_id, stage=stage, label=label, status='running', role=role, metadata=metadata)

    def is_commentary_agent_message(self, item_id: str) -> bool:
        return self._item_phases.get(item_id) == 'commentary'

    def _source_function_output(self, item: dict[str, Any], turn_id: str) -> bool:
        if not self.capture_agent_output or item.get('type') != 'functionCallOutput':
            return False
        output = item.get('output')
        if isinstance(output, list):
            output = '\n'.join((str(part.get('text') or '') for part in output if isinstance(part, dict) and part.get('type') == 'input_text'))
        self._publish(item_id=str(item.get('id') or ''), turn_id=turn_id, stage='execute_role', label=f"Tool result: {item.get('name') or 'tool'}", status='completed', role=PUBLIC_TOOL_ROLE, metadata={'kind': 'functionCallOutput', 'tool': str(item.get('name') or ''), 'output': _activity_text(output)})
        return True

    def start_item(self, params: dict[str, Any]) -> None:
        item = params.get('item') if isinstance(params.get('item'), dict) else {}
        item_id = str(item.get('id') or '')
        turn_id = str(params.get('turnId') or '')
        item_type = str(item.get('type') or '')
        if self._source_function_output(item, turn_id):
            return
        if item_type == 'agentMessage':
            phase = str(item.get('phase') or '')
            if item_id:
                self._item_phases[item_id] = phase
            is_commentary = phase == 'commentary'
            if not is_commentary and (not self.capture_agent_output):
                return
            self._publish(item_id=item_id, turn_id=turn_id, stage='execute_role', label='OpenClass progress' if is_commentary else 'Source AI is responding', status='running', role=PUBLIC_AGENT_ROLE, metadata={'kind': 'commentary' if is_commentary else 'agent_output', 'detail' if is_commentary else 'output': str(item.get('text') or '')})
            return
        descriptors: dict[str, tuple[str, str, str]] = {'reasoning': ('build_context', 'OpenClass is thinking', PUBLIC_AGENT_ROLE), 'plan': ('build_context', 'Preparing work plan', PUBLIC_AGENT_ROLE), 'commandExecution': ('execute_role', 'Running command', PUBLIC_TOOL_ROLE), 'fileChange': ('execute_role', 'Updating board document', PUBLIC_TOOL_ROLE), 'mcpToolCall': ('execute_role', 'Using tool', PUBLIC_TOOL_ROLE), 'dynamicToolCall': ('execute_role', 'Using tool', PUBLIC_TOOL_ROLE), 'collabAgentToolCall': ('execute_role', 'Collaborating', PUBLIC_AGENT_ROLE), 'subAgentActivity': ('execute_role', 'Collaborating', PUBLIC_AGENT_ROLE), 'webSearch': ('build_context', 'Searching the web', PUBLIC_TOOL_ROLE), 'imageView': ('build_context', 'Viewing image', PUBLIC_TOOL_ROLE), 'imageGeneration': ('execute_role', 'Generating image', PUBLIC_TOOL_ROLE), 'contextCompaction': ('build_context', 'Organizing context', PUBLIC_AGENT_ROLE)}
        descriptor = descriptors.get(item_type)
        if descriptor is None:
            return
        stage, label, role = descriptor
        metadata: dict[str, Any] = {'kind': item_type}
        if item_type == 'commandExecution':
            metadata.update({'command': str(item.get('command') or ''), 'cwd': str(item.get('cwd') or ''), 'detail': str(item.get('aggregatedOutput') or '')})
        elif item_type == 'webSearch':
            metadata.update({'query': str(item.get('query') or ''), 'detail': str(item.get('query') or '')})
        elif item_type in {'mcpToolCall', 'dynamicToolCall'}:
            metadata.update({'tool': str(item.get('tool') or ''), 'server': str(item.get('server') or item.get('namespace') or ''), 'arguments': item.get('arguments')})
        self._publish(item_id=item_id, turn_id=turn_id, stage=stage, label=label, status='running', role=role, metadata=metadata)

    def append_notification_delta(self, method: str, params: dict[str, Any]) -> bool:
        item_id = str(params.get('itemId') or '')
        turn_id = str(params.get('turnId') or '')
        delta = str(params.get('delta') or params.get('message') or '')
        if method == 'item/agentMessage/delta' and (self.is_commentary_agent_message(item_id) or self.capture_agent_output):
            is_commentary = self.is_commentary_agent_message(item_id)
            self._append_detail(item_id=item_id, turn_id=turn_id, delta=delta, stage='execute_role', label='OpenClass progress' if is_commentary else 'Source AI is responding', role=PUBLIC_AGENT_ROLE, kind='commentary' if is_commentary else 'agent_output', text_field='detail' if is_commentary else 'output')
            return True
        mappings: dict[str, tuple[str, str, str, str]] = {'item/reasoning/summaryTextDelta': ('build_context', 'OpenClass is thinking', PUBLIC_AGENT_ROLE, 'reasoning'), 'item/plan/delta': ('build_context', 'Preparing work plan', PUBLIC_AGENT_ROLE, 'plan'), 'item/commandExecution/outputDelta': ('execute_role', 'Running command', PUBLIC_TOOL_ROLE, 'commandExecution'), 'item/fileChange/outputDelta': ('execute_role', 'Updating board document', PUBLIC_TOOL_ROLE, 'fileChange'), 'item/mcpToolCall/progress': ('execute_role', 'Using tool', PUBLIC_TOOL_ROLE, 'mcpToolCall')}
        mapping = mappings.get(method)
        if mapping is None:
            return False
        stage, label, role, kind = mapping
        self._append_detail(item_id=item_id, turn_id=turn_id, delta=delta, stage=stage, label=label, role=role, kind=kind)
        return True

    def complete_item(self, params: dict[str, Any]) -> None:
        item = params.get('item') if isinstance(params.get('item'), dict) else {}
        item_id = str(item.get('id') or '')
        turn_id = str(params.get('turnId') or '')
        item_type = str(item.get('type') or '')
        if self._source_function_output(item, turn_id):
            return
        if item_type == 'agentMessage':
            phase = str(item.get('phase') or self._item_phases.get(item_id) or '')
            if item_id:
                self._item_phases[item_id] = phase
            is_commentary = phase == 'commentary'
            if not is_commentary and (not self.capture_agent_output):
                return
            existing = self._events.get(item_id)
            metadata = dict(existing.metadata) if existing is not None else {'kind': 'commentary' if is_commentary else 'agent_output'}
            text = str(item.get('text') or '')
            if text:
                metadata['detail' if is_commentary else 'output'] = text
            self._publish(item_id=item_id, turn_id=turn_id, stage='execute_role', label='OpenClass progress' if is_commentary else 'Source AI returned a result', status='completed', role=PUBLIC_AGENT_ROLE, metadata=metadata)
            return
        if item_type == 'reasoning':
            summary = item.get('summary') if isinstance(item.get('summary'), list) else []
            detail = '\n\n'.join((str(part) for part in summary if str(part).strip()))
            existing = self._events.get(item_id)
            metadata = dict(existing.metadata) if existing is not None else {'kind': 'reasoning'}
            if detail:
                metadata['detail'] = detail
            self._publish(item_id=item_id, turn_id=turn_id, stage='build_context', label='OpenClass finished thinking', status='completed', role=PUBLIC_AGENT_ROLE, metadata=metadata)
            return
        descriptors: dict[str, tuple[str, str, str]] = {'plan': ('build_context', 'Work plan updated', PUBLIC_AGENT_ROLE), 'commandExecution': ('execute_role', 'Command completed', PUBLIC_TOOL_ROLE), 'fileChange': ('execute_role', 'Board document updated', PUBLIC_TOOL_ROLE), 'mcpToolCall': ('execute_role', 'Tool call completed', PUBLIC_TOOL_ROLE), 'dynamicToolCall': ('execute_role', 'Tool call completed', PUBLIC_TOOL_ROLE), 'collabAgentToolCall': ('execute_role', 'Collaboration completed', PUBLIC_AGENT_ROLE), 'subAgentActivity': ('execute_role', 'Collaboration completed', PUBLIC_AGENT_ROLE), 'webSearch': ('build_context', 'Web search completed', PUBLIC_TOOL_ROLE), 'imageView': ('build_context', 'Image viewed', PUBLIC_TOOL_ROLE), 'imageGeneration': ('execute_role', 'Image generated', PUBLIC_TOOL_ROLE), 'contextCompaction': ('build_context', 'Context organized', PUBLIC_AGENT_ROLE)}
        descriptor = descriptors.get(item_type)
        if descriptor is None:
            return
        stage, label, role = descriptor
        existing = self._events.get(item_id)
        metadata = dict(existing.metadata) if existing is not None else {'kind': item_type}
        if item_type == 'plan':
            metadata['detail'] = str(item.get('text') or metadata.get('detail') or '')
        elif item_type == 'commandExecution':
            metadata.update({'command': str(item.get('command') or metadata.get('command') or ''), 'cwd': str(item.get('cwd') or metadata.get('cwd') or ''), 'detail': str(item.get('aggregatedOutput') or metadata.get('detail') or ''), 'exit_code': item.get('exitCode'), 'duration_ms': item.get('durationMs')})
        elif item_type == 'fileChange':
            metadata['changes'] = item.get('changes') if isinstance(item.get('changes'), list) else []
        elif item_type in {'mcpToolCall', 'dynamicToolCall'}:
            metadata.update({'tool': str(item.get('tool') or metadata.get('tool') or ''), 'server': str(item.get('server') or item.get('namespace') or metadata.get('server') or ''), 'arguments': item.get('arguments', metadata.get('arguments')), 'result': item.get('result') or item.get('contentItems'), 'error': item.get('error'), 'duration_ms': item.get('durationMs')})
        elif item_type == 'webSearch':
            metadata.update({'query': str(item.get('query') or metadata.get('query') or ''), 'action': item.get('action')})
            metadata['detail'] = str(metadata.get('query') or '')
        self._publish(item_id=item_id, turn_id=turn_id, stage=stage, label=label, status=_activity_status(item.get('status')), role=role, metadata=metadata)
