"""Turn-scoped teaching decisions and the final no-write invariant."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Iterator
from openclass_core.models import BoardDocument, ChatRequest, TurnDecision
from openclass_core.services.rich_document import document_changed
_decision: ContextVar[TurnDecision | None] = ContextVar('teaching_turn_decision', default=None)
_answer_mode: ContextVar[str | None] = ContextVar('teaching_answer_mode', default=None)
_answer_topic: ContextVar[str] = ContextVar('teaching_answer_topic', default='')

def current_teaching_decision() -> TurnDecision | None:
    return _decision.get()

def learning_answer_mode() -> str | None:
    decision = current_teaching_decision()
    if decision and decision.board_write_opt_out:
        return 'read_only'
    return _answer_mode.get()

@contextmanager
def bind_learning_answer(mode: str, *, topic: str='') -> Iterator[None]:
    token = _answer_mode.set(mode)
    topic_token = _answer_topic.set(topic)
    try:
        yield
    finally:
        _answer_topic.reset(topic_token)
        _answer_mode.reset(token)

def answer_instructions() -> str:
    if learning_answer_mode() == 'brief_then_offer':
        return 'Give a brief, useful answer to this incidental question. Do not deliver a full lesson. Then ask naturally whether the learner wants to expand this topic into board-based teaching. Do not claim that any board content was created or changed.'
    return 'The learner explicitly asked to keep the board unchanged this turn. Answer in chat only. Do not offer, promise, or attempt a board change. Do not infer unseen board content.'

def expansion_offer_metadata(*, request, lesson, message: str) -> dict:
    if learning_answer_mode() != 'brief_then_offer':
        return {}
    from openclass_core.services.pending_teaching_offer import build_topic_expansion_offer
    offer = build_topic_expansion_offer(invitation=message, topic=_answer_topic.get() or request.message, lesson=lesson, request=request)
    return {'pending_teaching_offer': offer.model_dump(mode='json'), 'pending_teaching_offer_transition': 'created'}

@contextmanager
def bind_teaching_decision(decision: TurnDecision) -> Iterator[None]:
    token = _decision.set(decision)
    try:
        yield
    finally:
        _decision.reset(token)

def default_board_teaching() -> bool:
    decision = current_teaching_decision()
    return bool(decision and decision.intent == 'learning_need' and (decision.learning_flow == 'teach') and (not decision.board_write_opt_out))

def without_write_controls(request: ChatRequest) -> ChatRequest:
    """A current no-write instruction supersedes stale client controls."""
    return request.model_copy(update={'board_generation_action': None, 'board_task_confirmation': None, 'interaction_mode': 'ask', 'post_generation_action': 'stop_after_generation', 'formula_ink': request.formula_ink.model_copy(update={'action': 'reference'}) if request.formula_ink else None})

def _learning_json(value):
    if isinstance(value, list):
        return [_learning_json(item) for item in value]
    if isinstance(value, dict):
        result = {key: _learning_json(item) for key, item in value.items()}
        if isinstance(result.get('attrs'), dict):
            result['attrs'].pop('blockId', None)
            if not result['attrs']:
                result.pop('attrs')
        return result
    return value

def assert_board_unchanged(before: BoardDocument, after: BoardDocument) -> None:
    if document_changed(before, after) or _learning_json(deepcopy(before.content_json)) != _learning_json(deepcopy(after.content_json)):
        raise RuntimeError('This turn explicitly forbids changing the board; the result was not saved.')
