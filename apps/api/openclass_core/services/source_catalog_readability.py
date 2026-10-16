"""Recognize current positioned catalogs without changing source authorization."""
from __future__ import annotations

def verified_catalog_range(source, requested):
    if requested is None:
        return False
    pipeline = requested.metadata.get('source_pipeline')
    if pipeline == 'source_original_catalog_v1':
        return True
    if pipeline == 'anythingllm_source_catalog_v2':
        return source.metadata.get('catalog_owner') == 'anythingllm' and source.metadata.get('catalog_engine') == pipeline
    if pipeline == 'pi_catalog_refinement_v1':
        return source.metadata.get('catalog_engine') == pipeline and requested.metadata.get('source_version') == source.metadata.get('source_version') and (requested.metadata.get('source_content_hash') == source.metadata.get('content_hash'))
    return False
