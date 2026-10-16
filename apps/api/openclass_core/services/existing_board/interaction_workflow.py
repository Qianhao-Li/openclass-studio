from __future__ import annotations
from collections.abc import Callable
from openclass_core.models import AIModelSelection, AgentActivityEvent, BoardDecision, BoardTaskRequirementSheet, ChatRequest, ChatResponse, DecisionTrace, LearningClarificationStatus, Lesson, new_id
from openclass_core.services import workspace_state
from openclass_core.services.ai_execution_adapter import AIExecutionAdapter
from openclass_core.services.existing_board.interaction_runtime import InteractionRuntimeError, run_existing_board_interaction
from openclass_core.services.existing_board.interaction_session import InteractionSession
from openclass_core.services.existing_board.mutation_plan import board_document_hash
from openclass_core.services.history import commit_operations
from openclass_core.services.lesson_factory import build_requirements

class ExistingBoardInteractionReroute(RuntimeError):

    def __init__(self, request: ChatRequest, dispatch_key: str) -> None:
        super().__init__('The active interaction yielded a new task for the main turn router')
        self.request = request.model_copy(deep=True)
        self.dispatch_key = dispatch_key

class ExistingBoardInteractionWorkflowError(RuntimeError):
    pass

def process_interaction_turn(*, lesson_id: str, lesson: Lesson, request: ChatRequest, user_id: str, adapter: AIExecutionAdapter, selected_model: AIModelSelection, task: BoardTaskRequirementSheet, expected_branch: str, expected_head: str, run_id: str, version_id: str, decision_reason: str, prior_roles: list[dict[str, object]] | None=None, on_delta: Callable[[str], None] | None=None, on_board_task_update: Callable[[dict[str, object]], None] | None=None, on_agent_activity: Callable[[AgentActivityEvent], None] | None=None, is_cancelled: Callable[[], bool] | None=None) -> ChatResponse:
    focus = task.target_location
    if focus is None:
        raise ExistingBoardInteractionWorkflowError('An interaction task requires one resolved target')
    restored_session = _restore_session(task)
    if restored_session is not None:
        run_id = restored_session.source_board_task_run_id
        version_id = restored_session.source_board_task_version_id
    input_event_id = request.input_event_id or new_id('interaction_input')
    try:
        result = run_existing_board_interaction(adapter=adapter, board_task=task, resolved_focus=focus, current_message=request.message, input_event_id=input_event_id, board_task_run_id=run_id, board_task_version_id=version_id, session=restored_session, is_cancelled=is_cancelled, on_activity=on_agent_activity, on_text_delta=None)
    except InteractionRuntimeError as exc:
        raise ExistingBoardInteractionWorkflowError(str(exc)) from exc
    task.interaction_session = result.transition.session.model_dump(mode='json')
    state = result.transition.session.current_state
    phase = 'archived' if state == 'replaced' else 'consumed' if state == 'exited' else 'ready'
    clear_active = state in {'exited', 'replaced'}
    roles = [*(prior_roles or [])]
    if result.rule_built:
        roles.append(_role_execution('interaction_rule_builder', selected_model, ['interaction_requirement', 'approved_target_excerpt']))
    if not result.duplicate_input:
        roles.append(_role_execution('interaction_router', selected_model, ['interaction_rule', 'interaction_progress', 'current_message']))
    if result.chatbot_message:
        roles.append(_role_execution('chatbot', selected_model, ['interaction_rule', 'approved_target_excerpt', 'route_decision']))
    if not result.duplicate_input:
        _persist_interaction(lesson, user_id=user_id, expected_branch=expected_branch, expected_head=expected_head, task=task, phase=phase, run_id=run_id, version_id=version_id, request=request, chatbot_message=result.chatbot_message, selected_model=selected_model, decision_reason=decision_reason, route=result.transition.route, roles=roles, clear_active=clear_active)
        _publish_task_update(on_board_task_update, task, run_id=run_id, version_id=version_id, phase=phase, clear_active=clear_active)
    if result.should_reroute_original:
        if not result.reroute_dispatch_key:
            raise ExistingBoardInteractionWorkflowError('The interaction reroute lacks an idempotent dispatch key')
        raise ExistingBoardInteractionReroute(request, result.reroute_dispatch_key)
    if result.chatbot_message and on_delta is not None:
        on_delta(result.chatbot_message)
    return _build_response(lesson_id=lesson_id, user_id=user_id, task=task, active_task=None if clear_active else task, phase=phase, run_id=run_id, version_id=version_id, chatbot_message=result.chatbot_message, activity=result.activity, decision_reason=decision_reason)

