"""Learner-facing source records exclude cloud credentials, object paths and jobs."""
from __future__ import annotations
from openclass_core.services.source_refinement_presentation import public_refinement
_PUBLIC_METADATA = {'source_backend', 'adapter', 'source_processing_owner', 'source_version', 'content_hash', 'catalog_version', 'storage_status', 'parsing_status', 'search_status', 'catalog_status', 'search_ready', 'catalog_ready', 'parser_run_id', 'capabilities', 'pending_source_version', 'basic_parse_elapsed_ms', 'basic_unit_count', 'catalog_read_batches', 'catalog_total_batches', 'catalog_elapsed_ms', 'page_count', 'retrievable_pages', 'unmapped_pages', 'catalog_mode', 'original_read_ready', 'update_status', 'update_error', 'catalog_label', 'catalog_fallback', 'catalog_warning', 'catalog_pipeline', 'original_pages_read', 'model_call_count', 'stop_reason', 'catalog_verified_chapter_count', 'catalog_total_chapter_count'}
_PUBLIC_ORIGIN = {'origin', 'url', 'drive_file_id', 'drive_version', 'origin_mime_type', 'source_snapshot_kind', 'archive_name', 'member'}
_PUBLIC_METADATA.update({'processing_mode', 'basic_read_status', 'basic_read_error', 'basic_read_progress', 'catalog_input_method', 'catalog_error', 'catalog_progress', 'enhancement_status', 'enhancement_error', 'enhancement_progress', 'catalog_strategy', 'catalog_algorithm_version', 'catalog_budget_measurement', 'catalog_read_unit_count', 'catalog_total_unit_count', 'processing_metrics'})
_PUBLIC_METADATA.update({'source_processor', 'anythingllm_catalog_ready', 'anythingllm_index_status', 'anythingllm_index_error', 'catalog_origin'})
_PUBLIC_METADATA.update({'catalog_executor', 'catalog_directory_evidence', 'catalog_directory_candidates', 'catalog_directory_origin', 'catalog_read_ranges', 'catalog_refinement_status', 'catalog_search_engine', 'catalog_search_warning'})

def public_source(source):
    if source.metadata.get('source_backend') not in {'google', 'local'}:
        return source
    metadata = {key: value for key, value in source.metadata.items() if key in _PUBLIC_METADATA}
    refinement = public_refinement(source)
    if refinement and refinement.total_units:
        metadata['catalog_transcription_progress'] = {'completed_units': refinement.completed_units, 'total_units': refinement.total_units, 'discovery_complete': refinement.discovery_complete, 'complete': refinement.discovery_complete and refinement.completed_units == refinement.total_units}
    origin = source.metadata.get('origin', {})
    if isinstance(origin, dict):
        metadata['origin'] = {key: value for key, value in origin.items() if key in _PUBLIC_ORIGIN}
    return source.model_copy(update={'metadata': metadata, 'refinement': refinement})
