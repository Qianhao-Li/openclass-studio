from __future__ import annotations
import json
from typing import Literal
from pydantic import BaseModel, Field
from openclass_core.services.ai_execution_adapter import AIExecutionAdapter
FOLLOW_UP_SUGGESTION_INSTRUCTIONS = '\nYou generate the clickable next-turn suggestions shown below one OpenClass assistant reply.\n\nReturn 2 to 4 concise, natural messages that the learner could send next. Every suggestion must be\nspecific to the supplied user message and assistant reply; do not use a reusable generic menu or\nfixed teaching template. Suggestions may ask for an example, a deeper explanation, a comparison,\nan application, a check for understanding, or the next useful action only when that move follows\nnaturally from this particular reply.\n\nThe suggestions are proposals, not executed actions. Never claim that the board was changed or that\nan explanation was authorized. If the assistant reply asks for a required clarification or choice,\nsuggestions must help answer that question rather than bypass it. A later click will be submitted as\na new user turn and must still pass OpenClass target resolution, confirmation, and write gates.\n\nWrite each suggestion as a complete learner utterance in the language used by the learner. Keep it\nunder 48 characters when practical. Avoid duplicates, vague labels, quotation marks, numbering,\nand trailing punctuation.\n'.strip()

class FollowUpSuggestionSet(BaseModel):
    suggestions: list[str] = Field(default_factory=list, min_length=2, max_length=4)

def _normalize_suggestions(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        suggestion = ' '.join(value.split()).strip(' \t\r\n"\'“”‘’')
        if not suggestion or suggestion in seen:
            continue
        seen.add(suggestion)
        normalized.append(suggestion[:120])
        if len(normalized) == 4:
            break
    return normalized if len(normalized) >= 2 else []

def generate_follow_up_suggestions(*, adapter: AIExecutionAdapter, user_message: str, assistant_message: str, board_state: Literal['empty', 'non_empty'], workflow_state: str) -> list[str]:
    if not assistant_message.strip():
        return []
    try:
        response = adapter.parse_structured(system_prompt=FOLLOW_UP_SUGGESTION_INSTRUCTIONS, user_prompt=json.dumps({'board_state': board_state, 'workflow_state': workflow_state, 'user_message': user_message, 'assistant_message': assistant_message, 'response_contract': FollowUpSuggestionSet.model_json_schema()}, ensure_ascii=False), schema=FollowUpSuggestionSet)
        parsed = FollowUpSuggestionSet.model_validate(response.output_parsed)
        return _normalize_suggestions(parsed.suggestions)
    except Exception:
        return []
