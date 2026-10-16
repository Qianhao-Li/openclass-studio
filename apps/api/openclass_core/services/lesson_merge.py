from __future__ import annotations
import copy
import json
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable
from openclass_core.models import AIModelSelection, BoardDocument, BoardTaskRequirementSheet, CommitRecord, LearningRequirementSheet, Lesson, LessonMergeConflict, LessonMergeConflictResolution, LessonMergeMode, LessonMergeSession, LessonMergeSessionView, LessonRuntimeSnapshot, MergeRuntimeDraft, now_iso
from openclass_core.services.history import commit_merge, current_document_commit, ensure_document_block_ids, get_commit
from openclass_core.services.rich_document import rebuild_document_from_content_json

class LessonMergeError(ValueError):
    pass

class LessonMergeConflictError(LessonMergeError):
    pass

class LessonMergeStaleError(LessonMergeError):
    pass

@dataclass(frozen=True)
class _BlockHunk:
    side: str
    start: int
    end: int
    replacements: tuple[dict[str, Any], ...]

def create_merge_session(lesson: Lesson, *, owner_user_id: str, source_branch_name: str, mode: LessonMergeMode='manual', ai_model: AIModelSelection | None=None, supersedes_session_id: str | None=None) -> LessonMergeSession:
    target_branch_name = lesson.history_graph.current_branch
    if source_branch_name == target_branch_name:
        raise LessonMergeError('A branch cannot be merged into itself.')
    source_branch = lesson.history_graph.branches.get(source_branch_name)
    target_branch = lesson.history_graph.branches.get(target_branch_name)
    if source_branch is None:
        raise LessonMergeError(f'Source branch {source_branch_name} was not found.')
    if target_branch is None:
        raise LessonMergeError(f'Current branch {target_branch_name} was not found.')
    commits_by_id = {commit.id: commit for commit in lesson.history_graph.commits}
    target_head = get_commit(lesson, target_branch.head_commit_id)
    source_head = get_commit(lesson, source_branch.head_commit_id)
    target_document_commit = current_document_commit(lesson, target_branch_name)
    source_document_commit = current_document_commit(lesson, source_branch_name)
    if _is_ancestor(commits_by_id, source_head.id, target_head.id):
        raise LessonMergeError("The current branch already contains the source branch's content.")
    base_commit = _find_merge_base(lesson, target_head.id, source_head.id)
    board_conflicts, blueprint, draft_document = _merge_documents(base_commit.snapshot, target_document_commit.snapshot, source_document_commit.snapshot)
    runtime_conflicts, draft_runtime, runtime_audit = _merge_runtime(_runtime_for_commit(base_commit), _runtime_for_commit(target_head), _runtime_for_commit(source_head), board_changed=_document_signature(draft_document) != _document_signature(target_document_commit.snapshot))
    conflicts = [*board_conflicts, *runtime_conflicts]
    status = 'ready' if not conflicts else 'draft'
    return LessonMergeSession(owner_user_id=owner_user_id, lesson_id=lesson.id, target_branch_name=target_branch_name, source_branch_name=source_branch_name, base_commit_id=base_commit.id, target_head_commit_id=target_head.id, source_head_commit_id=source_head.id, mode=mode, status=status, conflicts=conflicts, merge_blueprint=blueprint, draft_document=draft_document, draft_runtime=draft_runtime, ai_model=ai_model, supersedes_session_id=supersedes_session_id, audit={'target_branch_name': target_branch_name, 'source_branch_name': source_branch_name, 'base_commit_id': base_commit.id, 'target_head_commit_id': target_head.id, 'source_head_commit_id': source_head.id, 'runtime': runtime_audit, 'board_conflict_count': len(board_conflicts), 'runtime_conflict_count': len(runtime_conflicts)})

