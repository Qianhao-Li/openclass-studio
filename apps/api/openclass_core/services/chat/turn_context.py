from __future__ import annotations
from typing import Literal
from openclass_core.models import ChatRequest, ConversationTurn, SelectionRef
from openclass_core.services.turn_intent import BoardWriteDecision, board_write_policy_prompt
BoardState = Literal['empty', 'non_empty']

def conversation_context(conversation: list[ConversationTurn]) -> str:
    lines = [f'{turn.role}: {turn.content.strip()}' for turn in conversation[-12:] if turn.content.strip()]
    return '\n'.join(lines)[-12000:]

def selection_context(selection: SelectionRef | None) -> str:
    if selection is None or selection.kind == 'source':
        return ''
    details = [f'kind: {selection.kind}', f'excerpt: {selection.excerpt}']
    if selection.heading_path:
        details.append(f"heading path: {' > '.join(selection.heading_path)}")
    if selection.source_title:
        details.append(f'source title: {selection.source_title}')
    if selection.source_chapter_title:
        details.append(f'source chapter: {selection.source_chapter_title}')
    if selection.source_page_range:
        details.append(f'source pages: {selection.source_page_range}')
    return '\n'.join(details)

def selection_contexts(request: ChatRequest) -> list[str]:
    selections = request.selections or ([request.selection] if request.selection is not None else [])
    return [context for selection in selections if (context := selection_context(selection))]

def board_state(content_text: str) -> BoardState:
    return 'empty' if not content_text.strip() else 'non_empty'

def board_state_context(board_state: BoardState) -> str:
    if board_state == 'empty':
        return 'Board state (computed by OpenClass): EMPTY.\nThe right-side board contains no learning content. For a teaching request, create the initial board before giving substantive teaching content.'
    return 'Board state (computed by OpenClass): NON_EMPTY.\nThe right-side board already contains learning content. Read it before responding and keep teaching grounded in it.'

def turn_prompt(request: ChatRequest, *, is_new_thread: bool, board_state: BoardState, verified_source_context: str='', board_write_decision: BoardWriteDecision | None=None, pending_board_write_offer: dict[str, str] | None=None) -> str:
    sections: list[str] = []
    sections.append(f'Interaction mode: {request.interaction_mode}')
    sections.append(board_state_context(board_state))
    if board_write_decision is not None:
        sections.append(board_write_policy_prompt(board_write_decision, pending_board_write_offer))
    if is_new_thread:
        conversation = conversation_context(request.conversation)
        if conversation:
            sections.append(f'Conversation already visible to the user:\n{conversation}')
    selection_context_values = selection_contexts(request)
    if selection_context_values:
        rendered = '\n\n'.join((f'Reference {index}:\n{context}' for index, context in enumerate(selection_context_values, start=1)))
        sections.append(f'Current user board references (ordered; use every reference without replacing earlier ones):\n{rendered}')
    if verified_source_context:
        sections.append(f'Verified source context (mandatory for this turn):\n{verified_source_context}')
    if request.formula_ink is not None and request.formula_ink.source_latex:
        sections.append(f'Formula context:\naction: {request.formula_ink.action}\nlatex: {request.formula_ink.source_latex}')
    sections.append(f'Current user message:\n{request.message}')
    return '\n\n'.join(sections)
