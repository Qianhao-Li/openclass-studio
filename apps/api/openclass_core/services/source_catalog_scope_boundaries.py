"""Host-verified horizontal chapter bands, independent of page-number inference."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import hmac
import math
from openclass_core.models import SourceRange
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_content_contract import content_digest, verified_read_receipt
CONTRACT_VERSION = 1
AGREEMENT_TOLERANCE = 0.005

def _binding(task):
    return {'contract_version': CONTRACT_VERSION, 'epoch': task.get('refinement_epoch') or task['request_id'], 'content_hash': task['content_hash'], 'source_version': task['source_version']}

def _state(task):
    value = task.get('chapter_scope_boundaries') or {}
    if value and any((value.get(key) != expected for key, expected in _binding(task).items())):
        raise GoogleSourceError('The chapter boundary checkpoint belongs to another original or review epoch.')
    return value or {**_binding(task), 'plans': {}, 'points': {}}

def _sign(domain, value):
    from openclass_core.services.source_content_contract import _receipt_key
    payload = {key: item for key, item in value.items() if key != 'signature'}
    return hmac.new(_receipt_key(), (domain + content_digest(payload)).encode(), hashlib.sha256).hexdigest()

def _signed(domain, value):
    try:
        return isinstance(value, dict) and isinstance(value.get('signature'), str) and hmac.compare_digest(value['signature'], _sign(domain, value))
    except (TypeError, ValueError):
        return False

def _rows(nodes):
    from openclass_core.services.source_catalog_refinement_evidence import RefinementNode
    return [RefinementNode.model_validate(node.model_dump() if hasattr(node, 'model_dump') else node) for node in nodes]

def _semantic(node):
    return {key: getattr(node, key) for key in ('key', 'title', 'level', 'printed_page_label')}

def _plan(task, nodes, page_count, directory_pages, target_key=None):
    from openclass_core.services.source_catalog_pagination import mapped_positions
    target_key = target_key or task.get('scope_target_key')
    if not target_key:
        return None
    rows = _rows(nodes)
    index = next((i for i, node in enumerate(rows) if node.key == target_key), None)
    if index is None or type(page_count) is not int or page_count < 1:
        raise GoogleSourceError('The requested chapter or physical PDF length is unavailable.')
    positions = mapped_positions(task, rows, page_count=page_count, directory_pages=directory_pages)

    def physical(node):
        value = positions.get(node.key)
        return value if value is not None else node.source_range.start if node.source_range and node.source_range.kind == 'pdf_pages' else None
    target = rows[index]
    successor = next((node for node in rows[index + 1:] if node.level <= target.level), None)
    first = physical(target)
    last = physical(successor) if successor else page_count
    if type(first) is not int or not 1 <= first <= page_count or first in directory_pages:
        return {'target_key': target_key, 'reason': 'chapter_location_unavailable'}
    if successor and (type(last) is not int or last < first or last in directory_pages):
        return {'target_key': target_key, 'reason': 'following_boundary_unavailable'}
    document_end = successor is None or last > page_count
    last = page_count if document_end else last

    def request(node, page, kind):
        item = {'key': ('document_end:' if kind == 'document_end' else 'heading:') + node.key, 'node_key': node.key, 'heading': node.title, 'kind': kind, 'page': page, 'node_fingerprint': content_digest(_semantic(node))}
        item['point_digest'] = content_digest({**_binding(task), **item})
        return item
    requests = [request(target, first, 'heading'), request(target if document_end else successor, last, 'document_end' if document_end else 'heading')]
    result = {'target_key': target_key, 'node': _semantic(target), 'requests': requests, 'page_count': page_count, 'reason': ''}
    result['plan_digest'] = content_digest({**_binding(task), **result})
    return result

def _point(state, request):
    point = state.get('points', {}).get(request['point_digest'])
    if not _signed('openclass-pdf-boundary-point-v1:', point) or any((point.get(key) != state.get(key) for key in ('epoch', 'content_hash', 'source_version', 'source_id', 'owner_user_id', 'package_id'))) or any((point.get(key) != request.get(key) for key in ('key', 'node_key', 'node_fingerprint', 'page', 'kind', 'point_digest'))):
        return None
    return point

def _pending(task, plan):
    state = _state(task)
    saved = state.get('plans', {}).get(plan['plan_digest'], {})
    if saved.get('reason'):
        return None
    missing = [request for request in plan['requests'] if _point(state, request) is None]
    if not missing:
        return None
    page = min((request['page'] for request in missing))
    passes = saved.get('pages', {}).get(str(page), {}).get('passes', [])
    if len(passes) >= 2:
        return None
    return {'stage': 'verify_chapter_scope', 'target_key': plan['target_key'], 'plan_digest': plan['plan_digest'], 'physical_page': page, 'source_range': {'kind': 'pdf_pages', 'start': page, 'end': page}, 'pass_index': len(passes) + 1, 'requests': [{key: request[key] for key in ('key', 'node_key', 'heading', 'kind')} for request in plan['requests'] if request['page'] == page]}

def work(task, nodes, *, page_count, directory_pages=frozenset()):
    plan = _plan(task, nodes, page_count, directory_pages)
    return _pending(task, plan) if plan and (not plan['reason']) else None

def scope_read_context(task, nodes, *, page_count, directory_pages=frozenset()):
    current = work(task, nodes, page_count=page_count, directory_pages=directory_pages)
    return {**_binding(task), **{key: current[key] for key in ('plan_digest', 'target_key', 'physical_page', 'pass_index')}} if current else {}

def _read_proof(source, task, current, read_ids, previous_ids):
    valid, hashes = ([], set())
    context = {**_binding(task), **{key: current[key] for key in ('plan_digest', 'target_key', 'physical_page', 'pass_index')}}
    expected = {'source_id': source.id, 'owner_user_id': source.owner_user_id, 'package_id': source.package_id, 'source_version': task['source_version'], 'content_hash': task['content_hash'], 'role': 'source_catalog_refinement', 'authorization': 'explicit', 'grant_id': task['request_id']}
    for identity in read_ids:
        entry = task.get('reads', {}).get(identity, {})
        receipt = entry.get('receipt', {})
        if identity in previous_ids or entry.get('request_id') != task['request_id'] or entry.get('scope_read_context') != context or (receipt.get('read_id') != identity) or (not verified_read_receipt(receipt)) or any((receipt.get(key) != value for key, value in expected.items())):
            continue
        scope = receipt.get('range', {})
        if scope.get('kind') != 'pdf_pages' or scope.get('start') != current['physical_page'] or scope.get('end') != current['physical_page'] or scope.get('start_anchor') or scope.get('end_anchor') or any((scope.get('metadata', {}).get(key) for key in ('page_clips', 'crop_bbox', 'boundary_precision'))):
            continue
        for image in entry.get('page_images', []):
            bounds = image.get('source_range', {})
            digest = image.get('sha256')
            if image.get('physical_page') == current['physical_page'] and image.get('source_content_hash') == task['content_hash'] and (image.get('source_version') == task['source_version']) and (bounds.get('kind') == 'pdf_pages') and (bounds.get('start') == bounds.get('end') == current['physical_page']) and (not image.get('crop_bbox')) and (not bounds.get('start_anchor')) and (not bounds.get('end_anchor')) and (not any((bounds.get('metadata', {}).get(key) for key in ('page_clips', 'crop_bbox', 'boundary_precision')))) and isinstance(digest, str) and (len(digest) == 64) and all((c in '0123456789abcdef' for c in digest)):
                valid.append(identity)
                hashes.add(digest)
    if not valid or len(hashes) != 1:
        raise GoogleSourceError("Boundary observations require this pass's fresh authenticated whole-page PNG read.")
    return (list(dict.fromkeys(valid)), hashes.pop())

def record(source, task, nodes, *, page_count, plan_digest, physical_page, pass_index, read_ids, layout, observations, directory_pages=frozenset()):
    plan = _plan(task, nodes, page_count, directory_pages)
    current = work(task, nodes, page_count=page_count, directory_pages=directory_pages)
    if not current or any((current[key] != value for key, value in (('plan_digest', plan_digest), ('physical_page', physical_page), ('pass_index', pass_index)))):
        raise GoogleSourceError('The chapter boundary request, page or verification pass changed.')
    if layout not in {'full_width_bands', 'unsupported', 'uncertain'}:
        raise GoogleSourceError('Declare whether full-width horizontal bands isolate these chapter boundaries.')
    keys = [item['key'] for item in current['requests']]
    if not isinstance(observations, list) or len(observations) != len(keys) or any((not isinstance(item, dict) for item in observations)) or (len({item.get('key') for item in observations}) != len(keys)) or ({item.get('key') for item in observations} != set(keys)):
        raise GoogleSourceError('Observe every requested boundary exactly once, without an arbitrary endpoint.')
    for item in observations:
        y = item.get('y')
        if set(item) != {'key', 'heading_found', 'unique', 'y'} or type(item['heading_found']) is not bool or type(item['unique']) is not bool or (y is not None and (type(y) not in (int, float) or not math.isfinite(y) or (not 0 <= y <= 1))):
            raise GoogleSourceError('Boundary observations need found/unique booleans and a finite normalized y position.')
    if source.metadata.get('content_hash') != task['content_hash'] or source.metadata.get('source_version') != task['source_version']:
        raise GoogleSourceError('The original changed before chapter boundary verification.')
    state = deepcopy(_state(task))
    identity = {'source_id': source.id, 'owner_user_id': source.owner_user_id, 'package_id': source.package_id}
    if any((key in state and state[key] != value for key, value in identity.items())):
        raise GoogleSourceError('The boundary checkpoint belongs to another authorized source.')
    state.update(identity)
    saved = state.setdefault('plans', {}).setdefault(plan_digest, {'target_key': plan['target_key'], 'pages': {}, 'reason': ''})
    page = saved['pages'].setdefault(str(physical_page), {'passes': []})
    earlier_ids = {value for observed in page['passes'] for value in observed['read_ids']}
    reads, image_hash = _read_proof(source, task, current, read_ids, earlier_ids)
    page['passes'].append({'layout': layout, 'observations': deepcopy(observations), 'read_ids': reads, 'page_image_sha256': image_hash, 'request_id': task['request_id']})
    if len(page['passes']) == 2:
        first, second = page['passes']
        a, b = ({item['key']: item for item in observed['observations']} for observed in (first, second))
        new_points = {}
        for request in (request for request in plan['requests'] if request['page'] == physical_page):
            pair = (a[request['key']], b[request['key']])
            valid = first['layout'] == second['layout'] == 'full_width_bands' and first['page_image_sha256'] == second['page_image_sha256'] and all((item['heading_found'] and item['unique'] and (item['y'] is not None) for item in pair)) and (abs(pair[0]['y'] - pair[1]['y']) <= AGREEMENT_TOLERANCE)
            if valid and request['kind'] == 'document_end':
                valid = all((item['y'] == 1 for item in pair))
            if valid and request['kind'] == 'heading':
                valid = all((item['y'] < 1 for item in pair))
            if not valid:
                saved['reason'] = 'boundary_layout_unsupported' if first['layout'] != 'full_width_bands' or second['layout'] != 'full_width_bands' else 'boundary_observations_disagree'
                continue
            point = {**_binding(task), **identity, **request, 'y': sum((item['y'] for item in pair)) / 2, 'page_image_sha256': image_hash, 'read_ids': [*first['read_ids'], *second['read_ids']]}
            previous = _point(state, request)
            if previous is not None:
                if abs(previous['y'] - point['y']) > AGREEMENT_TOLERANCE:
                    saved['reason'] = 'boundary_observations_disagree'
                continue
            new_points[request['point_digest']] = point
        start, end = [new_points.get(request['point_digest']) or _point(state, request) for request in plan['requests']]
        if start and end:
            if start['page'] > end['page'] or (start['page'] == end['page'] and start['y'] >= end['y']):
                saved['reason'] = 'boundary_order_unverified'
            elif start['page'] == end['page'] and start['page_image_sha256'] != end['page_image_sha256']:
                saved['reason'] = 'boundary_observations_disagree'
        if not saved['reason']:
            for digest, point in new_points.items():
                point['signature'] = _sign('openclass-pdf-boundary-point-v1:', point)
                state.setdefault('points', {})[digest] = point
    task['chapter_scope_boundaries'] = state
    return {'saved': True, 'physical_page': physical_page, 'pass_index': pass_index, 'status': status(task, nodes, page_count=page_count, directory_pages=directory_pages)}

def _scope(task, state, plan):
    first, last = [_point(state, item) for item in plan['requests']]
    if not first or not last:
        return None
    start, end, top, bottom = (first['page'], last['page'], first['y'], last['y'])
    if end < start or (start == end and top >= bottom):
        return None
    image_hashes = {str(point['page']): point['page_image_sha256'] for point in (first, last)}
    if first['page'] == last['page'] and first['page_image_sha256'] != last['page_image_sha256']:
        return None
    clips = {str(start): [0.0, top, 1.0, bottom if start == end else 1.0]}
    if bottom == 0 and end > start:
        end -= 1
    else:
        clips[str(end)] = [0.0, top if start == end else 0.0, 1.0, bottom]
    clips = {page: clip for page, clip in clips.items() if clip != [0.0, 0.0, 1.0, 1.0]}
    value = SourceRange(kind='pdf_pages', start=start, end=end, metadata={'page_clips': clips, 'boundary_precision': 'verified_visual_region', 'boundary_verification_passes': 2, 'boundary_verified_image_hashes': image_hashes, 'read_original_images': True})
    reads = list(dict.fromkeys([*first['read_ids'], *last['read_ids']]))
    proof = {**_binding(task), **{key: state[key] for key in ('source_id', 'owner_user_id', 'package_id')}, 'node_key': plan['target_key'], 'node_fingerprint': content_digest(plan['node']), 'start': {key: first[key] for key in ('key', 'page', 'y', 'page_image_sha256', 'read_ids')}, 'end': {key: last[key] for key in ('key', 'page', 'y', 'kind', 'page_image_sha256', 'read_ids')}, 'scope_digest': scope_digest(value)}
    proof['signature'] = _sign('openclass-pdf-chapter-scope-v1:', proof)
    return (value, reads, proof)

def scope_digest(value):
    return content_digest({'kind': value.kind, 'start': value.start, 'end': value.end, 'container': value.container, 'start_anchor': value.start_anchor, 'end_anchor': value.end_anchor, 'end_inclusive': value.end_inclusive, 'metadata': {key: value.metadata.get(key) for key in ('page_clips', 'boundary_precision', 'boundary_verification_passes', 'boundary_verified_image_hashes', 'read_original_images')}})

def verify_scope_proof(source, value, proof):
    return value is not None and value.kind == 'pdf_pages' and isinstance(proof, dict) and _signed('openclass-pdf-chapter-scope-v1:', proof) and (proof.get('contract_version') == CONTRACT_VERSION) and all((proof.get(key) == expected for key, expected in (('source_id', source.id), ('owner_user_id', source.owner_user_id), ('package_id', source.package_id), ('content_hash', source.metadata.get('content_hash')), ('source_version', source.metadata.get('source_version'))))) and (proof.get('scope_digest') == scope_digest(value))

def verified_scopes(task, nodes, *, page_count, directory_pages=frozenset()):
    if not task.get('chapter_scope_boundaries', {}).get('points'):
        return {}
    state = _state(task)
    result = {}
    candidates = {point.get('node_key') for point in state['points'].values() if isinstance(point, dict)}
    for node in _rows(nodes):
        if node.key not in candidates:
            continue
        plan = _plan(task, nodes, page_count, directory_pages, node.key)
        if plan and (not plan['reason']) and (not state.get('plans', {}).get(plan['plan_digest'], {}).get('reason')):
            scope = _scope(task, state, plan)
            if scope is not None:
                result[node.key] = scope
    return result

def status(task, nodes, *, page_count, directory_pages=frozenset()):
    plan = _plan(task, nodes, page_count, directory_pages)
    if plan is None:
        return {'state': 'not_requested', 'reason': ''}
    if plan['reason']:
        return {'state': 'unavailable', 'reason': plan['reason']}
    state = _state(task)
    reason = state.get('plans', {}).get(plan['plan_digest'], {}).get('reason', '')
    if reason:
        return {'state': 'unavailable', 'reason': reason}
    if _scope(task, state, plan) is not None:
        return {'state': 'verified', 'reason': ''}
    if reason or _pending(task, plan) is None:
        return {'state': 'unavailable', 'reason': reason or 'boundary_order_unverified'}
    return {'state': 'pending', 'reason': ''}

def reset_unverified_target(task, nodes, *, page_count, directory_pages=frozenset()):
    plan = _plan(task, nodes, page_count, directory_pages)
    if plan and (not plan['reason']) and (status(task, nodes, page_count=page_count, directory_pages=directory_pages)['state'] == 'unavailable'):
        state = deepcopy(_state(task))
        state.get('plans', {}).pop(plan['plan_digest'], None)
        task['chapter_scope_boundaries'] = state

def progress(task, nodes, *, page_count, directory_pages=frozenset()):
    plan = _plan(task, nodes, page_count, directory_pages)
    if plan is None:
        return {}
    state = _state(task)
    saved = state.get('plans', {}).get(plan.get('plan_digest'), {})
    return {'plan_digest': plan.get('plan_digest'), 'status': status(task, nodes, page_count=page_count, directory_pages=directory_pages), 'pages': {page: [{'layout': item['layout'], 'observations': item['observations'], 'page_image_sha256': item['page_image_sha256']} for item in value['passes']] for page, value in saved.get('pages', {}).items()}}
