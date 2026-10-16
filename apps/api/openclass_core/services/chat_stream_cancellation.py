from __future__ import annotations
import threading
from collections import OrderedDict
from dataclasses import dataclass

@dataclass(frozen=True)
class ChatStreamCancellationKey:
    user_id: str
    lesson_id: str
    session_id: str
    input_event_id: str

@dataclass(frozen=True)
class ChatStreamCancellationHandle:
    key: ChatStreamCancellationKey | None
    event: threading.Event

class ChatStreamCancellationRegistry:
    """Separate explicit user cancellation from a dropped SSE connection."""

    def __init__(self, *, pending_limit: int=256) -> None:
        self._lock = threading.Lock()
        self._active: dict[ChatStreamCancellationKey, set[threading.Event]] = {}
        self._pending: OrderedDict[ChatStreamCancellationKey, None] = OrderedDict()
        self._pending_limit = max(1, pending_limit)

    def register(self, *, user_id: str, lesson_id: str, session_id: str | None, input_event_id: str | None) -> ChatStreamCancellationHandle:
        key = self._key(user_id=user_id, lesson_id=lesson_id, session_id=session_id, input_event_id=input_event_id)
        event = threading.Event()
        if key is None:
            return ChatStreamCancellationHandle(key=None, event=event)
        with self._lock:
            if key in self._pending:
                self._pending.pop(key, None)
                event.set()
            self._active.setdefault(key, set()).add(event)
        return ChatStreamCancellationHandle(key=key, event=event)

    def cancel(self, *, user_id: str, lesson_id: str, session_id: str, input_event_id: str) -> bool:
        key = ChatStreamCancellationKey(user_id=user_id, lesson_id=lesson_id, session_id=session_id, input_event_id=input_event_id)
        with self._lock:
            events = self._active.get(key)
            if events:
                for event in events:
                    event.set()
                return True
            self._pending[key] = None
            self._pending.move_to_end(key)
            while len(self._pending) > self._pending_limit:
                self._pending.popitem(last=False)
        return False

    def release(self, handle: ChatStreamCancellationHandle) -> None:
        if handle.key is None:
            return
        with self._lock:
            events = self._active.get(handle.key)
            if events is not None:
                events.discard(handle.event)
                if not events:
                    self._active.pop(handle.key, None)
            self._pending.pop(handle.key, None)

    def cancel_lessons(self, user_id: str, lesson_ids: set[str]) -> None:
        """End tasks belonging to an isolated course copy without touching other sessions."""
        with self._lock:
            for key, events in self._active.items():
                if key.user_id == user_id and key.lesson_id in lesson_ids:
                    for event in events:
                        event.set()

    def is_active(self, *, user_id: str, lesson_id: str, session_id: str, input_event_id: str) -> bool:
        key = ChatStreamCancellationKey(user_id=user_id, lesson_id=lesson_id, session_id=session_id, input_event_id=input_event_id)
        with self._lock:
            return bool(self._active.get(key))

    @staticmethod
    def _key(*, user_id: str, lesson_id: str, session_id: str | None, input_event_id: str | None) -> ChatStreamCancellationKey | None:
        if not session_id or not input_event_id:
            return None
        return ChatStreamCancellationKey(user_id=user_id, lesson_id=lesson_id, session_id=session_id, input_event_id=input_event_id)
chat_stream_cancellation_registry = ChatStreamCancellationRegistry()
