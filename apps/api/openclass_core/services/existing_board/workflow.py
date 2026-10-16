from __future__ import annotations
import json
from collections.abc import Callable
from typing import Literal
from openclass_core.models import AgentActivityEvent, AIModelSelection, BoardDecision, BoardDocument, BoardFocusRef, BoardTaskRequirementSheet, BoardTaskRunStatus, ChatRequest, ChatResponse, DecisionTrace, LearningClarificationStatus, Lesson, SelectionRef, new_id
from openclass_core.services import workspace_state
from openclass_core.services.ai_execution_adapter import AIExecutionAdapter
from openclass_core.services.existing_board import task_manager
from openclass_core.services.existing_board.document_destination_workflow import run_document_destination_workflow
from openclass_core.services.existing_board.explanation_workflow import BoardFreeRecentConversation, run_existing_board_explanation
from openclass_core.services.existing_board.focus_resolver import FocusResolver, TargetResolution
from openclass_core.services.existing_board.interaction_workflow import process_interaction_turn
from openclass_core.services.existing_board.mutation_binding import ConfirmedWriteAnchor, bind_and_execute_board_mutation
from openclass_core.services.existing_board.mutation_plan import board_document_hash
from openclass_core.services.existing_board.mutation_planner import MutationPlannerError, plan_existing_board_mutation, resolved_insertion_position
from openclass_core.services.history import commit_operations, current_head_commit
from openclass_core.services.lesson_factory import build_requirements
from openclass_core.services.turn_intent import has_explicit_board_location_cue, wants_direct_learning_answer_without_board_target
TASK_CHATBOT_INSTRUCTIONS = '\nYou are the learner-facing Chatbot for an existing-board task. Use only the\nstructured task and bounded target candidates in the payload. In\ntask_clarification mode, generate exactly one context-specific question that\nresolves the most important missing field or candidate choice. In pending-stage\nmodes, generate only the status or confirmation response appropriate to the\nstructured task. In mutation_completed mode, state that the authorized mutation\ncompleted and ask whether the learner wants the changed content explained. Do\nIn confirmation_declined mode, acknowledge that the pending task was cancelled\nwithout claiming a document change. Do not explain lesson content, perform\nanother action, invent board text, or reuse fixed wording. Claim a document\nchange only when the supplied execution status explicitly says it succeeded.\n'.strip()
TaskResponseMode = Literal['task_clarification', 'future_action_status', 'confirmation_required', 'confirmation_declined', 'mutation_completed']

class ExistingBoardWorkflowError(RuntimeError):
    pass

class ExistingBoardAnswerThenOfferReroute(RuntimeError):
    """Send an off-board learning question to the answer_then_offer path."""

    def __init__(self, request: ChatRequest, *, reason: str, allow_teaching_offer: bool=True, topic: str='') -> None:
        super().__init__(reason)
        self.request = request.model_copy(deep=True)
        self.reason = reason
        self.allow_teaching_offer = allow_teaching_offer
        self.topic = topic or request.message

