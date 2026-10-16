from __future__ import annotations
import json
import asyncio
import queue
import threading
from contextvars import copy_context
from collections.abc import Iterator
from dataclasses import dataclass, field
from time import perf_counter
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from openclass_core.models import AgentActivityEvent, ChatCancellationRequest, ChatRequest, ChatResponse, UserView, new_id, now_iso
from openclass_core.studio_dependencies import current_user
from openclass_core.services import workspace_state
from openclass_core.services.ai_logging import ai_log_context, ai_usage_logger
from openclass_core.services.chat_service import process_chat_on_lesson
from openclass_core.services.chat_stream_cancellation import chat_stream_cancellation_registry
from openclass_core.services.codex_app_server import CodexTurnCancelledError
router = APIRouter()
CHAT_STREAM_HEARTBEAT_SECONDS = 10.0
CHAT_STREAM_DOCUMENT_DELTA_CHARS = 256

@dataclass
class ChatStreamState:
    trace_id: str
    lesson_id: str
    user_id: str
    user_message_excerpt: str
    started_at: float = field(default_factory=perf_counter)
    last_phase: str = 'request'
    first_chat_delta_ms: int | None = None
    first_document_delta_ms: int | None = None
    process_returned_ms: int | None = None
    final_enqueued: bool = False
    final_yielded: bool = False
    error_enqueued: bool = False
    produced_commit_id: str | None = None

def _message_excerpt(message: str, limit: int=180) -> str:
    compact = ' '.join(message.split())
    if len(compact) <= limit:
        return compact
    return f'{compact[:limit - 1]}…'

def _head_commit_id(response: ChatResponse, lesson_id: str) -> str | None:
    lesson = next((item for item in response.course_package.lessons if item.id == lesson_id), None)
    if lesson is None:
        return None
    branch = lesson.history_graph.branches.get(lesson.history_graph.current_branch)
    if branch is not None:
        return branch.head_commit_id
    if lesson.history_graph.commits:
        return lesson.history_graph.commits[-1].id
    return None

def _lesson_document_text(response: ChatResponse, lesson_id: str) -> str:
    lesson = next((item for item in response.course_package.lessons if item.id == lesson_id), None)
    if lesson is None:
        return ''
    return lesson.board_document.content_text or ''

def _log_stream_lifecycle(state: ChatStreamState, event: str, **payload: object) -> None:
    ai_usage_logger.log_event('chat_stream_lifecycle', stream_event=event, trace_id=state.trace_id, lesson_id=state.lesson_id, user_id=state.user_id, user_message_excerpt=state.user_message_excerpt, last_phase=state.last_phase, final_enqueued=state.final_enqueued, final_yielded=state.final_yielded, error_enqueued=state.error_enqueued, produced_commit_id=state.produced_commit_id, **payload)

def _sse_event(event: str, data: object) -> str:
    return f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'

def _elapsed_ms_since(started_at: float) -> int:
    return max(0, round((perf_counter() - started_at) * 1000))

def _document_delta_chunks(document_text: str) -> Iterator[str]:
    chunk_size = max(1, CHAT_STREAM_DOCUMENT_DELTA_CHARS)
    for start in range(0, len(document_text), chunk_size):
        yield document_text[start:start + chunk_size]

