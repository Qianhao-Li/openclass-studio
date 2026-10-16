from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable, Literal
from pydantic import BaseModel, Field
from openclass_core.models import AgentActivityEvent, BoardExplanationDirective, BoardFocusRef, BoardSectionTeachingPlan, BoardTaskRequirementSheet, BoardTeachingGuide, BoardTeachingProgress, SectionTeachingProgressView, new_id
from openclass_core.services import workspace_state
from openclass_core.services.ai_execution_adapter import AIExecutionAdapter, CodexAIExecutionAdapter
from openclass_core.services.board_heading_outline import build_board_heading_teaching_units
from openclass_core.services.history import commit_operations, current_head_commit
from openclass_core.services.rich_document import document_to_markdown
BOARD_DIRECTIVE_INSTRUCTIONS = '\nYou are the Board AI in OpenClass. You do not talk to the learner and you do not edit the board.\nAuthorize a bounded explanation only when the supplied title-scoped target excerpt supports it. The directive\nmust keep the Chatbot inside that excerpt, identify a useful teaching order, and forbid inventing\nfacts that are absent from the target. Return needs_clarification or blocked when the excerpt is not\nusable. Do not output learner-facing prose.\n'
_CHATBOT_EXPLANATION_CORE_INSTRUCTIONS = '\nYou are the learner-facing Chatbot in OpenClass. The Board AI has already selected and authorized\none title-scoped teaching unit. Explain only from the supplied directive. Do not claim that you read the whole\nboard, do not add unsupported facts, do not write or edit the board, and do not ask whether to begin.\nThe target title has already been resolved mechanically from the saved board. Begin teaching that exact target\nimmediately. Never ask which section, unit, title, or "first section" the learner means, and do not reinterpret\nthe learner\'s original wording as an unresolved request. Do not ask a clarification question in this response.\nTeach with the warmth, energy, and attentiveness of an excellent teacher in a live conversation. Address the\nlearner directly, respond to the goals or starting point that are actually present in the directive, and use\nnatural transitions instead of mechanically restating the excerpt item by item. Keep the tone genuinely\nencouraging without canned greetings, exaggerated praise, slogans, or performative enthusiasm.\n\nWhen `teaching_context.is_opening_unit` is true, make the first paragraph feel like the beginning of a shared\nlearning experience: welcome the learner into the topic, connect it to something they want to understand or\nbe able to do, and create a clear sense of where the lesson is going. End with an inviting bridge into the next\nunit when one exists. Generate this language from the supplied directive; never reuse a fixed opening template.\nFor later units, continue naturally from the current teaching sequence instead of greeting the learner again.\n\nKeep authorization limits invisible in learner-facing prose. Never open with a scope disclaimer, a statement\nthat the excerpt is only a learning objective or is not the complete content, or an explanation of what the\nsystem is forbidden to cover. Obey those limits silently. Teach the current title scope naturally and stop\nbefore the next title so the learner can continue later.\nAlso return 2 to 4 concise, context-specific `follow_up_suggestions` that the learner could send as\ntheir next turn. They are proposals, not executed actions, and must stay within the authorized\ntitle scope or ask to continue through the normal workflow. Do not use a fixed generic menu.\n'
CHATBOT_EXPLANATION_INSTRUCTIONS = _CHATBOT_EXPLANATION_CORE_INSTRUCTIONS.strip()
CHATBOT_EXPLANATION_TEXT_INSTRUCTIONS = (_CHATBOT_EXPLANATION_CORE_INSTRUCTIONS.rsplit('Also return', maxsplit=1)[0] + 'Return only the learner-facing explanation as plain text, without JSON or Markdown fences.').strip()

class _LearnerExplanation(BaseModel):
    chatbot_message: str
    follow_up_suggestions: list[str] = Field(default_factory=list, max_length=4)