def process_existing_board_workflow(lesson_id: str, request: ChatRequest, *, user_id: str, adapter: AIExecutionAdapter, selected_model: AIModelSelection, board_write_opt_out: bool | None=None, explicit_teaching_request: bool | None=None, on_delta: Callable[[str], None] | None=None, on_board_task_update: Callable[[dict[str, object]], None] | None=None, on_agent_activity: Callable[[AgentActivityEvent], None] | None=None, is_cancelled: Callable[[], bool] | None=None) -> ChatResponse:
    workspace = workspace_state.load_workspace_for_user(user_id)
    _package, lesson = workspace_state.find_lesson_package(workspace, lesson_id)
    if not lesson.board_document.content_text.strip():
        raise ExistingBoardWorkflowError('Existing-board workflow requires a non-empty board')
    from openclass_core.services.teaching_policy import current_teaching_decision
    teaching_decision = current_teaching_decision()
    teaching_policy_active = teaching_decision is not None or board_write_opt_out is not None or explicit_teaching_request is not None
    if board_write_opt_out is None:
        board_write_opt_out = bool(getattr(teaching_decision, 'board_write_opt_out', False))
    if explicit_teaching_request is None:
        explicit_teaching_request = bool(getattr(teaching_decision, 'explicit_teaching_request', False))
    branch_name = lesson.history_graph.current_branch
    base_head = current_head_commit(lesson)
    active_task = _restore_active_task(lesson, base_head)
    if board_write_opt_out and request.board_task_confirmation is not None:
        raise ExistingBoardAnswerThenOfferReroute(request, reason='The current turn forbids board changes.', allow_teaching_offer=False)
    if request.board_task_confirmation is not None:
        return _process_active_task_confirmation(lesson_id=lesson_id, lesson=lesson, request=request, user_id=user_id, adapter=adapter, selected_model=selected_model, active_task=active_task, branch_name=branch_name, base_head_id=base_head.id, base_metadata=base_head.metadata, on_delta=on_delta, on_board_task_update=on_board_task_update, on_agent_activity=on_agent_activity, is_cancelled=is_cancelled)
    if active_task is not None and active_task.interaction_session is not None:
        return process_interaction_turn(lesson_id=lesson_id, lesson=lesson, request=request, user_id=user_id, adapter=adapter, selected_model=selected_model, task=active_task, expected_branch=branch_name, expected_head=base_head.id, run_id=str(base_head.metadata.get('board_task_run_id') or new_id('boardtaskrun')), version_id=str(base_head.metadata.get('board_task_version_id') or new_id('boardtaskver')), decision_reason='The active interaction session owns this input until it exits or yields a new task.', prior_roles=[_role_execution('interaction_session_restore', None, ['persisted_interaction_session', 'current_input_event'])], on_delta=on_delta, on_board_task_update=on_board_task_update, on_agent_activity=on_agent_activity, is_cancelled=is_cancelled)
    manager_input = task_manager.build_task_manager_input(message=request.message, conversation=request.conversation, explicit_controls=_explicit_controls(request), references=_frozen_references(request), active_task=_active_task_summary(active_task))
    manager_response = adapter.parse_structured(system_prompt=task_manager.TASK_MANAGER_INSTRUCTIONS, user_prompt=json.dumps(manager_input.model_dump(mode='json'), ensure_ascii=False, separators=(',', ':')), schema=task_manager.BoardTaskManagerDraft, allow_live_web_search=False, on_activity=on_agent_activity)
    draft = task_manager.BoardTaskManagerDraft.model_validate(manager_response.output_parsed)
    draft = _apply_explicit_controls(draft, manager_input.explicit_controls)
    if board_write_opt_out and draft.action in {'write', 'edit', 'delete'}:
        draft = draft.model_copy(update={'action': 'explain', 'location_kind': 'target_range', 'insertion_position': None})
    decision = task_manager._finalize_decision(draft)
    activity = list(getattr(manager_response, 'activity', []))
    resolution = _resolve_target(lesson, request, decision.target_hint, content_extent=decision.extent)
    pending_teaching = active_task is not None and decision.relation_to_active in {'continue', 'supplement'} and bool(getattr(active_task, 'teach_after_mutation', False) or (base_head.metadata.get('board_task_decision') or {}).get('teach_after_mutation'))
    teach_after_mutation = bool((explicit_teaching_request or pending_teaching) and (not board_write_opt_out) and (getattr(teaching_decision, 'learning_flow', 'teach') != 'board'))
    if teaching_policy_active and (decision.action == 'explain' or (pending_teaching and decision.action == 'write' and (resolution.focus is None) and (decision.destination != 'new_lesson'))):
        from openclass_core.services.existing_board.teaching_target import prepare_teaching_target
        decision, resolution, coverage_activity = prepare_teaching_target(lesson=lesson, request=request, decision=decision, resolution=resolution, adapter=adapter, explicit_teaching_request=teach_after_mutation, board_write_opt_out=bool(board_write_opt_out), on_activity=on_agent_activity)
        activity.extend(coverage_activity)
    decision = decision.model_copy(update={'teach_after_mutation': teach_after_mutation})
    task = _build_task_sheet(lesson, decision, resolution, active_task=active_task, base_commit_id=base_head.id)
    task.insertion_position = resolved_insertion_position(task)
    from openclass_core.services.google_sources.board_scope import bind_task_source_scope
    bind_task_source_scope(task=task, active_task=active_task, relation_to_active=decision.relation_to_active, request=request, owner_user_id=user_id, package_id=_package.id, adapter=adapter, is_cancelled=is_cancelled)
    run_id = _task_run_id(base_head.metadata, active_task, decision.relation_to_active)
    version_id = new_id('boardtaskver')
    phase = _next_phase(task, decision.requires_confirmation)
    roles = [_role_execution('task_manager', selected_model, ['message', 'recent_conversation', 'reference_identity', 'active_task_summary']), _role_execution('focus_resolver', None, ['board_segment_index', 'target_hint', 'frozen_selection_identity'])]
    if _should_divert_explain_to_answer_then_offer(request, decision, task, teaching_policy_active=teaching_policy_active, board_write_opt_out=bool(board_write_opt_out)):
        divert_reason = 'The board does not cover this topic; provide a brief answer' + (' without a write offer.' if board_write_opt_out else ' and offer to expand teaching.')
        divert_trace = {'intent_signals': ['board_task:explain', 'default_question_first'], 'matched_rules': ['existing_board_bounded_workflow', 'answer_before_board_write'], 'selected_action': 'answer_then_offer', 'target_resolver': 'FocusResolver', 'sequence_mode': 'single_turn', 'role_executed': 'chatbot', 'board_access': 'bounded_board_role', 'requirement_effect': 'cleared', 'document_changed': False, 'board_task_phase': 'not_executed', 'reason': divert_reason}
        _persist_task_phase(lesson, user_id=user_id, expected_branch=branch_name, expected_head=base_head.id, task=task, phase='not_executed', run_id=run_id, version_id=version_id, request=request, chatbot_message='', selected_model=selected_model, decision=decision, trace=divert_trace, roles=roles, clear_active=True)
        _publish_task_update(on_board_task_update, task, run_id, version_id, 'not_executed')
        raise ExistingBoardAnswerThenOfferReroute(request, reason=divert_reason, allow_teaching_offer=not board_write_opt_out, topic=task.question_or_topic)
    if phase == 'collecting':
        needs_clarification = True
        response_mode: TaskResponseMode = 'task_clarification'
        chatbot_message, chatbot_activity = _generate_task_message(adapter, task, mode=response_mode, on_activity=on_agent_activity, is_cancelled=is_cancelled)
        activity.extend(chatbot_activity)
        if needs_clarification:
            task.clarification_question = chatbot_message
        roles.append(_role_execution('chatbot', selected_model, ['board_task_sheet', 'bounded_target_candidates']))
        trace = _decision_trace(decision, task, phase, role='chatbot')
        _persist_task_phase(lesson, user_id=user_id, expected_branch=branch_name, expected_head=base_head.id, task=task, phase=phase, run_id=run_id, version_id=version_id, request=request, chatbot_message=chatbot_message, selected_model=selected_model, decision=decision, trace=trace, roles=roles)
        _publish_task_update(on_board_task_update, task, run_id, version_id, phase)
        _publish_delta(on_delta, chatbot_message)
        return _build_response(lesson_id=lesson_id, user_id=user_id, task=task, active_task=task, phase=phase, run_id=run_id, version_id=version_id, chatbot_message=chatbot_message, activity=activity, decision_reason=decision.reason, needs_clarification=needs_clarification)
    if board_write_opt_out and task.requested_action in {'edit', 'write', 'delete'}:
        raise ExistingBoardAnswerThenOfferReroute(request, reason='The current turn forbids board changes.', allow_teaching_offer=False)
    if task.requested_action in {'edit', 'write'} and phase == 'ready':
        return _execute_ready_mutation(lesson_id=lesson_id, lesson=lesson, request=request, user_id=user_id, adapter=adapter, selected_model=selected_model, decision=decision, task=task, branch_name=branch_name, base_head_id=base_head.id, run_id=run_id, version_id=version_id, roles=roles, activity=activity, teach_after_mutation=teach_after_mutation, on_delta=on_delta, on_board_task_update=on_board_task_update, on_agent_activity=on_agent_activity, is_cancelled=is_cancelled)
    if task.requested_action == 'interact' and phase == 'ready':
        return process_interaction_turn(lesson_id=lesson_id, lesson=lesson, request=request, user_id=user_id, adapter=adapter, selected_model=selected_model, task=task, expected_branch=branch_name, expected_head=base_head.id, run_id=run_id, version_id=version_id, decision_reason=decision.reason, prior_roles=roles, on_delta=on_delta, on_board_task_update=on_board_task_update, on_agent_activity=on_agent_activity, is_cancelled=is_cancelled)
    if task.requested_action != 'explain':
        response_mode: TaskResponseMode = 'confirmation_required' if phase == 'awaiting_confirmation' else 'future_action_status'
        chatbot_message, chatbot_activity = _generate_task_message(adapter, task, mode=response_mode, on_activity=on_agent_activity, is_cancelled=is_cancelled)
        activity.extend(chatbot_activity)
        roles.append(_role_execution('chatbot', selected_model, ['board_task_sheet', 'bounded_target_candidates']))
        trace = _decision_trace(decision, task, phase, role='chatbot')
        _persist_task_phase(lesson, user_id=user_id, expected_branch=branch_name, expected_head=base_head.id, task=task, phase=phase, run_id=run_id, version_id=version_id, request=request, chatbot_message=chatbot_message, selected_model=selected_model, decision=decision, trace=trace, roles=roles)
        _publish_task_update(on_board_task_update, task, run_id, version_id, phase)
        _publish_delta(on_delta, chatbot_message)
        return _build_response(lesson_id=lesson_id, user_id=user_id, task=task, active_task=task, phase=phase, run_id=run_id, version_id=version_id, chatbot_message=chatbot_message, activity=activity, decision_reason=decision.reason, needs_clarification=False)
    assert task.target_location is not None
    ready_trace = _decision_trace(decision, task, 'ready', role='focus_resolver')
    ready_roles = list(roles)
    ready_commit_id = _persist_task_phase(lesson, user_id=user_id, expected_branch=branch_name, expected_head=base_head.id, task=task, phase='ready', run_id=run_id, version_id=version_id, request=request, chatbot_message='', selected_model=selected_model, decision=decision, trace=ready_trace, roles=ready_roles)
    _publish_task_update(on_board_task_update, task, run_id, version_id, 'ready')
    from openclass_core.services.google_sources.board_context import source_context_provider, request_source_scope
    explanation = run_existing_board_explanation(adapter=adapter, board_task=task, resolved_focus=task.target_location, teaching_requirements=[task.question_or_topic], current_user_message=request.message, recent_conversation=BoardFreeRecentConversation(board_content_included=False, turns=[]), is_cancelled=is_cancelled, on_activity=on_agent_activity, on_text_delta=None, source_context_provider=source_context_provider(owner_user_id=user_id, lesson_id=lesson_id, package_id=_package.id, question=task.question_or_topic, adapter=adapter, is_cancelled=is_cancelled, on_activity=on_agent_activity, source_scope=task.source_query_scope))
    activity.extend(explanation.activity)
    final_roles = [*ready_roles, _role_execution('board_manager', selected_model, ['board_task_sheet', 'resolved_target_excerpt', 'teaching_requirements']), _role_execution('chatbot', selected_model, ['board_explanation_directive', 'current_user_message'])]
    final_phase: BoardTaskRunStatus = 'consumed' if explanation.substantive_explanation_allowed else 'collecting' if explanation.directive.status == 'needs_clarification' else 'not_executed'
    needs_final_clarification = final_phase == 'collecting'
    if needs_final_clarification:
        task.clarification_question = explanation.chatbot_message
    latest_workspace, latest_lesson = _load_expected_lesson(lesson_id, user_id=user_id, branch_name=branch_name, head_commit_id=ready_commit_id)
    del latest_workspace
    clear_final_task = final_phase in {'consumed', 'not_executed'}
    latest_lesson.board_task_requirements = None if clear_final_task else task
    final_trace = _decision_trace(decision, task, final_phase, role='chatbot')
    _persist_task_phase(latest_lesson, user_id=user_id, expected_branch=branch_name, expected_head=ready_commit_id, task=task, phase=final_phase, run_id=run_id, version_id=version_id, request=request, chatbot_message=explanation.chatbot_message, selected_model=selected_model, decision=decision, trace=final_trace, roles=final_roles, directive=explanation.directive.model_dump(mode='json'), clear_active=clear_final_task)
    _publish_task_update(on_board_task_update, task, run_id, version_id, final_phase)
    _publish_delta(on_delta, explanation.chatbot_message)
    return _build_response(lesson_id=lesson_id, user_id=user_id, task=task, active_task=None if clear_final_task else task, phase=final_phase, run_id=run_id, version_id=version_id, chatbot_message=explanation.chatbot_message, activity=activity, decision_reason=decision.reason, needs_clarification=needs_final_clarification)

