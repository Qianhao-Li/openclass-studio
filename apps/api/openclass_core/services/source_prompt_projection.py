"""Compact model views; authenticated read evidence stays unchanged in the backend."""
from __future__ import annotations
from copy import deepcopy
import json

def _value(item):
    return item.model_dump(mode='json') if hasattr(item, 'model_dump') else item

def range_for_model(value):
    value = _value(value)
    if not isinstance(value, dict):
        return value
    keys = ('kind', 'start', 'end', 'container', 'path', 'object', 'snapshot_page', 'segment', 'query_plan', 'index_base', 'display_label', 'end_inclusive', 'start_anchor', 'end_anchor')
    result = {key: deepcopy(value[key]) for key in keys if value.get(key) not in (None, '', [])}
    metadata = value.get('metadata') or {}
    coordinate_metadata = {key: deepcopy(metadata[key]) for key in ('index_base', 'end_anchor_exclusive', 'page_coordinate_system', 'printed_page_label', 'boundary_precision', 'native_text_region', 'locator_method') if key in metadata}
    if coordinate_metadata:
        result['metadata'] = coordinate_metadata
    return result

def provenance_for_model(value):
    """Keep representation semantics and snapshot identity, not processing audit."""
    value = value or {}
    keys = ('input_method', 'representation', 'calculation_engine', 'operation', 'formulas_executed', 'snapshot_kind', 'conversion', 'snapshot_sha256', 'snapshot_page', 'transformation', 'original_object', 'visual_content_processed', 'original_media_read', 'input_row_count', 'content_scope')
    result = {key: deepcopy(value[key]) for key in keys if key in value and (not isinstance(value[key], (dict, list)))}
    if value.get('crop_bbox'):
        result['crop_bbox'] = deepcopy(value['crop_bbox'])
    if value.get('input_range'):
        result['input_range'] = range_for_model(value['input_range'])
    return result

def scope_for_model(scope):
    """Keep the chosen scope while removing internal range verification audit."""
    result = deepcopy(_value(scope))
    if isinstance(result, dict):
        for reference in result.get('refs', []):
            if reference.get('source_range') is not None:
                reference['source_range'] = range_for_model(reference['source_range'])
    return result

def image_manifest_for_model(items):
    keys = ('image_index', 'evidence_id', 'source_id', 'physical_page', 'page_number', 'mime_type', 'sha256')
    result = []
    for entry in items or []:
        item = {key: deepcopy(entry[key]) for key in keys if entry.get(key) is not None}
        if entry.get('source_range') is not None:
            item['source_range'] = range_for_model(entry['source_range'])
        result.append(item)
    return result

def evidence_for_model(items, *, include_source_identity=True):
    result = []
    for value in items:
        item = _value(value)
        metadata = item.get('metadata') or {}
        view = {'id': item.get('id'), 'source_ingestion_id': item.get('source_ingestion_id'), 'expanded_text': item.get('expanded_text') or item.get('excerpt') or '', 'excerpt': ''}
        for key in ('section_path', 'page_range'):
            if item.get(key):
                view[key] = deepcopy(item[key])
        visible = {key: deepcopy(metadata[key]) for key in ('citation_number', 'citation_numbers', 'page_coordinate_system', 'page_label', 'input_kind', 'input_method', 'source_transport', 'retrieval_mode', 'layout', 'repository_commit_sha', 'repository_source_url', 'repository_path', 'line_start', 'line_end') if metadata.get(key) not in (None, '', [], {})}
        if metadata.get('source_range') is not None:
            visible['source_range'] = range_for_model(metadata['source_range'])
        form = provenance_for_model(metadata.get('provenance'))
        if form:
            visible['provenance'] = form
        if metadata.get('image_manifest'):
            visible['image_manifest'] = image_manifest_for_model(metadata['image_manifest'])
        if include_source_identity:
            view['source_title'] = item.get('source_title', '')
            visible.update({key: metadata[key] for key in ('source_version', 'source_content_hash') if metadata.get(key)})
        view['metadata'] = visible
        result.append(view)
    return result