def update_merge_session(session: LessonMergeSession, *, expected_version: int, resolutions: Iterable[LessonMergeConflictResolution]=(), draft_document: BoardDocument | None=None, draft_runtime: MergeRuntimeDraft | None=None) -> LessonMergeSession:
    if session.version != expected_version:
        raise LessonMergeStaleError('The merge draft changed during another operation. Refresh to continue.')
    if session.status not in {'draft', 'ready', 'failed'}:
        raise LessonMergeError('This merge session cannot be edited in its current state.')
    by_id = {conflict.id: conflict for conflict in session.conflicts}
    board_resolution_changed = False
    for incoming in resolutions:
        conflict = by_id.get(incoming.conflict_id)
        if conflict is None:
            raise LessonMergeConflictError(f'Conflict {incoming.conflict_id} was not found.')
        if incoming.resolution == 'unresolved':
            conflict.resolution = 'unresolved'
            conflict.custom_value = None
            conflict.resolved = False
        else:
            conflict.resolution = incoming.resolution
            conflict.custom_value = incoming.custom_value
            conflict.resolved = True
        if conflict.kind == 'board':
            board_resolution_changed = True
        else:
            _apply_runtime_conflict(session.draft_runtime, conflict)
    if board_resolution_changed:
        session.draft_document = _render_blueprint(session)
    if draft_document is not None:
        session.draft_document = ensure_document_block_ids(draft_document)
    if draft_runtime is not None:
        session.draft_runtime = draft_runtime
    session.version += 1
    session.updated_at = now_iso()
    session.status = 'ready' if all((conflict.resolved for conflict in session.conflicts)) else 'draft'
    return session

def abandon_merge_session(session: LessonMergeSession) -> LessonMergeSession:
    if session.status == 'committed':
        raise LessonMergeError('A committed merge session cannot be discarded.')
    session.status = 'abandoned'
    session.version += 1
    session.updated_at = now_iso()
    return session

def submit_merge_session(lesson: Lesson, session: LessonMergeSession, *, expected_version: int) -> CommitRecord:
    if session.version != expected_version:
        raise LessonMergeStaleError('The merge draft version changed. Refresh and try again.')
    if session.status not in {'ready', 'draft'}:
        raise LessonMergeError('This merge session cannot be committed in its current state.')
    unresolved = [conflict for conflict in session.conflicts if not conflict.resolved]
    if unresolved:
        raise LessonMergeConflictError('Some merge conflicts are still unresolved.')
    if lesson.history_graph.current_branch != session.target_branch_name:
        session.status = 'stale'
        session.version += 1
        session.updated_at = now_iso()
        raise LessonMergeStaleError('The current branch changed. Switch back to the target branch and try again.')
    target_branch = lesson.history_graph.branches.get(session.target_branch_name)
    source_branch = lesson.history_graph.branches.get(session.source_branch_name)
    if target_branch is None or source_branch is None or target_branch.head_commit_id != session.target_head_commit_id or (source_branch.head_commit_id != session.source_head_commit_id):
        session.status = 'stale'
        session.version += 1
        session.updated_at = now_iso()
        raise LessonMergeStaleError('The branch changed during the merge. Recalculate from the latest nodes.')
    runtime = LessonRuntimeSnapshot(learning_requirements=session.draft_runtime.learning_requirements, board_task_requirements=session.draft_runtime.board_task_requirements, board_teaching_guide=None, board_teaching_progress=None)
    commit = commit_merge(lesson, source_head_commit_id=session.source_head_commit_id, new_document=session.draft_document, runtime_snapshot=runtime, label=f'Merge {session.source_branch_name} into {session.target_branch_name}', message=f'Merge branch {session.source_branch_name} into {session.target_branch_name}, preserving both histories.', metadata={'merge_session_id': session.id, 'merge_mode': session.mode, 'merge_base_commit_id': session.base_commit_id, 'merge_target_head_commit_id': session.target_head_commit_id, 'merge_source_head_commit_id': session.source_head_commit_id, 'merge_target_branch': session.target_branch_name, 'merge_source_branch': session.source_branch_name, 'merge_conflict_count': len(session.conflicts), 'merge_resolutions': [{'conflict_id': conflict.id, 'kind': conflict.kind, 'path': conflict.path, 'resolution': conflict.resolution} for conflict in session.conflicts], 'reset_codex_thread': True, 'document_changed': True, 'teaching_state_invalidated': session.draft_runtime.invalidated_teaching_state, 'ai_model': session.ai_model.model_dump(mode='json') if session.ai_model else None, 'lesson_contribution_id': session.audit.get('lesson_contribution_id'), 'lesson_contribution_revision': session.audit.get('lesson_contribution_revision'), 'contributor_user_id': session.audit.get('contributor_user_id')})
    session.status = 'committed'
    session.committed_commit_id = commit.id
    session.version += 1
    session.updated_at = now_iso()
    return commit