def _start_chat_stream(lesson_id: str, request: ChatRequest, *, user_id: str, events=None):
    if events is None:
        events = queue.Queue()
    consumer_connected = threading.Event()
    consumer_connected.set()
    cancellation_handle = chat_stream_cancellation_registry.register(user_id=user_id, lesson_id=lesson_id, session_id=request.session_id, input_event_id=request.input_event_id)
    cancel_event = cancellation_handle.event
    state = ChatStreamState(trace_id=new_id('chat'), lesson_id=lesson_id, user_id=user_id, user_message_excerpt=_message_excerpt(request.message))
    chat_delta_emitted = False
    document_delta_emitted = False
    native_teaching_active = False
    emitted_activity_payloads: dict[str, str] = {}

    def emit(event: str, data: object) -> None:
        if consumer_connected.is_set():
            events.put((event, data))

    def log_first_delta_once(*, metric: str, role: str, field: str) -> None:
        if metric == 'chat' and state.first_chat_delta_ms is None:
            state.first_chat_delta_ms = _elapsed_ms_since(state.started_at)
            _log_stream_lifecycle(state, 'first_chat_delta_sent', elapsed_ms=state.first_chat_delta_ms, role=role, field=field)
        elif metric == 'document' and state.first_document_delta_ms is None:
            state.first_document_delta_ms = _elapsed_ms_since(state.started_at)
            _log_stream_lifecycle(state, 'first_document_delta_sent', elapsed_ms=state.first_document_delta_ms, role=role, field=field)

    def emit_codex_delta(delta: str) -> None:
        nonlocal chat_delta_emitted
        if not delta or not consumer_connected.is_set():
            return
        state.last_phase = 'codex'
        log_first_delta_once(metric='chat', role='codex', field='agent_message')
        emit('chat_delta', {'delta': delta})
        chat_delta_emitted = True

    def emit_requirement_update(payload: dict[str, object]) -> None:
        if 'board_task_sheet' in payload:
            state.last_phase = 'board_task'
            emit('board_task_update', payload)
            return
        state.last_phase = 'learning_requirement'
        emit('requirement_update', payload)

    def emit_agent_activity_event(event: AgentActivityEvent) -> None:
        nonlocal native_teaching_active
        state.last_phase = 'codex_activity'
        payload = event.model_dump(mode='json')
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if emitted_activity_payloads.get(event.id) == serialized:
            return
        emitted_activity_payloads[event.id] = serialized
        activity_kind = event.metadata.get('kind')
        if activity_kind == 'teaching_task':
            native_teaching_active = True
            emit('teaching_task', {'task_id': event.metadata.get('teaching_task_id'), 'status': event.metadata.get('status'), 'session_id': event.metadata.get('session_id'), 'label': event.label})
        elif activity_kind == 'tool_activity':
            emit('tool_activity', {'task_id': event.metadata.get('teaching_task_id'), 'tool_name': event.metadata.get('tool_name'), 'status': event.metadata.get('status'), 'parameters': event.metadata.get('parameters'), 'duration_ms': event.metadata.get('duration_ms'), 'result': event.metadata.get('result'), 'error': event.metadata.get('error'), 'label': event.label})
        emit('agent_activity', payload)

    def emit_document_commit(document_text: str, commit_id: str) -> None:
        nonlocal document_delta_emitted
        state.produced_commit_id = commit_id
        if not document_text or not consumer_connected.is_set():
            return
        state.last_phase = 'board_committed'
        log_first_delta_once(metric='document', role='codex', field='board.md')
        if native_teaching_active:
            emit('document_snapshot', {'content_text': document_text, 'commit_id': commit_id, 'reason': 'Native Pi teaching tool committed board progress.'})
            document_delta_emitted = True
            return
        if document_delta_emitted:
            return
        for delta in _document_delta_chunks(document_text):
            emit('document_delta', {'delta': delta})
        document_delta_emitted = True

    def emit_missing_visible_deltas(response: ChatResponse) -> None:
        nonlocal chat_delta_emitted, document_delta_emitted
        if not consumer_connected.is_set():
            return
        if not chat_delta_emitted and response.chatbot_message:
            log_first_delta_once(metric='chat', role='codex', field='agent_message')
            emit('chat_delta', {'delta': response.chatbot_message})
            chat_delta_emitted = True
        if not document_delta_emitted and response.board_document_operation_status == 'succeeded':
            document_text = _lesson_document_text(response, lesson_id)
            if document_text:
                log_first_delta_once(metric='document', role='codex', field='board.md')
                for delta in _document_delta_chunks(document_text):
                    emit('document_delta', {'delta': delta})
                document_delta_emitted = True

    def emit_agent_activity(response: ChatResponse) -> None:
        for event in response.agent_activity:
            emit_agent_activity_event(event)

    def run() -> None:
        with ai_log_context(trace_id=state.trace_id, route='/api/lessons/{lesson_id}/chat/stream', lesson_id=lesson_id, user_id=user_id):
            _log_stream_lifecycle(state, 'stream_started', elapsed_ms=0)
            try:
                emit('phase', {'label': 'Preparing reply', 'role': 'request'})
                response = process_chat_on_lesson(lesson_id, request, user_id=user_id, on_delta=emit_codex_delta, on_requirement_update=emit_requirement_update, on_agent_activity=emit_agent_activity_event, on_document_commit=emit_document_commit, is_cancelled=cancel_event.is_set)
                state.process_returned_ms = _elapsed_ms_since(state.started_at)
                _log_stream_lifecycle(state, 'process_chat_returned', elapsed_ms=state.process_returned_ms)
                state.produced_commit_id = _head_commit_id(response, lesson_id)
                emit_missing_visible_deltas(response)
                emit_agent_activity(response)
                state.final_enqueued = True
                emit('final', response.model_dump(mode='json'))
                _log_stream_lifecycle(state, 'stream_final_sent' if consumer_connected.is_set() else 'background_process_completed', elapsed_ms=_elapsed_ms_since(state.started_at))
            except CodexTurnCancelledError:
                _log_stream_lifecycle(state, 'stream_cancelled', elapsed_ms=_elapsed_ms_since(state.started_at))
            except Exception as exc:
                state.error_enqueued = True
                emit('error', {'message': str(exc), 'trace_id': state.trace_id})
                _log_stream_lifecycle(state, 'stream_error', elapsed_ms=_elapsed_ms_since(state.started_at), error_message=str(exc))
            finally:
                chat_stream_cancellation_registry.release(cancellation_handle)
                events.put(None)
    worker_context = copy_context()
    worker = threading.Thread(target=lambda: worker_context.run(run), daemon=True)
    worker.start()
    return (events, consumer_connected, state)

