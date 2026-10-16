"""Account-scoped identical uploads reuse Sources, never another owner's artifacts."""
from pathlib import Path
import hashlib

def queue_reusing_original(service, queue, **kwargs):
    from openclass_core.services.source_ingestion_service import _source_content_processing_slot
    from openclass_core.services.google_sources.config import GoogleSourceError
    content = kwargs['content']
    if not kwargs['owner_user_id'] or not kwargs['package_id'] or (not content) or (len(content) > service.config.max_upload_bytes):
        raise GoogleSourceError('The source identity, file contents, or size does not meet upload requirements.')
    digest = hashlib.sha256(content).hexdigest()
    suffix = Path(kwargs['file_name']).suffix.lower()
    from openclass_core.services.anythingllm.config import selected, eligible
    owned_catalog = selected() and eligible(kwargs['file_name'], kwargs.get('mime_type') or '')
    with _source_content_processing_slot(database_path=service.store.path, owner_user_id=kwargs['owner_user_id'], package_id='account-source-uploads', content_identity=digest + suffix):
        for previous in service.store.list_sources(owner_user_id=kwargs['owner_user_id'], package_id=kwargs['package_id']):
            if owned_catalog and (not kwargs.get('source_id')) and (previous.metadata.get('catalog_owner') != 'anythingllm' or previous.metadata.get('catalog_engine') != 'anythingllm_source_catalog_v2' or previous.metadata.get('catalog_policy') != 'anythingllm_direct_catalog_v1'):
                continue
            if kwargs.get('source_id') and previous.id != kwargs['source_id'] or previous.metadata.get('source_backend') not in {'google', 'local'} or previous.metadata.get('content_hash') != digest or (Path(previous.file_name).suffix.lower() != suffix) or (previous.mime_type != (kwargs.get('mime_type') or 'application/octet-stream')) or (previous.metadata.get('storage_status') != 'ready') or (not previous.metadata.get('google_original', previous.metadata.get('local_original'))):
                continue
            current = service.state.current(previous, active=False)
            service._verify_blob_binding(current, current.metadata.get('google_original', current.metadata.get('local_original')), original=True)
            return current
        return queue(service, **kwargs)