def merge_session_view(session: LessonMergeSession) -> LessonMergeSessionView:
    payload = session.model_dump(mode='json')
    payload['conflicts'] = [{**conflict.model_dump(mode='json'), 'base_value': _sanitize_public_value(conflict.base_value), 'target_value': _sanitize_public_value(conflict.target_value), 'source_value': _sanitize_public_value(conflict.source_value), 'custom_value': _sanitize_public_value(conflict.custom_value)} for conflict in session.conflicts]
    payload['audit'] = _sanitize_public_value(session.audit)
    return LessonMergeSessionView.model_validate(payload)

def branch_history_summary(lesson: Lesson, head_commit_id: str, *, limit: int=12) -> list[dict[str, str]]:
    commits = _first_parent_lineage(lesson, head_commit_id)
    result: list[dict[str, str]] = []
    for commit in commits[-limit:]:
        metadata = commit.metadata if isinstance(commit.metadata, dict) else {}
        user_message = metadata.get('user_message')
        assistant_message = metadata.get('assistant_message')
        if isinstance(user_message, str) and user_message.strip():
            result.append({'role': 'user', 'content': user_message.strip()})
        if isinstance(assistant_message, str) and assistant_message.strip():
            result.append({'role': 'assistant', 'content': assistant_message.strip()})
    return result[-limit * 2:]

def _find_merge_base(lesson: Lesson, target_head_id: str, source_head_id: str) -> CommitRecord:
    commits_by_id = {commit.id: commit for commit in lesson.history_graph.commits}
    target_distances = _ancestor_distances(commits_by_id, target_head_id)
    source_distances = _ancestor_distances(commits_by_id, source_head_id)
    common = set(target_distances) & set(source_distances)
    if not common:
        raise LessonMergeError('The branches have no usable common ancestor.')
    commit_order = {commit.id: index for index, commit in enumerate(lesson.history_graph.commits)}
    best_id = min(common, key=lambda commit_id: (max(target_distances[commit_id], source_distances[commit_id]), target_distances[commit_id] + source_distances[commit_id], -commit_order.get(commit_id, -1)))
    return commits_by_id[best_id]

def _ancestor_distances(commits: dict[str, CommitRecord], head_id: str) -> dict[str, int]:
    distances: dict[str, int] = {}
    pending: list[tuple[str, int]] = [(head_id, 0)]
    while pending:
        commit_id, distance = pending.pop(0)
        if commit_id in distances and distances[commit_id] <= distance:
            continue
        distances[commit_id] = distance
        commit = commits.get(commit_id)
        if commit is not None:
            pending.extend(((parent_id, distance + 1) for parent_id in commit.parent_ids))
    return distances

def _is_ancestor(commits: dict[str, CommitRecord], ancestor_id: str, descendant_id: str) -> bool:
    return ancestor_id in _ancestor_distances(commits, descendant_id)

def _first_parent_lineage(lesson: Lesson, head_id: str) -> list[CommitRecord]:
    commits_by_id = {commit.id: commit for commit in lesson.history_graph.commits}
    result: list[CommitRecord] = []
    cursor = commits_by_id.get(head_id)
    seen: set[str] = set()
    while cursor is not None and cursor.id not in seen:
        seen.add(cursor.id)
        result.append(cursor)
        cursor = commits_by_id.get(cursor.parent_ids[0]) if cursor.parent_ids else None
    return list(reversed(result))