def _missing_for_model(items):
    result = []
    for item in items or []:
        if not isinstance(item, dict):
            result.append(deepcopy(item))
            continue
        result.append({key: range_for_model(value) if key in {'range', 'source_range'} else deepcopy(value) for key, value in item.items() if key in {'reason', 'message', 'kind', 'range', 'source_range', 'page', 'pages', 'start', 'end', 'count', 'matched_rows'}})
    return result

def coverage_for_model(parts):
    final = {}
    for part in parts or []:
        view = {key: deepcopy(part[key]) for key in ('source_id', 'complete', 'next_cursor', 'warning', 'reason', 'remaining_ranges', 'range_coverage_verified', 'unmapped_pages') if key in part and part[key] not in (None, '', [], {})}
        if part.get('read_range') is not None:
            view['read_range'] = range_for_model(part['read_range'])
        if part.get('missing'):
            view['missing'] = _missing_for_model(part['missing'])
        identity = (part.get('source_id'), json.dumps(part.get('read_range'), sort_keys=True, ensure_ascii=False), part.get('reason') if not part.get('source_id') else None)
        final[identity] = view
    return list(final.values())

def context_for_model(items, *, image_manifest=(), coverage=(), citation_map=()):
    items = [_value(item) for item in items]
    sources = {}
    for item in items:
        meta = item.get('metadata') or {}
        identity = (item.get('source_ingestion_id'), meta.get('source_version'), meta.get('source_content_hash'))
        sources[identity] = {key: value for key, value in {'source_id': identity[0], 'title': item.get('source_title'), 'source_version': identity[1], 'content_hash': identity[2]}.items() if value is not None}
    return {'sources': list(sources.values()), 'evidence': evidence_for_model(items, include_source_identity=False), 'image_manifest': image_manifest_for_model(image_manifest), 'coverage': coverage_for_model(coverage), 'citation_map': [{key: deepcopy(item[key]) for key in ('evidence_id', 'citation_number', 'citation_numbers') if key in item} for item in citation_map or []]}

def requirement_for_model(requirement):
    """Copy the frozen requirement for inference; never replace the saved object."""
    result = deepcopy(_value(requirement))
    grounding = result.get('source_grounding')
    if isinstance(grounding, dict) and grounding.get('frozen_evidence'):
        items = grounding.get('frozen_evidence') or []
        coverage = [part for item in items for part in (item.get('metadata') or {}).get('source_coverage', [])]
        grounding.pop('reading_notes', None)
        grounding.pop('reading_representation', None)
        grounding.pop('original_units_read', None)
        context = context_for_model(items, coverage=coverage)
        grounding['frozen_evidence'] = context['evidence']
        grounding['sources'] = context['sources']
        if context['coverage']:
            grounding['coverage'] = context['coverage']
    if isinstance(grounding, dict):
        grounding.pop('reading_notes', None)
        for reference in grounding.get('confirmed_references', []):
            if reference.get('source_range') is not None:
                reference['source_range'] = range_for_model(reference['source_range'])
    return result

def qa_evidence_for_model(items):
    """Source QA's existing wire field names with compact evidence metadata."""
    result = []
    for item in evidence_for_model(items):
        metadata = item['metadata']
        result.append({'evidence_id': item['id'], 'source_id': item['source_ingestion_id'], 'source_title': item.get('source_title', ''), 'text': item['expanded_text'], 'section_path': item.get('section_path', []), 'page_range': item.get('page_range', ''), **metadata})
    return result

def tool_payload_for_model(payload):
    """Project only after the caller validates and registers its complete evidence."""
    result = {key: deepcopy(payload[key]) for key in ('source_id', 'source_version', 'source_content_hash', 'complete', 'next_cursor', 'usage', 'page_coordinate_contract', 'calculation', 'warning') if key in payload}
    result['items'] = evidence_for_model(payload.get('items', []), include_source_identity=False)
    context = context_for_model(payload.get('items', []), image_manifest=payload.get('image_manifest', []), citation_map=payload.get('citation_map', []))
    result.update({key: context[key] for key in ('sources', 'image_manifest', 'citation_map')})
    result['missing'] = _missing_for_model(payload.get('missing', []))
    if payload.get('images'):
        result['images'] = [{key: entry[key] for key in ('data', 'mimeType', 'mime_type', 'evidence_id') if key in entry} for entry in payload['images']]
    return result
