"""Explicit catalog-only recovery when the configured Google service is unavailable."""
from __future__ import annotations
import copy
from pathlib import Path
from openclass_core.models import AIModelSelection, now_iso
from openclass_core.services.ai_model_catalog import default_text_selection
from openclass_core.services.source_ingestion_jobs import source_task_cancel_requested
from .config import GoogleSourceError, GoogleSourceUnavailableError
from .storage import scope_prefix
from .original_cache import cache_original, delete_cached_original, normalize_cached_source, original_cache_root, original_path

def queue_with_catalog_recovery(service, *, catalog_model=None, **kwargs):
    from openclass_core.services.source_upload_reuse import queue_reusing_original
    return queue_reusing_original(service, _queue_catalog_original, catalog_model=catalog_model, **kwargs)

def _queue_catalog_original(service, *, catalog_model=None, **kwargs):
    """Retain exact bytes before asynchronous parsing; no body extraction here."""
    content = kwargs['content']
    if not kwargs['owner_user_id'] or not kwargs['package_id'] or (not content) or (len(content) > service.config.max_upload_bytes):
        raise GoogleSourceError('The source identity, file contents, or size does not meet upload requirements.')
    source = service.queue_file(**kwargs)
    source = normalize_cached_source(source)
    metadata = {} if source.metadata.get('original_cache') else cache_original(source, kwargs['content'])
    metadata['catalog_model_selection'] = (catalog_model or default_text_selection()).model_dump(mode='json')
    mime = kwargs.get('mime_type', '')
    if not mime.startswith(('audio/', 'video/')) and Path(kwargs['file_name']).suffix.lower() not in {'.mp3', '.wav', '.mp4', '.mpeg', '.ogg', '.oga', '.opus', '.m4a', '.webm'}:
        metadata.update({'processing_mode': 'ordinary', 'basic_read_status': 'pending', 'basic_read_progress': 0, 'catalog_status': 'pending', 'enhancement_status': 'not_prepared', 'enhancement_progress': 0})
    if metadata.get('processing_mode') == 'ordinary':
        from openclass_core.services.anythingllm.config import selected, eligible
        if selected() and eligible(source.file_name, source.mime_type):
            metadata.update({'source_processor': 'anythingllm', 'catalog_owner': 'anythingllm', 'catalog_engine': 'anythingllm_source_catalog_v2', 'catalog_policy': 'anythingllm_direct_catalog_v1'})
    return service.state.update(source, metadata=metadata)

class _CapturedCatalogStore:

    def __init__(self, store):
        self.store = store
        self.bundle = None

    def __getattr__(self, name):
        return getattr(self.store, name)

    def publish_catalog(self, *, structure, chapters, run=None):
        self.store._validate_catalog_bundle(structure=structure, chapters=chapters, run=run)
        previous = self.store.get_structure(owner_user_id=structure.owner_user_id, package_id=structure.package_id, source_id=structure.source_ingestion_id)
        version = max(structure.catalog_version, (previous.catalog_version if previous else 0) + 1)
        stamp = now_iso()
        chapters = [chapter.model_copy(update={'catalog_version': version, 'source_content_hash': chapter.source_content_hash or structure.source_content_hash, 'mapping_status': 'verified' if chapter.mapping_status == 'unverified' and chapter.anchor_status == 'verified' else chapter.mapping_status}) for chapter in chapters]
        structure = structure.model_copy(update={'catalog_version': version, 'catalog_updated_at': stamp, 'updated_at': stamp, 'chapter_count': len(chapters), 'chunk_count': 0, 'visual_count': 0, 'visual_index_status': 'unsupported', 'visual_index_version': 0})
        if run is not None:
            verified_count = sum((chapter.mapping_status == 'verified' for chapter in chapters))
            run = run.model_copy(update={'catalog_version': version, 'chapter_count': len(chapters), 'verified_chapter_count': verified_count, 'verification_rate': verified_count / len(chapters) if chapters else 0.0, 'updated_at': stamp, 'completed_at': run.completed_at or (stamp if run.status == 'succeeded' else None)})
        self.bundle = (structure, chapters, run)
        return structure