@dataclass(frozen=True)
class AutoTeachingResult:
    status: Literal['succeeded', 'failed']
    chatbot_message: str = ''
    follow_up_suggestions: list[str] = field(default_factory=list)
    failure_reason: str | None = None
    board_task: BoardTaskRequirementSheet | None = None
    board_task_run_id: str | None = None
    board_task_version_id: str | None = None
    progress: SectionTeachingProgressView | None = None
    activity: list[AgentActivityEvent] = field(default_factory=list)
    chatbot_streamed: bool = False

def start_auto_board_teaching(*, owner_user_id: str, lesson_id: str, model: str | None=None, adapter: AIExecutionAdapter | None=None, on_delta: Callable[[str], None] | None=None, on_activity: Callable[[AgentActivityEvent], None] | None=None, is_cancelled: Callable[[], bool] | None=None) -> AutoTeachingResult:
    return _teach_section(owner_user_id=owner_user_id, lesson_id=lesson_id, model=model, adapter=adapter, requested_index=0, rebuild_guide=True, trigger='post_generation_auto_teach', target_heading='', user_message='', on_delta=on_delta, on_activity=on_activity, is_cancelled=is_cancelled)

def start_board_teaching(*, owner_user_id: str, lesson_id: str, model: str | None=None, adapter: AIExecutionAdapter | None=None, target_heading: str, user_message: str, on_delta: Callable[[str], None] | None=None, on_activity: Callable[[AgentActivityEvent], None] | None=None, is_cancelled: Callable[[], bool] | None=None) -> AutoTeachingResult:
    return _teach_section(owner_user_id=owner_user_id, lesson_id=lesson_id, model=model, adapter=adapter, requested_index=0, rebuild_guide=True, trigger='board_teaching_start', target_heading=target_heading, user_message=user_message, on_delta=on_delta, on_activity=on_activity, is_cancelled=is_cancelled)

def continue_board_teaching(*, owner_user_id: str, lesson_id: str, model: str | None=None, adapter: AIExecutionAdapter | None=None, restart: bool, user_message: str='', on_delta: Callable[[str], None] | None=None, on_activity: Callable[[AgentActivityEvent], None] | None=None, is_cancelled: Callable[[], bool] | None=None) -> AutoTeachingResult:
    workspace = workspace_state.load_workspace_for_user(owner_user_id)
    _package, lesson = workspace_state.find_lesson_package(workspace, lesson_id)
    current_index = lesson.board_teaching_progress.current_section_index if lesson.board_teaching_progress else -1
    target_heading = lesson.board_teaching_guide.target_heading if lesson.board_teaching_guide else ''
    return _teach_section(owner_user_id=owner_user_id, lesson_id=lesson_id, model=model, adapter=adapter, requested_index=0 if restart else current_index + 1, rebuild_guide=restart or lesson.board_teaching_guide is None, trigger='board_teaching_restart' if restart else 'board_teaching_continue', target_heading=target_heading, user_message=user_message, on_delta=on_delta, on_activity=on_activity, is_cancelled=is_cancelled)

