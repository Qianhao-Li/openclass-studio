"""OpenClass boundary for the AnythingLLM-owned source catalog workflow."""
from __future__ import annotations
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_ingestion_jobs import source_task_cancel_requested
from openclass_core.services.source_processing_metrics import begin_run, finish_run, phase, parsing_slot
from .client import AnythingLLMClient
from .config import CONTRACT, UPSTREAM_REVISION
from .owned_workflow import ENGINE, persist_content, run_catalog

def process_source(service, source, *, client=None, model_adapter=None):
    client = client or AnythingLLMClient()
    service._check(source)
    source = begin_run(service, source)
    try:
        from openclass_core.services.google_sources.catalog_fallback import ensure_original_cache
        with phase(service, source, 'original_verification'):
            source, path = ensure_original_cache(service, source)
        if not source.metadata.get('google_original', source.metadata.get('local_original')):
            raise GoogleSourceError('The exact original must be preserved by the current storage adapter before AnythingLLM processing.')
        service._verify_blob_binding(source, source.metadata.get('google_original', source.metadata.get('local_original')), original=True)
        existing = source.metadata.get('anythingllm_catalog_ready') and source.metadata.get('catalog_ready')
        if existing and (not source.metadata.get('catalog_rebuild_requested')):
            try:
                indexed = client.activate(source)
                source = service.state.update(source, metadata={'anythingllm_index_status': indexed['index_status']})
            except GoogleSourceError:
                source = service.state.update(source, metadata={'anythingllm_index_status': 'failed', 'anythingllm_index_error': 'Search preparation failed. Verified original reading remains available.'})
            return service.structure_store.attach_summary(finish_run(service, source))
        source = service.state.update(source, status='parsing', error='', metadata={'basic_read_status': 'parsing', 'basic_read_progress': 25, 'catalog_owner': 'anythingllm', 'catalog_engine': ENGINE, 'catalog_policy': 'anythingllm_direct_catalog_v1', 'catalog_display_mode': 'direct', 'catalog_quality_gates': 'disabled', 'catalog_error': '', 'catalog_warning': ''})
        service._job(source, 'anythingllm_native_collection', 25)
        with parsing_slot(service, source, source_task_cancel_requested), phase(service, source, 'anythingllm_native_collection'):
            collected = client.collect_source(source, path)
        source = persist_content(service, source, collected['content'])
        source = service.state.update(source, metadata={'anythingllm_document': collected['document_name'], 'anythingllm_contract': CONTRACT, 'anythingllm_upstream_revision': UPSTREAM_REVISION, 'anythingllm_index_status': 'not_prepared'})
        service._check(source)
        with phase(service, source, 'anythingllm_catalog'):
            structure, chapters = run_catalog(service, source, path, client, model_adapter)
        metadata = structure.metadata
        service._check(source)
        source = service.state.publish(source, structure_store=service.structure_store, structure=structure, chapters=chapters, metadata={**metadata, 'source_processor': 'anythingllm', 'catalog_owner': 'anythingllm', 'catalog_engine': ENGINE, 'catalog_pipeline': ENGINE, 'anythingllm_catalog_ready': True, 'catalog_ready': True, 'catalog_status': 'ready', 'catalog_rebuild_requested': False, 'catalog_error': '', 'catalog_warning': '', 'catalog_progress': 100, 'original_read_ready': True, 'anythingllm_index_status': 'queued'})
        try:
            indexed = client.activate(source)
            source = service.state.update(source, metadata={'anythingllm_index_status': indexed['index_status'], 'anythingllm_index_error': ''})
        except GoogleSourceError:
            source = service.state.update(source, metadata={'anythingllm_index_status': 'failed', 'anythingllm_index_error': 'Search preparation failed. Verified original reading remains available.'})
        service._job(source, 'anythingllm_catalog_ready', 100)
        return service.structure_store.attach_summary(finish_run(service, source))
    except Exception as exc:
        from openclass_core.services.external_connections import ExternalConnectionError
        cancelled = source_task_cancel_requested()
        current = service.state.current(source, active=False)
        readable = bool(current.metadata.get('basic_content_artifact'))
        message = str(exc) if isinstance(exc, (GoogleSourceError, ExternalConnectionError)) else 'AnythingLLM could not return the source directory. Retry processing.'
        current = service.state.update(current, active=False, status='ready' if readable else 'cancelled' if cancelled else 'failed', error='' if readable else message, metadata={'source_processor': 'anythingllm', 'catalog_owner': 'anythingllm', 'catalog_engine': ENGINE, 'catalog_status': 'ready' if current.metadata.get('catalog_ready') else 'cancelled' if cancelled else 'failed', 'catalog_error': message, 'catalog_warning': message, 'catalog_progress': 100, 'update_status': 'cancelled' if cancelled else 'failed', 'update_error': message, 'anythingllm_error_kind': type(exc).__name__, **({} if readable else {'basic_read_status': 'failed', 'basic_read_error': message})})
        service._job(current.model_copy(update={'status': 'cancelled' if cancelled else 'failed'}), 'anythingllm_catalog_failed', 100, message)
        return finish_run(service, current, 'cancelled' if cancelled else 'failed')