def _original_work_source(service, source):
    """Authorize the exact stored version, including an unpublished legacy upgrade."""
    current = service.state.current(source)
    if any((getattr(current, key) != getattr(source, key) for key in ('id', 'owner_user_id', 'package_id'))) or any((current.metadata.get(key) != source.metadata.get(key) for key in ('content_hash', 'source_version'))):
        raise GoogleSourceError('The requested original identity or hash changed before cache preparation.')
    try:
        service.get(owner_user_id=current.owner_user_id, package_id=current.package_id, source_id=current.id, ready=False)
    except GoogleSourceError:
        published = service.store.get_source(owner_user_id=current.owner_user_id, package_id=current.package_id, source_id=current.id)
        version = current.metadata.get('source_version')
        if published is None or published.metadata.get('source_backend') in {'google', 'local'} or current.metadata.get('source_backend') not in {'google', 'local'} or (not version) or (published.metadata.get('source_version') == version) or (published.metadata.get('pending_source_version') != version):
            raise
        service.state.current(current)
        if any((record.metadata.get('external_turn_temporary') for record in (published, current))):
            raise GoogleSourceError('Add this external resource to Sources before preparing its original.')
        from openclass_core.services.google_drive_sources import ensure_drive_source_access, DriveSourceError
        try:
            for record in (published, current):
                ensure_drive_source_access(record, service.store.path)
        except DriveSourceError as exc:
            raise GoogleSourceError(str(exc)) from exc
    return current

def ensure_original_cache(service, source):
    """Return hash-checked original bytes without Google parsing or retrieval."""
    from .original_cache import _bound_cache_path
    current = _original_work_source(service, source)
    if current.metadata.get('original_cache') is not None or current.metadata.get('local_source_path') is not None:
        path = _bound_cache_path(current)
        if path.is_file():
            path = original_path(current)
            service._check(current)
            return (current, path)
    ref = current.metadata.get('google_original', current.metadata.get('local_original'))
    if not ref:
        raise GoogleSourceError('The original file has not been uploaded to Google Cloud Storage successfully. Please retry the upload.')
    service._verify_blob_binding(current, ref, original=True)
    content = service.storage.read(ref)
    service._check(current)
    current = service.state.update(current, metadata=cache_original(current, content))
    return (current, original_path(current))

def build_fallback_catalog(ingestion, source, error):
    return _build_original_catalog(ingestion, source, error=error)

def build_manual_catalog(ingestion, source):
    return _build_original_catalog(ingestion, source)