def _runtime_for_commit(commit: CommitRecord) -> LessonRuntimeSnapshot:
    if commit.runtime_snapshot is not None:
        return LessonRuntimeSnapshot.model_validate(commit.runtime_snapshot.model_dump(mode='json'))
    metadata = commit.metadata if isinstance(commit.metadata, dict) else {}
    return LessonRuntimeSnapshot(learning_requirements=metadata.get('active_requirement_sheet_after') if isinstance(metadata.get('active_requirement_sheet_after'), dict) else None, board_task_requirements=metadata.get('active_board_task_sheet_after') if isinstance(metadata.get('active_board_task_sheet_after'), dict) else None)

def _merge_documents(base: BoardDocument, target: BoardDocument, source: BoardDocument) -> tuple[list[LessonMergeConflict], list[dict[str, Any]], BoardDocument]:
    base_nodes = _top_level_nodes(base)
    target_nodes = _top_level_nodes(target)
    source_nodes = _top_level_nodes(source)
    target_hunks = _build_hunks('target', base_nodes, target_nodes)
    source_hunks = _build_hunks('source', base_nodes, source_nodes)
    clusters = _change_clusters([*target_hunks, *source_hunks])
    conflicts: list[LessonMergeConflict] = []
    blueprint: list[dict[str, Any]] = []
    cursor = 0
    for cluster in clusters:
        start = min((hunk.start for hunk in cluster))
        end = max((hunk.end for hunk in cluster))
        if cursor < start:
            blueprint.append({'kind': 'nodes', 'nodes': copy.deepcopy(base_nodes[cursor:start])})
        target_changes = [hunk for hunk in cluster if hunk.side == 'target']
        source_changes = [hunk for hunk in cluster if hunk.side == 'source']
        if target_changes and (not source_changes):
            nodes = _apply_hunks_to_range(base_nodes, start, end, target_changes)
            blueprint.append({'kind': 'nodes', 'nodes': nodes})
        elif source_changes and (not target_changes):
            nodes = _apply_hunks_to_range(base_nodes, start, end, source_changes)
            blueprint.append({'kind': 'nodes', 'nodes': nodes})
        else:
            target_result = _apply_hunks_to_range(base_nodes, start, end, target_changes)
            source_result = _apply_hunks_to_range(base_nodes, start, end, source_changes)
            if _nodes_equal(target_result, source_result):
                blueprint.append({'kind': 'nodes', 'nodes': target_result})
            else:
                conflict = LessonMergeConflict(kind='board', path=f'content[{start}:{end}]', title=_conflict_title(base_nodes[start:end], target_result, source_result), base_value=copy.deepcopy(base_nodes[start:end]), target_value=target_result, source_value=source_result)
                conflicts.append(conflict)
                blueprint.append({'kind': 'conflict', 'conflict_id': conflict.id})
        cursor = max(cursor, end)
    if cursor < len(base_nodes):
        blueprint.append({'kind': 'nodes', 'nodes': copy.deepcopy(base_nodes[cursor:])})
    shell = target.model_copy(update={'content_json': {'type': 'doc', 'content': []}})
    temporary = LessonMergeSession.model_construct(conflicts=conflicts, merge_blueprint=blueprint, draft_document=shell)
    draft = _render_blueprint(temporary)
    return (conflicts, blueprint, draft)

def _top_level_nodes(document: BoardDocument) -> list[dict[str, Any]]:
    content = document.content_json.get('content') if isinstance(document.content_json, dict) else None
    return [copy.deepcopy(node) for node in content or [] if isinstance(node, dict)]

def _canonical_node(node: dict[str, Any]) -> str:
    value = copy.deepcopy(node)
    attrs = value.get('attrs')
    if isinstance(attrs, dict):
        attrs.pop('blockId', None)
        attrs.pop('id', None)
        if not attrs:
            value.pop('attrs', None)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def _build_hunks(side: str, base: list[dict[str, Any]], other: list[dict[str, Any]]) -> list[_BlockHunk]:
    matcher = SequenceMatcher(a=[_canonical_node(node) for node in base], b=[_canonical_node(node) for node in other], autojunk=False)
    return [_BlockHunk(side=side, start=i1, end=i2, replacements=tuple(copy.deepcopy(other[j1:j2]))) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != 'equal']

