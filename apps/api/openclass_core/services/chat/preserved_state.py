"""Carry the active learning workflow through a successful read-only source answer."""
from openclass_core.services.history import current_head_commit
from openclass_core.services.lesson_factory import build_requirements

def preserved_learning_response_state(lesson):
    from openclass_core.services.chat_turn_gate import _latest_metadata_with_any_key, _clarification_from_metadata, _metadata_text, _requirement_phase, _board_task_phase
    runtime = current_head_commit(lesson).runtime_snapshot
    requirement = runtime.learning_requirements if runtime is not None else lesson.learning_requirements
    board_task = runtime.board_task_requirements if runtime is not None else lesson.board_task_requirements
    requirement_metadata = _latest_metadata_with_any_key(lesson, {'active_requirement_sheet_after', 'requirement_phase'})
    board_metadata = _latest_metadata_with_any_key(lesson, {'active_board_task_sheet_after', 'board_task_phase'})
    return {'learning_requirement_sheet': requirement or build_requirements(lesson.title), 'active_requirement_sheet': requirement, 'learning_clarification': _clarification_from_metadata(requirement_metadata), 'requirement_run_id': _metadata_text(requirement_metadata, 'requirement_run_id'), 'requirement_version_id': _metadata_text(requirement_metadata, 'active_requirement_version_id') or _metadata_text(requirement_metadata, 'requirement_version_id'), 'requirement_phase': _requirement_phase(requirement_metadata, requirement), 'board_task_sheet': board_task, 'active_board_task_sheet': board_task, 'board_task_run_id': _metadata_text(board_metadata, 'board_task_run_id'), 'board_task_version_id': _metadata_text(board_metadata, 'board_task_version_id'), 'board_task_phase': _board_task_phase(board_metadata, board_task), 'board_task_questions': [board_task.clarification_question] if board_task is not None and board_task.clarification_question else [], 'requirement_cleared': False}