def _restore_active_task(lesson: Lesson, head) -> BoardTaskRequirementSheet | None:
    runtime = head.runtime_snapshot
    active = lesson.board_task_requirements
    if active is None and runtime is not None:
        active = runtime.board_task_requirements
    lesson.board_task_requirements = active.model_copy(deep=True) if active else None
    return lesson.board_task_requirements

def _explicit_controls(request: ChatRequest) -> task_manager.BoardTaskExplicitControls:
    whole_board = request.board_generation_action == 'start'
    action = 'edit' if request.interaction_mode == 'direct_edit' or whole_board else None
    location_kind = None
    for reference in _frozen_references(request):
        if reference.location_kind in {'target_range', 'insertion_anchor'}:
            location_kind = reference.location_kind
            break
    return task_manager.BoardTaskExplicitControls(action=action, location_kind=location_kind, extent='whole_board' if whole_board else None, destination='current_lesson' if whole_board else None, topic_relation='current_document' if whole_board else None)

def _apply_explicit_controls(draft: task_manager.BoardTaskManagerDraft, controls: task_manager.BoardTaskExplicitControls) -> task_manager.BoardTaskManagerDraft:
    updates: dict[str, object] = {}
    for field_name in ('action', 'location_kind', 'extent', 'destination', 'topic_relation', 'special_interaction_requirements'):
        value = getattr(controls, field_name)
        if value is not None:
            updates[field_name] = value
    if controls.extent == 'whole_board':
        updates['target_hint'] = ''
        updates['location_kind'] = 'unresolved'
    return draft.model_copy(update=updates)

