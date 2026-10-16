"""Independent, detail-only literal review of primary directory headings."""
from __future__ import annotations
from copy import deepcopy
from collections import Counter
import hashlib
import hmac
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_content_contract import content_digest, verified_read_receipt
VERSION = 1

def enabled(task):
    return type(task.get('heading_line_protocol_version')) is int and task['heading_line_protocol_version'] == VERSION

def _binding(task):
    return {'schema_version': VERSION, 'epoch': task.get('refinement_epoch') or task['request_id'], 'content_hash': task['content_hash'], 'source_version': task['source_version']}

def _signature(value):
    from openclass_core.services.source_content_contract import _receipt_key
    payload = {key: item for key, item in value.items() if key != 'signature'}
    return hmac.new(_receipt_key(), ('openclass-heading-line-v1:' + content_digest(payload)).encode(), hashlib.sha256).hexdigest()

def _wording_conflicts(task, page, inventory):
    """Pair one displaced primary row between two unique exact page-local anchors."""
    from openclass_core.services.source_catalog_transcription import unit_key
    archived = task.get('directory_clue_archive', {}).get(unit_key(page['source_range']), [])
    if not isinstance(archived, list):
        return {}
    earlier = [row['title'] for row in archived if isinstance(row, dict) and row.get('level') == 1 and isinstance(row.get('title'), str) and row['title'].strip()]
    current = [(index, row['title']) for index, row in enumerate(inventory['headings']) if row['level'] == 1]
    normalize = lambda title: ''.join(title.split())
    old_keys, new_keys = (list(map(normalize, earlier)), [normalize(title) for _, title in current])
    old_counts, new_counts = (Counter(old_keys), Counter(new_keys))
    old_positions = {key: index for index, key in enumerate(old_keys) if old_counts[key] == 1}
    result = {}
    for position in range(1, len(current) - 1):
        left, right = (new_keys[position - 1], new_keys[position + 1])
        if not all((old_counts[key] == new_counts[key] == 1 for key in (left, right))):
            continue
        start, end = (old_positions[left], old_positions[right])
        if end != start + 2:
            continue
        previous, observed = (old_keys[start + 1], new_keys[position])
        if previous == observed or old_counts[previous] != 1 or new_counts[observed] != 1 or new_counts[previous] or old_counts[observed]:
            continue
        result[current[position][0]] = [current[position][1], earlier[start + 1]]
    return result

def _fingerprint(task, page, inventory, index):
    payload = {**_binding(task), 'source_range': page['source_range'], 'plan_revision': page['plan_revision'], 'inventory_schema': inventory['schema_version'], 'inventory_revision': inventory['observed_merged_revision'], 'index': index, 'heading': inventory['headings'][index]}
    candidates = _wording_conflicts(task, page, inventory).get(index)
    if candidates:
        payload['wording_candidates_digest'] = content_digest(sorted((''.join(title.split()) for title in candidates)))
    return content_digest(payload)

def _candidates(task):
    if not enabled(task):
        return []
    from openclass_core.services.source_catalog_fragments import _state
    from openclass_core.services.source_catalog_heading_inventory import _inventory, _owners
    from openclass_core.services.source_catalog_transcription import unit_key
    state, result = (_state(task), [])
    for unit in task.get('directory_transcription', {}).get('units', []):
        key = unit_key(unit['source_range'])
        page = state['pages'].get(key)
        inventory = _inventory(task, page) if page else None
        if inventory is None or inventory.get('schema_version', 0) < 2:
            continue
        conflicts = _wording_conflicts(task, page, inventory)
        owners = _owners(page, inventory['headings'])
        for index, heading in enumerate(inventory['headings']):
            if heading['level'] != 1:
                continue
            fragment = next((item for item in page['fragments'] if item['id'] == owners[index]))
            x0, _, x1, _ = fragment['ownership_region']
            y = heading['ownership_point'][1]
            region = [x0, max(0.0, round(y - 0.015, 6)), x1, min(1.0, round(y + 0.055, 6))]
            line_id = 'heading-line:' + content_digest({**_binding(task), 'page': key, 'plan_revision': page['plan_revision'], 'index': index, 'ownership_point': heading['ownership_point']})[:32]
            result.append({'page_key': key, 'page': page, 'unit': unit, 'inventory': inventory, 'index': index, 'heading': heading, 'work': {'stage': 'review_directory_heading_line', 'inspection_mode': 'heading_line', 'line_id': line_id, 'source_range': {'kind': 'pdf_pages', 'start': unit['source_range']['start'], 'end': unit['source_range']['end']}, 'fragment_id': fragment['id'], 'plan_revision': page['plan_revision'], 'merged_revision': page['merged_revision'], 'inventory_fingerprint': _fingerprint(task, page, inventory, index), 'region': region, 'level': 1, **({'wording_candidates': conflicts[index]} if index in conflicts else {})}})
    return result

def _verified(task, candidate):
    current = candidate['work']
    proof = candidate['page'].get('heading_lines', {}).get(current['line_id'])
    try:
        return isinstance(proof, dict) and isinstance(proof.get('signature'), str) and hmac.compare_digest(proof['signature'], _signature(proof)) and all((proof.get(key) == value for key, value in _binding(task).items())) and (proof.get('line_id') == current['line_id']) and (proof.get('inventory_fingerprint') == current['inventory_fingerprint']) and (proof.get('literal_title') == candidate['heading']['title'])
    except (TypeError, ValueError):
        return False

def work(task):
    candidate = next((item for item in _candidates(task) if not _verified(task, item)), None)
    return deepcopy(candidate['work']) if candidate else None

