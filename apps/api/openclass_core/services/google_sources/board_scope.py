"""Freeze current-turn source boundaries in an existing-board task's runtime state."""
from openclass_core.models import SourceQueryRef, SourceQueryScope
from openclass_core.services.source_evidence_store import source_evidence_store
from openclass_core.services.source_retrieval_preferences import auto_source_retrieval_enabled
from .config import GoogleSourceError

def _scope(refs):
    mode = 'chapter' if all((ref.source_chapter_id for ref in refs)) else 'page_range' if all((ref.page_start is not None for ref in refs)) else 'source' if len(refs) == 1 else 'sources'
    return SourceQueryScope(mode=mode, refs=refs)

def bind_task_source_scope(*, task, active_task, relation_to_active, request, owner_user_id, package_id, adapter, is_cancelled=None, store=None):
    """Only current input or a confirmed continuation of the same task can bind scope."""
    from .board_context import request_source_scope
    from openclass_core.services.source_retrieval_policy import decide_source_access, source_available_for_query
    store = store or source_evidence_store
    direct = request_source_scope(request)
    inherited = active_task is not None and task.task_id == active_task.task_id and (relation_to_active in {'continue', 'supplement'}) and (active_task.source_query_scope is not None)
    if request.board_task_confirmation is not None:
        task.source_query_scope = active_task.source_query_scope.model_copy(deep=True) if active_task is not None and active_task.source_query_scope is not None else None
        task.source_scope_origin = 'continued_task' if task.source_query_scope is not None else 'none'
        return
    enabled = auto_source_retrieval_enabled(owner_user_id, store.path)
    if direct is None and (not enabled):
        task.source_query_scope = None
        task.source_scope_origin = 'none'
        return
    if direct is None:
        focus = task.target_location
        references = [*([request.selection] if request.selection is not None else []), *request.selections]
        plan = decide_source_access(owner_user_id=owner_user_id, package_id=package_id, message=request.message, adapter=adapter, is_cancelled=is_cancelled, store=store, reference_context={'reference_kinds': sorted({item.kind for item in references}), 'board_task_action': task.requested_action, 'location_namespace': 'board'}, approved_target={'excerpt': focus.excerpt, 'heading_path': list(focus.heading_path)} if focus is not None else None)
        if inherited and (not (plan.decision.source_hint or plan.decision.chapter_number is not None or plan.decision.section_number is not None)):
            task.source_query_scope = active_task.source_query_scope.model_copy(deep=True)
            task.source_scope_origin = 'continued_task'
            return
        if plan.refs or plan.decision.explicit_source_request:
            if plan.clarification:
                raise GoogleSourceError(plan.clarification)
            direct = _scope(plan.refs) if plan.refs else SourceQueryScope(mode='all_ready_sources')
    if direct is None and inherited:
        task.source_query_scope = active_task.source_query_scope.model_copy(deep=True)
        task.source_scope_origin = 'continued_task'
        return
    if direct is None:
        task.source_query_scope = None
        task.source_scope_origin = 'none'
        return
    refs = list(direct.refs)
    if direct.mode == 'all_ready_sources':
        refs = [SourceQueryRef(source_ingestion_id=source.id, source_content_hash=str(source.metadata.get('content_hash') or '')) for source in store.list_sources(owner_user_id=owner_user_id, package_id=package_id) if source_available_for_query(source)]
    if not refs:
        raise GoogleSourceError('The current task has no verifiable source range.')
    task.source_query_scope = _scope(refs)
    task.source_scope_origin = 'current_request'
