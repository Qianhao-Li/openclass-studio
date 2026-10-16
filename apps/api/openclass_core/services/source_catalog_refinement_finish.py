"""Finalize one refinement under the same source write boundary as cancellation."""
from __future__ import annotations
from openclass_core.services.google_sources.config import GoogleSourceError

def finish_refinement(service, source, runtime, request_id, *, failure_reason=None):
    from openclass_core.services.source_catalog_refinement import completion_reason
    from openclass_core.services.source_ingestion_jobs import source_task_cancel_requested

    def finish():
        try:
            current = service.state.current(source, active=False)
        except GoogleSourceError:
            return service.store.get_source(owner_user_id=source.owner_user_id, package_id=source.package_id, source_id=source.id) or source
        task = dict(current.metadata.get('catalog_refinement', {}))
        if task.get('request_id') != request_id or task.get('content_hash') != current.metadata.get('content_hash') or task.get('source_version') != current.metadata.get('source_version'):
            return current
        reason = failure_reason or task.get('execution', {}).get('stop_reason') or completion_reason(current, task) or 'no_progress'
        if source_task_cancel_requested() or task.get('status') == 'cancelled':
            reason = 'cancelled'
        if runtime is not None:
            runtime.stop(reason)
            current = service.state.current(source, active=False)
            task = dict(current.metadata['catalog_refinement'])
        completed = reason in {'completed', 'completed_with_limits'}
        status = 'ready' if completed else 'cancelled' if reason == 'cancelled' else 'partial' if reason in {'budget_exhausted', 'call_limit', 'step_timeout', 'no_progress'} else 'failed'
        task.update(status=status, requested=False)
        if completed:
            task['completed_validation_version'] = 2
            if not task.get('target_chapter_id'):
                from openclass_core.services.source_catalog_pagination import PAGE_MAPPING_POLICY_VERSION
                task['completed_page_mapping_policy_version'] = PAGE_MAPPING_POLICY_VERSION
        message = '' if completed else 'Pi catalog refinement stopped (' + reason + '). Saved progress is available to continue.'
        current = service.state.update(current, active=False, metadata={'catalog_refinement': task, 'catalog_refinement_status': status, 'catalog_status': 'ready' if completed or current.metadata.get('catalog_ready') else status, 'catalog_progress': 100, 'catalog_error': message, 'catalog_warning': message, 'update_status': 'ready' if completed else status, 'update_error': message})
        service._job(current.model_copy(update={'status': 'ready' if status == 'partial' else status}), 'pi_catalog_refinement_' + status, 100, message)
        return current
    return service.store.coordinator.run_write(service.store.path, finish)
