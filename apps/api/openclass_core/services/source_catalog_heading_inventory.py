"""Independent original-page heading observations, separate from draft catalog rows."""
from __future__ import annotations
from collections import defaultdict, deque
from copy import deepcopy
import math
from openclass_core.models import SourceRange
from openclass_core.services.google_sources.config import GoogleSourceError
VERSION = 2

def enabled(task):
    return type(task.get('heading_inventory_protocol_version')) is int and task['heading_inventory_protocol_version'] in {1, VERSION}

def _binding(task, page):
    return {'schema_version': task['heading_inventory_protocol_version'], 'epoch': task.get('refinement_epoch') or task['request_id'], 'content_hash': task['content_hash'], 'source_version': task['source_version'], 'plan_revision': page['plan_revision']}

def _owners(page, headings):
    from openclass_core.services.source_catalog_fragments import _owns
    owners = []
    for heading in headings:
        found = [fragment['id'] for fragment in page['fragments'] if _owns(fragment['ownership_region'], heading['ownership_point'])]
        if len(found) != 1:
            raise GoogleSourceError('An observed heading must belong to exactly one planned fragment.')
        owners.append(found[0])
    return owners

def _headings(page, values):
    if not isinstance(values, list):
        raise GoogleSourceError('Heading inventory must be an explicit list, including an empty list when none are present.')
    for heading in values:
        if not isinstance(heading, dict) or set(heading) != {'title', 'level', 'ownership_point'}:
            raise GoogleSourceError('Each heading observation requires only a title, level and ownership point.')
        if not isinstance(heading['title'], str) or not heading['title'].strip() or type(heading['level']) is not int or (heading['level'] < 1):
            raise GoogleSourceError('A heading needs a nonempty literal title and positive integer level.')
        point = heading['ownership_point']
        if not isinstance(point, list) or len(point) != 2 or any((type(value) not in (int, float) or not math.isfinite(value) or (not 0 <= value <= 1) for value in point)):
            raise GoogleSourceError('A heading ownership point requires two finite normalized page coordinates.')
    owners = _owners(page, values)
    ranks = {fragment['id']: index for index, fragment in enumerate(page['fragments'])}
    for previous, current, earlier, later in zip(values, values[1:], owners, owners[1:]):
        if ranks[later] < ranks[earlier] or (later == earlier and current['ownership_point'][1] < previous['ownership_point'][1]):
            raise GoogleSourceError('Heading reading order contradicts the planned regions or observed vertical positions. List original headings in region order and top-to-bottom, not grouped by hierarchy level; split the plan if one fragment contains multiple reading columns.')
    return deepcopy(values)

def _inventory(task, page):
    from openclass_core.services.source_catalog_refinement_evidence import read_units, range_was_read
    observed = page.get('heading_inventory')
    if not isinstance(observed, dict) or any((observed.get(key) != value for key, value in _binding(task, page).items())):
        return None
    revision = observed.get('observed_merged_revision')
    if type(revision) is not int or revision < 1 or revision > page['merged_revision']:
        return None
    ids = observed.get('read_ids')
    if not isinstance(ids, list) or not ids or any((not isinstance(identity, str) for identity in ids)):
        return None
    if not range_was_read(SourceRange.model_validate(page['source_range']), read_units(task, ids)):
        return None
    try:
        _headings(page, observed.get('headings'))
        if task['heading_inventory_protocol_version'] >= 2:
            from openclass_core.services.source_catalog_heading_detail import detail_reads
            detail_reads(task, page, ids, observed['headings'], merged_revision=revision)
    except GoogleSourceError:
        return None
    return observed

def _get_page(task, source_range):
    from openclass_core.services.source_catalog_fragments import _state, _page
    key, unit = _page(task, source_range)
    return (key, unit, _state(task)['pages'].get(key))

def _node(value):
    if hasattr(value, 'model_dump'):
        value = value.model_dump()
    value = value.get('node', value) if isinstance(value, dict) else None
    if not isinstance(value, dict) or not isinstance(value.get('title'), str) or type(value.get('level', 1)) is not int or (value.get('level', 1) < 1):
        raise GoogleSourceError('Heading validation requires well-formed current directory rows.')
    return value

def _key(value):
    return (''.join(value['title'].split()), value.get('level', 1))

def _positions(rows, headings):
    if not isinstance(rows, list):
        raise GoogleSourceError('Heading validation requires an explicit list of current directory rows.')
    available = defaultdict(deque)
    for index, row in enumerate(rows):
        available[_key(_node(row))].append(index)
    return [available[_key(heading)].popleft() if available[_key(heading)] else None for heading in headings]

def _matches(rows, headings):
    positions = _positions(rows, headings)
    return all((position is not None for position in positions)) and all((a < b for a, b in zip(positions, positions[1:])))

