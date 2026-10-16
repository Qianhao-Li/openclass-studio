from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Any, Literal
BoardWriteAction = Literal['answer_then_offer', 'chat_without_offer', 'edit_now', 'confirm_offered_write', 'decline_offered_write']

@dataclass(frozen=True)
class BoardWriteDecision:
    action: BoardWriteAction
    intent_signals: tuple[str, ...]
    matched_rules: tuple[str, ...]
    reason: str

def pending_board_write_offer(lesson: Any) -> dict[str, str] | None:
    branch_name = lesson.history_graph.current_branch
    commits_by_id = {commit.id: commit for commit in lesson.history_graph.commits}
    branch = lesson.history_graph.branches.get(branch_name)
    pending = [branch.head_commit_id] if branch is not None else []
    visited: set[str] = set()
    while pending:
        commit_id = pending.pop()
        if commit_id in visited:
            continue
        visited.add(commit_id)
        commit = commits_by_id.get(commit_id)
        if commit is None or commit.branch_name != branch_name:
            continue
        metadata = commit.metadata if isinstance(commit.metadata, dict) else {}
        if 'pending_board_write_offer_after' in metadata:
            offer = metadata.get('pending_board_write_offer_after')
            if not isinstance(offer, dict) or offer.get('status') != 'awaiting_confirmation':
                return None
            question = offer.get('question')
            content = offer.get('content')
            if isinstance(question, str) and isinstance(content, str) and content.strip():
                return {'status': 'awaiting_confirmation', 'question': question, 'content': content}
            return None
        pending.extend(commit.parent_ids)
    return None

def board_write_policy_prompt(decision: BoardWriteDecision, pending_offer: dict[str, str] | None) -> str:
    lines = ['Board write policy (computed by OpenClass; mandatory):', f'action: {decision.action}', f'reason: {decision.reason}']
    if decision.action == 'confirm_offered_write' and pending_offer is not None:
        lines.extend(['Pending offered question:', pending_offer['question'], 'Pending offered content:', pending_offer['content']])
    return '\n'.join(lines)

def pending_board_write_offer_after(decision: BoardWriteDecision, *, question: str, content: str) -> dict[str, str] | None:
    if decision.action != 'answer_then_offer':
        return None
    return {'status': 'awaiting_confirmation', 'question': question, 'content': content}

def board_write_decision_trace(decision: BoardWriteDecision, *, document_write_authorized: bool, document_changed: bool) -> dict[str, object]:
    return {'intent_signals': list(decision.intent_signals), 'matched_rules': list(decision.matched_rules), 'selected_action': decision.action, 'target_resolver': 'current_board' if document_write_authorized else 'none', 'sequence_mode': 'single_turn', 'role_executed': 'board_editor' if document_changed else 'chatbot', 'document_changed': document_changed, 'reason': decision.reason}

def has_explicit_document_mutation_request(message: str, *, has_board_selection: bool=False) -> bool:
    normalized = re.sub('[\\s，。！？,.!?、；;：:]+', '', message or '').casefold()
    if not normalized:
        return False
    action = '(?:生成|续写|补写|写入|写进|新增|添加|扩展|完善|修改|改写|重写|替换|删除|generate|continue|extend|write|append|add|edit|rewrite|replace|delete)'
    target = '(?:板书|文档|讲义|章节|小节|标题|段落|表格|列表|公式|图片|这段|选中内容|board|document|lesson|section|heading|title|paragraph|table|list|formula|image)'
    modifier = '(?:再|去|直接|继续)?'
    conjunction = '(?:或者|以及|并且|或|和|与|及|并|也|or|and|nor)?'
    normalized = re.sub(f"(?:不要|不必|不用|无需|请勿|别|不|donot|don't|dont|never){modifier}{action}(?:{conjunction}{modifier}{action})*", '', normalized)
    if re.search(f'(?:是否应该|要不要|需不需要|有没有必要).{{0,16}}(?:{action}|{target})', normalized):
        return False
    if re.search(f'(?:如何|怎么|怎样|为什么|how|why).{{0,32}}(?:{action}|{target})', normalized):
        return False
    if re.search(f'{action}.{{0,256}}{target}', normalized) or re.search(f'{target}.{{0,256}}{action}', normalized):
        return True
    return has_board_selection and bool(re.search(action, normalized))

