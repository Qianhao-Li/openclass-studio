from __future__ import annotations
import json
from collections.abc import Callable
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from openclass_core.models import AgentActivityEvent, BoardFocusRef, BoardTaskRequirementSheet, new_id
from openclass_core.services.ai_execution_adapter import AIExecutionAdapter
from openclass_core.services.existing_board.focus_resolver import MAX_APPROVED_BOARD_TARGET_CHARS
from openclass_core.services.existing_board.interaction_session import InteractionRoute, InteractionRouteDecision, InteractionRule, InteractionSession, InteractionTransition, transition_interaction
MAX_INTERACTION_MESSAGE_CHARS = 12000
INTERACTION_RULE_BUILDER_INSTRUCTIONS = "\nYou are the Interaction Rule Builder for an existing OpenClass board task. Convert the learner's\nexplicit interaction requirement into the supplied structured InteractionRule. Define a general,\nexecutable rule, its goal, what compliant learner input means, and the assistant's next-turn\nbehavior. Use only the supplied learner requirement, task goal, and approved bounded target. Do\nnot answer the learner, invent board content, add subject-specific behavior, or use a fixed\ndialogue template.\n".strip()
INTERACTION_ROUTE_INSTRUCTIONS = '\nYou are the Interaction Route role for one active, structured interaction session. Classify the\ncurrent learner input as exactly one route:\n- continue_rule: the input follows or advances the active rule, including accepting or beginning\n  the rule on its first turn;\n- rule_violation: the input is still part of this interaction but does not meet the current rule;\n- exit_rule: the learner explicitly ends or leaves this interaction;\n- new_task: the learner introduces a separate task that must return to the main turn router.\n\nFor rule_violation, provide a concrete correction_note that the learner-facing Chatbot can apply\ninside the rule. Do not treat an ordinary rule mistake as a new task. Do not answer the learner,\nmodify the board, infer unseen board content, or override the supplied input event identity.\n'.strip()
CHATBOT_RULE_RESPONSE_INSTRUCTIONS = '\nYou are the learner-facing Chatbot continuing an active structured interaction. Respond according\nto the supplied InteractionRule and route decision. For continue_rule, perform only the next\nrule-defined assistant behavior. For rule_violation, correct the input within the active rule and\nkeep the interaction active. Use only the approved bounded target excerpt and supplied rule state;\ndo not infer adjacent board content, modify the board, start a separate task, or use a fixed\nopening or closing template.\n'.strip()
CHATBOT_EXIT_RESPONSE_INSTRUCTIONS = '\nYou are the learner-facing Chatbot closing a structured interaction after an explicit exit. Create\nan appropriate learner-facing ending from the supplied rule state and exit decision. Do not\ncontinue the rule, introduce a new task, modify the board, infer unseen board content, or use a\nfixed response template.\n'.strip()

class InteractionRuntimeError(ValueError):
    pass

class InteractionRouteModelDraft(BaseModel):
    model_config = ConfigDict(extra='forbid')
    route: InteractionRoute
    reason: str = Field(default='', max_length=2000)
    progress_note: str = Field(default='', max_length=2000)
    correction_note: str = Field(default='', max_length=2000)

    @field_validator('reason', 'progress_note', 'correction_note')
    @classmethod
    def normalize_notes(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode='after')
    def require_violation_correction(self) -> InteractionRouteModelDraft:
        if self.route == 'rule_violation' and (not self.correction_note):
            raise ValueError('rule_violation requires a correction_note')
        return self

class ExistingBoardInteractionResult(BaseModel):
    transition: InteractionTransition
    chatbot_message: str = ''
    rule_built: bool = False
    duplicate_input: bool = False
    should_reroute_original: bool = False
    original_input_for_reroute: str | None = None
    reroute_dispatch_key: str | None = None
    document_changed: Literal[False] = False
    activity: list[AgentActivityEvent] = Field(default_factory=list)

    @model_validator(mode='after')
    def validate_runtime_result(self) -> ExistingBoardInteractionResult:
        if self.should_reroute_original:
            if not self.transition.should_reroute_original:
                raise ValueError('runtime reroute must come from the validated transition')
            if not self.original_input_for_reroute or not self.reroute_dispatch_key:
                raise ValueError('runtime reroute must preserve its original input and key')
            if self.chatbot_message:
                raise ValueError('new-task rerouting must not consume the input with a reply')
        elif self.original_input_for_reroute is not None or self.reroute_dispatch_key is not None:
            raise ValueError('reroute payload is only valid for a new-task transition')
        if self.duplicate_input and self.transition.transition_applied:
            raise ValueError('a duplicate input cannot apply another transition')
        return self

