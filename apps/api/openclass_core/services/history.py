from __future__ import annotations
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from openclass_core.models import BoardDocument, BranchRef, CommitRecord, Lesson, LessonRuntimeSnapshot, PatchOperation, new_id, now_iso
_CHAT_HISTORY_KINDS = {'basic_chat', 'learning_requirement_refinement', 'board_task_requirement_refinement', 'chat_flow', 'board_section_teaching'}
_DOCUMENT_HISTORY_KINDS = {'manual_document_save', 'manual_document_edit', 'auto_document_save', 'board_document_generation', 'board_document_edit', 'import_docx'}
_RESTORE_HISTORY_KINDS = {'restore_snapshot'}
_SYSTEM_HISTORY_KINDS = {'initial_document'}
_HISTORY_NODE_KINDS = {'chat', 'document', 'restore', 'merge', 'system'}
_commit_metadata_context: ContextVar[dict[str, object] | None] = ContextVar('openclass_commit_metadata_context', default=None)

@contextmanager
def bind_commit_metadata(metadata: dict[str, object]) -> Iterator[None]:
    token = _commit_metadata_context.set(metadata or None)
    try:
        yield
    finally:
        _commit_metadata_context.reset(token)

def current_head_commit(lesson: Lesson) -> CommitRecord:
    branch = lesson.history_graph.branches[lesson.history_graph.current_branch]
    for commit in reversed(lesson.history_graph.commits):
        if commit.id == branch.head_commit_id:
            return commit
    return lesson.history_graph.commits[-1]

def get_commit(lesson: Lesson, commit_id: str) -> CommitRecord:
    for commit in lesson.history_graph.commits:
        if commit.id == commit_id:
            return commit
    raise ValueError(f'Unknown commit {commit_id}')

def _documents_equal(left: BoardDocument, right: BoardDocument) -> bool:
    return left.model_dump(mode='json') == right.model_dump(mode='json')

def is_document_version_commit(commit: CommitRecord) -> bool:
    metadata = commit.metadata if isinstance(commit.metadata, dict) else {}
    kind = str(metadata.get('kind') or '')
    explicit = str(metadata.get('history_node_kind') or '')
    return bool(explicit in {'document', 'restore', 'merge'} or metadata.get('document_changed') is True or commit.operations or (kind in _DOCUMENT_HISTORY_KINDS) or (kind in _RESTORE_HISTORY_KINDS) or (kind in {'branch_merge', 'board_document_mutation', 'repository_pull'}))

def document_version_at_or_before(lesson: Lesson, commit_id: str) -> CommitRecord:
    commits = {commit.id: commit for commit in lesson.history_graph.commits}
    cursor = commits.get(commit_id)
    seen: set[str] = set()
    fallback = cursor
    while cursor and cursor.id not in seen:
        seen.add(cursor.id)
        if is_document_version_commit(cursor) or not cursor.parent_ids:
            return cursor
        cursor = commits.get(cursor.parent_ids[0]) if cursor.parent_ids else None
    if fallback is not None:
        return fallback
    return lesson.history_graph.commits[0]

def normalize_document_history(lesson: Lesson) -> Lesson:
    commit_ids = {commit.id for commit in lesson.history_graph.commits}
    for branch in lesson.history_graph.branches.values():
        if branch.document_cursor_commit_id not in commit_ids:
            branch.document_cursor_commit_id = document_version_at_or_before(lesson, branch.head_commit_id).id
        branch.document_redo_commit_ids = [commit_id for commit_id in branch.document_redo_commit_ids if commit_id in commit_ids and commit_id != branch.document_cursor_commit_id]
    return lesson

def current_document_commit(lesson: Lesson, branch_name: str | None=None) -> CommitRecord:
    normalize_document_history(lesson)
    name = branch_name or lesson.history_graph.current_branch
    branch = lesson.history_graph.branches[name]
    return get_commit(lesson, branch.document_cursor_commit_id)

def document_parent_commit_id(lesson: Lesson, commit: CommitRecord) -> str | None:
    explicit = commit.metadata.get('document_parent_commit_id')
    if isinstance(explicit, str) and explicit:
        try:
            return get_commit(lesson, explicit).id
        except ValueError:
            pass
    commits = {item.id: item for item in lesson.history_graph.commits}
    cursor = commits.get(commit.parent_ids[0]) if commit.parent_ids else None
    seen: set[str] = set()
    while cursor and cursor.id not in seen:
        seen.add(cursor.id)
        if (is_document_version_commit(cursor) or not cursor.parent_ids) and (not _documents_equal(cursor.snapshot, commit.snapshot)):
            return cursor.id
        cursor = commits.get(cursor.parent_ids[0]) if cursor.parent_ids else None
    return None