def _teach_section(*, owner_user_id: str, lesson_id: str, model: str | None, adapter: AIExecutionAdapter | None, requested_index: int, rebuild_guide: bool, trigger: str, target_heading: str, user_message: str, on_delta: Callable[[str], None] | None, on_activity: Callable[[AgentActivityEvent], None] | None, is_cancelled: Callable[[], bool] | None) -> AutoTeachingResult:
    if adapter is None:
        if not model:
            raise ValueError('A model or AI execution adapter is required')
        adapter = CodexAIExecutionAdapter(owner_user_id=owner_user_id, model=model)
    workspace = workspace_state.load_workspace_for_user(owner_user_id)
    _package, lesson = workspace_state.find_lesson_package(workspace, lesson_id)
    branch_name = lesson.history_graph.current_branch
    base_commit_id = current_head_commit(lesson).id
    board_text = document_to_markdown(lesson.board_document)
    board_hash = _text_hash(board_text)
    guide = lesson.board_teaching_guide
    if rebuild_guide or guide is None or guide.board_snapshot_hash != board_hash:
        guide = _build_teaching_guide(document_id=lesson.board_document.id, board_title=lesson.board_document.title, board_text=board_text, target_heading=target_heading)
    if requested_index < 0 or requested_index >= len(guide.section_plans):
        return AutoTeachingResult(status='failed', failure_reason='no_teachable_section')
    section = guide.section_plans[requested_index]
    focus = _focus_for_section(lesson_id=lesson.id, document_id=lesson.board_document.id, section=section)
    task = BoardTaskRequirementSheet(location_kind='target_range', target_hint=section.heading, target_location=focus, location_status='resolved', requested_action='explain', question_or_topic=section.heading, missing_items=[], progress=100, confirmation_status='confirmed')
    run_id = new_id('boardtaskrun')
    version_id = new_id('boardtaskver')
    lesson.board_task_requirements = task
    lesson.board_teaching_guide = guide
    lesson.board_teaching_progress = BoardTeachingProgress(board_document_id=lesson.board_document.id, board_snapshot_hash=board_hash, current_section_index=requested_index, completed_section_indexes=list(range(requested_index)), waiting_for_continue=False, target_heading_path=guide.target_heading_path, current_heading_path=section.heading_path)
    decision_trace = {'intent_signals': [trigger], 'matched_rules': ['heading_tree_ordered_teaching'], 'selected_action': 'explain', 'target_resolver': 'board_heading_outline', 'sequence_mode': guide.sequence_mode, 'role_executed': 'board_ai', 'document_changed': False, 'reason': 'The current board has a deterministic first or next title-scoped teaching unit.'}
    commit_operations(lesson, operations=[], label='Board explanation task ready', message='Prepared a bounded board explanation task.', new_document=lesson.board_document, metadata={'kind': 'board_task_requirement_ready', 'user_message': user_message, 'document_changed': False, 'board_task_run_id': run_id, 'board_task_version_id': version_id, 'board_task_phase': 'ready', 'board_task_route': 'explain', 'active_board_task_sheet_after': task.model_dump(mode='json'), 'board_task_cleared': False, 'resolved_focus': focus.model_dump(mode='json'), 'decision_trace': decision_trace})
    if not workspace_state.save_lesson_for_user_if_head(owner_user_id, lesson, expected_branch_name=branch_name, expected_head_commit_id=base_commit_id):
        return AutoTeachingResult(status='failed', failure_reason='lesson_changed')
    ready_commit_id = current_head_commit(lesson).id
    activity: list[AgentActivityEvent] = []
    chatbot_streamed = False
    try:
        from openclass_core.services.teaching_original_sources import first_teaching_original_context
        from openclass_core.services.source_content_contract import source_context_image_kwargs
        original_context = first_teaching_original_context(section.board_excerpt, owner_user_id=owner_user_id)
        original_payload = {'original_source_context': str(original_context)} if original_context else {}
        directive_response = adapter.parse_structured(system_prompt=BOARD_DIRECTIVE_INSTRUCTIONS + ('\nFor this current source-referenced turn, verify factual support against the supplied original source inputs. The board excerpt defines only the bounded teaching target.' if original_context else ''), user_prompt=json.dumps({'target_focus': focus.model_dump(mode='json'), 'target_excerpt': section.board_excerpt, 'requested_action': 'explain', 'response_contract': BoardExplanationDirective.model_json_schema(), **original_payload}, ensure_ascii=False), schema=BoardExplanationDirective, on_activity=on_activity, **source_context_image_kwargs(original_context))
        activity.extend(directive_response.activity)
        directive = BoardExplanationDirective.model_validate(directive_response.output_parsed)
        if directive.status != 'approved' or not directive.target_excerpt.strip():
            raise RuntimeError(directive.reason or 'board_explanation_not_approved')
        explanation_payload = {**original_payload, 'board_explanation_directive': directive.model_dump(mode='json'), 'resolved_target_heading': section.heading, 'teaching_context': {'is_opening_unit': requested_index == 0, 'has_next_unit': requested_index + 1 < len(guide.section_plans)}}
        complete_text = getattr(adapter, 'complete_text', None)
        if callable(complete_text):

            def publish_chat_delta(delta: str) -> None:
                nonlocal chatbot_streamed
                if not delta or on_delta is None:
                    return
                chatbot_streamed = True
                on_delta(delta)
            explanation_response = complete_text(system_prompt=CHATBOT_EXPLANATION_TEXT_INSTRUCTIONS + ("\nThe learner explicitly referenced source material this turn. Use the supplied original source contents and images as factual evidence; the authorized board excerpt only bounds what to teach. Do not replace originals with the board writer's wording." if original_context else ''), user_prompt=json.dumps(explanation_payload, ensure_ascii=False), is_cancelled=is_cancelled, on_activity=on_activity, on_text_delta=publish_chat_delta, **source_context_image_kwargs(original_context))
            activity.extend(explanation_response.activity)
            chatbot_message = explanation_response.output_text.strip()
            follow_up_suggestions: list[str] = []
        else:
            explanation_call = adapter.parse_structured if original_context else adapter.explain_from_directive
            explanation_response = explanation_call(system_prompt=CHATBOT_EXPLANATION_INSTRUCTIONS, user_prompt=json.dumps({**explanation_payload, 'response_contract': _LearnerExplanation.model_json_schema()}, ensure_ascii=False), schema=_LearnerExplanation, **source_context_image_kwargs(original_context))
            activity.extend(explanation_response.activity)
            explanation = _LearnerExplanation.model_validate(explanation_response.output_parsed)
            chatbot_message = explanation.chatbot_message.strip()
            follow_up_suggestions = [suggestion.strip() for suggestion in explanation.follow_up_suggestions[:4] if suggestion.strip()]
        if not chatbot_message:
            raise RuntimeError('empty_board_explanation')
    except Exception as exc:
        _record_auto_teaching_failure(owner_user_id=owner_user_id, lesson_id=lesson_id, branch_name=branch_name, expected_head_commit_id=ready_commit_id, run_id=run_id, version_id=version_id, task=task, reason=str(exc), decision_trace=decision_trace)
        return AutoTeachingResult(status='failed', failure_reason=str(exc), board_task=task, board_task_run_id=run_id, board_task_version_id=version_id, activity=activity)
    workspace = workspace_state.load_workspace_for_user(owner_user_id)
    _package, lesson = workspace_state.find_lesson_package(workspace, lesson_id)
    if current_head_commit(lesson).id != ready_commit_id:
        return AutoTeachingResult(status='failed', failure_reason='lesson_changed')
    lesson.board_task_requirements = None
    lesson.board_teaching_guide = guide
    lesson.board_teaching_progress = BoardTeachingProgress(board_document_id=lesson.board_document.id, board_snapshot_hash=board_hash, current_section_index=requested_index, completed_section_indexes=list(range(requested_index + 1)), waiting_for_continue=requested_index + 1 < len(guide.section_plans), target_heading_path=guide.target_heading_path, current_heading_path=section.heading_path)
    progress = _progress_view(guide, requested_index)
    commit_operations(lesson, operations=[], label='Board-directed explanation', message='Chatbot explained the Board AI-authorized title-scoped unit.', new_document=lesson.board_document, metadata={'kind': 'board_directed_explanation', 'user_message': user_message, 'assistant_message': chatbot_message, 'assistant_message_source': 'chatbot_board_directed', 'follow_up_suggestions': follow_up_suggestions, 'document_changed': False, 'board_task_run_id': run_id, 'board_task_version_id': version_id, 'board_task_phase': 'consumed', 'board_task_route': 'explain', 'board_task_decision': 'explain', 'board_task_cleared': True, 'active_board_task_sheet_after': None, 'resolved_focus': focus.model_dump(mode='json'), 'board_explanation_directive': directive.model_dump(mode='json'), 'teaching_progress': progress.model_dump(mode='json'), 'decision_trace': {**decision_trace, 'role_executed': 'chatbot'}})
    if not workspace_state.save_lesson_for_user_if_head(owner_user_id, lesson, expected_branch_name=branch_name, expected_head_commit_id=ready_commit_id):
        return AutoTeachingResult(status='failed', failure_reason='lesson_changed')
    return AutoTeachingResult(status='succeeded', chatbot_message=chatbot_message, follow_up_suggestions=follow_up_suggestions, board_task=task, board_task_run_id=run_id, board_task_version_id=version_id, progress=progress, activity=activity, chatbot_streamed=chatbot_streamed)