def run_existing_board_interaction(*, adapter: AIExecutionAdapter, board_task: BoardTaskRequirementSheet, resolved_focus: BoardFocusRef, current_message: str, input_event_id: str, board_task_run_id: str, board_task_version_id: str, session: InteractionSession | None=None, initial_rule: InteractionRule | None=None, interaction_session_id: str | None=None, is_cancelled: Callable[[], bool] | None=None, on_activity: Callable[[AgentActivityEvent], None] | None=None, on_text_delta: Callable[[str], None] | None=None) -> ExistingBoardInteractionResult:
    """Run one bounded interaction turn without reading or mutating the board."""
    safe_focus = _validate_interaction_task(board_task, resolved_focus)
    message = _require_nonblank(current_message, field_name='current_message', max_length=MAX_INTERACTION_MESSAGE_CHARS)
    event_id = _require_nonblank(input_event_id, field_name='input_event_id', max_length=256)
    run_id = _require_nonblank(board_task_run_id, field_name='board_task_run_id', max_length=256)
    version_id = _require_nonblank(board_task_version_id, field_name='board_task_version_id', max_length=256)
    activity: list[AgentActivityEvent] = []
    rule_built = False
    if session is None:
        rule = initial_rule.model_copy(deep=True) if initial_rule is not None else None
        if rule is None:
            rule, rule_activity = _build_initial_rule(adapter=adapter, board_task=board_task, safe_focus=safe_focus, on_activity=on_activity)
            activity.extend(rule_activity)
            rule_built = True
        current_session = InteractionSession(session_id=_require_nonblank(interaction_session_id or new_id('interaction_session'), field_name='interaction_session_id', max_length=256), source_board_task_id=board_task.task_id, source_board_task_run_id=run_id, source_board_task_version_id=version_id, target=safe_focus, interaction_rule=rule)
        first_turn = True
    else:
        current_session = InteractionSession.model_validate(session.model_dump(mode='python'))
        _validate_restored_session(current_session, board_task=board_task, safe_focus=safe_focus, board_task_run_id=run_id, board_task_version_id=version_id)
        first_turn = current_session.turn_count == 0
    duplicate_record = next((record for record in current_session.progress.records if record.input_event_id == event_id), None)
    if duplicate_record is not None:
        duplicate_transition = transition_interaction(current_session, InteractionRouteDecision(input_event_id=event_id, route=duplicate_record.route, reason=duplicate_record.reason, progress_note=duplicate_record.progress_note, correction_note=duplicate_record.correction_note))
        return ExistingBoardInteractionResult(transition=duplicate_transition, rule_built=rule_built, duplicate_input=True, activity=activity)
    if current_session.current_state != 'active':
        raise InteractionRuntimeError('interaction session is no longer active')
    route_response = adapter.parse_structured(system_prompt=INTERACTION_ROUTE_INSTRUCTIONS, user_prompt=_json_payload({'current_message': message, 'is_first_turn': first_turn, 'interaction_rule': current_session.interaction_rule.model_dump(mode='json'), 'interaction_progress': current_session.progress.model_dump(mode='json'), 'approved_target': _safe_focus_payload(safe_focus), 'response_contract': InteractionRouteModelDraft.model_json_schema()}), schema=InteractionRouteModelDraft, allow_live_web_search=False, on_activity=on_activity)
    activity.extend(list(getattr(route_response, 'activity', [])))
    route_draft = InteractionRouteModelDraft.model_validate(route_response.output_parsed)
    decision = InteractionRouteDecision(input_event_id=event_id, route=route_draft.route, reason=route_draft.reason, progress_note=route_draft.progress_note, correction_note=route_draft.correction_note)
    transition = transition_interaction(current_session, decision)
    if not transition.transition_applied:
        raise InteractionRuntimeError('interaction transition was not applied')
    if transition.should_reroute_original:
        return ExistingBoardInteractionResult(transition=transition, rule_built=rule_built, should_reroute_original=True, original_input_for_reroute=message, reroute_dispatch_key=transition.reroute_dispatch_key, activity=activity)
    chatbot_response = adapter.complete_text(system_prompt=CHATBOT_EXIT_RESPONSE_INSTRUCTIONS if transition.route == 'exit_rule' else CHATBOT_RULE_RESPONSE_INSTRUCTIONS, user_prompt=_json_payload({'current_message': message, 'route_decision': decision.model_dump(mode='json'), 'interaction_rule': current_session.interaction_rule.model_dump(mode='json'), 'interaction_progress': transition.session.progress.model_dump(mode='json'), 'approved_target': _safe_focus_payload(safe_focus)}), is_cancelled=is_cancelled, on_activity=on_activity, on_text_delta=on_text_delta)
    activity.extend(list(getattr(chatbot_response, 'activity', [])))
    chatbot_message = str(chatbot_response.output_text).strip()
    if not chatbot_message:
        raise InteractionRuntimeError('Chatbot returned an empty interaction response')
    return ExistingBoardInteractionResult(transition=transition, chatbot_message=chatbot_message, rule_built=rule_built, activity=activity)

