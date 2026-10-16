from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from typing import Literal
from pydantic import BaseModel
from openclass_core.models import AgentActivityEvent
from openclass_core.services import workspace_state
from openclass_core.services.ai_execution_adapter import AIExecutionAdapter, CodexAIExecutionAdapter
from openclass_core.services.board_heading_outline import BoardHeadingTargetError, board_heading_outline_payload, build_board_heading_teaching_units
from openclass_core.services.rich_document import document_to_markdown
from openclass_core.services.turn_intent import has_explicit_document_mutation_request
TEACHING_TURN_DECISION_INSTRUCTIONS = "\nYou are TurnDecision for ordered board teaching in OpenClass. You do not teach, edit, or write the\nboard. Decide whether the learner's current message explicitly starts, continues, or restarts an\nordered explanation of the current board. A broad heading request should start an ordered sequence\nwhen that heading has child headings. A request for one specific leaf heading remains an ordinary\ntargeted explanation unless the learner explicitly asks for ordered or sequential teaching. While\nan ordered sequence is active, a short request to continue advances it, but a new question, a\nlocation-specific explanation, or any write/edit/generate request must return `none`. For `start`,\ncopy one exact heading string from the supplied outline into `target_heading`; leave it empty only\nwhen the learner clearly asks to start from the whole board. Return no learner-facing prose.\n"

class BoardTeachingTurnDecision(BaseModel):
    action: Literal['none', 'start', 'continue', 'restart'] = 'none'
    target_heading: str = ''
    reason: str = ''

@dataclass(frozen=True)
class BoardTeachingDecisionResult:
    decision: BoardTeachingTurnDecision = field(default_factory=BoardTeachingTurnDecision)
    activity: list[AgentActivityEvent] = field(default_factory=list)

def decide_board_teaching_turn(*, owner_user_id: str, lesson_id: str, model: str | None=None, adapter: AIExecutionAdapter | None=None, user_message: str, has_selection: bool) -> BoardTeachingDecisionResult:
    workspace = workspace_state.load_workspace_for_user(owner_user_id)
    _package, lesson = workspace_state.find_lesson_package(workspace, lesson_id)
    if has_explicit_document_mutation_request(user_message, has_board_selection=has_selection):
        return BoardTeachingDecisionResult()
    if not lesson.board_teaching_progress and (not _may_be_ordered_teaching_request(user_message)):
        return BoardTeachingDecisionResult()
    board_markdown = document_to_markdown(lesson.board_document)
    outline = board_heading_outline_payload(board_markdown)
    if not outline:
        return BoardTeachingDecisionResult()
    if not has_selection and _explicit_first_section_start(user_message):
        return BoardTeachingDecisionResult(decision=BoardTeachingTurnDecision(action='start', target_heading='', reason='The learner explicitly requested teaching from the first board section.'))
    guide = lesson.board_teaching_guide
    progress = lesson.board_teaching_progress
    if adapter is None:
        if not model:
            raise ValueError('A model or AI execution adapter is required')
        adapter = CodexAIExecutionAdapter(owner_user_id=owner_user_id, model=model)
    try:
        response = adapter.parse_structured(system_prompt=TEACHING_TURN_DECISION_INSTRUCTIONS, user_prompt=json.dumps({'user_message': user_message, 'has_current_selection': has_selection, 'active_sequence': bool(progress and progress.waiting_for_continue), 'active_target_heading': guide.target_heading if guide else '', 'current_heading': guide.section_plans[progress.current_section_index].heading if guide and progress and (0 <= progress.current_section_index < len(guide.section_plans)) else '', 'heading_outline': outline, 'response_contract': BoardTeachingTurnDecision.model_json_schema()}, ensure_ascii=False), schema=BoardTeachingTurnDecision)
    except Exception:
        return BoardTeachingDecisionResult()
    decision = BoardTeachingTurnDecision.model_validate(response.output_parsed)
    if has_selection and decision.action == 'start' and (not decision.target_heading.strip()):
        decision = BoardTeachingTurnDecision(action='none', reason='A selection-specific explanation is not a whole-board ordered sequence.')
    if decision.action in {'continue', 'restart'} and (not progress):
        decision = BoardTeachingTurnDecision(action='start', target_heading=decision.target_heading, reason=decision.reason)
    if decision.action == 'continue' and progress and (not progress.waiting_for_continue):
        decision = BoardTeachingTurnDecision(action='none', reason='The active ordered teaching sequence is already complete.')
    if decision.action == 'start' and decision.target_heading.strip():
        try:
            build_board_heading_teaching_units(board_markdown, target_heading=decision.target_heading)
        except BoardHeadingTargetError:
            decision = BoardTeachingTurnDecision(action='none', reason='The proposed heading does not resolve uniquely in the current board.')
    return BoardTeachingDecisionResult(decision=decision, activity=response.activity)

def _may_be_ordered_teaching_request(message: str) -> bool:
    normalized = re.sub('\\s+', '', message or '').casefold()
    return bool(re.search('依次|按顺序|逐(?:项|节|个|段)?讲|从头|从开头|开始(?:为我|给我|帮我)?讲(?:解)?|继续(?:为我|给我|帮我)?讲(?:解)?|接着讲|下一(?:节|项|部分).{0,8}讲|重来.{0,8}讲|讲解|解释|说明', normalized) or re.search('inorder|stepbystep|start(?:the)?teaching|continueteaching|teach(?:the)?next(?:section|item)|restart(?:the)?teaching|teach|explain', normalized))

def _explicit_first_section_start(message: str) -> bool:
    normalized = re.sub('\\s+', '', message or '').casefold()
    return bool(re.search('(?:从)?(?:第一(?:节|章|部分|项)|开头|头)(?:开始)?(?:为我|给我|帮我)?(?:讲|讲解|教学|解释|说明)', normalized) or re.search('(?:start|teach|explain)(?:from|at)?(?:the)?(?:firstsection|beginning|start)', normalized))
