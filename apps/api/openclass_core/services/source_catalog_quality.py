"""Pure range-proof checks shared by catalog publication and original readers."""
from __future__ import annotations
from copy import deepcopy
from openclass_core.services.source_content_contract import content_digest
RANGE_CONTRACT = 'source_catalog_range_v1'
PI_PIPELINE = 'pi_catalog_refinement_v1'

def _binding(value):
    return content_digest(value.model_dump(mode='json', exclude={'metadata'}))

def same_range_scope(first, second):
    """Ignore proof envelopes, retaining every native/crop interpretation field."""
    if first is None or second is None:
        return False
    proof_fields = {'source_pipeline', 'source_version', 'source_content_hash', 'catalog_range_contract', 'range_verification', 'printed_page_label'}

    def scope(value):
        result = value.model_dump(mode='json', exclude={'display_label'})
        result['metadata'] = {key: item for key, item in value.metadata.items() if key not in proof_fields}
        return result
    return scope(first) == scope(second)

def verified_range_metadata(source, value, *, method, read_ids, scope_proof=None):
    proof = {'method': method, 'read_ids': list(dict.fromkeys(read_ids)), 'range_digest': _binding(value)}
    if method == 'original_pdf_pagination' and isinstance(value.metadata.get('pagination_boundary_proof'), dict):
        proof['pagination_boundaries'] = deepcopy(value.metadata['pagination_boundary_proof'])
    if method == 'original_pdf_pagination_pages':
        from openclass_core.services.source_catalog_page_mapping_proof import seal_page_mapping
        proof['page_mapping'] = seal_page_mapping(source, value, proof['read_ids'])
    metadata = {'source_pipeline': PI_PIPELINE, 'source_version': source.metadata.get('source_version'), 'source_content_hash': source.metadata.get('content_hash'), 'catalog_range_contract': RANGE_CONTRACT, 'range_verification': proof}
    if method == 'original_pdf_pagination_pages':
        metadata['citation_precision'] = 'page'
    if method == 'original_pdf_visual_boundaries':
        proof['visual_boundary_proof'] = deepcopy(scope_proof)
        metadata.update({key: deepcopy(value.metadata[key]) for key in ('page_clips', 'boundary_precision', 'boundary_verification_passes', 'boundary_verified_image_hashes', 'read_original_images') if key in value.metadata})
    return metadata

def _visual_scope_proof(source, value, proof):
    signed = proof.get('visual_boundary_proof')
    if not isinstance(signed, dict):
        return False
    required = set()
    for key in ('start', 'end'):
        boundary = signed.get(key)
        if not isinstance(boundary, dict) or not isinstance(boundary.get('read_ids'), list) or (not boundary['read_ids']) or any((not isinstance(identity, str) or not identity for identity in boundary['read_ids'])):
            return False
        required.update(boundary['read_ids'])
    if not required <= set(proof['read_ids']):
        return False
    from openclass_core.services.source_catalog_scope_boundaries import verify_scope_proof
    return verify_scope_proof(source, value, signed)

def _whole_pdf_boundaries(value, proof):
    from openclass_core.services.source_catalog_pagination import PAGINATION_RANGE_CONTRACT_VERSION
    boundaries = proof.get('pagination_boundaries')
    if value.kind != 'pdf_pages' or type(value.start) is not int or type(value.end) is not int or (not 1 <= value.start <= value.end) or (not isinstance(boundaries, dict)) or (boundaries.get('contract_version') != PAGINATION_RANGE_CONTRACT_VERSION):
        return False
    for key, expected in (('start', value.start), ('end_before', value.end + 1)):
        anchor = boundaries.get(key)
        if not isinstance(anchor, dict) or anchor.get('starts_at_page_top') is not True or type(anchor.get('pdf_file_page')) is not int or (anchor['pdf_file_page'] != expected) or (not isinstance(anchor.get('node_key'), str)) or (not anchor['node_key']) or (not isinstance(anchor.get('read_ids'), list)) or (not anchor['read_ids']) or any((identity not in proof['read_ids'] for identity in anchor['read_ids'])):
            return False
    return value.start < boundaries['end_before']['pdf_file_page']