def decide_board_write_action(*, message: str, interaction_mode: str, has_pending_offer: bool, has_board_selection: bool=False) -> BoardWriteDecision:
    if interaction_mode == 'direct_edit':
        return BoardWriteDecision(action='edit_now', intent_signals=('direct_edit_mode',), matched_rules=('explicit_board_edit',), reason='The user selected direct document editing.')
    normalized = re.sub('[\\s，。！？,.!?、；;：:]+', '', message or '').casefold()
    if has_pending_offer and _is_standalone_write_confirmation(normalized):
        return BoardWriteDecision(action='confirm_offered_write', intent_signals=('pending_write_offer', 'write_confirmation'), matched_rules=('confirm_pending_board_write',), reason='The user explicitly accepted the pending board-write offer.')
    if has_pending_offer and _is_standalone_write_decline(normalized):
        return BoardWriteDecision(action='decline_offered_write', intent_signals=('pending_write_offer', 'write_decline'), matched_rules=('decline_pending_board_write',), reason='The user explicitly declined the pending board-write offer.')
    if has_explicit_document_mutation_request(message, has_board_selection=has_board_selection):
        return BoardWriteDecision(action='edit_now', intent_signals=('explicit_document_mutation',), matched_rules=('explicit_board_edit',), reason='The current message explicitly requests a board change.')
    if not _asks_for_learning_answer(message):
        return BoardWriteDecision(action='chat_without_offer', intent_signals=('ordinary_conversation',), matched_rules=('chat_without_board_offer',), reason='The turn is conversational and does not ask for a learning answer or board change.')
    return BoardWriteDecision(action='answer_then_offer', intent_signals=('default_question_first',), matched_rules=('answer_before_board_write',), reason='No explicit board-write authorization is present in this turn.')

def _is_standalone_write_confirmation(normalized: str) -> bool:
    return bool(re.fullmatch('(?:可以(?:写进去吧|写入板书)?|好|好的|行|同意|没问题|写吧|写进去吧|写入板书|加进去吧|yes|ok|okay|doit|writeit|addittotheboard)', normalized))

def _is_standalone_write_decline(normalized: str) -> bool:
    return bool(re.fullmatch('(?:不用|不要|先不(?:写|写入板书)?|不需要|别写|不写进板书|保持不变|no|nope|donotwriteit|donotaddittotheboard|leavetheboardunchanged)', normalized))

def _asks_for_learning_answer(message: str) -> bool:
    normalized = re.sub('\\s+', '', message or '').casefold()
    if not normalized:
        return False
    if '?' in message or '？' in message:
        return True
    return bool(re.search('为什么|什么|怎么|如何|是否|哪个|哪些|多少|什么意思|吗$|呢$|解释|讲解|说明|告诉我|why|what|how|which|explain|teach|tellme', normalized))

def has_explicit_board_location_cue(message: str, *, has_board_selection: bool=False) -> bool:
    """True when the learner points at a board place, not merely a topic."""
    if has_board_selection:
        return True
    normalized = re.sub('\\s+', '', message or '').casefold()
    if not normalized:
        return False
    return bool(re.search('这段|这段话|这段文字|选中|选区|板书|文档里|讲义里|这一节|这一段|这一章|上面那段|下面那段|刚才那|标题下|该节|该段|第[一二三四五六七八九十百零〇\\d]+[章节小节部分]|this(?:section|paragraph|heading|part)|(?:the)?(?:board|document|selection|selected(?:text|part|section)?)|section\\d+|paragraph\\d+', normalized))

def wants_direct_learning_answer_without_board_target(message: str) -> bool:
    """True when the learner asks to be answered now and skip board location work."""
    normalized = re.sub('[\\s，。！？,.!?、；;：:]+', '', message or '').casefold()
    if not normalized:
        return False
    return bool(re.search('(?:先|直接|别管位置|不用定位|先别(?:问位置|追问)|不要再问位置).{0,12}(?:讲|讲解|解释|说明|回答)|(?:讲|讲解|解释|说明|回答).{0,12}(?:就行|即可|就好|一下就行)|justexplain|explain(?:it|this)?(?:directly|first)|answerme(?:first|directly)?', normalized))

def has_explicit_board_interaction_request(message: str) -> bool:
    """True when the learner asks for a rule-bound board interaction loop."""
    normalized = re.sub('\\s+', '', message or '').casefold()
    if not normalized:
        return False
    return bool(re.search('练习|测验|角色扮演|情景对话|跟读|纠错|按规则|互动(?:一下|练习|问答)?|轮流|quiz|roleplay|flashcard|drill|practice(?:withme|together)', normalized))