def _build_original_catalog(ingestion, source, error=None):
    service = ingestion.google_service
    service._check(source)
    published = service.store.get_source(owner_user_id=source.owner_user_id, package_id=source.package_id, source_id=source.id)
    if error is not None and published.metadata.get('source_version') != source.metadata.get('source_version'):
        failed = service.state.update(source, status='failed', error=str(error), metadata={'catalog_warning': str(error)})
        service._job(failed, 'failed', 100, str(error))
        return failed
    source, path = ensure_original_cache(service, source)
    indexed = bool(source.metadata.get('search_ready') and source.metadata.get('google_artifact'))
    was_ready = source.status == 'ready'
    prior_view = ingestion.structure_store.get_catalog_view(source=published)
    had_catalog = bool(published.metadata.get('catalog_ready') and prior_view.chapters and (prior_view.chapter_count == prior_view.verified_chapter_count) and (prior_view.directory_status == 'complete'))
    if not was_ready:
        source = service.state.update(source, status='parsing', error='', metadata={'catalog_mode': 'manual', 'parsing_status': 'manual'})
    is_pdf = source.mime_type == 'application/pdf' or Path(source.file_name).suffix.lower() == '.pdf'
    bundle, catalog_metadata = (None, {})
    service._job(source.model_copy(update={'status': 'parsing'}), 'manual_original_catalog', 30)

    def progress(phase, amount):
        service._check(source)
        service._job(source.model_copy(update={'status': 'parsing'}), phase, amount)
    model = AIModelSelection.model_validate(source.metadata.get('catalog_model_selection') or default_text_selection().model_dump(mode='json'))
    try:
        if is_pdf:
            from .adapters import adapt_source
            from .catalog_publication import build_source_catalog
            adapted = adapt_source(path.read_bytes(), source.file_name, source.mime_type)
            source = service.state.update(source, metadata={'page_count': sum((part.page_count for part in adapted.parts))})
            selected = source.model_copy(update={'metadata': {**source.metadata, 'catalog_model_selection': model.model_dump(mode='json')}})
            structure, chapters, catalog_metadata = build_source_catalog(service, selected, adapted, [], require_cloud_layout=False)
            bundle = (structure, chapters, None)
        else:
            processor = copy.copy(ingestion.directory_processor)
            captured = _CapturedCatalogStore(ingestion.structure_store)
            processor.store = captured
            processor.process(record=source, path=path, catalog_model=model, progress_callback=progress, force_ai=True)
            bundle = captured.bundle
    except Exception as exc:
        service._check(source)
        message = 'AI catalog generation failed. The original file has been preserved; you can retry.'
        failed = service.state.update(source, status='ready' if was_ready else 'failed', error='' if was_ready else message, metadata={'catalog_warning': message, 'update_status': 'failed', 'update_error': message, **({} if had_catalog else {'catalog_status': 'failed', 'catalog_ready': False})})
        service._job(failed.model_copy(update={'status': 'failed'}), 'failed', 100, message)
        raise GoogleSourceError(message) from exc
    service._check(source)
    original_path(source)
    if source_task_cancel_requested() or bundle is None:
        raise GoogleSourceError('The catalog task was cancelled or returned no verifiable catalog.')
    if error is None and service.cloud_enabled(source):
        current = service.state.current(source)
        result = ingestion._retry_google_source(current) if current.metadata.get('search_ready') else service.process(current)
        service._job(result, 'ready' if result.status == 'ready' else 'failed', 100, result.error)
        return result
    structure, chapters, run = bundle
    complete = bool(chapters) and all((chapter.range is not None and chapter.mapping_status == 'verified' for chapter in chapters))
    complete = complete and structure.metadata.get('directory_status', 'complete') == 'complete'
    catalog_status = 'ready' if complete else 'partial' if chapters else 'unavailable'
    structure = structure.model_copy(update={'metadata': {**structure.metadata, 'source_backend': source.metadata.get('source_backend'), 'catalog_mode': 'manual', 'catalog_fallback': error is not None, 'catalog_warning': str(error) if error else '', 'catalog_status': catalog_status, 'search_ready': indexed}})
    capabilities = list(dict.fromkeys([*(source.metadata.get('capabilities', []) if indexed else []), 'download', 'catalog', 'read_original']))
    try:
        ready = service.state.publish(source, structure_store=ingestion.structure_store, structure=structure, chapters=chapters, metadata={**catalog_metadata, 'catalog_mode': 'manual', 'catalog_fallback': error is not None, 'catalog_warning': str(error) if error else structure.metadata.get('stop_reason', ''), 'catalog_status': catalog_status, 'catalog_version': structure.catalog_version, 'catalog_ready': complete, 'search_ready': indexed, 'search_status': 'ready' if indexed else 'unavailable' if error else 'disabled', 'parsing_status': 'ready' if indexed else 'cloud_unavailable' if error else 'manual', 'original_read_ready': True, 'capabilities': capabilities})
    except GoogleSourceError as exc:
        service._check(source)
        failed = service.state.update(source, status='ready' if was_ready else 'failed', error='' if was_ready else str(exc), metadata={'catalog_warning': str(exc), **({'update_status': 'failed', 'update_error': str(exc)} if was_ready else {}), **({} if had_catalog else {'catalog_status': 'failed', 'catalog_ready': False})})
        service._job(failed.model_copy(update={'status': 'failed'}), 'failed', 100, str(exc))
        raise
    if run is not None:
        ingestion.structure_store.save_catalog_run(run)
    service._job(ready, 'manual_catalog_ready' if complete else 'manual_catalog_partial', 100)
    return ingestion.structure_store.attach_summary(ready)

def process_with_catalog_recovery(ingestion, source):
    service = ingestion.google_service
    if source.metadata.get('source_backend') == 'local':
        return service.process(source)
    if not source.metadata.get('google_original', source.metadata.get('local_original')):
        try:
            service.config.validate(write=True)
            path = original_path(source)
            prefix = scope_prefix(source.owner_user_id, source.package_id, source.id, source.metadata['source_version'])
            original = service.storage.put(bucket=service.config.source_bucket, name=f"{prefix}/original/{source.metadata['content_hash']}", content=path.read_bytes(), mime_type=source.mime_type)
            source = service.state.update(source, metadata={'google_original': original, 'google_objects': [original], 'storage_status': 'ready'})
        except GoogleSourceUnavailableError:
            raise
    from .native_locations import prepare_native_source
    source = prepare_native_source(service, source)
    try:
        return service.process(source)
    except GoogleSourceUnavailableError as exc:
        return build_fallback_catalog(ingestion, source, exc)