def current_range_proof(source, value):
    """A retained status flag is not a proof; match the recorded original first."""
    if value is None:
        return False
    meta = value.metadata
    if not source.metadata.get('source_version') or not source.metadata.get('content_hash') or meta.get('source_version') != source.metadata['source_version'] or (meta.get('source_content_hash') != source.metadata['content_hash']):
        return False
    pipeline = meta.get('source_pipeline')
    if pipeline == PI_PIPELINE:
        proof = meta.get('range_verification') or {}
        if not isinstance(proof, dict):
            return False
        valid = meta.get('catalog_range_contract') == RANGE_CONTRACT and proof.get('method') in {'original_boundary_reads', 'original_pdf_pagination', 'original_pdf_visual_boundaries', 'original_pdf_pagination_pages'} and isinstance(proof.get('read_ids'), list) and bool(proof['read_ids']) and all((isinstance(identity, str) and identity for identity in proof['read_ids'])) and (proof.get('range_digest') == _binding(value)) and (not value.start_anchor) and (not value.end_anchor)
        if not valid:
            return False
        if proof.get('method') == 'original_pdf_visual_boundaries':
            return _visual_scope_proof(source, value, proof)
        if proof.get('method') == 'original_pdf_pagination_pages':
            from openclass_core.services.source_catalog_page_mapping_proof import verify_page_mapping
            return verify_page_mapping(source, value, proof.get('page_mapping'), proof['read_ids'])
        return (proof.get('method') != 'original_pdf_pagination' or _whole_pdf_boundaries(value, proof)) and (not any((key in meta for key in ('page_clips', 'boundary_precision', 'basic_start_char', 'basic_end_char'))))
    if pipeline == 'source_original_catalog_v1':
        return meta.get('evidence_method') in {'original_native_structure', 'original_native_text'} or (meta.get('boundary_precision') == 'verified_native_region' and bool(meta.get('native_boundary_points'))) or (meta.get('boundary_precision') == 'verified_visual_region' and meta.get('boundary_verification_passes') == 2 and bool(meta.get('boundary_verified_image_hashes')))
    if pipeline == 'anythingllm_source_catalog_v2':
        if value.kind == 'pdf_pages' and source.metadata.get('basic_read_status') == 'visual':
            return False
        return (meta.get('evidence_method') == 'original_native_text' or (meta.get('parser_provider') == 'anythingllm' and meta.get('parser_version') == 'anythingllm_positioned_document_v2')) and (not meta.get('read_original_images')) and (type(meta.get('basic_unit_start')) is int) and (type(meta.get('basic_unit_end')) is int) and (1 <= meta['basic_unit_start'] <= meta['basic_unit_end']) and (type(meta.get('basic_start_char')) is int) and (meta['basic_start_char'] >= 0)
    return False

def is_page_level_range(source, value):
    return value is not None and value.metadata.get('citation_precision') == 'page' and (value.metadata.get('range_verification', {}).get('method') == 'original_pdf_pagination_pages') and current_range_proof(source, value)

def precise_native_boundary(value):
    return value is not None and (value.metadata.get('boundary_precision') == 'verified_native_region' and bool(value.metadata.get('native_boundary_points')) or (value.metadata.get('boundary_precision') == 'verified_visual_region' and value.metadata.get('boundary_verification_passes') == 2 and bool(value.metadata.get('page_clips'))) or (not value.metadata.get('read_original_images') and type(value.metadata.get('basic_unit_start')) is int and (type(value.metadata.get('basic_unit_end')) is int) and (type(value.metadata.get('basic_start_char')) is int)))

def chapter_range_issue(source, structure, chapter):
    """Check the authoritative chapter, including after a compatibility alias."""
    value = chapter.range
    if value is None or chapter.mapping_status != 'verified':
        return 'This chapter has no verified original-content range.'
    state = chapter.metadata.get('location_state')
    if state and state != 'verified':
        return 'This chapter is not citable: ' + str(chapter.metadata.get('location_reason') or state) + '.'
    meta = value.metadata
    if any((meta.get(key) != source.metadata.get(expected) for key, expected in (('source_content_hash', 'content_hash'), ('source_version', 'source_version')) if key in meta)):
        return 'This chapter boundary proof belongs to an older original or catalog version.'
    engines = {getattr(structure, 'catalog_schema_version', ''), chapter.metadata.get('catalog_engine'), source.metadata.get('catalog_engine'), meta.get('source_pipeline')}
    progressive = {PI_PIPELINE, 'anythingllm_source_catalog_v2', 'anythingllm_direct_catalog_v1'}
    if engines.intersection(progressive) and (not current_range_proof(source, value)):
        return 'This chapter uses an older unverified directory boundary. Continue catalog refinement before citing this chapter; explicit physical-page reading remains available.'
    if meta.get('purpose') == 'directory_inspection' or meta.get('representation') == 'original_page_detail':
        return 'A directory inspection crop is not a verified chapter boundary.'
    count = source.metadata.get('catalog_pdf_page_count') or source.metadata.get('page_count')
    if value.kind == 'pdf_pages' and type(count) is int and (not 1 <= value.start <= value.end <= count):
        return 'The verified chapter boundary is outside the uploaded PDF.'
    return None
