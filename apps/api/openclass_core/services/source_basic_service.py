"""Ordinary source preparation and atomic complete-catalog publication."""
from __future__ import annotations
from openclass_core.models import SourceChapter, SourceRange, SourceStructure, SourceStructureQuality
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_basic_cache import prepare_basic_content
from openclass_core.services.source_basic_catalog import PIPELINE, build_basic_catalog

def process_basic_source(service, source):
    from openclass_core.services.anythingllm.config import selected, eligible
    if selected(source) and eligible(source.file_name, source.mime_type):
        from openclass_core.services.anythingllm.processor import process_source
        return process_source(service, source)
    service._check(source)
    if source.metadata.get('catalog_ready') and (not source.metadata.get('catalog_rebuild_requested')):
        return source
    from openclass_core.services.source_processing_metrics import begin_run, phase, finish_run
    try:
        source = begin_run(service, source)
        source, data = prepare_basic_content(service, source)
        if source.metadata.get('catalog_ready') and (not source.metadata.get('catalog_rebuild_requested')):
            return source
        if data['format'] == 'pdf' and data.get('visual_only'):
            with phase(service, source, 'original_page_catalog'):
                ready = service.process_original_catalog(source)
            return finish_run(service, ready, 'completed' if ready.metadata.get('catalog_ready') else 'failed')
        if data['format'] == 'visual':
            structure, chapters = _image_catalog(source, data['frame_count'])
        else:
            if source.metadata.get('catalog_ready') and source.metadata.get('catalog_rebuild_requested'):
                source = source.model_copy(update={'metadata': {**source.metadata, 'catalog_version': int(source.metadata.get('catalog_version', 1)) + 1}})
            with phase(service, source, 'catalog_generation_total'):
                structure, chapters = build_basic_catalog(service, source, data)
            if data['format'] == 'pdf':
                from openclass_core.services.google_sources.original_cache import original_path
                from openclass_core.services.source_basic_pdf_boundaries import bind_original_pdf_boundaries
                with phase(service, source, 'original_boundary_verification'):
                    chapters = bind_original_pdf_boundaries(original_path(source), data, chapters)
            structure = structure.model_copy(update={'metadata': {**structure.metadata, 'native_catalog_complete': True}})
        service._check(source)
        service._job(source.model_copy(update={'status': 'parsing'}), 'basic_catalog_verifying', 90)
        with phase(service, source, 'catalog_publication'):
            ready = service.state.publish(source, structure_store=service.structure_store, structure=structure, chapters=chapters, metadata={'processing_mode': 'ordinary', 'catalog_status': 'ready', 'catalog_ready': True, 'catalog_pipeline': PIPELINE, 'catalog_input_method': structure.metadata.get('catalog_input_method', 'basic_parsed_text'), 'catalog_rebuild_requested': False, 'catalog_error': '', 'catalog_warning': '', 'catalog_progress': 100, 'catalog_total_chapter_count': len(chapters), 'catalog_verified_chapter_count': len(chapters), 'catalog_version': structure.catalog_version, 'model_call_count': structure.metadata.get('model_call_count', 0), 'original_read_ready': True, 'catalog_strategy': structure.metadata.get('catalog_strategy', 'full_text'), 'catalog_algorithm_version': structure.metadata.get('catalog_algorithm_version', 'ordinary_catalog_speed_v2')})
        ready = finish_run(service, ready)
        service._job(ready, 'basic_catalog_ready', 100)
        return service.structure_store.attach_summary(ready)
    except Exception as exc:
        from openclass_core.services.source_ingestion_jobs import source_task_cancel_requested
        live = service.state.current(source, active=False)
        readable = bool(live.metadata.get('basic_content_artifact'))
        published = service.store.get_source(owner_user_id=live.owner_user_id, package_id=live.package_id, source_id=live.id)
        replacing = published.metadata.get('source_version') != live.metadata.get('source_version')
        cancelled = source_task_cancel_requested()
        from openclass_core.services.source_basic_formats import BasicContentError
        message = str(exc) if isinstance(exc, (GoogleSourceError, BasicContentError)) and len(str(exc)) < 300 else 'The complete source catalog could not be verified. Retry catalog generation.'
        if cancelled:
            message = 'Ordinary source processing was cancelled.'
        failed = service.state.update(live, active=False, status='ready' if readable and (not replacing) else 'cancelled' if cancelled else 'failed', error='' if readable and (not replacing) else message, metadata={'catalog_status': 'ready' if live.metadata.get('catalog_ready') else 'cancelled' if cancelled else 'failed', 'catalog_error': message, 'catalog_warning': message, 'catalog_progress': 100, **({} if readable else {'basic_read_status': 'failed', 'basic_read_error': message})})
        failed = finish_run(service, failed, 'cancelled' if cancelled else 'failed')
        service._job(failed.model_copy(update={'status': 'cancelled' if cancelled else 'failed'}), 'basic_catalog_failed', 100, message)
        return failed

def _image_catalog(source, count):
    digest = source.metadata['content_hash']
    version = int(source.metadata.get('catalog_version', 1))
    chapters = [SourceChapter(owner_user_id=source.owner_user_id, package_id=source.package_id, source_ingestion_id=source.id, title=f'Image {index}', level=1, path=[f'Image {index}'], order_index=index - 1, source_content_hash=digest, catalog_version=version, mapping_status='verified', anchor_status='verified', range=SourceRange(kind='image_frames', start=index, end=index, metadata={'source_pipeline': PIPELINE, 'source_version': source.metadata['source_version'], 'source_content_hash': digest})) for index in range(1, count + 1)]
    structure = SourceStructure(owner_user_id=source.owner_user_id, package_id=source.package_id, source_ingestion_id=source.id, status='ready', strategy='native_directory_v1', catalog_schema_version=PIPELINE, source_content_hash=digest, catalog_version=version, chapter_count=count, quality=SourceStructureQuality(level='fully_verified', total_chapter_count=count, verified_chapter_count=count), metadata={'catalog_status': 'ready', 'directory_status': 'complete', 'catalog_pipeline': PIPELINE, 'catalog_label': 'Content structure'})
    return (structure, chapters)
