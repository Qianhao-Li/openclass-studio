"""Host seals for calibrated page scopes, without claiming pixel boundaries."""
from copy import deepcopy
import hashlib
import hmac
from openclass_core.services.source_content_contract import content_digest
PAGE_MAPPING_POLICY_VERSION = 1
_FORBIDDEN = {'page_clips', 'crop_bbox', 'boundary_precision', 'native_boundary_points', 'basic_unit_start', 'basic_unit_end', 'basic_start_char', 'basic_end_char'}
_END_RULES = {'next_heading_page', 'before_observed_page_top', 'file_end', 'start_page_only'}

def _binding(source):
    return {'source_id': source.id, 'owner_user_id': source.owner_user_id, 'package_id': source.package_id, 'content_hash': source.metadata.get('content_hash'), 'source_version': source.metadata.get('source_version')}

def _range_digest(value):
    return content_digest(value.model_dump(mode='json', exclude={'metadata'}))

def _signature(proof):
    from openclass_core.services.source_content_contract import _receipt_key
    body = {key: value for key, value in proof.items() if key != 'signature'}
    return hmac.new(_receipt_key(), ('openclass-calibrated-page-scope-v1:' + content_digest(body)).encode(), hashlib.sha256).hexdigest()

def _valid_mapping(value, proof, read_ids):
    if value is None or value.kind != 'pdf_pages' or value.start_anchor or value.end_anchor or value.container or (value.end_inclusive is not True) or _FORBIDDEN.intersection(value.metadata) or (not isinstance(proof, dict)) or (type(proof.get('policy_version')) is not int) or (proof['policy_version'] != PAGE_MAPPING_POLICY_VERSION) or (type(proof.get('page_count')) is not int) or (type(value.start) is not int) or (type(value.end) is not int) or (not 1 <= value.start <= value.end <= proof['page_count']) or (proof.get('start') != value.start) or (proof.get('end') != value.end) or (not isinstance(proof.get('end_rule'), str)) or (proof['end_rule'] not in _END_RULES) or (not isinstance(proof.get('node_key'), str)) or (not proof['node_key']):
        return False
    for key in ('calibration_digest', 'node_fingerprint'):
        digest = proof.get(key)
        if not isinstance(digest, str) or len(digest) != 64 or any((char not in '0123456789abcdef' for char in digest)):
            return False
    ids = proof.get('read_ids')
    return isinstance(ids, list) and bool(ids) and all((isinstance(item, str) and item for item in ids)) and isinstance(read_ids, list) and (set(ids) <= set(read_ids))

def seal_page_mapping(source, value, read_ids):
    """Only the host mapper calls this after validating stored calibration reads."""
    proof = deepcopy(value.metadata.get('page_mapping_proof'))
    if not _valid_mapping(value, proof, read_ids) or any((proof.get(key) != source.metadata.get(key) for key in ('content_hash', 'source_version'))):
        raise ValueError('A page scope requires a current host-derived pagination mapping.')
    proof.update(_binding(source), range_digest=_range_digest(value))
    proof['signature'] = _signature(proof)
    return proof

def verify_page_mapping(source, value, proof, read_ids):
    if not _valid_mapping(value, proof, read_ids) or value.metadata.get('citation_precision') != 'page':
        return False
    count = source.metadata.get('catalog_pdf_page_count') or source.metadata.get('page_count')
    if type(count) is int and count != proof['page_count']:
        return False
    try:
        return all((proof.get(key) == expected for key, expected in _binding(source).items())) and proof.get('range_digest') == _range_digest(value) and isinstance(proof.get('signature'), str) and hmac.compare_digest(proof['signature'], _signature(proof))
    except (TypeError, ValueError):
        return False
