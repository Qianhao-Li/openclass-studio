"""Reproduce frozen original inputs without converting search hits into evidence."""
from openclass_core.services.source_content_contract import verified_read_evidence, SourceReadBudget
from openclass_core.services.source_content_reader import read_source_content
from openclass_core.services.source_range_reader import SourceRangeReadError

def prepare_frozen_original_inputs(*, owner_user_id, requirement, is_cancelled=None):
    from openclass_core.services.source_evidence_store import source_evidence_store
    from openclass_core.services.google_sources.service import get_google_source_service
    from openclass_core.services.google_sources.turn_evidence import remember
    from openclass_core.services.source_content_scope import ensure_unit_authorized
    grounding = requirement.source_grounding
    if any((item.metadata.get('source_backend') in {'google', 'local'} and (not verified_read_evidence(item)) for item in grounding.frozen_evidence)):
        raise SourceRangeReadError('The frozen requirement contains a source that was not read from its original content.')
    candidates = [item for item in grounding.frozen_evidence if item.metadata.get('read_contract') == 'source_read_v1' or 'source_read_receipt' in item.metadata]
    if any((not verified_read_evidence(item) for item in candidates)):
        raise SourceRangeReadError('The frozen original-source reading receipt is invalid or has been modified.')
    items = [item for item in candidates if not uses_legacy_original_reader(item)]
    if not items:
        return None
    bundle = source_evidence_store.get_bundle(owner_user_id=owner_user_id, bundle_id=grounding.confirmed_bundle_id)
    if bundle is None or bundle.owner_user_id != owner_user_id or bundle.status not in {'confirmed', 'consumed'} or (grounding.confirmation_status != 'confirmed'):
        raise SourceRangeReadError('The frozen original-source authorization is unavailable.')
    stored = {item.id: item for item in bundle.evidence_items}
    service = get_google_source_service()
    images, manifest = ([], [])
    budget = SourceReadBudget.continuation()
    total_bytes = 0
    groups = {}
    for item in items:
        from openclass_core.services.original_source_delivery import require_original_evidence
        require_original_evidence(item, owner_user_id=owner_user_id, package_id=bundle.package_id)
        if item.id not in stored or not verified_read_evidence(item) or (not verified_read_evidence(stored[item.id])):
            raise SourceRangeReadError('The frozen source input has no valid original-reading receipt.')
        receipt = item.metadata['source_read_receipt']
        if receipt['owner_user_id'] != owner_user_id or receipt['package_id'] != bundle.package_id:
            raise SourceRangeReadError('The frozen original-source input belongs to another authorization.')
        if item.metadata['content_unit_sha256'] != stored[item.id].metadata.get('content_unit_sha256'):
            raise SourceRangeReadError('The frozen original-source content was modified.')
        requested = dict(item.metadata['source_range'])
        if receipt.get('authorized_range'):
            ensure_unit_authorized(requested, receipt['authorized_range'])
        original_request = item.metadata.get('source_read_request') or {}
        for key in ('start_anchor', 'end_anchor', 'metadata'):
            if original_request.get(key):
                requested[key] = original_request[key]
        if requested.get('object') and (not original_request.get('object')):
            requested.pop('object', None)
        import json
        key = (item.source_ingestion_id, json.dumps(requested, sort_keys=True), receipt['authorization'], receipt['grant_id'])
        groups.setdefault(key, (requested, receipt, []))[2].append(item)
    jobs = list(groups.values())
    merged = []
    for requested, receipt, expected in jobs:
        if requested.get('kind') in {'pdf_pages', 'text_lines', 'docx_paragraphs', 'ppt_slides', 'sheet_rows', 'epub_spine', 'image_frames'} and merged:
            previous, previous_receipt, prior = merged[-1]
            same = {k: v for k, v in previous.items() if k not in {'start', 'end'}} == {k: v for k, v in requested.items() if k not in {'start', 'end'}}
            if same and previous_receipt['source_id'] == receipt['source_id'] and (previous_receipt['grant_id'] == receipt['grant_id']) and (previous['end'] + 1 == requested['start']) and (requested['end'] - previous['start'] < 400):
                previous['end'] = requested['end']
                prior.extend(expected)
                continue
        merged.append((dict(requested), receipt, list(expected)))
    for requested, receipt, expected in merged:
        wanted = {item.id: item for item in expected}
        cursor = None
        while wanted:
            result = read_source_content(service, owner_user_id=owner_user_id, package_id=bundle.package_id, source_id=receipt['source_id'], content_hash=receipt['content_hash'], source_range=requested, authorization=receipt['authorization'], grant_id=receipt['grant_id'], role='board_editor', budget=budget, cursor=cursor, is_cancelled=is_cancelled)
            matched = []
            for candidate in result.evidence_items:
                original = wanted.get(candidate.id)
                if original is None:
                    continue
                if candidate.metadata.get('content_unit_sha256') != original.metadata['content_unit_sha256']:
                    raise SourceRangeReadError('The original content no longer reproduces its frozen input.')
                matched.append(candidate)
                wanted.pop(candidate.id)
            ids = {candidate.id for candidate in matched}
            for image, entry in zip(result.image_inputs, result.image_manifest, strict=True):
                if entry['evidence_id'] in ids:
                    total_bytes += entry['size_bytes']
                    from openclass_core.services.source_pdf_grounding import MAX_PDF_AI_INPUT_BYTES
                    if total_bytes > MAX_PDF_AI_INPUT_BYTES:
                        raise SourceRangeReadError('The frozen original images exceed the aggregate 40 MiB input limit. Select fewer source units.')
                    manifest.append({**entry, 'image_index': len(images)})
                    images.append(image)
            remember(matched)
            cursor = result.next_cursor
            if wanted and (not cursor):
                raise SourceRangeReadError('The original content no longer reproduces its frozen input.')
    return (images, manifest)

def uses_legacy_original_reader(item) -> bool:
    """Use authenticated issuer provenance, not an editable transport label."""
    return bool(verified_read_evidence(item) and item.metadata.get('source_read_receipt', {}).get('role') == 'original_range_reader' and (item.metadata.get('provenance', {}).get('reader') == 'authenticated_original_range'))
