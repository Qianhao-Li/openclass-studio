"""Explicit per-file enhancement jobs; ordinary source readiness is independent."""
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_ingestion_jobs import source_ingestion_task_manager

def prepare_enhancement(service, source, *, expected_hash=None, task_manager=source_ingestion_task_manager):
    if source.metadata.get('source_backend') == 'local':
        raise GoogleSourceError('Cloud enhancement is unavailable in this local workspace. Ordinary source reading remains available.')

    def queue():
        current = service.state.current(source, active=False)
        if expected_hash and expected_hash != current.metadata.get('content_hash'):
            raise GoogleSourceError('The source version changed. Refresh before preparing enhanced search.')
        if current.metadata.get('search_ready'):
            return service.state.update(current, metadata={'enhancement_status': 'ready', 'enhancement_progress': 100, 'enhancement_error': ''})
        if current.metadata.get('enhancement_status') in {'queued', 'processing'}:
            return current
        if current.metadata.get('pending_source_version'):
            raise GoogleSourceError('A file replacement is pending. Finish or cancel it before preparing enhanced search.')
        if not current.metadata.get('original_read_ready'):
            raise GoogleSourceError('Wait for ordinary source reading to finish before preparing enhanced search.')
        key = (current.owner_user_id, current.package_id, current.id)
        if task_manager.is_active(key):
            raise GoogleSourceError('The ordinary source task is still running. Wait for it to finish or cancel it first.')
        service.config.validate(write=True)
        updated = service.state.update(current, metadata={'enhancement_status': 'queued', 'enhancement_progress': 0, 'enhancement_error': '', 'enhancement_requested': True, 'preserve_catalog_on_enhancement': True})
        service._job(updated.model_copy(update={'status': 'queued'}), 'enhancement_queued', 0)
        task_manager.submit(owner_user_id=current.owner_user_id, package_id=current.package_id, source_id=current.id, retry=True)
        return updated
    return service.store.coordinator.run_write(service.store.path, queue)

def cancel_enhancement(service, source, *, task_manager=source_ingestion_task_manager):
    current = service.state.current(source, active=False)
    if current.metadata.get('enhancement_status') not in {'queued', 'processing'}:
        return current
    task_manager.cancel((source.owner_user_id, source.package_id, source.id))
    current = service.state.current(source, active=False)
    cancelled = service.state.update(current, active=False, metadata={'enhancement_status': 'cancelled', 'enhancement_requested': False, 'enhancement_progress': 100, 'enhancement_error': 'Enhanced search preparation was cancelled; ordinary contents remain available.'})
    service._job(cancelled.model_copy(update={'status': 'cancelled'}), 'enhancement_cancelled', 100)
    return cancelled