def document_history_targets(lesson: Lesson) -> tuple[str | None, str | None]:
    branch = lesson.history_graph.branches[lesson.history_graph.current_branch]
    cursor = current_document_commit(lesson)
    undo_id = document_parent_commit_id(lesson, cursor)
    redo_id = branch.document_redo_commit_ids[0] if branch.document_redo_commit_ids else None
    return (undo_id, redo_id)

def step_document_history(lesson: Lesson, *, direction: str, target_commit_id: str, expected_branch_name: str, expected_head_commit_id: str, expected_document_cursor_commit_id: str) -> Lesson:
    normalize_document_history(lesson)
    if lesson.history_graph.current_branch != expected_branch_name:
        raise ValueError('The active branch changed')
    branch = lesson.history_graph.branches[expected_branch_name]
    if branch.head_commit_id != expected_head_commit_id:
        raise ValueError('The lesson history changed')
    if branch.document_cursor_commit_id != expected_document_cursor_commit_id:
        raise ValueError('The document history cursor changed')
    undo_id, redo_id = document_history_targets(lesson)
    expected_target = undo_id if direction == 'undo' else redo_id
    if direction not in {'undo', 'redo'} or not expected_target or expected_target != target_commit_id:
        raise ValueError('The requested document history step is no longer available')
    previous_cursor_id = branch.document_cursor_commit_id
    if direction == 'undo':
        branch.document_redo_commit_ids.insert(0, previous_cursor_id)
    else:
        branch.document_redo_commit_ids.pop(0)
    branch.document_cursor_commit_id = target_commit_id
    lesson.board_document = BoardDocument.model_validate(get_commit(lesson, target_commit_id).snapshot.model_dump(mode='json'))
    lesson.updated_at = now_iso()
    return lesson

def ensure_document_divergence_branch(lesson: Lesson, *, expected_branch_name: str, expected_head_commit_id: str, expected_document_cursor_commit_id: str) -> str | None:
    normalize_document_history(lesson)
    if lesson.history_graph.current_branch != expected_branch_name:
        raise ValueError('The active branch changed')
    source = lesson.history_graph.branches[expected_branch_name]
    if source.head_commit_id != expected_head_commit_id:
        raise ValueError('The lesson history changed')
    if source.document_cursor_commit_id != expected_document_cursor_commit_id:
        raise ValueError('The document history cursor changed')
    if not source.document_redo_commit_ids:
        return None
    withdrawn_cursor_id = source.document_cursor_commit_id
    original_document_tip_id = source.document_redo_commit_ids[-1]
    timestamp = datetime.now().astimezone().strftime('%m%d-%H%M')
    base_name = f'{source.name}-文档分支-{timestamp}'
    branch_name = base_name
    suffix = 2
    while branch_name in lesson.history_graph.branches:
        branch_name = f'{base_name}-{suffix}'
        suffix += 1
    source.document_cursor_commit_id = original_document_tip_id
    source.document_redo_commit_ids = []
    lesson.history_graph.branches[branch_name] = BranchRef(name=branch_name, head_commit_id=source.head_commit_id, base_commit_id=source.head_commit_id, document_cursor_commit_id=withdrawn_cursor_id)
    lesson.history_graph.current_branch = branch_name
    lesson.board_document = get_commit(lesson, withdrawn_cursor_id).snapshot.model_copy(deep=True)
    lesson.updated_at = now_iso()
    return branch_name

