"""Re-read frozen original coordinates in bounded batches, preserving evidence IDs."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import replace
import json
from openclass_core.models import SelectionRef, SourceRange
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_content_contract import SourceReadBudget, SourceReadResult, issue_read_receipt
from openclass_core.services.source_content_scope import ensure_unit_authorized

def _coordinate(item):
    coordinate = deepcopy(item.metadata.get('source_range') or {})
    if item.metadata.get('page_start') is not None and coordinate.get('kind') == 'pdf_pages':
        coordinate.update(start=item.metadata['page_start'], end=item.metadata.get('page_end', item.metadata['page_start']))
    return coordinate

def _unit_key(item):
    coordinate = _coordinate(item)
    return (item.source_ingestion_id, item.metadata.get('source_version'), item.metadata.get('source_content_hash'), item.metadata.get('content_unit_sha256'), json.dumps({key: value for key, value in coordinate.items() if value not in (None, '', [], {})}, sort_keys=True))

def _rebind_result(result, expected, *, owner_user_id, package_id):
    from openclass_core.services.original_source_delivery import require_original_evidence
    matches = {}
    for item in expected:
        matches.setdefault(_unit_key(item), []).append(item)
    rebound, mapping, matched = ([], {}, set())
    for candidate in result.evidence_items:
        require_original_evidence(candidate, owner_user_id=owner_user_id, package_id=package_id)
        originals = matches.get(_unit_key(candidate))
        if not originals:
            originals = [item for item in expected if item.source_ingestion_id == candidate.source_ingestion_id and item.metadata.get('source_version') == candidate.metadata.get('source_version') and (item.metadata.get('source_content_hash') == candidate.metadata.get('source_content_hash')) and (item.metadata.get('content_unit_sha256') == candidate.metadata.get('content_unit_sha256')) and (item.metadata.get('page_start') is not None) and (item.metadata.get('page_start') == candidate.metadata.get('page_start')) and (item.metadata.get('page_end') == candidate.metadata.get('page_end'))]
        if len(originals) != 1:
            raise GoogleSourceError('An original unit cannot be matched uniquely to its frozen identity.')
        original = originals[0]
        if original.id in matched:
            raise GoogleSourceError('An original unit was reproduced more than once.')
        matched.add(original.id)
        mapping[candidate.id] = original.id
        copied = candidate.model_copy(deep=True, update={'id': original.id})
        for key in ('citation_number', 'citation_numbers'):
            if key in original.metadata:
                copied.metadata[key] = original.metadata[key]
        receipt = candidate.metadata['source_read_receipt']
        issue_read_receipt(items=[copied], owner_user_id=owner_user_id, package_id=package_id, source_id=candidate.source_ingestion_id, source_version=receipt['source_version'], content_hash=receipt['content_hash'], source_range=receipt['range'], authorized_range=receipt.get('authorized_range'), authorization=receipt['authorization'], grant_id=receipt['grant_id'], role=receipt['role'])
        rebound.append(copied)
    if {item.id for item in rebound} != {item.id for item in expected}:
        raise GoogleSourceError('The original reader did not reproduce every requested original unit.')
    result.evidence_items = rebound
    result.image_manifest = [{**entry, 'evidence_id': mapping[entry['evidence_id']]} for entry in result.image_manifest]
    return result

def _selected_result(result, expected):
    keys = {_unit_key(item) for item in expected}
    items = [item for item in result.evidence_items if _unit_key(item) in keys]
    ids = {item.id for item in items}
    pairs = [(image, entry) for image, entry in zip(result.image_inputs, result.image_manifest, strict=True) if entry['evidence_id'] in ids]
    return replace(result, evidence_items=items, image_inputs=[image for image, _ in pairs], prompt_context='', image_manifest=[{**entry, 'image_index': index} for index, (_, entry) in enumerate(pairs)])

def rehydrate_original_batches(delivery, *, unit_ids=None):
    from openclass_core.services.source_evidence_store import source_evidence_store
    from openclass_core.services.source_structure_store import source_structure_store
    from openclass_core.services.source_content_reader import read_source_content
    from openclass_core.services.google_sources.service import get_google_source_service
    from openclass_core.services.source_range_reader import iter_source_range_for_ai
    from openclass_core.services.source_content_frozen import uses_legacy_original_reader
    store, structures = (delivery.source_store or source_evidence_store, delivery.structure_store or source_structure_store)
    chosen = [item for item in delivery.evidence_items if unit_ids is None or item.id in unit_ids]
    jobs = []
    for item in chosen:
        receipt = item.metadata['source_read_receipt']
        coordinate = _coordinate(item)
        if not coordinate:
            raise GoogleSourceError('The frozen original has no exact native coordinate.')
        scope = receipt.get('authorized_range') or receipt['range']
        if scope and scope.get('kind') != 'whole_source':
            ensure_unit_authorized(coordinate, scope)
        jobs.append((item, coordinate, receipt, uses_legacy_original_reader(item)))
    grouped, positioned_groups = ([], {})
    for item, coordinate, receipt, legacy in jobs:
        positioned = bool(coordinate.get('metadata', {}).get('original_unit_spans'))
        if positioned and (not legacy) and (receipt.get('role') != 'legacy_native_reader'):
            requested = deepcopy(item.metadata.get('source_read_request'))
            scope = receipt.get('authorized_range')
            if scope and scope.get('kind') != 'whole_source':
                if requested is None:
                    raise GoogleSourceError('The frozen positioned original has no authorized reading range.')
                ensure_unit_authorized(requested, scope)
            key = (item.source_ingestion_id, item.metadata.get('source_version'), item.metadata.get('source_content_hash'), item.metadata.get('catalog_version'), receipt['authorization'], receipt['grant_id'], receipt['role'], json.dumps(requested, sort_keys=True))
            if key in positioned_groups:
                positioned_groups[key][0].append(item)
            else:
                group = ([item], requested, receipt, legacy, True)
                grouped.append(group)
                positioned_groups[key] = group
            continue
        if grouped:
            previous, prior_range, prior_receipt, prior_legacy, prior_positioned = grouped[-1]
            if not prior_positioned and coordinate.get('kind') == prior_range.get('kind') == 'pdf_pages' and (len(previous) < 6) and (item.source_ingestion_id == previous[0].source_ingestion_id) and (legacy == prior_legacy) and (receipt.get('authorization') == prior_receipt.get('authorization')) and (type(prior_range.get('end')) is int) and (prior_range['end'] + 1 == coordinate.get('start')) and ({key: value for key, value in coordinate.items() if key not in {'start', 'end'}} == {key: value for key, value in prior_range.items() if key not in {'start', 'end'}}):
                previous.append(item)
                prior_range['end'] = coordinate['end']
                continue
        grouped.append(([item], coordinate, receipt, legacy, False))
    for expected, coordinate, receipt, legacy, positioned in grouped:
        delivery.validate_current()
        source_id = expected[0].source_ingestion_id
        if receipt.get('role') == 'legacy_native_reader':
            from openclass_core.services.legacy_original_content import iter_native_original_batches
            source = store.get_source(owner_user_id=delivery.owner_user_id, package_id=delivery.package_id, source_id=source_id)
            structure = structures.get_structure(owner_user_id=delivery.owner_user_id, package_id=delivery.package_id, source_id=source_id)
            reads = list(iter_native_original_batches(source=source, structure=structure, source_range=coordinate, authorized_range=receipt.get('authorized_range'), owner_user_id=delivery.owner_user_id, package_id=delivery.package_id, is_cancelled=delivery.is_cancelled))
            if len(reads) != 1:
                raise GoogleSourceError('A frozen native original unit cannot be reproduced as one input.')
            result = reads[0]
        elif legacy:
            source = store.get_source(owner_user_id=delivery.owner_user_id, package_id=delivery.package_id, source_id=source_id)
            structure = structures.get_structure(owner_user_id=delivery.owner_user_id, package_id=delivery.package_id, source_id=source_id)
            if coordinate.get('kind') != 'pdf_pages':
                raise GoogleSourceError('This legacy native source requires a current exact original reader.')
            selection = SelectionRef(kind='source', excerpt='', source_ingestion_id=source_id, source_content_hash=receipt['content_hash'], source_scope_kind='page_range', source_page_start=coordinate['start'], source_page_end=coordinate['end'], source_range=SourceRange.model_validate(coordinate), catalog_version=expected[0].metadata['catalog_version'])
            remaining = {item.id: item for item in expected}
            for read in iter_source_range_for_ai(owner_user_id=delivery.owner_user_id, package_id=delivery.package_id, source=source, structure=structure, chapter=None, selection=selection, is_cancelled=delivery.is_cancelled):
                manifest = [{**entry, 'source_id': source_id, 'source_range': {'kind': 'pdf_pages', 'start': entry['physical_page'], 'end': entry['physical_page']}} for entry in read.image_manifest]
                pages = {entry['physical_page'] for entry in manifest}
                subset = [item for item in remaining.values() if item.metadata.get('page_start') in pages]
                result = SourceReadResult(evidence_items=read.evidence_items, image_inputs=read.image_inputs, image_manifest=manifest, complete=True, coverage=[{'source_id': source_id, 'read_range': coordinate, 'complete': True}])
                yield _rebind_result(result, subset, owner_user_id=delivery.owner_user_id, package_id=delivery.package_id)
                for item in subset:
                    remaining.pop(item.id)
            if remaining:
                raise GoogleSourceError('Original image continuation did not deliver the entire requested scope.')
            continue
        else:
            service = delivery.google_service or get_google_source_service(store=store, structure_store=structures)
            remaining = {item.id: item for item in expected}
            cursor = None
            while remaining:
                result = read_source_content(service, owner_user_id=delivery.owner_user_id, package_id=delivery.package_id, source_id=source_id, content_hash=receipt['content_hash'], source_range=coordinate, authorization=receipt['authorization'], grant_id=receipt['grant_id'], role='original_source_delivery', budget=SourceReadBudget.continuation(), cursor=cursor, is_cancelled=delivery.is_cancelled)
                from openclass_core.services.original_source_delivery import require_original_evidence
                for candidate in result.evidence_items:
                    require_original_evidence(candidate, owner_user_id=delivery.owner_user_id, package_id=delivery.package_id)
                keys = {_unit_key(item) for item in result.evidence_items}
                subset = [item for item in remaining.values() if _unit_key(item) in keys]
                if not subset and (not positioned):
                    raise GoogleSourceError('The original reader produced no matching units for this continuation.')
                if subset:
                    selected = _selected_result(result, subset) if positioned else result
                    yield _rebind_result(selected, subset, owner_user_id=delivery.owner_user_id, package_id=delivery.package_id)
                for item in subset:
                    remaining.pop(item.id)
                if remaining and (not result.next_cursor or result.next_cursor == cursor):
                    raise GoogleSourceError('The original reader did not complete its authorized scope.')
                cursor = result.next_cursor
            continue
        yield _rebind_result(result, expected, owner_user_id=delivery.owner_user_id, package_id=delivery.package_id)
