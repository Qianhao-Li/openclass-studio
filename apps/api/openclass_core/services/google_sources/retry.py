"""Record terminal retry outcomes without replacing a usable published source."""
from __future__ import annotations
from .config import GoogleSourceError

def _finish(service, source, status, message):
    current = service.state.current(source, active=False)
    published_ready = current.status == 'ready'
    current = service.state.update(current, active=False, status='ready' if published_ready else status, error='' if published_ready else message, metadata={'update_status': status, 'update_error': message, **({'catalog_status': 'failed', 'catalog_ready': False, 'catalog_warning': message} if not current.metadata.get('catalog_ready') else {})})
    service._job(current.model_copy(update={'status': status, 'error': message}), status, 100, message)
    return current

def retry_source(ingestion, source, catalog_model=None):
    from openclass_core.services.source_ingestion_jobs import source_task_cancel_requested, SourceTaskNotActiveError
    service = ingestion.google_service
    candidate = service.work_source(source)
    try:
        result = ingestion._run_google_source_retry(candidate, catalog_model)
    except Exception as exc:
        message = 'Catalog retry failed. The original file and existing catalog have been preserved.'
        try:
            current = service.state.current(candidate, active=False)
            cancelled = source_task_cancel_requested() or current.status == 'cancelled' or isinstance(exc, SourceTaskNotActiveError)
            status = 'cancelled' if cancelled else 'failed'
            message = 'Catalog retry cancelled. The original file and existing catalog have been preserved.' if cancelled else str(exc) if isinstance(exc, GoogleSourceError) else 'Catalog retry failed. The original file and existing catalog have been preserved.'
            _finish(service, current, status, message)
        except GoogleSourceError:
            pass
        if isinstance(exc, GoogleSourceError):
            raise
        raise GoogleSourceError(message) from None
    if result.status in {'failed', 'cancelled', 'interrupted'}:
        result = _finish(service, result, result.status, result.error)
    return result