def _frozen_references(request: ChatRequest) -> list[SelectionRef]:
    references = [*([request.selection] if request.selection is not None else []), *request.selections]
    unique: dict[str, SelectionRef] = {}
    for reference in references:
        key = json.dumps(reference.model_dump(mode='json'), sort_keys=True, ensure_ascii=False)
        unique.setdefault(key, reference.model_copy(deep=True))
    return list(unique.values())[:8]

def _active_task_summary(active: BoardTaskRequirementSheet | None) -> task_manager.ActiveBoardTaskSummary | None:
    if active is None:
        return None
    action = 'interact' if active.requested_action == 'chat' else active.requested_action
    return task_manager.ActiveBoardTaskSummary(action=action, location_kind=active.location_kind if active.location_kind != 'unspecified' else None, target_hint=active.target_hint, insertion_position=active.insertion_position, question_or_topic=active.question_or_topic, special_interaction_requirements=active.special_interaction_requirements or 'none', extent=active.content_extent, destination=active.document_destination, topic_relation=active.topic_relation)

def _resolve_target(lesson: Lesson, request: ChatRequest, target_hint: str, *, content_extent: str | None=None) -> TargetResolution:
    resolver = FocusResolver()
    selections = [item for item in _frozen_references(request) if item.kind == 'board']
    if not selections:
        return resolver.resolve(lesson, target_text=target_hint, content_extent=content_extent)
    return resolver.resolve_many(lesson, selections=selections, content_extent=content_extent)

def _build_task_sheet(lesson: Lesson, decision: task_manager.BoardTaskManagerDecision, resolution: TargetResolution, *, active_task: BoardTaskRequirementSheet | None, base_commit_id: str) -> BoardTaskRequirementSheet:
    focus = resolution.focus.model_copy(deep=True) if resolution.focus is not None else None
    missing = list(dict.fromkeys(decision.missing_items))
    target_required = _decision_requires_resolved_target(decision)
    if not target_required or resolution.status == 'resolved':
        missing = [item for item in missing if item != 'target']
    elif 'target' not in missing:
        missing.append('target')
    inferred_extent = decision.extent
    if focus is not None and resolution.machine_reason == 'resolved_by_selection' and (inferred_extent == 'unresolved'):
        inferred_extent = 'section' if focus.order_start is not None and focus.order_end is not None and (focus.order_end > focus.order_start) else 'paragraph'
        missing = [item for item in missing if item != 'extent']
    relation_continues = decision.relation_to_active in {'continue', 'supplement'}
    task_id = active_task.task_id if active_task is not None and relation_continues else new_id('boardtask')
    action = None if decision.action == 'unresolved' else decision.action
    location_kind = decision.location_kind if decision.location_kind in {'target_range', 'insertion_anchor'} else 'unspecified'
    candidates = [_safe_focus(item) for item in resolution.candidates[:5]]
    if decision.destination == 'new_lesson' and focus is None:
        location_status = 'content_absent'
    elif decision.extent == 'whole_board' and focus is None:
        location_status = 'resolved'
    else:
        location_status = 'resolved' if focus is not None else 'ambiguous' if candidates else 'missing'
    return BoardTaskRequirementSheet(task_id=task_id, teach_after_mutation=decision.teach_after_mutation, location_kind=location_kind, target_hint=decision.target_hint.strip(), insertion_position=decision.insertion_position, target_location=focus, location_status=location_status, requested_action=action, question_or_topic=decision.question_or_topic.strip(), special_interaction_requirements=decision.special_interaction_requirements.strip(), content_extent=None if inferred_extent == 'unresolved' else inferred_extent, topic_relation=decision.topic_relation, document_destination=decision.destination, target_candidates=candidates, target_resolution_reason=resolution.machine_reason, base_commit_id=base_commit_id, base_document_hash=_document_hash(lesson), missing_items=missing, progress=100 if not missing else min(decision.completeness, 99), confirmation_status='awaiting' if decision.requires_confirmation else 'none')

def _next_phase(task: BoardTaskRequirementSheet, requires_confirmation: bool) -> BoardTaskRunStatus:
    target_required = not (task.document_destination == 'new_lesson' or task.content_extent == 'whole_board')
    if task.missing_items or (target_required and task.location_status != 'resolved') or task.requested_action is None:
        return 'collecting'
    return 'awaiting_confirmation' if requires_confirmation else 'ready'

def _should_divert_explain_to_answer_then_offer(request: ChatRequest, decision: task_manager.BoardTaskManagerDecision, task: BoardTaskRequirementSheet, *, teaching_policy_active: bool=False, board_write_opt_out: bool=False) -> bool:
    """Only absent incidental topics may bypass board-directed explanation."""
    if decision.action != 'explain':
        return False
    references = _frozen_references(request)
    has_board_selection = any((item.kind == 'board' for item in references))
    if not has_board_selection and decision.topic_relation == 'independent' and (not decision.target_hint.strip()) and (task.target_location is None) and (task.location_status == 'missing'):
        return True
    source_scoped = request.source_query_scope is not None or any((item.kind == 'source' for item in references))
    if has_board_selection or (not source_scoped and (not board_write_opt_out) and has_explicit_board_location_cue(request.message, has_board_selection=has_board_selection)):
        return False
    if task.location_status == 'resolved' and task.target_location is not None:
        return False
    if teaching_policy_active and task.target_resolution_reason not in {'target_not_found', 'target_missing'}:
        return False
    if wants_direct_learning_answer_without_board_target(request.message):
        return True
    return task.location_status in {'missing', 'ambiguous', 'content_absent'}