def _change_clusters(hunks: list[_BlockHunk]) -> list[list[_BlockHunk]]:
    ordered = sorted(hunks, key=lambda item: (item.start, item.end, item.side))
    clusters: list[list[_BlockHunk]] = []
    for hunk in ordered:
        if clusters and any((_hunks_overlap(existing, hunk) for existing in clusters[-1])):
            clusters[-1].append(hunk)
        else:
            clusters.append([hunk])
    return clusters

def _hunks_overlap(left: _BlockHunk, right: _BlockHunk) -> bool:
    left_insert = left.start == left.end
    right_insert = right.start == right.end
    if left_insert and right_insert:
        return left.start == right.start
    if left_insert:
        return right.start <= left.start <= right.end
    if right_insert:
        return left.start <= right.start <= left.end
    return max(left.start, right.start) < min(left.end, right.end)

def _apply_hunks_to_range(base: list[dict[str, Any]], start: int, end: int, hunks: list[_BlockHunk]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    cursor = start
    for hunk in sorted(hunks, key=lambda item: (item.start, item.end)):
        if cursor < hunk.start:
            result.extend(copy.deepcopy(base[cursor:hunk.start]))
        result.extend(copy.deepcopy(list(hunk.replacements)))
        cursor = max(cursor, hunk.end)
    if cursor < end:
        result.extend(copy.deepcopy(base[cursor:end]))
    return result

def _nodes_equal(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
    return [_canonical_node(node) for node in left] == [_canonical_node(node) for node in right]

def _render_blueprint(session: LessonMergeSession) -> BoardDocument:
    conflicts = {conflict.id: conflict for conflict in session.conflicts}
    nodes: list[dict[str, Any]] = []
    for item in session.merge_blueprint:
        if item.get('kind') == 'nodes':
            nodes.extend(copy.deepcopy(item.get('nodes') or []))
            continue
        conflict = conflicts.get(str(item.get('conflict_id') or ''))
        if conflict is None:
            continue
        nodes.extend(_nodes_for_resolution(conflict))
    rebuilt = rebuild_document_from_content_json(session.draft_document, {'type': 'doc', 'content': nodes or [{'type': 'paragraph'}]})
    return ensure_document_block_ids(rebuilt)

def _nodes_for_resolution(conflict: LessonMergeConflict) -> list[dict[str, Any]]:
    resolution = conflict.resolution
    if resolution in {'unresolved', 'target'}:
        value = conflict.target_value
    elif resolution == 'source':
        value = conflict.source_value
    elif resolution == 'both':
        value = [*(conflict.target_value or []), *(conflict.source_value or [])]
    elif resolution == 'clear':
        value = []
    else:
        value = conflict.custom_value
    if isinstance(value, list):
        return [copy.deepcopy(node) for node in value if isinstance(node, dict)]
    if isinstance(value, dict):
        return [copy.deepcopy(value)]
    if isinstance(value, str) and value.strip():
        return [{'type': 'paragraph', 'content': [{'type': 'text', 'text': value.strip()}]}]
    return []

def _conflict_title(base: list[dict[str, Any]], target: list[dict[str, Any]], source: list[dict[str, Any]]) -> str:
    kinds = [str(node.get('type') or 'content') for node in target or source or base]
    label = ' / '.join(dict.fromkeys(kinds[:3])) or 'content'
    return f'Both branches changed {label}'

def _merge_runtime(base: LessonRuntimeSnapshot, target: LessonRuntimeSnapshot, source: LessonRuntimeSnapshot, *, board_changed: bool) -> tuple[list[LessonMergeConflict], MergeRuntimeDraft, dict[str, Any]]:
    conflicts: list[LessonMergeConflict] = []
    learning, learning_conflicts = _merge_model_fields('learning_requirements', 'learning_requirement', LearningRequirementSheet, base.learning_requirements, target.learning_requirements, source.learning_requirements)
    board_task, task_conflicts = _merge_model_fields('board_task_requirements', 'board_task', BoardTaskRequirementSheet, base.board_task_requirements, target.board_task_requirements, source.board_task_requirements)
    conflicts.extend(learning_conflicts)
    conflicts.extend(task_conflicts)
    invalidated = board_changed and any((value is not None for value in (target.board_teaching_guide, target.board_teaching_progress, source.board_teaching_guide, source.board_teaching_progress)))
    return (conflicts, MergeRuntimeDraft(learning_requirements=learning, board_task_requirements=board_task, invalidated_teaching_state=invalidated), {'teaching_state_invalidated': invalidated, 'target_had_teaching_state': bool(target.board_teaching_guide or target.board_teaching_progress), 'source_had_teaching_state': bool(source.board_teaching_guide or source.board_teaching_progress)})

def _merge_model_fields(path_prefix: str, default_kind: str, model_type: type[LearningRequirementSheet] | type[BoardTaskRequirementSheet], base_model: Any, target_model: Any, source_model: Any) -> tuple[Any, list[LessonMergeConflict]]:
    base = base_model.model_dump(mode='json') if base_model is not None else {}
    target = target_model.model_dump(mode='json') if target_model is not None else {}
    source = source_model.model_dump(mode='json') if source_model is not None else {}
    if not target and (not source):
        return (None, [])
    result = copy.deepcopy(target)
    conflicts: list[LessonMergeConflict] = []
    for key in sorted(set(base) | set(target) | set(source)):
        base_value = base.get(key)
        target_value = target.get(key)
        source_value = source.get(key)
        if key == 'source_grounding':
            merged_value, source_conflict = _merge_source_grounding(base_value or {}, target_value or {}, source_value or {}, path=f'{path_prefix}.{key}')
            result[key] = merged_value
            if source_conflict is not None:
                conflicts.append(source_conflict)
            continue
        if target_value == source_value:
            result[key] = copy.deepcopy(target_value)
        elif target_value == base_value:
            result[key] = copy.deepcopy(source_value)
        elif source_value == base_value:
            result[key] = copy.deepcopy(target_value)
        else:
            kind = 'source_reference' if key == 'source_grounding' else default_kind
            conflicts.append(LessonMergeConflict(kind=kind, path=f'{path_prefix}.{key}', title=f'Runtime state {key} differs between the branches', base_value=copy.deepcopy(base_value), target_value=copy.deepcopy(target_value), source_value=copy.deepcopy(source_value)))
            result[key] = copy.deepcopy(target_value)
    try:
        return (model_type.model_validate(result), conflicts)
    except Exception:
        return (target_model or source_model, conflicts)

def _merge_source_grounding(base: dict[str, Any], target: dict[str, Any], source: dict[str, Any], *, path: str) -> tuple[dict[str, Any], LessonMergeConflict | None]:
    """Merge frozen source identities without discarding independent references."""
    list_identities = {'confirmed_references': _confirmed_reference_identity, 'frozen_evidence': _retrieval_evidence_identity, 'frozen_visual_evidence': _visual_evidence_identity}
    target_candidate: dict[str, Any] = {}
    source_candidate: dict[str, Any] = {}
    has_conflict = False
    for key in sorted(set(base) | set(target) | set(source)):
        base_value = base.get(key)
        target_value = target.get(key)
        source_value = source.get(key)
        identity = list_identities.get(key)
        if identity is not None:
            merged_target, merged_source, list_conflict = _merge_identity_list(base_value or [], target_value or [], source_value or [], identity=identity)
            target_candidate[key] = merged_target
            source_candidate[key] = merged_source
            has_conflict = has_conflict or list_conflict
            continue
        if target_value == source_value:
            target_candidate[key] = copy.deepcopy(target_value)
            source_candidate[key] = copy.deepcopy(target_value)
        elif target_value == base_value:
            target_candidate[key] = copy.deepcopy(source_value)
            source_candidate[key] = copy.deepcopy(source_value)
        elif source_value == base_value:
            target_candidate[key] = copy.deepcopy(target_value)
            source_candidate[key] = copy.deepcopy(target_value)
        else:
            has_conflict = True
            target_candidate[key] = copy.deepcopy(target_value)
            source_candidate[key] = copy.deepcopy(source_value)
    if not has_conflict:
        return (target_candidate, None)
    return (target_candidate, LessonMergeConflict(kind='source_reference', path=path, title='The branches contain different versions of the saved source evidence', base_value=copy.deepcopy(base), target_value=target_candidate, source_value=source_candidate))

def _merge_identity_list(base: list[dict[str, Any]], target: list[dict[str, Any]], source: list[dict[str, Any]], *, identity) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    base_map = {identity(item): item for item in base}
    target_map = {identity(item): item for item in target}
    source_map = {identity(item): item for item in source}
    ordered_ids = list(dict.fromkeys([identity(item) for item in target] + [identity(item) for item in source] + [identity(item) for item in base]))
    target_result: list[dict[str, Any]] = []
    source_result: list[dict[str, Any]] = []
    has_conflict = False
    for item_id in ordered_ids:
        base_item = base_map.get(item_id)
        target_item = target_map.get(item_id)
        source_item = source_map.get(item_id)
        if target_item == source_item:
            target_choice = source_choice = target_item
        elif target_item == base_item:
            target_choice = source_choice = source_item
        elif source_item == base_item:
            target_choice = source_choice = target_item
        else:
            has_conflict = True
            target_choice = target_item
            source_choice = source_item
        if target_choice is not None:
            target_result.append(copy.deepcopy(target_choice))
        if source_choice is not None:
            source_result.append(copy.deepcopy(source_choice))
    return (target_result, source_result, has_conflict)

def _confirmed_reference_identity(item: dict[str, Any]) -> tuple[Any, ...]:
    return (item.get('source_ingestion_id'), item.get('source_chapter_id') or item.get('scope_chapter_id'), item.get('source_locator'), item.get('page_start'), item.get('page_end'))

def _retrieval_evidence_identity(item: dict[str, Any]) -> tuple[Any, ...]:
    return (item.get('source_ingestion_id'), item.get('chapter_id'), tuple(item.get('chunk_ids') or ()), item.get('page_range'))

def _visual_evidence_identity(item: dict[str, Any]) -> tuple[Any, ...]:
    return (item.get('source_ingestion_id'), item.get('visual_id'))

def _apply_runtime_conflict(draft: MergeRuntimeDraft, conflict: LessonMergeConflict) -> None:
    root, _, field = conflict.path.partition('.')
    if not field or root not in {'learning_requirements', 'board_task_requirements'}:
        return
    current_model = getattr(draft, root)
    current = current_model.model_dump(mode='json') if current_model is not None else {}
    if conflict.resolution == 'source':
        value = conflict.source_value
    elif conflict.resolution == 'both':
        if isinstance(conflict.target_value, list) and isinstance(conflict.source_value, list):
            value = [*conflict.target_value, *conflict.source_value]
        else:
            value = conflict.custom_value
    elif conflict.resolution == 'clear':
        value = None
    elif conflict.resolution in {'custom', 'ai'}:
        value = conflict.custom_value
    else:
        value = conflict.target_value
    current[field] = copy.deepcopy(value)
    model_type = LearningRequirementSheet if root == 'learning_requirements' else BoardTaskRequirementSheet
    try:
        setattr(draft, root, model_type.model_validate(current))
    except Exception as exc:
        raise LessonMergeConflictError(f'The runtime state for conflict {conflict.id} is invalid.') from exc

def _document_signature(document: BoardDocument) -> str:
    return json.dumps([_canonical_node(node) for node in _top_level_nodes(document)], ensure_ascii=False, separators=(',', ':'))

def _sanitize_public_value(value: Any) -> Any:
    blocked = {'local_source_path', 'original_source_path', 'parser_artifacts_path', 'expanded_text', 'context_text'}
    if isinstance(value, dict):
        return {key: _sanitize_public_value(item) for key, item in value.items() if key not in blocked}
    if isinstance(value, list):
        return [_sanitize_public_value(item) for item in value]
    return value
