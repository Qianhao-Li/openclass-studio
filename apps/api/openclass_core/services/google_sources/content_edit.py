"""Text edits prepare a new source version instead of mutating cached originals."""
from __future__ import annotations
from openclass_core.models import AIModelSelection
from .catalog_fallback import process_with_catalog_recovery, queue_with_catalog_recovery
from .config import GoogleSourceError

def update_text_source(ingestion, source, content):
    from openclass_core.services.source_ingestion_service import SourceIngestionError
    origin = source.metadata.get('origin', {})
    origin = origin if isinstance(origin, dict) else {}
    if origin.get('origin') == 'google_drive' or origin.get('drive_file_id') or origin.get('drive_connection_id'):
        raise SourceIngestionError('Edit the original file in Drive, then sync it. Drive files cannot be edited here.')
    mime = source.mime_type.split(';', 1)[0].strip().lower()
    if not mime.startswith('text/') or mime == 'text/html':
        raise SourceIngestionError('This Google source format does not support direct text editing. Edit the original file and upload it again.')
    selection = source.metadata.get('catalog_model_selection')
    try:
        model = AIModelSelection.model_validate(selection) if selection else None
    except ValueError as exc:
        raise SourceIngestionError('The catalog model configuration is no longer valid. Please select a model again.') from exc
    try:
        candidate = queue_with_catalog_recovery(ingestion.google_service, owner_user_id=source.owner_user_id, package_id=source.package_id, source_id=source.id, file_name=source.file_name, mime_type=source.mime_type, title=source.title, content=content.encode('utf-8'), source_uri=source.source_uri, origin_metadata=dict(origin), catalog_model=model)
        if source.metadata.get('source_processor') == 'anythingllm':
            candidate = ingestion.google_service.state.update(candidate, metadata={'source_processor': 'anythingllm'})
        return process_with_catalog_recovery(ingestion, candidate)
    except GoogleSourceError as exc:
        raise SourceIngestionError(str(exc)) from exc