def commit_operations(lesson: Lesson, operations: list[PatchOperation], label: str, message: str, new_document: BoardDocument | None=None, metadata: dict[str, object] | None=None) -> Lesson:
    head = current_head_commit(lesson)
    branch_name = lesson.history_graph.current_branch
    if new_document is None:
        new_document = lesson.board_document
    from openclass_core.services.rich_document import document_changed
    if document_changed(head.snapshot, new_document):
        from openclass_core.services.latex_validation import DocumentMathError, validate_document_math
        try:
            validate_document_math(new_document)
        except DocumentMathError as exc:
            from fastapi import HTTPException
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    new_document = ensure_document_block_ids(new_document)
    snapshot_document = BoardDocument.model_validate(new_document.model_dump(mode='json'))
    context_metadata = _commit_metadata_context.get() or {}
    commit_metadata = {**context_metadata, **(metadata or {})}
    commit_metadata = _with_history_node_metadata(metadata=commit_metadata, operations=operations, label=label, message=message)
    node_kind = str(commit_metadata.get('history_node_kind') or '')
    changes_document = node_kind in {'document', 'restore', 'merge'}
    if changes_document:
        normalize_document_history(lesson)
        commit_metadata.setdefault('document_parent_commit_id', lesson.history_graph.branches[branch_name].document_cursor_commit_id)
    commit = CommitRecord(label=label, message=message, branch_name=branch_name, parent_ids=[head.id], operations=operations, snapshot=snapshot_document, runtime_snapshot=snapshot_lesson_runtime(lesson), metadata=commit_metadata)
    lesson.board_document = new_document
    lesson.history_graph.commits.append(commit)
    lesson.history_graph.branches[branch_name].head_commit_id = commit.id
    if changes_document:
        lesson.history_graph.branches[branch_name].document_cursor_commit_id = commit.id
        lesson.history_graph.branches[branch_name].document_redo_commit_ids = []
    lesson.updated_at = now_iso()
    return lesson

def snapshot_lesson_runtime(lesson: Lesson) -> LessonRuntimeSnapshot:
    snapshot = LessonRuntimeSnapshot(learning_requirements=lesson.learning_requirements, board_task_requirements=lesson.board_task_requirements, board_teaching_guide=lesson.board_teaching_guide, board_teaching_progress=lesson.board_teaching_progress)
    return LessonRuntimeSnapshot.model_validate(snapshot.model_dump(mode='json'))

def ensure_document_block_ids(document: BoardDocument) -> BoardDocument:
    content_json = BoardDocument.model_validate(document.model_dump(mode='json')).content_json
    raw_content = content_json.get('content') if isinstance(content_json, dict) else None
    if not isinstance(raw_content, list):
        return document
    changed = False
    for node in raw_content:
        if not isinstance(node, dict):
            continue
        attrs = node.get('attrs')
        attrs = dict(attrs) if isinstance(attrs, dict) else {}
        if not isinstance(attrs.get('blockId'), str) or not attrs['blockId'].strip():
            attrs['blockId'] = new_id('block')
            node['attrs'] = attrs
            changed = True
    if not changed:
        return document
    return document.model_copy(update={'content_json': content_json})

def _with_history_node_metadata(*, metadata: dict[str, object], operations: list[PatchOperation], label: str, message: str) -> dict[str, object]:
    node_kind = _history_node_kind(metadata=metadata, operations=operations)
    inferred = {'history_node_kind': node_kind, 'history_node_title': _history_node_title(label=label, metadata=metadata, node_kind=node_kind), 'history_node_summary': _history_node_summary(message=message, metadata=metadata, node_kind=node_kind)}
    return {**inferred, **metadata}

def _history_node_kind(*, metadata: dict[str, object], operations: list[PatchOperation]) -> str:
    explicit = metadata.get('history_node_kind')
    if isinstance(explicit, str) and explicit in _HISTORY_NODE_KINDS:
        return explicit
    metadata_kind = metadata.get('kind')
    kind = metadata_kind if isinstance(metadata_kind, str) else ''
    if kind == 'branch_merge':
        return 'merge'
    if kind in _RESTORE_HISTORY_KINDS:
        return 'restore'
    if kind in _SYSTEM_HISTORY_KINDS:
        return 'system'
    if metadata.get('document_changed') is True or operations or kind in _DOCUMENT_HISTORY_KINDS:
        return 'document'
    if kind in _CHAT_HISTORY_KINDS or _metadata_text(metadata, 'user_message') or _metadata_text(metadata, 'assistant_message'):
        return 'chat'
    return 'system'

def _history_node_title(*, label: str, metadata: dict[str, object], node_kind: str) -> str:
    explicit = _metadata_text(metadata, 'history_node_title')
    if explicit:
        return explicit
    if node_kind == 'chat':
        user_message = _metadata_text(metadata, 'user_message')
        return _compact_history_text(user_message, 64) if user_message else label
    return label

