from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from openclass_core.models import BoardFocusRef
InteractionRoute = Literal['continue_rule', 'rule_violation', 'exit_rule', 'new_task']
InteractionSessionState = Literal['active', 'exited', 'replaced']

class InteractionRule(BaseModel):
    rule_text: str = Field(min_length=1, max_length=4000)
    interaction_goal: str = Field(min_length=1, max_length=2000)
    compliant_input_description: str = Field(min_length=1, max_length=2000)
    assistant_behavior_instruction: str = Field(min_length=1, max_length=2000)

    @field_validator('rule_text', 'interaction_goal', 'compliant_input_description', 'assistant_behavior_instruction')
    @classmethod
    def require_nonblank_rule_fields(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError('interaction rule fields must not be blank')
        return normalized

class InteractionRouteDecision(BaseModel):
    input_event_id: str = Field(min_length=1, max_length=256)
    route: InteractionRoute
    reason: str = Field(default='', max_length=2000)
    progress_note: str = Field(default='', max_length=2000)
    correction_note: str = Field(default='', max_length=2000)

    @field_validator('input_event_id')
    @classmethod
    def require_input_event_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError('input_event_id must not be blank')
        return normalized

    @model_validator(mode='after')
    def require_violation_correction(self) -> 'InteractionRouteDecision':
        if self.route == 'rule_violation' and (not self.correction_note.strip()):
            raise ValueError('rule_violation requires a correction_note')
        return self

class InteractionProgressRecord(BaseModel):
    turn_number: int = Field(ge=1)
    input_event_id: str = Field(min_length=1, max_length=256)
    route: InteractionRoute
    reason: str = ''
    progress_note: str = ''
    correction_note: str = ''

class InteractionProgress(BaseModel):
    completed_rule_turns: int = Field(default=0, ge=0)
    rule_violation_count: int = Field(default=0, ge=0)
    last_route: InteractionRoute | None = None
    records: list[InteractionProgressRecord] = Field(default_factory=list)

class InteractionSession(BaseModel):
    session_id: str = Field(min_length=1, max_length=256)
    source_board_task_id: str = Field(min_length=1, max_length=256)
    source_board_task_run_id: str = Field(min_length=1, max_length=256)
    source_board_task_version_id: str = Field(min_length=1, max_length=256)
    target: BoardFocusRef
    interaction_rule: InteractionRule
    progress: InteractionProgress = Field(default_factory=InteractionProgress)
    current_state: InteractionSessionState = 'active'
    turn_count: int = Field(default=0, ge=0)
    reroute_count: int = Field(default=0, ge=0, le=1)
    processed_input_event_ids: list[str] = Field(default_factory=list)

    @field_validator('session_id', 'source_board_task_id', 'source_board_task_run_id', 'source_board_task_version_id')
    @classmethod
    def require_source_ids(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError('interaction session source ids must not be blank')
        return normalized

    @model_validator(mode='after')
    def validate_session_invariants(self) -> 'InteractionSession':
        if self.target.source != 'board':
            raise ValueError('interaction target must be a board focus')
        if not self.target.lesson_id or not self.target.document_id or (not self.target.segment_id):
            raise ValueError('interaction target must identify its lesson, document, and segment')
        if self.current_state == 'replaced' and self.reroute_count != 1:
            raise ValueError('a replaced interaction must record exactly one reroute')
        if self.current_state != 'replaced' and self.reroute_count != 0:
            raise ValueError('only a replaced interaction may record a reroute')
        if len(self.processed_input_event_ids) != self.turn_count:
            raise ValueError('turn_count must match processed input events')
        if len(set(self.processed_input_event_ids)) != len(self.processed_input_event_ids):
            raise ValueError('processed input event ids must be unique')
        if len(self.progress.records) != self.turn_count:
            raise ValueError('turn_count must match progress records')
        if self.progress.completed_rule_turns + self.progress.rule_violation_count > self.turn_count:
            raise ValueError('interaction progress counters exceed turn_count')
        if self.progress.records:
            if self.progress.last_route != self.progress.records[-1].route:
                raise ValueError('last_route must match the latest progress record')
        elif self.progress.last_route is not None:
            raise ValueError('last_route requires at least one progress record')
        return self

class InteractionTransition(BaseModel):
    route: InteractionRoute
    session: InteractionSession
    transition_applied: bool
    should_continue_rule: bool = False
    should_reroute_original: bool = False
    reroute_dispatch_key: str | None = None
    document_changed: Literal[False] = False

    @model_validator(mode='after')
    def validate_reroute_contract(self) -> 'InteractionTransition':
        if self.should_reroute_original:
            if self.route != 'new_task' or self.session.current_state != 'replaced':
                raise ValueError('only a new_task transition may reroute the original input')
            if not self.reroute_dispatch_key:
                raise ValueError('rerouting requires an idempotent dispatch key')
        elif self.reroute_dispatch_key is not None:
            raise ValueError('reroute_dispatch_key is only valid when rerouting')
        return self

def transition_interaction(session: InteractionSession, decision: InteractionRouteDecision) -> InteractionTransition:
    """Apply one already-classified interaction route without touching the board."""
    current = InteractionSession.model_validate(session.model_dump(mode='python'))
    if current.current_state != 'active' or decision.input_event_id in current.processed_input_event_ids:
        return InteractionTransition(route=decision.route, session=current, transition_applied=False)
    next_session = current.model_copy(deep=True)
    next_turn = next_session.turn_count + 1
    next_session.turn_count = next_turn
    next_session.processed_input_event_ids.append(decision.input_event_id)
    next_session.progress.last_route = decision.route
    next_session.progress.records.append(InteractionProgressRecord(turn_number=next_turn, input_event_id=decision.input_event_id, route=decision.route, reason=decision.reason.strip(), progress_note=decision.progress_note.strip(), correction_note=decision.correction_note.strip()))
    should_continue_rule = False
    should_reroute_original = False
    reroute_dispatch_key: str | None = None
    if decision.route == 'continue_rule':
        next_session.progress.completed_rule_turns += 1
        should_continue_rule = True
    elif decision.route == 'rule_violation':
        next_session.progress.rule_violation_count += 1
        should_continue_rule = True
    elif decision.route == 'exit_rule':
        next_session.current_state = 'exited'
    else:
        next_session.current_state = 'replaced'
        next_session.reroute_count = 1
        should_reroute_original = True
        reroute_dispatch_key = f'{next_session.session_id}:{decision.input_event_id}'
    validated_session = InteractionSession.model_validate(next_session.model_dump(mode='python'))
    return InteractionTransition(route=decision.route, session=validated_session, transition_applied=True, should_continue_rule=should_continue_rule, should_reroute_original=should_reroute_original, reroute_dispatch_key=reroute_dispatch_key)