def work(task):
    if not enabled(task):
        return None
    from openclass_core.services.source_catalog_fragments import _state
    from openclass_core.services.source_catalog_transcription import unit_key
    pages = _state(task)['pages']
    for unit in task.get('directory_transcription', {}).get('units', []):
        source_range = unit['source_range']
        if source_range.get('kind') != 'pdf_pages':
            continue
        page = pages.get(unit_key(source_range))
        if page is not None and _inventory(task, page) is None:
            return {'stage': 'verify_directory_page', 'inspection_mode': 'heading_inventory', 'source_range': {key: source_range[key] for key in ('kind', 'start', 'end')}, 'plan_revision': page['plan_revision'], 'merged_revision': page['merged_revision'], 'current_rows': [], 'high_resolution_details_required': task['heading_inventory_protocol_version'] >= 2, 'reading_regions': [{'order': index, 'ownership_region': list(fragment['ownership_region'])} for index, fragment in enumerate(page['fragments'])]}
    return None

def record(task, source_range, *, plan_revision, merged_revision, read_ids, headings):
    from openclass_core.services.source_catalog_fragments import _state, _reads, _merged
    if not enabled(task):
        raise GoogleSourceError('The independent heading inventory protocol is not enabled.')
    key, unit, current = _get_page(task, source_range)
    if current is None or type(plan_revision) is not int or type(merged_revision) is not int or (current['plan_revision'] != plan_revision) or (current['merged_revision'] != merged_revision):
        raise GoogleSourceError('The heading inventory page or draft revision changed.')
    if not isinstance(read_ids, list) or not read_ids or any((not isinstance(identity, str) or not identity for identity in read_ids)):
        raise GoogleSourceError('Heading inventory requires fresh whole-page original read identities.')
    headings = _headings(current, headings)
    checked = {**current, 'verification_read_ids': [*current.get('verification_read_ids', []), *current.get('heading_inventory', {}).get('read_ids', [])] if isinstance(current.get('heading_inventory', {}), dict) else current.get('verification_read_ids', [])}
    valid = _reads(task, source_range, read_ids, page=checked, latest=True)
    if task['heading_inventory_protocol_version'] >= 2:
        from openclass_core.services.source_catalog_heading_detail import detail_reads
        valid += detail_reads(task, checked, read_ids, headings, merged_revision=merged_revision, fresh=True)
    owners = _owners(current, headings)
    bad = set()
    for fragment in current['fragments']:
        required = [heading for heading, owner in zip(headings, owners, strict=True) if owner == fragment['id']]
        if not _matches(fragment['rows'], required):
            bad.add(fragment['id'])
    positions = _positions(_merged(current), headings)
    previous = None
    for index, position in enumerate(positions):
        if position is None:
            bad.add(owners[index])
        elif previous is not None and positions[previous] >= position:
            bad.update((owners[previous], owners[index]))
        if position is not None:
            previous = index
    state = deepcopy(_state(task))
    page = state['pages'][key]
    page['heading_inventory'] = {**_binding(task, page), 'observed_merged_revision': merged_revision, 'read_ids': list(dict.fromkeys(valid)), 'headings': headings}
    if bad:
        for fragment in page['fragments']:
            if fragment['id'] in bad:
                fragment['complete'] = False
        page['verified_revision'] = None
        page['merged_revision'] += 1
        unit['complete'] = False
        unit['reviewed_for_request_id'] = ''
    task['directory_fragments'] = state
    return {'saved': True, 'heading_count': len(headings), 'plan_revision': plan_revision, 'observed_merged_revision': merged_revision, 'reopened_fragment_ids': [fragment['id'] for fragment in page['fragments'] if fragment['id'] in bad]}

def constraints(task, source_range, fragment_id=None):
    if not enabled(task):
        return []
    _, _, page = _get_page(task, source_range)
    observed = _inventory(task, page) if page is not None else None
    if observed is None:
        return []
    headings = observed['headings']
    if fragment_id is not None:
        if fragment_id not in {fragment['id'] for fragment in page['fragments']}:
            raise GoogleSourceError('Unknown heading inventory fragment.')
        headings = [heading for heading, owner in zip(headings, _owners(page, headings), strict=True) if owner == fragment_id]
    return deepcopy(headings)

def _require_inventory(task, source_range):
    _, _, page = _get_page(task, source_range)
    observed = _inventory(task, page) if page is not None else None
    if observed is None:
        raise GoogleSourceError("Independently read and record this original page's heading inventory before completion.")
    return (page, observed)

def require_fragment(task, source_range, fragment_id, rows):
    if not enabled(task):
        return
    _require_inventory(task, source_range)
    if not _matches(rows, constraints(task, source_range, fragment_id)):
        raise GoogleSourceError('Current fragment rows must include every independently observed heading, with its level and original order.')

def require_page(task, source_range, nodes):
    if not enabled(task):
        return
    _, observed = _require_inventory(task, source_range)
    if not _matches(nodes, observed['headings']):
        raise GoogleSourceError('Current page rows must include every independently observed heading, with its level and original order.')

def coverage_complete(task):
    if not enabled(task):
        return True
    from openclass_core.services.source_catalog_fragments import _merged
    units = task.get('directory_transcription', {}).get('units', [])
    if not units:
        return False
    try:
        for unit in units:
            if unit['source_range'].get('kind') != 'pdf_pages':
                return False
            _, _, page = _get_page(task, unit['source_range'])
            if page is None or _inventory(task, page) is None:
                return False
            for fragment in page['fragments']:
                require_fragment(task, unit['source_range'], fragment['id'], fragment['rows'])
            require_page(task, unit['source_range'], _merged(page))
    except GoogleSourceError:
        return False
    return True