def _decision_requires_resolved_target(decision: task_manager.BoardTaskManagerDecision) -> bool:
    return not (decision.destination == 'new_lesson' or decision.extent == 'whole_board' or (decision.action == 'write' and decision.destination == 'unresolved' and (decision.topic_relation in {'independent', 'unresolved'})))

def _process_active_task_confirmation(*, lesson_id: str, lesson: Lesson, request: ChatRequest, user_id: str, adapter: AIExecutionAdapter, selected_model: AIModelSelection, active_task: BoardTaskRequirementSheet | None, branch_name: str, base_head_id: str, base_metadata: dict[str, object], on_delta: Callable[[str], None] | None, on_board_task_update: Callable[[dict[str, object]], None] | None, on_agent_activity: Callable[[AgentActivityEvent], None] | None, is_cancelled: Callable[[], bool] | None) -> ChatResponse:
    if active_task is None or active_task.confirmation_status != 'awaiting':
        raise ExistingBoardWorkflowError('Board task confirmation requires one active awaiting-confirmation task')
    task = active_task.model_copy(deep=True)
    run_id = _task_run_id(base_metadata, active_task, 'continue')
    version_id = new_id('boardtaskver')
    confirmed = request.board_task_confirmation == 'confirm'
    task.confirmation_status = 'confirmed' if confirmed else 'declined'
    task.base_commit_id = base_head_id
    task.base_document_hash = _document_hash(lesson)
    task.mutation_plan = None
    decision = _decision_from_task(task, execution_allowed=confirmed)
    decision = decision.model_copy(update={'teach_after_mutation': bool(getattr(task, 'teach_after_mutation', False) or (base_metadata.get('board_task_decision') or {}).get('teach_after_mutation'))})
    roles = [_role_execution('confirmation_gate', None, ['explicit_confirmation', 'active_board_task', 'current_version_identity'])]
    if confirmed:
        if task.document_destination == 'new_lesson' or task.content_extent == 'whole_board':
            return _execute_confirmed_document_destination(lesson_id=lesson_id, request=request, user_id=user_id, adapter=adapter, selected_model=selected_model, decision=decision, task=task, base_head_id=base_head_id, run_id=run_id, version_id=version_id, on_delta=on_delta, on_board_task_update=on_board_task_update, on_agent_activity=on_agent_activity, is_cancelled=is_cancelled)
        return _execute_ready_mutation(lesson_id=lesson_id, lesson=lesson, request=request, user_id=user_id, adapter=adapter, selected_model=selected_model, decision=decision, task=task, branch_name=branch_name, base_head_id=base_head_id, run_id=run_id, version_id=version_id, roles=roles, activity=[], teach_after_mutation=decision.teach_after_mutation, on_delta=on_delta, on_board_task_update=on_board_task_update, on_agent_activity=on_agent_activity, is_cancelled=is_cancelled)
    chatbot_message, chatbot_activity = _generate_task_message(adapter, task, mode='confirmation_declined', on_activity=on_agent_activity, is_cancelled=is_cancelled, status_context={'execution_status': 'declined', 'document_changed': False})
    roles.append(_role_execution('chatbot', selected_model, ['board_task_sheet', 'confirmation_status']))
    trace = _decision_trace(decision, task, 'archived', role='chatbot')
    _persist_task_phase(lesson, user_id=user_id, expected_branch=branch_name, expected_head=base_head_id, task=task, phase='archived', run_id=run_id, version_id=version_id, request=request, chatbot_message=chatbot_message, selected_model=selected_model, decision=decision, trace=trace, roles=roles, clear_active=True)
    _publish_task_update(on_board_task_update, task, run_id, version_id, 'archived')
    _publish_delta(on_delta, chatbot_message)
    return _build_response(lesson_id=lesson_id, user_id=user_id, task=task, active_task=None, phase='archived', run_id=run_id, version_id=version_id, chatbot_message=chatbot_message, activity=chatbot_activity, decision_reason=decision.reason, needs_clarification=False)

def _decision_from_task(task: BoardTaskRequirementSheet, *, execution_allowed: bool) -> task_manager.BoardTaskManagerDecision:
    draft = task_manager.BoardTaskManagerDraft(action='interact' if task.requested_action == 'chat' else task.requested_action or 'unresolved', target_hint=task.target_hint, location_kind=task.location_kind if task.location_kind in {'target_range', 'insertion_anchor'} else 'unresolved', question_or_topic=task.question_or_topic, special_interaction_requirements=task.special_interaction_requirements or 'none', extent=task.content_extent or 'unresolved', destination=task.document_destination, topic_relation=task.topic_relation, relation_to_active='continue', missing_items=list(task.missing_items), reason='An explicit confirmation was applied to the active board task.')
    decision = task_manager._finalize_decision(draft)
    return decision.model_copy(update={'execution_allowed': execution_allowed})

def _execute_confirmed_document_destination(*, lesson_id: str, request: ChatRequest, user_id: str, adapter: AIExecutionAdapter, selected_model: AIModelSelection, decision: task_manager.BoardTaskManagerDecision, task: BoardTaskRequirementSheet, base_head_id: str, run_id: str, version_id: str, on_delta: Callable[[str], None] | None, on_board_task_update: Callable[[dict[str, object]], None] | None, on_agent_activity: Callable[[AgentActivityEvent], None] | None, is_cancelled: Callable[[], bool] | None=None) -> ChatResponse:
    workspace_revision = None
    if task.document_destination == 'new_lesson':
        _workspace, workspace_revision = workspace_state.load_workspace_for_user_with_revision(user_id)
    result = run_document_destination_workflow(user_id=user_id, source_lesson_id=lesson_id, user_message=request.message, board_task=task, adapter=adapter, selected_model=selected_model, expected_source_head_commit_id=base_head_id, expected_workspace_revision=workspace_revision, teach_after_generation=decision.teach_after_mutation, on_activity=on_agent_activity, is_cancelled=is_cancelled)
    if result.status != 'succeeded':
        raise ExistingBoardWorkflowError(f'The confirmed document destination was not persisted: {result.reason}')
    phase: BoardTaskRunStatus = 'archived' if result.destination == 'new_lesson' else 'consumed'
    trace = _decision_trace(decision, task, phase, role='chatbot')
    trace['document_changed'] = True
    _publish_task_update(on_board_task_update, task, run_id, version_id, phase)
    _publish_delta(on_delta, result.chatbot_message)
    return _build_response(lesson_id=lesson_id, user_id=user_id, task=task, active_task=None, phase=phase, run_id=run_id, version_id=version_id, chatbot_message=result.chatbot_message, activity=result.activity, decision_reason=decision.reason, needs_clarification=False, document_changed=True, document_operation_status='succeeded')

