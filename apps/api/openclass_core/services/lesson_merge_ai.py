from __future__ import annotations
import json
from collections.abc import Callable
from typing import Any, Literal
from pydantic import BaseModel, Field
from openclass_core.models import AgentActivityEvent, LessonMergeConflictResolution, LessonMergeSession, new_id, now_iso
from openclass_core.services.ai_execution_adapter import AIExecutionAdapter, build_ai_execution_adapter
from openclass_core.services.lesson_merge import LessonMergeError, update_merge_session

class AIConflictDecision(BaseModel):
    conflict_id: str
    resolution: Literal['target', 'source', 'both', 'custom', 'clear']
    custom_value: Any = None
    explanation: str = Field(default='', max_length=500)

class AIConflictProposal(BaseModel):
    decisions: list[AIConflictDecision] = Field(default_factory=list)
AI_MERGE_SYSTEM_PROMPT = '\nYou resolve explicit conflicts in an OpenClass lesson merge draft. The deterministic merge has\nalready preserved every non-conflicting board block and runtime field. Decide only the supplied\nconflicts; never rewrite the full board. Treat rich-text blocks, tables, lists, formulas, images,\nand source-evidence blocks as atomic values. Preserve stable source identities and do not invent\nfacts, citations, runtime state, or chronology.\n\nFor every conflict_id return exactly one decision. Choose target, source, both, clear, or custom.\nUse both only when the two values can safely coexist in target-then-source order. Use custom only\nwhen a genuine synthesis is necessary, and return a value with the same data shape as the target\nor source value. Explanations must be concise decision notes, not hidden reasoning or chain of\nthought.\n'.strip()

def propose_ai_merge(session: LessonMergeSession, *, expected_version: int, adapter: AIExecutionAdapter | None=None, is_cancelled: Callable[[], bool] | None=None, on_activity: Callable[[AgentActivityEvent], None] | None=None) -> LessonMergeSession:
    if session.version != expected_version:
        raise LessonMergeError('The merge draft changed. Refresh and try again.')
    if session.status not in {'draft', 'ready', 'failed'}:
        raise LessonMergeError('This merge session cannot generate AI suggestions in its current state.')
    if session.ai_model is None:
        raise LessonMergeError('No text model is selected for this merge session.')
    unresolved = [conflict for conflict in session.conflicts if not conflict.resolved]
    if not unresolved:
        session.mode = 'ai'
        return session
    if is_cancelled is not None and is_cancelled():
        raise LessonMergeError('AI merge cancelled.')
    turn_id = new_id('mergeturn')

    def emit(event: AgentActivityEvent) -> None:
        session.agent_activity.append(event)
        if on_activity is not None:
            on_activity(event)
    emit(AgentActivityEvent(turn_id=turn_id, stage='build_context', label='Organizing unresolved merge conflicts', status='completed', role='lesson_merge', metadata={'conflict_count': len(unresolved)}))
    selected_adapter = adapter or build_ai_execution_adapter(session.ai_model, owner_user_id=session.owner_user_id)
    session.status = 'ai_running'
    session.mode = 'ai'
    try:
        result = selected_adapter.parse_structured(system_prompt=AI_MERGE_SYSTEM_PROMPT, user_prompt=json.dumps({'merge': {'base_commit_id': session.base_commit_id, 'target_branch': session.target_branch_name, 'target_head_commit_id': session.target_head_commit_id, 'source_branch': session.source_branch_name, 'source_head_commit_id': session.source_head_commit_id}, 'conflicts': [{'conflict_id': conflict.id, 'kind': conflict.kind, 'path': conflict.path, 'title': conflict.title, 'base_value': conflict.base_value, 'target_value': conflict.target_value, 'source_value': conflict.source_value} for conflict in unresolved]}, ensure_ascii=False, separators=(',', ':')), schema=AIConflictProposal, allow_live_web_search=False, on_activity=emit)
        if is_cancelled is not None and is_cancelled():
            session.status = 'draft'
            session.version += 1
            session.updated_at = now_iso()
            raise LessonMergeError('AI merge cancelled.')
        proposal = AIConflictProposal.model_validate(result.output_parsed)
        decision_by_id = {decision.conflict_id: decision for decision in proposal.decisions}
        expected_ids = {conflict.id for conflict in unresolved}
        if set(decision_by_id) != expected_ids:
            missing = sorted(expected_ids - set(decision_by_id))
            unknown = sorted(set(decision_by_id) - expected_ids)
            raise LessonMergeError(f'The conflicts returned by AI do not match the current draft. missing={missing} unknown={unknown}')
        resolutions = [LessonMergeConflictResolution(conflict_id=decision.conflict_id, resolution=decision.resolution, custom_value=decision.custom_value) for decision in proposal.decisions]
        session.status = 'draft'
        update_merge_session(session, expected_version=expected_version, resolutions=resolutions)
        session.audit['ai_proposal'] = {'provider': session.ai_model.provider, 'model': session.ai_model.model, 'reasoning_effort': session.ai_model.reasoning_effort, 'service_tier': session.ai_model.service_tier, 'input_target_head_commit_id': session.target_head_commit_id, 'input_source_head_commit_id': session.source_head_commit_id, 'decisions': [decision.model_dump(mode='json') for decision in proposal.decisions]}
        emit(AgentActivityEvent(turn_id=turn_id, stage='final', label='AI merge suggestions saved to the draft', status='completed', role='lesson_merge', metadata={'decision_count': len(proposal.decisions)}))
        session.updated_at = now_iso()
        return session
    except Exception:
        if session.status == 'ai_running':
            session.status = 'failed'
            session.version += 1
            session.updated_at = now_iso()
        raise
