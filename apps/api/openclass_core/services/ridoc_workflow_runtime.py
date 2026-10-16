"""Restore transient workflow checkpoints without adding technical RIDOC nodes."""
from __future__ import annotations
from copy import deepcopy
from openclass_core.models import Lesson, LessonRuntimeSnapshot
from openclass_core.services.history import current_head_commit
WORKFLOW_METADATA_KEYS = frozenset({'pending_teaching_offer', 'pending_teaching_offer_transition', 'pending_board_write_offer_after', 'pending_board_write_offer_transition', 'requirement_run_id', 'requirement_version_id', 'requirement_ready_version_id', 'requirement_parent_version_id', 'active_requirement_version_id', 'requirement_phase', 'requirement_change_kind', 'requirement_cleared', 'frozen_requirement_payload', 'active_requirement_sheet_after', 'learning_clarification_after', 'active_requirement_assistant_message', 'teaching_plan', 'board_task_run_id', 'board_task_version_id', 'board_task_phase', 'board_task_decision', 'board_task_cleared', 'active_board_task_sheet_after'})
RUNTIME_FIELDS = ('learning_requirements', 'board_task_requirements', 'board_teaching_guide', 'board_teaching_progress')

def workflow_metadata(lesson: Lesson) -> dict:
    """Use nearest explicit values, including nulls that consume old tasks."""
    head = current_head_commit(lesson)
    commits = {commit.id: commit for commit in lesson.history_graph.commits}
    pending, seen, result = ([head.id], set(), {})
    while pending:
        identity = pending.pop(0)
        if identity in seen:
            continue
        seen.add(identity)
        commit = commits.get(identity)
        if commit is None:
            continue
        for key in WORKFLOW_METADATA_KEYS:
            if key in commit.metadata and key not in result:
                result[key] = deepcopy(commit.metadata[key])
        pending.extend(commit.parent_ids)
    return result

def runtime_payload(lesson: Lesson, authoritative_lesson: Lesson, revision: str) -> dict:
    payload = {name: getattr(lesson, name).model_dump(mode='json') if getattr(lesson, name) is not None else None for name in RUNTIME_FIELDS}
    metadata = workflow_metadata(lesson)
    head_id = current_head_commit(authoritative_lesson).id
    authoritative_ids = {commit.id for commit in authoritative_lesson.history_graph.commits}
    offer = metadata.get('pending_teaching_offer')
    if isinstance(offer, dict) and offer.get('source_generation_commit_id') not in authoritative_ids:
        offer['source_generation_commit_id'] = head_id
    if isinstance(offer, dict) and offer.get('branch_name') == lesson.history_graph.current_branch:
        offer['branch_name'] = authoritative_lesson.history_graph.current_branch
    payload['workflow_metadata'] = metadata
    payload['workflow_anchor'] = {'revision': revision, 'branch': authoritative_lesson.history_graph.current_branch, 'head': head_id}
    return payload

def restore_runtime(lesson: Lesson, runtime: dict, revision: str) -> Lesson:
    anchor = runtime.get('workflow_anchor')
    if anchor is not None and anchor != {'revision': revision, 'branch': lesson.history_graph.current_branch, 'head': current_head_commit(lesson).id}:
        return lesson
    payload = lesson.model_dump(mode='json')
    payload.update({name: runtime[name] for name in RUNTIME_FIELDS if name in runtime})
    restored = Lesson.model_validate(payload)
    metadata = runtime.get('workflow_metadata')
    if isinstance(metadata, dict):
        head = current_head_commit(restored)
        head.metadata.update({key: deepcopy(value) for key, value in metadata.items() if key in WORKFLOW_METADATA_KEYS})
        head.runtime_snapshot = LessonRuntimeSnapshot.model_validate({name: payload.get(name) for name in RUNTIME_FIELDS})
    return restored

def checkpoint_frozen_requirement(*, user_id: str, lesson_id: str) -> None:
    from openclass_core.services import workspace_state
    scope = workspace_state.current_ridoc_turn_workspace_scope()
    if scope is None:
        return
    if scope.owner_user_id != user_id:
        raise ValueError('A frozen requirement checkpoint must belong to this turn owner')
    package, lesson = workspace_state.find_lesson_package(scope.workspace, lesson_id)
    from openclass_core.services.ridoc_historical_requirement import current_historical_requirement_checkpoint
    historical = current_historical_requirement_checkpoint()
    if historical is not None:
        historical.save(lesson)
        return
    workspace_state.get_ridoc_workspace_store().save_lesson_runtime(owner_user_id=user_id, package_id=package.id, lesson=lesson, expected_revision=scope.expected_revision)

def same_revalidated_source_scope(frozen, fresh) -> bool:
    """Fresh evidence must have passed current authorization and original reading."""
    from openclass_core.services.source_content_contract import verified_read_evidence
    if not frozen.confirmed_references and (not frozen.frozen_evidence):
        return True
    if fresh is None or not fresh.frozen_evidence or any((not verified_read_evidence(item) for item in fresh.frozen_evidence)):
        return False
    from openclass_core.services.source_board_coverage import require_complete_board_evidence
    try:
        require_complete_board_evidence(fresh.frozen_evidence)
    except ValueError:
        return False

    def identities(grounding):
        import json
        return {json.dumps({'source_id': item.source_ingestion_id, 'hash': item.metadata.get('source_content_hash'), 'version': item.metadata.get('source_version'), 'catalog': item.metadata.get('catalog_version'), 'range': item.metadata.get('source_read_request') or item.metadata.get('source_range')}, sort_keys=True, ensure_ascii=False) for item in grounding.frozen_evidence}
    return bool(frozen.frozen_evidence) and identities(frozen) == identities(fresh)