def _restore_session(task: BoardTaskRequirementSheet) -> InteractionSession | None:
    if task.interaction_session is None:
        return None
    try:
        return InteractionSession.model_validate(task.interaction_session)
    except Exception as exc:
        raise ExistingBoardInteractionWorkflowError('The persisted interaction session is invalid') from exc

def _persist_interaction(lesson: Lesson, *, user_id: str, expected_branch: str, expected_head: str, task: BoardTaskRequirementSheet, phase: str, run_id: str, version_id: str, request: ChatRequest, chatbot_message: str, selected_model: AIModelSelection, decision_reason: str, route: str, roles: list[dict[str, object]], clear_active: bool) -> None:
    lesson.board_task_requirements = None if clear_active else task.model_copy(deep=True)
    document_hash = board_document_hash(lesson.board_document)
    trace = {'intent_signals': ['active_interaction_session'], 'matched_rules': ['interaction_session_runtime'], 'selected_action': 'learning_need', 'target_resolver': 'persisted_interaction_target', 'sequence_mode': 'interaction_session', 'role_executed': 'chatbot' if chatbot_message else 'interaction_router', 'board_access': 'bounded_board_role', 'requirement_effect': 'updated', 'document_changed': False, 'reason': decision_reason}
    commit_operations(lesson, operations=[], label=f'Interaction session {phase}', message=f'Recorded interaction route {route}.', new_document=lesson.board_document, metadata={'kind': 'board_interaction_session', 'user_message': request.message, 'assistant_message': chatbot_message, 'assistant_message_source': 'interaction_session', 'document_changed': False, 'document_write_authorized': False, 'document_hash_before': document_hash, 'document_hash_after': document_hash, 'board_task_run_id': run_id, 'board_task_version_id': version_id, 'board_task_phase': phase, 'board_task_route': 'interact', 'interaction_route': route, 'interaction_session': task.interaction_session, 'board_task_cleared': clear_active, 'active_board_task_sheet_after': None if clear_active else task.model_dump(mode='json'), 'decision_trace': trace, 'selected_model': selected_model.model_dump(mode='json'), 'ai_provider': selected_model.provider, 'ai_model': selected_model.model, 'agent_backend': selected_model.agent_backend, 'role_executions': roles})
    if not workspace_state.save_lesson_for_user_if_head(user_id, lesson, expected_branch_name=expected_branch, expected_head_commit_id=expected_head):
        raise ExistingBoardInteractionWorkflowError('The lesson changed while the interaction turn was being persisted')

def _build_response(*, lesson_id: str, user_id: str, task: BoardTaskRequirementSheet, active_task: BoardTaskRequirementSheet | None, phase: str, run_id: str, version_id: str, chatbot_message: str, activity: list[AgentActivityEvent], decision_reason: str) -> ChatResponse:
    workspace = workspace_state.load_workspace_for_user(user_id)
    package, lesson = workspace_state.find_lesson_package(workspace, lesson_id)
    return ChatResponse(chatbot_message=chatbot_message, decision_trace=DecisionTrace(intent_signals=['active_interaction_session'], matched_rules=['interaction_session_runtime'], selected_action='learning_need', target_resolver='persisted_interaction_target', sequence_mode='interaction_session', role_executed='chatbot' if chatbot_message else 'interaction_router', board_access='bounded_board_role', requirement_effect='updated', document_changed=False, reason=decision_reason), agent_activity=activity, learning_requirement_sheet=build_requirements(lesson.title), active_requirement_sheet=None, learning_clarification=LearningClarificationStatus(progress=0, label='', reason=''), board_task_sheet=task, active_board_task_sheet=active_task, board_task_run_id=run_id, board_task_version_id=version_id, board_task_phase=phase, board_decision=BoardDecision(action='no_change', reason=decision_reason), requirement_cleared=False, board_document_operation_status='none', course_package=workspace_state.package_view_for_lesson(workspace, package, lesson.id))

def _publish_task_update(callback: Callable[[dict[str, object]], None] | None, task: BoardTaskRequirementSheet, *, run_id: str, version_id: str, phase: str, clear_active: bool) -> None:
    if callback is None:
        return
    callback({'board_task_sheet': task.model_dump(mode='json'), 'active_board_task_sheet': None if clear_active else task.model_dump(mode='json'), 'board_task_run_id': run_id, 'board_task_version_id': version_id, 'board_task_phase': phase})

def _role_execution(role: str, model: AIModelSelection | None, input_scope: list[str]) -> dict[str, object]:
    return {'role': role, 'model': model.model_dump(mode='json') if model is not None else None, 'input_scope': input_scope, 'document_write_allowed': False}
