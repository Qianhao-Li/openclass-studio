"""Source-native catalog orchestration, separate from search readiness."""
from __future__ import annotations
import json
from openclass_core.models import SourceStructure, SourceStructureQuality
from openclass_core.services.source_content_contract import content_digest
from .config import GoogleSourceError
from .storage import scope_prefix

def build_source_catalog(service, source, adapted, outputs, *, require_cloud_layout=True):
    """Return a catalog plus source-only checkpoint metadata; never teach from chunks."""
    service._job(source.model_copy(update={'status': 'parsing'}), 'catalog_generation', 80)
    service.state.update(source, metadata={'catalog_pipeline': 'source_original_catalog_v1', 'catalog_status': 'running'})
    if adapted.format != 'pdf':
        from openclass_core.services.source_content_catalog import build_native_content_catalog
        structure, chapters = build_native_content_catalog(service, source, adapted, outputs)
        return (structure, chapters, catalog_display_metadata(structure))
    from .catalog_fallback import ensure_original_cache
    from .catalog_native_pdf import build_native_pdf_catalog
    current, path = ensure_original_cache(service, source)
    from .catalog_native_review import review_missing_bookmarks
    native = build_native_pdf_catalog(source, path, allow_visual_pages=not require_cloud_layout or (bool(outputs) and all((output.get('layout_verified') and output.get('processing_complete') for output in outputs))), verify_missing=lambda missing: review_missing_bookmarks(service, source, path, missing))
    if native:
        structure, chapters = native
        return (structure, chapters, catalog_display_metadata(structure))
    if not source.metadata.get('catalog_model_selection'):
        return (SourceStructure(owner_user_id=source.owner_user_id, package_id=source.package_id, source_ingestion_id=source.id, status='linear_only', strategy='linear_text', catalog_version=source.metadata.get('catalog_version', 1), source_content_hash=source.metadata['content_hash'], catalog_schema_version='source_original_catalog_v1', quality=SourceStructureQuality(level='unverified'), metadata={'catalog_status': 'unavailable', 'catalog_pipeline': 'source_original_catalog_v1', 'directory_status': 'uncertain', 'stop_reason': 'Select an image-capable catalog model to read the original directory pages.'}), [], {})
    from .catalog_original import build_original_catalog
    checkpoint = None
    ref = source.metadata.get('original_catalog_checkpoint')
    if ref:
        service._verify_blob_binding(source, ref)
        checkpoint = service.storage.json(ref)
    structure, chapters, checkpoint = build_original_catalog(service, source, adapted, outputs, checkpoint=checkpoint, hints=None if require_cloud_layout else [], require_cloud_layout=require_cloud_layout)
    metadata = catalog_display_metadata(structure)
    if checkpoint:
        (service._check_cloud if require_cloud_layout else service._check)(source)
        service.config.validate(write=True)
        prefix = scope_prefix(source.owner_user_id, source.package_id, source.id, source.metadata['source_version'])
        ref = service.storage.put(bucket=service.config.artifact_bucket, name=f'{prefix}/catalog-checkpoints/{content_digest(checkpoint)}.json', content=json.dumps(checkpoint, ensure_ascii=False).encode(), mime_type='application/json')
        metadata['original_catalog_checkpoint'] = ref
    return (structure, chapters, metadata)

def catalog_display_metadata(structure):
    keys = {'catalog_pipeline', 'original_pages_read', 'model_call_count', 'stop_reason'}
    return {**{key: value for key, value in structure.metadata.items() if key in keys}, 'catalog_verified_chapter_count': structure.quality.verified_chapter_count, 'catalog_total_chapter_count': structure.chapter_count}