def _execute_ready_mutation(*, lesson_id: str, lesson: Lesson, request: ChatRequest, user_id: str, adapter: AIExecutionAdapter, selected_model: AIModelSelection, decision: task_manager.BoardTaskManagerDecision, task: BoardTaskRequirementSheet, branch_name: str, base_head_id: str, run_id: str, version_id: str, roles: list[dict[str, object]], activity: list[AgentActivityEvent], on_delta: Callable[[str], None] | None, on_board_task_update: Callable[[dict[str, object]], None] | None, on_agent_activity: Callable[[AgentActivityEvent], None] | None, is_cancelled: Callable[[], bool] | None, teach_after_mutation: bool=False) -> ChatResponse:
    focus = task.target_location
    if focus is None:
        raise ExistingBoardWorkflowError('A ready mutation requires one resolved target')
    from openclass_core.services.google_sources.board_context import source_context_provider, request_source_scope
    try:
        planned = plan_existing_board_mutation(adapter=adapter, board_task=task, current_commit_id=base_head_id, current_document_hash=_document_hash(lesson), parent_heading_path=focus.heading_path, resolved_focus=focus, is_cancelled=is_cancelled, on_activity=on_agent_activity, source_context_provider=source_context_provider(owner_user_id=user_id, lesson_id=lesson_id, question=task.question_or_topic, adapter=adapter, is_cancelled=is_cancelled, on_activity=on_agent_activity, source_scope=task.source_query_scope, citation_style='identity', require_complete=True, allow_deferred=True))
    except MutationPlannerError as exc:
        raise ExistingBoardWorkflowError(f'The mutation planner rejected the authorized task: {exc}') from exc
    activity.extend(planned.activity)
    write_anchors = [ConfirmedWriteAnchor(confirmed=True, operation_id=operation.operation_id, lesson_id=operation.binding.lesson_id, document_id=operation.binding.document_id, segment_id=operation.binding.segment_id, text_hash=operation.binding.text_hash, position=operation.binding.position, parent_heading_path=list(operation.binding.parent_heading_path)) for operation in planned.plan.operations if operation.action == 'write']
    execution = bind_and_execute_board_mutation(draft=planned.plan, document=lesson.board_document, current_commit_id=base_head_id, resolved_focus=focus, confirmed_write_anchors=write_anchors)
    if execution.status != 'applied' or execution.execution_audit is None:
        raise ExistingBoardWorkflowError(f'The mutation was rejected before persistence: {execution.reason}')
    if planned.original_preparation is not None:
        execution.document = planned.original_preparation.apply_visuals(execution.document, lesson_id=lesson_id)
        from openclass_core.services.existing_board.mutation_plan import board_document_hash
        execution.execution_audit.document_hash_after = board_document_hash(execution.document)
    task.mutation_plan = planned.plan.model_dump(mode='json')
    roles.extend([_role_execution('content_planner_editor', selected_model, ['board_task_sheet', 'resolved_target_excerpt', 'version_identity']), _role_execution('backend_mutation_executor', None, ['bounded_mutation_plan', 'current_document', 'version_identity'], document_write_allowed=True)])
    explanation_directive = None
    if teach_after_mutation:
        from openclass_core.services.existing_board.teaching_target import first_changed_target
        staged_lesson = lesson.model_copy(deep=True)
        staged_lesson.board_document = execution.document.model_copy(deep=True)
        changed_resolution = first_changed_target(before=lesson, after=staged_lesson)
        if changed_resolution.focus is None:
            raise ExistingBoardWorkflowError('The generated content has no bounded teaching target')
        explanation_task = task.model_copy(deep=True, update={'requested_action': 'explain', 'target_location': changed_resolution.focus, 'location_status': 'resolved', 'missing_items': [], 'progress': 100, 'mutation_plan': None})
        fallback_source = source_context_provider(owner_user_id=user_id, lesson_id=lesson_id, question=task.question_or_topic, adapter=adapter, is_cancelled=is_cancelled, on_activity=on_agent_activity, source_scope=task.source_query_scope)

        def teaching_source(approved):
            from openclass_core.services.teaching_original_sources import first_teaching_original_context
            excerpt = getattr(approved, 'target_excerpt', getattr(approved, 'excerpt', ''))
            return first_teaching_original_context(excerpt, owner_user_id=user_id) or fallback_source(approved)
        explanation = run_existing_board_explanation(adapter=adapter, board_task=explanation_task, resolved_focus=changed_resolution.focus, teaching_requirements=[task.question_or_topic], current_user_message=request.message, on_activity=on_agent_activity, is_cancelled=is_cancelled, source_context_provider=teaching_source)
        if not explanation.substantive_explanation_allowed:
            raise ExistingBoardWorkflowError('The new board content was not approved for teaching')
        chatbot_message = explanation.chatbot_message
        activity.extend(explanation.activity)
        explanation_directive = explanation.directive.model_dump(mode='json')
        roles.extend([_role_execution('board_manager', selected_model, ['board_task_sheet', 'new_target_excerpt']), _role_execution('chatbot', selected_model, ['board_explanation_directive'])])
    else:
        chatbot_message, chatbot_activity = _generate_task_message(adapter, task, mode='mutation_completed', on_activity=on_agent_activity, is_cancelled=is_cancelled, status_context={'execution_status': 'succeeded', 'operation_count': execution.atomic_operation_count, 'applied_actions': [operation.action for operation in planned.plan.operations]})
        activity.extend(chatbot_activity)
        roles.append(_role_execution('chatbot', selected_model, ['board_task_sheet', 'mutation_execution_status']))
    trace = _decision_trace(decision, task, 'consumed', role='chatbot')
    trace['document_changed'] = True
    _persist_mutation(lesson, user_id=user_id, expected_branch=branch_name, expected_head=base_head_id, task=task, run_id=run_id, version_id=version_id, request=request, chatbot_message=chatbot_message, selected_model=selected_model, decision=decision, trace=trace, roles=roles, next_document=execution.document, mutation_audit=execution.execution_audit.model_dump(mode='json'), explanation_directive=explanation_directive)
    from openclass_core.services.google_sources.turn_evidence import remember_document_numbering
    from openclass_core.services.rich_document import document_to_markdown
    remember_document_numbering(document_to_markdown(execution.document), normalize=False)
    _publish_task_update(on_board_task_update, task, run_id, version_id, 'consumed')
    _publish_delta(on_delta, chatbot_message)
    return _build_response(lesson_id=lesson_id, user_id=user_id, task=task, active_task=None, phase='consumed', run_id=run_id, version_id=version_id, chatbot_message=chatbot_message, activity=activity, decision_reason=decision.reason, needs_clarification=False, document_changed=True, document_operation_status='succeeded')