def _build_initial_rule(*, adapter: AIExecutionAdapter, board_task: BoardTaskRequirementSheet, safe_focus: BoardFocusRef, on_activity: Callable[[AgentActivityEvent], None] | None) -> tuple[InteractionRule, list[AgentActivityEvent]]:
    requirement = (board_task.special_interaction_requirements or '').strip()
    if not requirement or requirement.casefold() == 'none':
        raise InteractionRuntimeError('an explicit interaction requirement is required to build a session rule')
    response = adapter.parse_structured(system_prompt=INTERACTION_RULE_BUILDER_INSTRUCTIONS, user_prompt=_json_payload({'special_interaction_requirements': requirement, 'interaction_goal': board_task.question_or_topic.strip(), 'approved_target': _safe_focus_payload(safe_focus), 'response_contract': InteractionRule.model_json_schema()}), schema=InteractionRule, allow_live_web_search=False, on_activity=on_activity)
    return (InteractionRule.model_validate(response.output_parsed), list(getattr(response, 'activity', [])))

def _validate_interaction_task(board_task: BoardTaskRequirementSheet, resolved_focus: BoardFocusRef) -> BoardFocusRef:
    if board_task.location_status != 'resolved':
        raise InteractionRuntimeError('interaction requires a resolved board task')
    if board_task.requested_action != 'interact':
        raise InteractionRuntimeError('interaction runtime requires an interact task')
    if board_task.missing_items:
        raise InteractionRuntimeError('interaction runtime requires a complete board task')
    if not board_task.question_or_topic.strip():
        raise InteractionRuntimeError('interaction task requires a goal or topic')
    if board_task.target_location is None:
        raise InteractionRuntimeError('interaction task requires a target location')
    if resolved_focus.source != 'board':
        raise InteractionRuntimeError('interaction focus must come from the board')
    if not all((resolved_focus.lesson_id, resolved_focus.document_id, resolved_focus.segment_id, resolved_focus.text_hash, resolved_focus.excerpt.strip())):
        raise InteractionRuntimeError('interaction focus lacks stable target identity')
    if resolved_focus.confidence <= 0:
        raise InteractionRuntimeError('interaction focus lacks reliable confidence')
    if len(resolved_focus.excerpt) > MAX_APPROVED_BOARD_TARGET_CHARS:
        raise InteractionRuntimeError('interaction target exceeds the bounded scope')
    if _focus_identity(board_task.target_location) != _focus_identity(resolved_focus):
        raise InteractionRuntimeError('interaction focus does not match the task target')
    if board_task.target_location.excerpt.strip() != resolved_focus.excerpt.strip():
        raise InteractionRuntimeError('interaction target excerpt changed after resolution')
    return resolved_focus.model_copy(deep=True, update={'excerpt': resolved_focus.excerpt.strip(), 'before_text': '', 'after_text': ''})

def _validate_restored_session(session: InteractionSession, *, board_task: BoardTaskRequirementSheet, safe_focus: BoardFocusRef, board_task_run_id: str, board_task_version_id: str) -> None:
    if session.source_board_task_id != board_task.task_id:
        raise InteractionRuntimeError('interaction session belongs to another board task')
    if session.source_board_task_run_id != board_task_run_id:
        raise InteractionRuntimeError('interaction session run identity changed')
    if session.source_board_task_version_id != board_task_version_id:
        raise InteractionRuntimeError('interaction session version identity changed')
    if _focus_identity(session.target) != _focus_identity(safe_focus):
        raise InteractionRuntimeError('interaction session target changed')
    if session.target.excerpt.strip() != safe_focus.excerpt:
        raise InteractionRuntimeError('interaction session target excerpt changed')

def _focus_identity(focus: BoardFocusRef) -> tuple[str | None, ...]:
    return (focus.lesson_id, focus.document_id, focus.segment_id, focus.text_hash)

def _safe_focus_payload(focus: BoardFocusRef) -> dict[str, object]:
    return {'source': 'board', 'lesson_id': focus.lesson_id, 'document_id': focus.document_id, 'segment_id': focus.segment_id, 'kind': focus.kind, 'heading_path': list(focus.heading_path), 'excerpt': focus.excerpt, 'before_text': '', 'after_text': '', 'text_hash': focus.text_hash, 'excerpt_hash': focus.excerpt_hash, 'confidence': focus.confidence}

def _require_nonblank(value: str, *, field_name: str, max_length: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise InteractionRuntimeError(f'{field_name} must not be blank')
    if len(normalized) > max_length:
        raise InteractionRuntimeError(f'{field_name} exceeds its bounded length')
    return normalized

def _json_payload(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