def read_context(task):
    current = work(task)
    step = task.get('execution', {}).get('current_step', {})
    if not current or step.get('stage') != current['stage'] or step.get('finished_at') or (not step.get('id')) or (step.get('line_id') != current['line_id']) or (step.get('inventory_fingerprint') != current['inventory_fingerprint']) or (step.get('source_range') != current['source_range']):
        return {}
    return {**_binding(task), **{key: current[key] for key in ('line_id', 'source_range', 'fragment_id', 'plan_revision', 'merged_revision', 'inventory_fingerprint', 'region')}, 'host_step_id': step['id'], 'delivery': 'heading_line_detail_only'}

def record(source, task, *, line_id, inventory_fingerprint, read_ids, literal_title):
    current = work(task)
    context = read_context(task)
    if not current or not context or line_id != current['line_id'] or (inventory_fingerprint != current['inventory_fingerprint']):
        raise GoogleSourceError('The independent heading line or host step changed.')
    if not isinstance(literal_title, str) or not literal_title.strip() or len(literal_title) > 1000:
        raise GoogleSourceError('Return the nonempty literal heading only.')
    if not isinstance(read_ids, list) or not read_ids or any((not isinstance(item, str) for item in read_ids)):
        raise GoogleSourceError('The heading line needs its current original detail read.')
    expected = {'owner_user_id': source.owner_user_id, 'package_id': source.package_id, 'source_id': source.id, 'content_hash': task['content_hash'], 'source_version': task['source_version'], 'grant_id': task['request_id'], 'role': 'source_catalog_refinement', 'authorization': 'explicit'}
    if any((source.metadata.get(key) != task[key] for key in ('content_hash', 'source_version'))):
        raise GoogleSourceError('The heading line original version changed.')
    accepted = []
    for identity in read_ids:
        entry = task.get('reads', {}).get(identity, {})
        receipt = entry.get('receipt', {})
        if entry.get('request_id') != task['request_id'] or entry.get('line_read_context') != context or receipt.get('read_id') != identity or (not verified_read_receipt(receipt)) or any((receipt.get(key) != value for key, value in expected.items())):
            continue
        scope = receipt.get('range', {})
        if any((scope.get(key) != value for key, value in current['source_range'].items())):
            continue
        for detail in entry.get('image_details', []):
            if detail.get('detail_source') == 'original_pdf' and type(detail.get('render_dpi')) is int and (detail['render_dpi'] >= 300) and (detail.get('region') == current['region']) and (detail.get('physical_page') == current['source_range']['start']) and (detail.get('source_content_hash') == task['content_hash']) and (detail.get('source_version') == task['source_version']) and all((isinstance(detail.get(key), str) and len(detail[key]) == 64 and all((char in '0123456789abcdef' for char in detail[key])) for key in ('sha256', 'parent_sha256'))):
                accepted.append({'read_id': identity, 'sha256': detail['sha256'], 'parent_sha256': detail['parent_sha256']})
    if not accepted:
        raise GoogleSourceError("The literal heading needs this host step's exact high-resolution original PDF line detail.")
    from openclass_core.services.source_catalog_fragments import _state
    candidate = next((item for item in _candidates(task) if item['work']['line_id'] == line_id))
    state = deepcopy(_state(task))
    page = state['pages'][candidate['page_key']]
    inventory = page['heading_inventory']
    observed_literal = literal_title.strip()
    previous_title = inventory['headings'][candidate['index']]['title']
    changed = ''.join(previous_title.split()) != ''.join(observed_literal.split())
    title = observed_literal if changed else previous_title
    inventory['headings'][candidate['index']]['title'] = title
    proof = {**_binding(task), **{key: expected[key] for key in ('source_id', 'owner_user_id', 'package_id')}, 'line_id': line_id, 'request_id': task['request_id'], 'host_step_id': context['host_step_id'], 'source_range': current['source_range'], 'region': current['region'], 'observed_inventory_fingerprint': inventory_fingerprint, 'inventory_fingerprint': _fingerprint(task, page, inventory, candidate['index']), 'literal_title': title, 'observed_literal': observed_literal, 'details': accepted}
    if current.get('wording_candidates'):
        proof['observed_wording_candidates'] = current['wording_candidates']
    proof['signature'] = _signature(proof)
    page.setdefault('heading_lines', {})[line_id] = proof
    if changed:
        next((item for item in page['fragments'] if item['id'] == current['fragment_id']))['complete'] = False
        page['verified_revision'] = None
        page['merged_revision'] += 1
    task['directory_fragments'] = state
    if changed:
        candidate['unit']['complete'] = False
        candidate['unit']['reviewed_for_request_id'] = ''
    return {'saved': True, 'line_id': line_id, 'changed': changed, 'reopened_fragment_ids': [current['fragment_id']] if changed else []}

def coverage_complete(task):
    if not enabled(task):
        return True
    from openclass_core.services.source_catalog_heading_inventory import _inventory
    from openclass_core.services.source_catalog_fragments import _state
    from openclass_core.services.source_catalog_transcription import unit_key
    units = task.get('directory_transcription', {}).get('units', [])
    if not units:
        return False
    pages = _state(task)['pages']
    for unit in units:
        page = pages.get(unit_key(unit['source_range']))
        inventory = _inventory(task, page) if page else None
        if inventory is None or inventory.get('schema_version', 0) < 2:
            return False
    return all((_verified(task, item) for item in _candidates(task)))

def progress(task):
    return {item['work']['line_id']: {'inventory_fingerprint': item['work']['inventory_fingerprint'], 'verified': bool(_verified(task, item))} for item in _candidates(task)}
