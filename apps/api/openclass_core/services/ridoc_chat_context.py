from __future__ import annotations
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
from dataclasses import dataclass
from typing import Any
from openclass_core.models import ChatRequest, ChatResponse, ConversationTurn
from openclass_core.services.ridoc_learning_sequence import branch_sequence, sequence_at_node
from openclass_core.services.ridoc_v3 import RidocArchiveV3, RidocMessageNode, RidocV3ConflictError, RidocV3Error

@dataclass(frozen=True)
class RidocChatContext:
    store: Any
    owner_user_id: str
    package_id: str
    lesson_id: str
    transaction_id: str
    session_id: str
    input_event_id: str
    epoch: int

    def assert_current(self) -> None:
        self.store.assert_turn_not_deleted(owner_user_id=self.owner_user_id, package_id=self.package_id, lesson_id=self.lesson_id, transaction_id=self.transaction_id, session_id=self.session_id, input_event_id=self.input_event_id)
        if self.store.context_epoch(owner_user_id=self.owner_user_id, package_id=self.package_id, lesson_id=self.lesson_id) != self.epoch:
            raise RidocV3ConflictError('Conversation history changed; reload before continuing')
_CURRENT_CONTEXT: ContextVar[RidocChatContext | None] = ContextVar('openclass_ridoc_chat_context', default=None)

def create_ridoc_chat_context(store: Any, *, owner_user_id: str, package_id: str, lesson_id: str, request: ChatRequest) -> RidocChatContext:
    identity = {'owner_user_id': owner_user_id, 'package_id': package_id, 'lesson_id': lesson_id, 'transaction_id': request.turn_id or '', 'session_id': request.session_id or '', 'input_event_id': request.input_event_id or ''}
    store.assert_turn_not_deleted(**identity)
    store.bind_turn_input_identity(**identity)
    context = RidocChatContext(store=store, **identity, epoch=store.context_epoch(owner_user_id=owner_user_id, package_id=package_id, lesson_id=lesson_id))
    context.assert_current()
    return context

@contextmanager
def bind_ridoc_chat_context(context: RidocChatContext) -> Iterator[None]:
    context.assert_current()
    token = _CURRENT_CONTEXT.set(context)
    try:
        yield
    finally:
        _CURRENT_CONTEXT.reset(token)

@contextmanager
def ridoc_completion_guard(context: RidocChatContext) -> Iterator[None]:
    from openclass_core.services.ridoc_lock import ridoc_file_lock
    path = context.store.package_path(context.owner_user_id, context.package_id)
    with context.store._lock, ridoc_file_lock(path):
        context.assert_current()
        yield
        context.assert_current()

def current_context_epoch() -> int | None:
    context = _CURRENT_CONTEXT.get()
    return context.epoch if context is not None else None

def current_context_scope() -> str | None:
    context = _CURRENT_CONTEXT.get()
    return context.store.deletion_scope_id(context.package_id) if context is not None else None

def provider_session_reuse_allowed() -> bool:
    context = _CURRENT_CONTEXT.get()
    if context is None:
        return True
    context.assert_current()
    return context.epoch == 0

def provider_session_context_key() -> str:
    context = _CURRENT_CONTEXT.get()
    if context is None or context.epoch == 0:
        return ''
    context.assert_current()
    scope = context.store.deletion_scope_id(context.package_id)
    identity = f'{context.owner_user_id}:{scope}:{context.lesson_id}:{context.epoch}'
    return hashlib.sha256(identity.encode('utf-8')).hexdigest()

def authoritative_conversation(archive: RidocArchiveV3, lesson_id: str, *, parent_id: str | None=None) -> list[ConversationTurn]:
    graph = archive.package.lessons.get(lesson_id)
    if graph is None:
        raise RidocV3Error('RIDOC lesson is unavailable')
    sequence = sequence_at_node(archive, lesson_id, parent_id) if parent_id else branch_sequence(archive, lesson_id)
    messages = [archive.nodes[f'{lesson_id}/{node_id}'] for node_id in sequence if isinstance(archive.nodes[f'{lesson_id}/{node_id}'], RidocMessageNode)]
    return [turn for node in messages[-4:] for turn in (ConversationTurn(role='user', content=node.user_message), ConversationTurn(role='assistant', content=node.assistant_message))]

def request_with_authoritative_conversation(request: ChatRequest, archive: RidocArchiveV3, lesson_id: str, *, parent_id: str | None=None) -> ChatRequest:
    return request.model_copy(update={'conversation': authoritative_conversation(archive, lesson_id, parent_id=parent_id)})

def guard_cached_response(response: ChatResponse) -> ChatResponse:
    context = _CURRENT_CONTEXT.get()
    copied = response.model_copy(deep=True)
    if context is None:
        return copied
    context.assert_current()
    from openclass_core.services import workspace_state
    package = context.store.load_package(context.owner_user_id, package_id=context.package_id)
    copied.course_package = workspace_state.package_view(package, is_standalone=package.package_kind == 'standalone', resource_lesson_id=context.lesson_id, isolate_lesson_resources=package.package_kind == 'standalone')
    copied.ridoc_revision = package.ridoc_revision
    context.assert_current()
    return copied

def authoritative_routing_request(lesson_id: str, request: ChatRequest, *, user_id: str) -> ChatRequest:
    context = _CURRENT_CONTEXT.get()
    if context is not None:
        context.assert_current()
        return request
    from openclass_core.services import workspace_state
    from openclass_core.services.ridoc_workspace_store import resolve_conversation_branch_parent
    if not workspace_state.is_ridoc_lesson_for_user(user_id, lesson_id):
        return request
    store = workspace_state.get_ridoc_workspace_store()
    archive = store.load_archive(user_id, lesson_id=lesson_id)
    actual_revision = archive.revision.value if archive.revision else ''
    if request.expected_ridoc_revision != actual_revision:
        raise RidocV3ConflictError('RIDOC revision changed; reload before routing')
    store.assert_turn_not_deleted(owner_user_id=user_id, package_id=archive.package.id, lesson_id=lesson_id, transaction_id=request.turn_id or '', session_id=request.session_id or '', input_event_id=request.input_event_id or '')
    parent_id = None
    if request.history_branch_action is not None:
        action = request.history_branch_action
        _, parent_id = resolve_conversation_branch_parent(archive, lesson_id, action.kind, action.source_node_id)
    return request_with_authoritative_conversation(request, archive, lesson_id, parent_id=parent_id)