def _build_teaching_guide(*, document_id: str, board_title: str, board_text: str, target_heading: str='') -> BoardTeachingGuide:
    units, target_path = build_board_heading_teaching_units(board_text, target_heading=target_heading)
    sections = [BoardSectionTeachingPlan(order_index=order_index, heading=unit.heading, heading_level=unit.heading_level, heading_path=unit.heading_path, parent_heading=unit.parent_heading, heading_order_index=unit.heading_order_index, line_start=unit.line_start, line_end=unit.line_end, has_child_headings=unit.has_child_headings, board_excerpt=unit.board_excerpt[:12000]) for order_index, unit in enumerate(units)]
    return BoardTeachingGuide(board_document_id=document_id, board_snapshot_hash=_text_hash(board_text), board_title=board_title, section_plans=sections, generation_rationale='deterministic_heading_tree_scales', target_heading=target_heading.strip(), target_heading_path=target_path, sequence_mode='heading_tree_preorder')

def _focus_for_section(*, lesson_id: str, document_id: str, section: BoardSectionTeachingPlan) -> BoardFocusRef:
    excerpt = section.board_excerpt.strip()
    return BoardFocusRef(source='board', lesson_id=lesson_id, document_id=document_id, kind='heading', heading_path=section.heading_path or [section.heading], excerpt=excerpt, text_hash=_text_hash(excerpt), excerpt_hash=_text_hash(excerpt), confidence=1.0, reason='Resolved from the deterministic board heading tree.', display_label=section.heading, order_start=section.heading_order_index, order_end=section.heading_order_index)