def _finish_stream_consumer(consumer_connected: threading.Event, state: ChatStreamState) -> None:
    consumer_connected.clear()
    if not state.final_yielded and (not state.error_enqueued):
        _log_stream_lifecycle(state, 'stream_disconnected_background_continues', elapsed_ms=_elapsed_ms_since(state.started_at))

async def _async_chat_stream_events(lesson_id: str, request: ChatRequest, *, user_id: str):
    """SSE waits do not borrow the synchronous HTTP request thread pool."""
    loop = asyncio.get_running_loop()
    events: asyncio.Queue = asyncio.Queue()

    class EventBridge:

        def put(self, value) -> None:
            if not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(events.put_nowait, value)
                except RuntimeError:
                    pass
    _, consumer_connected, state = _start_chat_stream(lesson_id, request, user_id=user_id, events=EventBridge())
    try:
        while True:
            try:
                item = await asyncio.wait_for(events.get(), CHAT_STREAM_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield _sse_event('heartbeat', {'trace_id': state.trace_id, 'ts': now_iso()})
                continue
            if item is None:
                break
            event, data = item
            if event == 'final':
                state.final_yielded = True
            yield _sse_event(event, data)
    finally:
        _finish_stream_consumer(consumer_connected, state)

@router.post('/api/lessons/{lesson_id}/chat', response_model=ChatResponse)
def chat_on_lesson(lesson_id: str, request: ChatRequest, user: UserView=Depends(current_user)) -> ChatResponse:
    with ai_log_context(trace_id=new_id('chat'), route='/api/lessons/{lesson_id}/chat', lesson_id=lesson_id, user_id=user.id):
        return process_chat_on_lesson(lesson_id, request, user_id=user.id)

@router.get('/api/lessons/{lesson_id}/model-run-history')
def model_run_history(lesson_id: str, limit: int=Query(default=500, ge=1, le=2000), after: str | None=Query(default=None), user: UserView=Depends(current_user)) -> dict[str, object]:
    workspace = workspace_state.load_workspace_for_user(user.id)
    workspace_state.find_lesson_package(workspace, lesson_id)
    history = ai_usage_logger.read_lesson_events(lesson_id=lesson_id, limit=limit, after_event_id=after)
    return {'lesson_id': lesson_id, **history}

@router.post('/api/lessons/{lesson_id}/chat/stream')
def stream_chat_on_lesson(lesson_id: str, request: ChatRequest, user: UserView=Depends(current_user)) -> StreamingResponse:
    return StreamingResponse(_async_chat_stream_events(lesson_id, request, user_id=user.id), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@router.post('/api/lessons/{lesson_id}/chat/cancel')
def cancel_stream_chat_on_lesson(lesson_id: str, request: ChatCancellationRequest, user: UserView=Depends(current_user)) -> dict[str, object]:
    workspace = workspace_state.load_workspace_for_user(user.id)
    workspace_state.find_lesson_package(workspace, lesson_id)
    active = chat_stream_cancellation_registry.cancel(user_id=user.id, lesson_id=lesson_id, session_id=request.session_id, input_event_id=request.input_event_id)
    ai_usage_logger.log_event('chat_stream_explicit_cancel_requested', lesson_id=lesson_id, user_id=user.id, session_id=request.session_id, input_event_id=request.input_event_id, active=active)
    return {'status': 'cancel_requested', 'active': active}

@router.get('/api/lessons/{lesson_id}/chat/status')
def stream_chat_status_on_lesson(lesson_id: str, session_id: str=Query(min_length=1, max_length=160), input_event_id: str=Query(min_length=1, max_length=200), user: UserView=Depends(current_user)) -> dict[str, object]:
    workspace = workspace_state.load_workspace_for_user(user.id)
    workspace_state.find_lesson_package(workspace, lesson_id)
    return {'status': 'running' if chat_stream_cancellation_registry.is_active(user_id=user.id, lesson_id=lesson_id, session_id=session_id, input_event_id=input_event_id) else 'finished'}
