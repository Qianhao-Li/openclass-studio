from __future__ import annotations
from typing import Literal
from openclass_core.models import CommitRecord, Lesson, now_iso
from openclass_core.services.history import get_commit
ChatVisibility = Literal['hidden', 'visible']
_DOCUMENT_HISTORY_KINDS = {'manual_document_save', 'manual_document_edit', 'auto_document_save', 'board_document_generation', 'board_document_edit', 'import_docx'}
_RESTORE_HISTORY_KINDS = {'restore_snapshot'}
_NON_CHAT_NODE_KINDS = {'document', 'restore', 'merge', 'system'}

class ChatCommitVisibilityError(ValueError):
    """Raised when a commit cannot change chat visibility."""

def _metadata_text(metadata: dict[str, object], key: str) -> str:
    value = metadata.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else ''

def _metadata_kind(commit: CommitRecord) -> str:
    kind = commit.metadata.get('kind')
    return kind if isinstance(kind, str) else ''

def _node_kind(commit: CommitRecord) -> str:
    explicit = commit.metadata.get('history_node_kind')
    if isinstance(explicit, str) and explicit.strip():
        return explicit
    kind = _metadata_kind(commit)
    if kind == 'branch_merge':
        return 'merge'
    if kind in _RESTORE_HISTORY_KINDS:
        return 'restore'
    if kind == 'initial_document':
        return 'system'
    if commit.metadata.get('document_changed') is True or commit.operations or kind in _DOCUMENT_HISTORY_KINDS:
        return 'document'
    if _metadata_text(commit.metadata, 'user_message') or _metadata_text(commit.metadata, 'assistant_message'):
        return 'chat'
    return 'system'

def is_hideable_chat_commit(commit: CommitRecord) -> bool:
    if commit.operations:
        return False
    if commit.metadata.get('document_changed') is True:
        return False
    kind = _metadata_kind(commit)
    if kind in _DOCUMENT_HISTORY_KINDS or kind in _RESTORE_HISTORY_KINDS or kind == 'branch_merge':
        return False
    node_kind = _node_kind(commit)
    if node_kind in _NON_CHAT_NODE_KINDS:
        return False
    return node_kind == 'chat'

def chat_visibility_of(commit: CommitRecord) -> ChatVisibility:
    value = _metadata_text(commit.metadata, 'chat_visibility')
    return 'hidden' if value == 'hidden' else 'visible'

def set_chat_commit_visibility(lesson: Lesson, commit_id: str, visibility: ChatVisibility) -> CommitRecord:
    commit = get_commit(lesson, commit_id)
    if not is_hideable_chat_commit(commit):
        raise ChatCommitVisibilityError('Only chat-only commits without board changes can change chat visibility')
    current = chat_visibility_of(commit)
    if current == visibility:
        return commit
    timestamp = now_iso()
    commit.metadata['chat_visibility'] = visibility
    if visibility == 'hidden':
        commit.metadata['chat_hidden_at'] = timestamp
        commit.metadata.pop('chat_restored_at', None)
    else:
        commit.metadata['chat_restored_at'] = timestamp
    lesson.updated_at = timestamp
    return commit