def _progress_view(guide: BoardTeachingGuide, index: int) -> SectionTeachingProgressView:
    has_next = index + 1 < len(guide.section_plans)
    return SectionTeachingProgressView(section_index=index, section_count=len(guide.section_plans), current_section_title=guide.section_plans[index].heading, has_next_section=has_next, waiting_for_continue=has_next, target_heading_path=guide.target_heading_path, current_heading_path=guide.section_plans[index].heading_path)

def _record_auto_teaching_failure(*, owner_user_id: str, lesson_id: str, branch_name: str, expected_head_commit_id: str, run_id: str, version_id: str, task: BoardTaskRequirementSheet, reason: str, decision_trace: dict[str, object]) -> None:
    workspace = workspace_state.load_workspace_for_user(owner_user_id)
    _package, lesson = workspace_state.find_lesson_package(workspace, lesson_id)
    if current_head_commit(lesson).id != expected_head_commit_id:
        return
    lesson.board_task_requirements = None
    commit_operations(lesson, operations=[], label='Automatic explanation failed', message='The generated board was preserved for an explanation retry.', new_document=lesson.board_document, metadata={'kind': 'auto_explain_failed', 'document_changed': False, 'board_task_run_id': run_id, 'board_task_version_id': version_id, 'board_task_phase': 'not_executed', 'board_task_route': 'explain', 'board_task_cleared': True, 'active_board_task_sheet_after': None, 'failed_board_task_sheet': task.model_dump(mode='json'), 'failure_reason': reason, 'decision_trace': {**decision_trace, 'role_executed': 'board_ai'}})
    workspace_state.save_lesson_for_user_if_head(owner_user_id, lesson, expected_branch_name=branch_name, expected_head_commit_id=expected_head_commit_id)

def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()
