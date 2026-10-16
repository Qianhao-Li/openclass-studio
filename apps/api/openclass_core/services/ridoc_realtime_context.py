"""Keep provider sessions bound to the history generation they were opened with."""
from __future__ import annotations
from collections.abc import Iterator
from contextlib import contextmanager
from openclass_core.models import RealtimeConnectRequest, new_id
from openclass_core.services.ridoc_chat_context import authoritative_conversation
from openclass_core.services.ridoc_lock import ridoc_file_lock
from openclass_core.services.ridoc_v3 import RidocV3ConflictError

def _native_store(lesson_id: str, user_id: str):
    from openclass_core.services import workspace_state
    if not workspace_state.is_ridoc_lesson_for_user(user_id, lesson_id):
        return None
    return workspace_state.get_ridoc_workspace_store()

def _ensure_session_table(connection) -> None:
    connection.execute('\n        CREATE TABLE IF NOT EXISTS ridoc_realtime_contexts (\n            owner_id TEXT NOT NULL, scope_id TEXT NOT NULL,\n            lesson_id TEXT NOT NULL, session_id TEXT NOT NULL,\n            epoch INTEGER NOT NULL,\n            PRIMARY KEY(owner_id, scope_id, lesson_id, session_id)\n        )\n    ')

def prepare_realtime_context(lesson_id: str, request: RealtimeConnectRequest, *, user_id: str) -> RealtimeConnectRequest:
    store = _native_store(lesson_id, user_id)
    if store is None:
        return request
    archive = store.load_archive(user_id, lesson_id=lesson_id)
    package_id = archive.package.id
    session_id = request.client_session_id or new_id('realtime')
    with store._lock, ridoc_file_lock(store.package_path(user_id, package_id)):
        archive = store.load_archive(user_id, package_id=package_id)
        epoch = store.context_epoch(owner_user_id=user_id, package_id=package_id, lesson_id=lesson_id)
        identity = (user_id, store.deletion_scope_id(package_id), lesson_id, session_id)
        with store._connect() as connection:
            _ensure_session_table(connection)
            previous = connection.execute('SELECT epoch FROM ridoc_realtime_contexts WHERE owner_id=? AND scope_id=? AND lesson_id=? AND session_id=?', identity).fetchone()
            if previous is not None and previous['epoch'] != epoch:
                raise RidocV3ConflictError('Conversation history changed; reconnect with a new voice session')
            connection.execute('INSERT OR IGNORE INTO ridoc_realtime_contexts VALUES (?,?,?,?,?)', (*identity, epoch))
        conversation = authoritative_conversation(archive, lesson_id)
    return request.model_copy(update={'client_session_id': session_id, 'latest_assistant_message': conversation[-1].content if conversation else ''})

def assert_realtime_context(lesson_id: str, *, user_id: str, session_id: str | None, transaction_id: str='', input_event_id: str='') -> str | None:
    store = _native_store(lesson_id, user_id)
    if store is None:
        return None
    archive = store.load_archive(user_id, lesson_id=lesson_id)
    package_id = archive.package.id
    with store._lock, ridoc_file_lock(store.package_path(user_id, package_id)):
        archive = store.load_archive(user_id, package_id=package_id)
        epoch = store.context_epoch(owner_user_id=user_id, package_id=package_id, lesson_id=lesson_id)
        with store._connect() as connection:
            _ensure_session_table(connection)
            saved = connection.execute('SELECT epoch FROM ridoc_realtime_contexts WHERE owner_id=? AND scope_id=? AND lesson_id=? AND session_id=?', (user_id, store.deletion_scope_id(package_id), lesson_id, session_id or '')).fetchone()
        if saved is None or saved['epoch'] != epoch:
            raise RidocV3ConflictError('Conversation history changed; reconnect voice before continuing')
        store.assert_turn_not_deleted(owner_user_id=user_id, package_id=package_id, lesson_id=lesson_id, transaction_id=transaction_id, session_id=session_id or '', input_event_id=input_event_id)
        return archive.revision.value

@contextmanager
def realtime_completion_guard(lesson_id: str, *, user_id: str, session_id: str | None, transaction_id: str='', input_event_id: str='') -> Iterator[str | None]:
    """Keep the captured voice-session epoch valid through the final write.

    HEAD alone cannot detect document undo or reading-order changes. Recheck
    the session's persisted epoch while holding the same lock as those edits,
    then let the caller commit against this exact revision under that lock.
    """
    store = _native_store(lesson_id, user_id)
    if store is None:
        yield None
        return
    archive = store.load_archive(user_id, lesson_id=lesson_id)
    with store._lock, ridoc_file_lock(store.package_path(user_id, archive.package.id)):
        revision = assert_realtime_context(lesson_id, user_id=user_id, session_id=session_id, transaction_id=transaction_id, input_event_id=input_event_id)
        yield revision