def _generate_task_message(adapter: AIExecutionAdapter, task: BoardTaskRequirementSheet, *, mode: TaskResponseMode, on_activity: Callable[[AgentActivityEvent], None] | None, is_cancelled: Callable[[], bool] | None, status_context: dict[str, object] | None=None) -> tuple[str, list[AgentActivityEvent]]:
    safe_task = task.model_copy(deep=True, update={'target_location': _safe_focus(task.target_location), 'target_candidates': [_safe_focus(item) for item in task.target_candidates], 'mutation_plan': None, 'interaction_session': None})
    response = adapter.complete_text(system_prompt=TASK_CHATBOT_INSTRUCTIONS, user_prompt=json.dumps({'response_mode': mode, 'board_task_requirement_sheet': safe_task.model_dump(mode='json'), 'target_candidates': [item.model_dump(mode='json') for item in safe_task.target_candidates], 'execution_status': dict(status_context or {})}, ensure_ascii=False, separators=(',', ':')), is_cancelled=is_cancelled, on_activity=on_activity, on_text_delta=None)
    message = response.output_text.strip()
    if not message:
        raise ExistingBoardWorkflowError('Task Chatbot returned an empty response')
    return (message, list(getattr(response, 'activity', [])))

def _board_task_route_for_phase(phase: BoardTaskRunStatus, task: BoardTaskRequirementSheet, trace: dict[str, object]) -> str:
    if phase == 'not_executed' and trace.get('selected_action') == 'answer_then_offer':
        return 'answer_then_offer'
    if phase == 'collecting':
        return 'clarify_location'
    if phase == 'awaiting_confirmation':
        return 'await_write_confirmation'
    return task.requested_action or 'clarify_location'

def _persist_task_phase(lesson: Lesson, *, user_id: str, expected_branch: str, expected_head: str, task: BoardTaskRequirementSheet, phase: BoardTaskRunStatus, run_id: str, version_id: str, request: ChatRequest, chatbot_message: str, selected_model: AIModelSelection, decision: task_manager.BoardTaskManagerDecision, trace: dict[str, object], roles: list[dict[str, object]], directive: dict[str, object] | None=None, clear_active: bool=False) -> str:
    lesson.board_task_requirements = None if clear_active else task.model_copy(deep=True)
    focus = task.target_location.model_dump(mode='json') if task.target_location else None
    document_hash = _document_hash(lesson)
    commit_operations(lesson, operations=[], label=f'Board task {phase}', message=f'Recorded board task phase {phase}.', new_document=lesson.board_document, metadata={'kind': 'board_directed_explanation' if phase == 'consumed' else 'board_task_requirement_refinement', 'user_message': request.message, 'assistant_message': chatbot_message, 'assistant_message_source': 'existing_board_workflow', 'document_changed': False, 'document_write_authorized': False, 'document_hash_before': document_hash, 'document_hash_after': document_hash, 'board_task_run_id': run_id, 'board_task_version_id': version_id, 'board_task_phase': phase, 'board_task_route': _board_task_route_for_phase(phase, task, trace), 'board_task_decision': decision.model_dump(mode='json'), 'board_task_cleared': clear_active, 'active_board_task_sheet_after': None if clear_active else task.model_dump(mode='json'), 'resolved_focus': focus, 'target_candidates': [item.model_dump(mode='json') for item in task.target_candidates], 'board_explanation_directive': directive, 'decision_trace': trace, 'selected_model': selected_model.model_dump(mode='json'), 'ai_provider': selected_model.provider, 'ai_model': selected_model.model, 'agent_backend': selected_model.agent_backend, 'role_executions': roles})
    if not workspace_state.save_lesson_for_user_if_head(user_id, lesson, expected_branch_name=expected_branch, expected_head_commit_id=expected_head):
        raise ExistingBoardWorkflowError('The lesson changed while the existing-board workflow was working')
    return current_head_commit(lesson).id

