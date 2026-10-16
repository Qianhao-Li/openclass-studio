"""Version-bound page fragments; only a fresh whole-page check commits a unit."""
from __future__ import annotations
from copy import deepcopy
from hashlib import sha256
import math
from openclass_core.models import SourceRange
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_catalog_image_detail import validate_region
from openclass_core.services.source_catalog_refinement_evidence import RefinementNode, range_was_read, read_units
from openclass_core.services.source_content_contract import verified_read_receipt
VERSION = 1
FULL_PAGE = [0, 0, 1, 1]

def enabled(task):
    return task.get('fragment_protocol_version') == VERSION or bool(task.get('directory_fragments'))

def _binding(task):
    return {'version': VERSION, 'epoch': task.get('refinement_epoch') or task['request_id'], 'content_hash': task['content_hash'], 'source_version': task['source_version']}

def _state(task):
    value = task.get('directory_fragments')
    if value and any((value.get(key) != expected for key, expected in _binding(task).items())):
        raise GoogleSourceError('The directory fragment checkpoint belongs to another original or review epoch.')
    return value or {**_binding(task), 'pages': {}}

def _page(task, source_range):
    from openclass_core.services.source_catalog_transcription import unit_key
    value = SourceRange.model_validate(source_range)
    if value.kind != 'pdf_pages' or type(value.start) is not int or value.start != value.end or (value.start < 1):
        raise GoogleSourceError('Directory fragments require one planned physical PDF page.')
    key = unit_key(value)
    unit = next((unit for unit in task.get('directory_transcription', {}).get('units', []) if unit_key(unit['source_range']) == key), None)
    if unit is None:
        raise GoogleSourceError('Plan this original directory page before its fragments.')
    return (key, unit)

