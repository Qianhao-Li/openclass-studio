"""Freeze only original content delivered in this turn, before the RIDOC commit."""
from __future__ import annotations
import base64
from openclass_core.services.ridoc_v3 import content_hash
from .config import GoogleSourceError
from .service import get_google_source_service
from .turn_evidence import read_evidence_for_citation

def _read_again(service, source, item, cache):
    """Reproduce a delivered unit; this read cannot mint citation eligibility."""
    from openclass_core.services.source_content_reader import read_source_content
    receipt = item.metadata['source_read_receipt']
    requested = item.metadata.get('source_range')
    if not isinstance(requested, dict) or not item.metadata.get('content_unit_sha256'):
        raise GoogleSourceError('The cited original content has no reproducible source range or content hash.')
    requested = dict(requested)
    original_request = item.metadata.get('source_read_request') or {}
    if requested.get('kind') == 'pdf_pages':
        requested.update({key: original_request[key] for key in ('start_anchor', 'end_anchor', 'metadata') if original_request.get(key)})
    cache_key = (source.id, source.metadata['source_version'], content_hash(requested))
    if cache_key not in cache:
        cache[cache_key] = read_source_content(service, owner_user_id=source.owner_user_id, package_id=source.package_id, source_id=source.id, content_hash=receipt['content_hash'], source_range=requested, authorization=receipt['authorization'], grant_id=receipt['grant_id'], role=receipt['role'])
    result = cache[cache_key]
    matches = [candidate for candidate in result.evidence_items if candidate.metadata.get('content_unit_sha256') == item.metadata['content_unit_sha256'] and candidate.metadata.get('source_range') == item.metadata.get('source_range')]
    if len(matches) != 1:
        raise GoogleSourceError('The cited original content changed or could not be reproduced exactly.')
    candidate = matches[0]
    if candidate.expanded_text != item.expanded_text or candidate.excerpt != item.excerpt:
        raise GoogleSourceError('The cited original text does not match the delivered content.')
    if len(result.image_inputs) != len(result.image_manifest):
        raise GoogleSourceError('The preserved original images do not match their manifest.')
    images = []
    for data_url, manifest in zip(result.image_inputs, result.image_manifest, strict=True):
        if manifest.get('evidence_id') != candidate.id:
            continue
        header, separator, encoded = data_url.partition(',')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise GoogleSourceError('The cited original image encoding is invalid.') from exc
        if not separator or not header.startswith('data:image/') or (not header.endswith(';base64')) or (content_hash(raw) != manifest.get('sha256')):
            raise GoogleSourceError('The cited original image does not match its verified hash.')
        images.append((raw, manifest, header[5:-7]))
    expected_image_hash = item.metadata.get('page_image_sha256')
    if not item.expanded_text and item.metadata['content_unit_sha256'] != content_hash(b''):
        expected_image_hash = item.metadata['content_unit_sha256']
    if expected_image_hash and (not any((content_hash(raw) == expected_image_hash for raw, _manifest, _mime in images))):
        raise GoogleSourceError('The original image delivered to the model could not be preserved.')
    return images

def freeze_citations(*, owner_user_id, package_id, lesson_id, citations, service=None, document=None):
    evidence, assets, frozen_citations = ({}, {}, [])
    if not citations:
        return (evidence, assets, frozen_citations)
    delivered_items = [(citation, read_evidence_for_citation(citation)) for citation in citations]
    from .citation_location_guard import validate_document_citation_locations
    validate_document_citation_locations(document, delivered_items)
    service = service or get_google_source_service()
    rereads = {}
    for citation, delivered in delivered_items:
        source = service.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=citation.source_id)
        if source is None and (citation.source_document_id or delivered is not None):
            raise GoogleSourceError('The source in the final citation has been deleted. This turn cannot save invalid evidence.')
        if source is None or source.metadata.get('source_backend') not in {'google', 'local'}:
            frozen_citations.append(citation)
            continue
        if delivered is None:
            raise GoogleSourceError('The final citation was not read from original content and registered in this turn.')
        receipt = delivered.metadata['source_read_receipt']
        if receipt.get('owner_user_id') != owner_user_id or receipt.get('package_id') != package_id:
            raise GoogleSourceError('The original-content read receipt belongs to another owner or course.')
        source = service.get(owner_user_id=owner_user_id, package_id=package_id, source_id=source.id, content_hash=citation.source_content_hash, ready=False)
        if receipt.get('source_version') != source.metadata.get('source_version'):
            raise GoogleSourceError('The original source version changed after it was read.')
        images = _read_again(service, source, delivered, rereads)
        payload = {'source_id': source.id, 'source_title': source.title, 'source_file_name': source.file_name, 'source_version': source.metadata['source_version'], 'source_content_hash': source.metadata['content_hash'], 'source_document_id': citation.source_document_id, 'chunk_ids': list(citation.chunk_ids), 'block_ids': list(citation.block_ids), 'source_transport': delivered.metadata.get('source_transport', 'original_source_content'), 'source_range': delivered.metadata.get('source_range'), 'provenance': delivered.metadata.get('provenance', {}), 'content_unit_sha256': delivered.metadata['content_unit_sha256'], 'citation_id': citation.evidence_id, 'text': delivered.expanded_text or delivered.excerpt, 'page_start': citation.page_start, 'page_end': citation.page_end, 'assets': []}
        for raw, manifest, mime_type in images:
            digest = content_hash(raw)
            assets[digest] = raw
            payload['assets'].append({'content_hash': digest, 'mime_type': mime_type, 'page_number': manifest.get('page_number', delivered.metadata.get('page_start')), 'source_range': manifest.get('source_range', delivered.metadata.get('source_range'))})
        service.get(owner_user_id=owner_user_id, package_id=package_id, source_id=source.id, content_hash=citation.source_content_hash, ready=False)
        service._check(source)
        digest = content_hash(payload)
        evidence[digest] = payload
        frozen_citations.append(citation.model_copy(update={'evidence_ref': digest, 'snapshot_url': f'/api/packages/{package_id}/lessons/{lesson_id}/source-evidence/{digest}'}))
    return (evidence, assets, frozen_citations)
