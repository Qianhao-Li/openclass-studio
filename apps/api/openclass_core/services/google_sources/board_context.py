"""RAG can consume a board excerpt only after the board-side role approves it."""
from __future__ import annotations
from openclass_core.services.source_retrieval_preferences import auto_source_retrieval_enabled

def source_context_provider(*, owner_user_id, lesson_id, question, adapter, package_id=None, is_cancelled=None, on_activity=None, source_scope=None, citation_style='numbers', require_complete=False, allow_deferred=False):

    def provide(approved_target):
        from openclass_core.services.teaching_attachments import with_turn_attachments
        if not auto_source_retrieval_enabled(owner_user_id) and (not (source_scope is not None and source_scope.refs)):
            return with_turn_attachments('')
        from openclass_core.services import workspace_state
        from openclass_core.services.existing_board.focus_resolver import MAX_APPROVED_BOARD_TARGET_CHARS
        from openclass_core.services.source_retrieval_policy import decide_source_access, source_access_context
        excerpt = getattr(approved_target, 'target_excerpt', getattr(approved_target, 'excerpt', '')) if approved_target is not None else ''
        if len(excerpt) > MAX_APPROVED_BOARD_TARGET_CHARS:
            raise ValueError('The authorized board excerpt exceeds the source retrieval context limit.')
        selected_package = package_id
        if selected_package is None:
            workspace = workspace_state.load_workspace_for_user(owner_user_id)
            package, _lesson = workspace_state.find_lesson_package(workspace, lesson_id)
            selected_package = package.id
        if source_scope is not None and source_scope.refs:
            from openclass_core.services.source_retrieval_policy import SourceAccessDecision, SourceAccessPlan
            action = 'read' if require_complete or any((ref.source_chapter_id or ref.page_start is not None or ref.source_range is not None for ref in source_scope.refs)) else 'search'
            plan = SourceAccessPlan(mode='explicit', decision=SourceAccessDecision(action=action, query=question, explicit_source_request=True), refs=list(source_scope.refs), explicit_selection=True, adapter=adapter)
        else:
            plan = decide_source_access(owner_user_id=owner_user_id, package_id=selected_package, message=question, adapter=adapter, is_cancelled=is_cancelled, approved_target={'excerpt': excerpt, 'heading_path': list(getattr(approved_target, 'heading_path', []) or [])})
            for event in plan.activity:
                if on_activity is not None:
                    on_activity(event)
        if plan.clarification:
            raise ValueError(plan.clarification)
        context = source_access_context(plan, owner_user_id=owner_user_id, package_id=selected_package, lesson_id=lesson_id, query=question, is_cancelled=is_cancelled, citation_style=citation_style)
        original_delivery = getattr(context, 'original_delivery', None)
        if original_delivery is not None:
            original_delivery.validate_current()
        if require_complete and source_scope is not None and source_scope.refs:
            from openclass_core.services.source_board_coverage import require_complete_board_evidence
            if plan.grounding is None:
                raise ValueError('No complete verified source content is available for this board task.')
            require_complete_board_evidence(plan.grounding.frozen_evidence)
        from openclass_core.services.original_role_context import materialize_original_context
        context = materialize_original_context(context, citation_style=citation_style, allow_deferred=allow_deferred)
        return with_turn_attachments(context)
    return provide

def request_source_scope(request):
    if request.source_query_scope is not None:
        return request.source_query_scope
    from openclass_core.models import SourceQueryRef, SourceQueryScope
    selections = [item for item in [request.selection, *request.selections] if item is not None and item.kind == 'source']
    if not selections:
        return None
    refs = [SourceQueryRef(source_ingestion_id=item.source_ingestion_id or '', source_content_hash=item.source_content_hash or '', source_chapter_id=item.source_chapter_id, catalog_version=item.catalog_version, page_start=item.source_page_start, page_end=item.source_page_end, source_range=item.source_range) for item in selections]
    mode = 'chapter' if all((ref.source_chapter_id for ref in refs)) else 'page_range' if all((ref.page_start is not None for ref in refs)) else 'source' if len(refs) == 1 else 'sources'
    return SourceQueryScope(mode=mode, refs=refs)