def _contains(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and (outer[2] >= inner[2]) and (outer[3] >= inner[3])

def _owns(region, point):
    return region[0] <= point[0] and (point[0] < region[2] or point[0] == region[2] == 1) and (region[1] <= point[1]) and (point[1] < region[3] or point[1] == region[3] == 1)

def _layout(fragments):
    if not isinstance(fragments, list) or not 1 <= len(fragments) <= 8:
        raise GoogleSourceError('Plan between one and eight directory fragments per page.')
    result, identities = ([], set())
    for fragment in fragments:
        identity = fragment.get('id')
        if not isinstance(identity, str) or not identity.strip() or len(identity) > 128 or (identity in identities):
            raise GoogleSourceError('Directory fragment identities must be distinct nonempty strings.')
        identities.add(identity)
        owner = validate_region(fragment.get('ownership_region'))
        observation = validate_region(fragment.get('observation_region'))
        if owner is None or observation is None or (not _contains(observation, owner)):
            raise GoogleSourceError('An observation region must contain its complete ownership region.')
        result.append({'id': identity, 'ownership_region': owner, 'observation_region': observation})
    xs = sorted({0, 1, *(x for item in result for x in item['ownership_region'][::2])})
    ys = sorted({0, 1, *(y for item in result for y in item['ownership_region'][1::2])})
    for left, right in zip(xs, xs[1:]):
        for top, bottom in zip(ys, ys[1:]):
            middle = [(left + right) / 2, (top + bottom) / 2] * 2
            if sum((_contains(item['ownership_region'], middle) for item in result)) != 1:
                raise GoogleSourceError('Directory ownership regions must cover the whole page without gaps or overlaps.')
    return result

def fragment_read_context(task, source_range, *, region=None, complete=True):
    """Host-created metadata: ties a later check to the merge actually read then."""
    if not source_range or SourceRange.model_validate(source_range).kind != 'pdf_pages':
        return {}
    from openclass_core.services.source_catalog_transcription import unit_key
    page = _state(task)['pages'].get(unit_key(source_range), {})
    return {**_binding(task), 'plan_revision': page.get('plan_revision', 0), 'merged_revision': page.get('merged_revision', 0), 'region': region, 'whole_page': region is None, 'complete': complete}

def _reads(task, source_range, read_ids, *, page=None, region=None, latest=False):
    valid = []
    requested = SourceRange.model_validate(source_range)
    for identity in read_ids:
        entry = task.get('reads', {}).get(identity, {})
        context = entry.get('catalog_fragment_context', {})
        if entry.get('request_id') != task['request_id']:
            continue
        receipt = entry.get('receipt', {})
        if not isinstance(receipt, dict) or not verified_read_receipt(receipt) or any((receipt.get(key) != value for key, value in {'read_id': identity, 'grant_id': task['request_id'], 'role': 'source_catalog_refinement', 'authorization': 'explicit', 'content_hash': task['content_hash'], 'source_version': task['source_version']}.items())) or (not range_was_read(requested, [{'locator': receipt.get('range', {})}])):
            continue
        if any((context.get(key) != value for key, value in _binding(task).items())) or not context.get('complete'):
            continue
        if not range_was_read(requested, read_units(task, [identity])):
            continue
        if page and context.get('plan_revision') != page['plan_revision']:
            continue
        if region is None:
            if not context.get('whole_page'):
                continue
        elif region != FULL_PAGE and (context.get('region') is None or not _contains(context['region'], region)):
            continue
        if latest and (context.get('merged_revision') != page['merged_revision'] or identity in page.get('verification_read_ids', [])):
            continue
        valid.append(identity)
    if not valid:
        message = 'Verification requires a new whole-page original read of the latest merged revision.' if latest else 'Directory fragments require a current original read of the requested page and observation region.'
        raise GoogleSourceError(message)
    return valid

def plan_fragments(task, source_range, fragments, read_ids, *, expected_plan_revision=None):
    key, unit = _page(task, source_range)
    layout = _layout(fragments)
    _reads(task, source_range, read_ids)
    state = deepcopy(_state(task))
    previous = state['pages'].get(key)
    if previous and expected_plan_revision != previous['plan_revision']:
        raise GoogleSourceError('The directory fragment plan changed. Read its current plan revision.')
    if not previous and expected_plan_revision not in {None, 0}:
        raise GoogleSourceError('The directory fragment plan revision does not exist.')
    old_layout = [{field: item[field] for field in ('id', 'ownership_region', 'observation_region')} for item in previous['fragments']] if previous else []
    if previous and old_layout == layout:
        return fragment_work(task)
    old = {item['id']: item for item in (previous or {}).get('fragments', [])}
    planned = []
    for item in layout:
        saved = old.get(item['id'], {})
        preserve = all((saved.get(field) == item[field] for field in item))
        planned.append({**item, 'rows': saved.get('rows', []) if preserve else [], 'read_ids': saved.get('read_ids', []) if preserve else [], 'complete': saved.get('complete', False) if preserve else False})
    state['pages'][key] = {'source_range': unit['source_range'], 'plan_revision': (previous or {}).get('plan_revision', 0) + 1, 'merged_revision': (previous or {}).get('merged_revision', 0) + 1, 'fragments': planned, 'verified_revision': None, 'verification_read_ids': []}
    task['directory_fragments'] = state
    unit['complete'] = False
    unit['reviewed_for_request_id'] = ''
    return fragment_work(task)

def _merged(page):
    result, positions = ([], {})
    for fragment in page['fragments']:
        for row in fragment['rows']:
            node = row['node']
            if node['key'] in positions:
                if row.get('continues_key') != node['key']:
                    raise GoogleSourceError('A repeated row key must explicitly continue its original fragment row.')
                prior = result[positions[node['key']]]
                if prior['level'] != node['level']:
                    raise GoogleSourceError('A continued row must preserve its hierarchy level.')
                result[positions[node['key']]] = node
            else:
                if row.get('continues_key'):
                    raise GoogleSourceError('A continued row must reference a preceding stable row key.')
                positions[node['key']] = len(result)
                result.append(node)
    return result

def transcribe_fragment(task, source_range, fragment_id, *, plan_revision, read_ids, nodes, complete, remove_keys=(), ordered_keys=None):
    key, _ = _page(task, source_range)
    state = deepcopy(_state(task))
    page = state['pages'].get(key)
    if not page or page['plan_revision'] != plan_revision:
        raise GoogleSourceError('The directory fragment plan revision changed.')
    index = next((index for index, item in enumerate(page['fragments']) if item['id'] == fragment_id), None)
    if index is None:
        raise GoogleSourceError('Unknown directory fragment.')
    if any((not item['complete'] for item in page['fragments'][:index])):
        raise GoogleSourceError('Complete the preceding fragments in original reading order first.')
    fragment = page['fragments'][index]
    valid = _reads(task, source_range, read_ids, page=page, region=fragment['observation_region'])
    rows = {row['node']['key']: row for row in fragment['rows']}
    for identity in remove_keys:
        if identity not in rows:
            raise GoogleSourceError('Only a row owned by this fragment may be removed.')
        rows.pop(identity)
    seen = set()
    for raw in nodes:
        raw = dict(raw)
        point, continues = (raw.pop('ownership_point', None), raw.pop('continues_key', ''))
        if not isinstance(point, list) or len(point) != 2 or any((type(value) not in (float, int) or not math.isfinite(value) for value in point)) or (not _owns(fragment['ownership_region'], point)):
            raise GoogleSourceError("The row ownership point must lie inside this fragment's ownership region.")
        node = RefinementNode.model_validate(raw)
        if node.source_range is not None or node.range_complete or node.read_ids or node.page_label_reviewed:
            raise GoogleSourceError('Directory fragment observations cannot authorize chapter citation ranges.')
        if node.key in seen or (continues and continues != node.key):
            raise GoogleSourceError('Fragment row keys must be distinct and continuation must keep the original key.')
        seen.add(node.key)
        node.directory_read_ids = valid
        rows[node.key] = {'node': node.model_dump(mode='json'), 'ownership_point': point, 'continues_key': continues}
    if ordered_keys is not None:
        if not isinstance(ordered_keys, list) or any((not isinstance(identity, str) for identity in ordered_keys)) or len(ordered_keys) != len(rows) or (set(ordered_keys) != set(rows)):
            raise GoogleSourceError('Fragment order must name every retained row key exactly once.')
        updated = [rows[identity] for identity in ordered_keys]
    else:
        updated = list(rows.values())
    if complete:
        from openclass_core.services.source_catalog_heading_inventory import require_fragment
        require_fragment(task, source_range, fragment_id, updated)
    changed = updated != fragment['rows'] or bool(complete) != fragment['complete']
    fragment.update(rows=updated, complete=bool(complete), read_ids=valid)
    _merged(page)
    if changed:
        page['merged_revision'] += 1
        page['verified_revision'] = None
    task['directory_fragments'] = state
    return fragment_work(task)

def verify_page(task, source_range, *, plan_revision, merged_revision, read_ids, verified, retry_fragment_ids=()):
    from openclass_core.services.source_catalog_transcription import transcribe_unit
    key, unit = _page(task, source_range)
    state = deepcopy(_state(task))
    page = state['pages'].get(key)
    if not page or page['plan_revision'] != plan_revision or page['merged_revision'] != merged_revision:
        raise GoogleSourceError('The merged directory page changed; inspect the latest merge before verification.')
    if not all((item['complete'] for item in page['fragments'])):
        raise GoogleSourceError('Every directory fragment must be complete before whole-page verification.')
    valid = _reads(task, source_range, read_ids, page=page, latest=True)
    page['verification_read_ids'] = list(dict.fromkeys([*page['verification_read_ids'], *valid]))
    if not verified:
        retry = set(retry_fragment_ids or [item['id'] for item in page['fragments']])
        if not retry <= {item['id'] for item in page['fragments']}:
            raise GoogleSourceError('Only planned directory fragments may be reopened.')
        for item in page['fragments']:
            if item['id'] in retry:
                item['complete'] = False
        page['verified_revision'] = None
        page['merged_revision'] += 1
        task['directory_fragments'] = state
        unit['complete'] = False
        unit['reviewed_for_request_id'] = ''
        return fragment_work(task)
    from openclass_core.services.source_catalog_heading_inventory import require_page
    require_page(task, source_range, _merged(page))
    page['verified_revision'] = merged_revision
    previous = task.get('directory_fragments')
    task['directory_fragments'] = state
    try:
        transcribe_unit(task, source_range, _merged(page), valid, complete=True, reconciliation_complete=bool(task.get('directory_reconciliation', {}).get(key)), _verified_fragment_page=True)
    except Exception:
        task['directory_fragments'] = previous
        raise
    return fragment_work(task)

def require_verified_page(task, source_range, nodes):
    if not enabled(task) or SourceRange.model_validate(source_range).kind != 'pdf_pages':
        return
    key, _ = _page(task, source_range)
    page = _state(task)['pages'].get(key)
    if page is None and task.get('fragment_protocol_version') != VERSION:
        return

    def semantic(rows):
        return [{key: row.get(key, default) for key, default in (('key', ''), ('title', ''), ('level', 1), ('printed_page_label', ''))} for row in rows]
    if not page or not all((fragment['complete'] for fragment in page['fragments'])) or page['verified_revision'] != page['merged_revision'] or (semantic(nodes) != semantic(_merged(page))):
        raise GoogleSourceError('Complete and verify the latest directory fragments before committing the whole page.')

def fragments_complete(task):
    from openclass_core.services.source_catalog_heading_inventory import coverage_complete
    if not coverage_complete(task):
        return False
    if not enabled(task):
        return True
    state = _state(task)
    from openclass_core.services.source_catalog_transcription import unit_key
    keys = {unit_key(unit['source_range']) for unit in task.get('directory_transcription', {}).get('units', [])}
    return all((page['verified_revision'] == page['merged_revision'] and all((item['complete'] for item in page['fragments'])) for key, page in state['pages'].items() if key in keys))

def fragment_work(task):
    if not enabled(task):
        return None
    from openclass_core.services.source_catalog_heading_inventory import work as heading_work, constraints, enabled as headings_enabled
    inventory = heading_work(task)
    if inventory is not None:
        return inventory
    from openclass_core.services.source_catalog_heading_lines import work as line_work
    line = line_work(task)
    if line is not None:
        return line
    from openclass_core.services.source_catalog_transcription import unit_key
    state = _state(task)
    directory = task.get('directory_transcription', {})
    units = directory.get('units', [])
    pending = task.get('directory_reconciliation', {})
    target = next((unit for unit in units if unit_key(unit['source_range']) in pending), None)
    if target is None:
        target = next((unit for unit in units if not unit['complete'] or (directory.get('review_request_id') and unit.get('reviewed_for_request_id') != directory['review_request_id']) or (headings_enabled(task) and unit['source_range']['kind'] == 'pdf_pages' and (unit_key(unit['source_range']) not in state['pages']))), None)
    if target is None or target['source_range']['kind'] != 'pdf_pages':
        return None
    ancestors = []

    def append_ancestors(rows):
        for node in rows:
            while ancestors and ancestors[-1]['level'] >= node.get('level', 1):
                ancestors.pop()
            ancestors.append({key: node.get(key, 1 if key == 'level' else '') for key in ('key', 'title', 'level')})
    for unit in units:
        if unit is target:
            break
        append_ancestors(unit['nodes'])
    preceding_page_ancestors = list(ancestors)
    key = unit_key(target['source_range'])
    page = state['pages'].get(key)
    base = {'source_range': target['source_range'], 'open_ancestors': ancestors}
    if page is None:
        if task.get('fragment_protocol_version') != VERSION and (not headings_enabled(task)):
            return None
        return {**base, 'stage': 'plan_directory_fragments', 'maximum_fragments': 8}
    base.update(plan_revision=page['plan_revision'], merged_revision=page['merged_revision'])
    for fragment in page['fragments']:
        if not fragment['complete']:
            return {**base, 'stage': 'transcribe_directory_fragment', 'fragment_id': fragment['id'], 'region': fragment['observation_region'], 'ownership_region': fragment['ownership_region'], 'current_rows': [row['node'] for row in fragment['rows']], 'required_headings': constraints(task, target['source_range'], fragment['id'])}
        append_ancestors([row['node'] for row in fragment['rows'] if not row.get('continues_key')])
    return {**base, 'stage': 'verify_directory_page', 'current_rows': _merged(page), 'open_ancestors': preceding_page_ancestors, 'required_headings': constraints(task, target['source_range']), 'fragment_ids': [item['id'] for item in page['fragments']]}

def fragment_progress(task):
    if not enabled(task):
        return {}
    from openclass_core.services.source_catalog_heading_inventory import _inventory, enabled as headings_enabled
    state = _state(task)

    def heading_progress(page):
        observed = page.get('heading_inventory')
        if not isinstance(observed, dict):
            return None
        verified = _inventory(task, page) if headings_enabled(task) else None
        return {'plan_revision': observed.get('plan_revision'), 'verified_schema_version': verified.get('schema_version') if verified else None, 'headings': [{field: heading.get(field) for field in ('title', 'level', 'ownership_point')} for heading in observed.get('headings', [])]}
    return {key: {'plan_revision': page['plan_revision'], 'verified': page['verified_revision'] == page['merged_revision'], 'heading_inventory': heading_progress(page), 'fragments': [{'id': item['id'], 'complete': item['complete'], 'nodes': [{field: row['node'][field] for field in ('key', 'title', 'level', 'printed_page_label')} for row in item['rows']]} for item in page['fragments']]} for key, page in state['pages'].items()}

def recover_timed_out_fragment(task, work, *, reason, request_id, content_hash, source_version):
    """Shrink unfinished work only; the caller owns the unchanged run budget."""
    if reason != 'step_timeout' or work.get('stage') != 'transcribe_directory_fragment' or (not enabled(task)):
        return None
    if any((task.get(key) != expected for key, expected in (('request_id', request_id), ('content_hash', content_hash), ('source_version', source_version)))):
        raise GoogleSourceError('The fragment recovery request or original version changed.')
    key, unit = _page(task, work.get('source_range'))
    state = deepcopy(_state(task))
    page = state['pages'].get(key)
    if not page or any((work.get(field) != page[field] for field in ('plan_revision', 'merged_revision'))):
        raise GoogleSourceError('The directory fragment revision changed before timeout recovery.')
    index = next((i for i, item in enumerate(page['fragments']) if item['id'] == work.get('fragment_id')), None)
    if index is None or page['fragments'][index]['complete'] or len(page['fragments']) >= 8 or any((not item['complete'] for item in page['fragments'][:index])):
        return None
    fragment = page['fragments'][index]
    owner, observation = (fragment['ownership_region'], fragment['observation_region'])
    points = [row.get('ownership_point') for row in fragment['rows']]
    if any((not isinstance(point, list) or len(point) != 2 or any((type(value) not in (int, float) or not math.isfinite(value) for value in point)) or (not _owns(owner, point)) for point in points)):
        return None
    original_rows = _merged(page)
    for axis in sorted((1, 0), key=lambda axis: owner[axis + 2] - owner[axis], reverse=True):
        middle = (owner[axis] + owner[axis + 2]) / 2
        if not owner[axis] < middle < owner[axis + 2]:
            continue
        groups = [int(point[axis] >= middle) for point in points]
        if groups != sorted(groups):
            continue
        identity = sha256(f"{request_id}:{key}:{page['plan_revision']}:{fragment['id']}".encode()).hexdigest()[:24]
        children = []
        context = (owner[axis + 2] - owner[axis]) * 0.05
        for half in (0, 1):
            owned, observed = (list(owner), list(observation))
            owned[axis + 2 if half == 0 else axis] = middle
            observed[axis + 2 if half == 0 else axis] = min(observation[axis + 2], middle + context) if half == 0 else max(observation[axis], middle - context)
            children.append({'id': f'fragment_{identity}_{half}', 'ownership_region': owned, 'observation_region': observed, 'rows': [row for row, group in zip(fragment['rows'], groups, strict=True) if group == half], 'read_ids': list(fragment['read_ids']), 'complete': False})
        candidate = {**page, 'fragments': [*page['fragments'][:index], *children, *page['fragments'][index + 1:]]}
        _layout(candidate['fragments'])
        if _merged(candidate) != original_rows:
            continue
        candidate.update(plan_revision=page['plan_revision'] + 1, merged_revision=page['merged_revision'] + 1, verified_revision=None, verification_read_ids=[])
        state['pages'][key] = candidate
        task['directory_fragments'] = state
        unit['complete'], unit['reviewed_for_request_id'] = (False, '')
        return fragment_work(task)
    return None