def _history_node_summary(*, message: str, metadata: dict[str, object], node_kind: str) -> str:
    explicit = _metadata_text(metadata, 'history_node_summary')
    if explicit:
        return explicit
    if node_kind == 'chat':
        assistant_message = _metadata_text(metadata, 'assistant_message')
        return _compact_history_text(assistant_message, 160) if assistant_message else message
    if node_kind == 'restore':
        restored_label = _metadata_text(metadata, 'restored_commit_label')
        return f'Restored snapshot from {restored_label}' if restored_label else message
    if node_kind == 'merge':
        return message
    return message

def _metadata_text(metadata: dict[str, object], key: str) -> str:
    value = metadata.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else ''

def _compact_history_text(value: str, limit: int) -> str:
    compact = ' '.join(value.split())
    if len(compact) <= limit:
        return compact
    return f'{compact[:limit - 1]}…'

def _restore_document_from_commit(lesson: Lesson, commit: CommitRecord) -> Lesson:
    lesson.board_document = commit.snapshot
    runtime = commit.runtime_snapshot
    lesson.board_teaching_guide = runtime.board_teaching_guide if runtime else None
    lesson.board_teaching_progress = runtime.board_teaching_progress if runtime else None
    lesson.learning_requirements = runtime.learning_requirements if runtime else None
    lesson.board_task_requirements = runtime.board_task_requirements if runtime else None
    lesson.updated_at = now_iso()
    return lesson

def commit_merge(lesson: Lesson, *, source_head_commit_id: str, new_document: BoardDocument, runtime_snapshot: LessonRuntimeSnapshot, label: str, message: str, metadata: dict[str, object]) -> CommitRecord:
    target_head = current_head_commit(lesson)
    branch_name = lesson.history_graph.current_branch
    normalize_document_history(lesson)
    document = ensure_document_block_ids(new_document)
    commit_metadata = _with_history_node_metadata(metadata={'kind': 'branch_merge', 'document_parent_commit_id': lesson.history_graph.branches[branch_name].document_cursor_commit_id, **metadata}, operations=[], label=label, message=message)
    commit = CommitRecord(label=label, message=message, branch_name=branch_name, parent_ids=[target_head.id, source_head_commit_id], operations=[], snapshot=BoardDocument.model_validate(document.model_dump(mode='json')), runtime_snapshot=runtime_snapshot, metadata=commit_metadata)
    lesson.board_document = document
    lesson.learning_requirements = runtime_snapshot.learning_requirements
    lesson.board_task_requirements = runtime_snapshot.board_task_requirements
    lesson.board_teaching_guide = runtime_snapshot.board_teaching_guide
    lesson.board_teaching_progress = runtime_snapshot.board_teaching_progress
    lesson.history_graph.commits.append(commit)
    lesson.history_graph.branches[branch_name].head_commit_id = commit.id
    lesson.history_graph.branches[branch_name].document_cursor_commit_id = commit.id
    lesson.history_graph.branches[branch_name].document_redo_commit_ids = []
    lesson.updated_at = now_iso()
    return commit

def create_branch(lesson: Lesson, branch_name: str, from_commit_id: str | None=None) -> Lesson:
    source_commit = get_commit(lesson, from_commit_id) if from_commit_id else current_head_commit(lesson)
    source_document = document_version_at_or_before(lesson, source_commit.id)
    lesson.history_graph.branches[branch_name] = BranchRef(name=branch_name, head_commit_id=source_commit.id, base_commit_id=source_commit.id, document_cursor_commit_id=source_document.id)
    lesson.history_graph.current_branch = branch_name
    _restore_document_from_commit(lesson, source_commit)
    return lesson

def switch_branch(lesson: Lesson, branch_name: str) -> Lesson:
    branch = lesson.history_graph.branches[branch_name]
    source_commit = get_commit(lesson, branch.head_commit_id)
    lesson.history_graph.current_branch = branch_name
    _restore_document_from_commit(lesson, source_commit)
    lesson.board_document = current_document_commit(lesson, branch_name).snapshot.model_copy(deep=True)
    return lesson

def restore_commit(lesson: Lesson, commit_id: str, label: str) -> Lesson:
    commit = get_commit(lesson, commit_id)
    _restore_document_from_commit(lesson, commit)
    return commit_operations(lesson, operations=[], label=label, message=f'Restored snapshot from {commit.label}', new_document=commit.snapshot, metadata={'kind': 'restore_snapshot', 'restored_commit_id': commit.id, 'restored_commit_label': commit.label})