def _persist_mutation(lesson: Lesson, *, user_id: str, expected_branch: str, expected_head: str, task: BoardTaskRequirementSheet, run_id: str, version_id: str, request: ChatRequest, chatbot_message: str, selected_model: AIModelSelection, decision: task_manager.BoardTaskManagerDecision, trace: dict[str, object], roles: list[dict[str, object]], next_document: BoardDocument, mutation_audit: dict[str, object], explanation_directive: dict[str, object] | None=None) -> str:
    lesson.board_document = next_document.model_copy(deep=True)
    lesson.board_task_requirements = None
    commit_operations(lesson, operations=[], label='Board mutation consumed', message='Applied one bounded atomic board mutation plan.', new_document=lesson.board_document, metadata={'kind': 'board_document_mutation', 'user_message': request.message, 'assistant_message': chatbot_message, 'assistant_message_source': 'existing_board_workflow', 'document_changed': True, 'document_write_authorized': True, 'document_hash_before': mutation_audit['document_hash_before'], 'document_hash_after': mutation_audit['document_hash_after'], 'board_task_run_id': run_id, 'board_task_version_id': version_id, 'board_task_phase': 'consumed', 'board_task_route': task.requested_action, 'board_task_decision': decision.model_dump(mode='json'), 'board_task_cleared': True, 'active_board_task_sheet_after': None, 'resolved_focus': task.target_location.model_dump(mode='json') if task.target_location is not None else None, 'board_mutation_plan': task.mutation_plan, 'board_mutation_audit': mutation_audit, 'board_explanation_directive': explanation_directive, 'board_content_extent': task.content_extent, 'board_topic_relation': task.topic_relation, 'board_document_destination': task.document_destination, 'decision_trace': trace, 'selected_model': selected_model.model_dump(mode='json'), 'ai_provider': selected_model.provider, 'ai_model': selected_model.model, 'agent_backend': selected_model.agent_backend, 'role_executions': roles})
    if not workspace_state.save_lesson_for_user_if_head(user_id, lesson, expected_branch_name=expected_branch, expected_head_commit_id=expected_head):
        raise ExistingBoardWorkflowError('The lesson changed while the board mutation was being persisted')
    return current_head_commit(lesson).id

def _build_response(*, lesson_id: str, user_id: str, task: BoardTaskRequirementSheet, active_task: BoardTaskRequirementSheet | None, phase: BoardTaskRunStatus, run_id: str, version_id: str, chatbot_message: str, activity: list[AgentActivityEvent], decision_reason: str, needs_clarification: bool, document_changed: bool=False, document_operation_status: Literal['none', 'succeeded', 'failed']='none') -> ChatResponse:
    workspace = workspace_state.load_workspace_for_user(user_id)
    package, lesson = workspace_state.find_lesson_package(workspace, lesson_id)
    clarification = _neutral_clarification(chatbot_message if needs_clarification else '')
    return ChatResponse(chatbot_message=chatbot_message, decision_trace=DecisionTrace(intent_signals=[f"board_task:{task.requested_action or 'unresolved'}"], matched_rules=['existing_board_bounded_workflow'], selected_action='learning_need', target_resolver='FocusResolver', role_executed='chatbot', board_access='bounded_board_role', requirement_effect='updated', document_changed=document_changed, reason=decision_reason), agent_activity=activity, learning_requirement_sheet=build_requirements(lesson.title), active_requirement_sheet=None, learning_clarification=clarification, board_task_sheet=task, active_board_task_sheet=active_task, board_task_run_id=run_id, board_task_version_id=version_id, board_task_phase=phase, board_task_questions=[chatbot_message] if needs_clarification else [], board_decision=BoardDecision(action='edit_board' if document_changed else 'no_change', reason=decision_reason), needs_clarification=needs_clarification, clarification_questions=[chatbot_message] if needs_clarification else [], requirement_cleared=False, board_document_operation_status=document_operation_status, course_package=workspace_state.package_view_for_lesson(workspace, package, lesson.id))

def _load_expected_lesson(lesson_id: str, *, user_id: str, branch_name: str, head_commit_id: str):
    workspace = workspace_state.load_workspace_for_user(user_id)
    _package, lesson = workspace_state.find_lesson_package(workspace, lesson_id)
    if lesson.history_graph.current_branch != branch_name or current_head_commit(lesson).id != head_commit_id:
        raise ExistingBoardWorkflowError('The lesson changed while the existing-board workflow was working')
    return (workspace, lesson)

def _task_run_id(metadata: dict[str, object], active_task: BoardTaskRequirementSheet | None, relation: str) -> str:
    existing = metadata.get('board_task_run_id')
    if active_task is not None and relation in {'continue', 'supplement'} and isinstance(existing, str) and existing:
        return existing
    return new_id('boardtaskrun')

def _safe_focus(focus: BoardFocusRef | None) -> BoardFocusRef | None:
    if focus is None:
        return None
    return focus.model_copy(deep=True, update={'before_text': '', 'after_text': ''})

def _decision_trace(decision: task_manager.BoardTaskManagerDecision, task: BoardTaskRequirementSheet, phase: str, *, role: str) -> dict[str, object]:
    return {'intent_signals': [f'board_task:{decision.action}'], 'matched_rules': ['existing_board_bounded_workflow'], 'selected_action': task.requested_action or 'clarify_location', 'target_resolver': 'FocusResolver', 'sequence_mode': 'single_turn', 'role_executed': role, 'board_access': 'bounded_board_role', 'requirement_effect': 'updated', 'document_changed': False, 'board_task_phase': phase, 'reason': decision.reason}

def _role_execution(role: str, model: AIModelSelection | None, input_scope: list[str], *, document_write_allowed: bool=False) -> dict[str, object]:
    return {'role': role, 'model': model.model_dump(mode='json') if model is not None else None, 'input_scope': input_scope, 'document_write_allowed': document_write_allowed}

def _neutral_clarification(question: str) -> LearningClarificationStatus:
    return LearningClarificationStatus(progress=0, label='', reason='', missing_items=[], can_start=False, forced_start=False, summary='', next_question=question, ready_for_board=False)

def _publish_task_update(callback: Callable[[dict[str, object]], None] | None, task: BoardTaskRequirementSheet, run_id: str, version_id: str, phase: BoardTaskRunStatus) -> None:
    if callback is not None:
        callback({'board_task_sheet': task.model_dump(mode='json'), 'active_board_task_sheet': None if phase in {'consumed', 'not_executed', 'archived'} else task.model_dump(mode='json'), 'board_task_run_id': run_id, 'board_task_version_id': version_id, 'board_task_phase': phase})

def _publish_delta(callback: Callable[[str], None] | None, message: str) -> None:
    if callback is not None and message:
        callback(message)

def _document_hash(lesson: Lesson) -> str:
    return board_document_hash(lesson.board_document)
