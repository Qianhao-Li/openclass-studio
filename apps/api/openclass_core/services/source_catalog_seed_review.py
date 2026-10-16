"""Review existing, page-attributed directory rows against fresh original reads."""
from copy import deepcopy
from openclass_core.models import SourceRange
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_content_contract import content_digest

def _page(value):
    try:
        scope = SourceRange.model_validate(value)
    except (TypeError, ValueError):
        return None
    return scope.start if scope.kind == 'pdf_pages' and type(scope.start) is int and (scope.start > 0) and (scope.start == scope.end) and (not scope.start_anchor) and (not scope.end_anchor) and (not scope.container) and (not any((scope.metadata.get(key) for key in ('page_clips', 'crop_bbox', 'boundary_precision')))) else None

def _binding(source):
    return {'source_id': source.id, 'owner_user_id': source.owner_user_id, 'package_id': source.package_id, 'content_hash': source.metadata.get('content_hash'), 'source_version': source.metadata.get('source_version')}

def seed_directory(source, chapters, *, catalog_version):
    """Upstream rows are review candidates, never imported Pi read receipts."""
    from openclass_core.services.source_catalog_refinement_evidence import RefinementNode
    from openclass_core.services.source_catalog_transcription import unit_key
    meta = source.metadata
    if source.mime_type != 'application/pdf' or meta.get('catalog_engine') != 'anythingllm_source_catalog_v2' or meta.get('catalog_origin') != 'authored' or (meta.get('directory_status') != 'complete') or (meta.get('anythingllm_catalog_ready') is not True) or (not chapters) or (type(catalog_version) is not int) or (catalog_version < 1):
        return None
    binding = _binding(source)

    def matches(item):
        return isinstance(item, dict) and all((item.get(key) == binding[key] for key in ('content_hash', 'source_version')))
    evidence = meta.get('catalog_directory_evidence')
    coverage = meta.get('catalog_read_ranges')
    if not isinstance(evidence, list) or not evidence or (not isinstance(coverage, list)):
        return None
    pages = {}
    for item in evidence:
        number = _page(item.get('locator')) if matches(item) else None
        if number is None or item.get('status') != 'model_observed' or item.get('unit_id') != number:
            return None
        pages[number] = []
    covered = set()
    for item in coverage:
        if not matches(item):
            return None
        try:
            loc = SourceRange.model_validate(item.get('locator'))
        except (TypeError, ValueError):
            return None
        if loc.kind == 'pdf_pages' and type(loc.start) is int and (type(loc.end) is int):
            covered.update((page for page in pages if loc.start <= page <= loc.end))
    if set(pages) - covered:
        return None
    seen, previous = (set(), 0)
    for index, chapter in enumerate(chapters):
        if chapter.id in seen or not chapter.title.strip() or chapter.order_index != index or (chapter.catalog_version != catalog_version) or (chapter.source_content_hash != binding['content_hash']) or (chapter.source_ingestion_id != source.id) or (chapter.owner_user_id != source.owner_user_id) or (chapter.package_id != source.package_id):
            return None
        item = chapter.metadata.get('directory_evidence')
        number = _page(item.get('locator')) if matches(item) else None
        if number not in pages or number < previous or item.get('unit_id') != number or (item.get('status') != 'model_observed'):
            return None
        seen.add(chapter.id)
        previous = number
        row = RefinementNode(key=chapter.id, title=chapter.title, level=chapter.level, printed_page_label=str(chapter.metadata.get('printed_page') or '')).model_dump(mode='json')
        pages[number].append(row)
    if any((not rows for rows in pages.values())):
        return None
    units, fingerprints = ([], {})
    for number, rows in sorted(pages.items()):
        loc = SourceRange(kind='pdf_pages', start=number, end=number).model_dump(mode='json')
        units.append({'source_range': loc, 'nodes': rows, 'read_ids': [], 'complete': False})
        fingerprints[unit_key(loc)] = content_digest(rows)
    return {'directory_transcription': {'discovery_complete': True, 'units': units}, 'directory_seed_review': {**binding, 'catalog_version': catalog_version, 'pages': fingerprints}}

def work(task):
    from openclass_core.services.source_catalog_transcription import state, unit_key
    saved = task.get('directory_seed_review') or {}
    if task.get('mode') != 'continue' or not saved or task.get('directory_review_requested') or (saved.get('content_hash') != task.get('content_hash')) or (saved.get('source_version') != task.get('source_version')) or (saved.get('catalog_version') != task.get('base_version')):
        return None
    directory = state(task)
    if not directory.discovery_complete or directory.review_request_id:
        return None
    unit = next((unit for unit in directory.units if not unit.complete), None)
    if unit is None or unit.read_ids or (not unit.nodes):
        return None
    key = unit_key(unit.source_range)
    if key in task.get('directory_fragments', {}).get('pages', {}):
        return None
    rows = [node.model_dump(mode='json') for node in unit.nodes]
    if saved.get('pages', {}).get(key) != content_digest(rows):
        return None
    return {'stage': 'review_seeded_directory_unit', 'source_range': unit.source_range.model_dump(mode='json'), 'current_rows': rows, 'seed_catalog_version': saved['catalog_version']}

def confirmed_rows(source, task, source_range, read_ids, *, unchanged):
    from openclass_core.services.source_catalog_refinement_evidence import current_boundary_read_ids, read_units, range_was_read
    from openclass_core.services.source_catalog_transcription import unit_key
    proposal = work(task)
    saved = task.get('directory_seed_review') or {}
    if unchanged is not True or proposal is None or any((saved.get(key) != value for key, value in _binding(source).items())) or (unit_key(source_range) != unit_key(proposal['source_range'])):
        raise GoogleSourceError("Confirm only this current directory page's unchanged, source-bound rows.")
    scope = SourceRange.model_validate(proposal['source_range'])
    valid = current_boundary_read_ids(source, task, read_ids)
    full = []
    for identity in valid:
        entry = task['reads'][identity]
        receipt = entry['receipt']
        if entry.get('catalog_fragment_context', {}).get('complete') is True and _page(receipt.get('range')) == scope.start and any((image.get('physical_page') == scope.start and image.get('source_content_hash') == task['content_hash'] and (image.get('source_version') == task['source_version']) and (_page(image.get('source_range')) == scope.start) and isinstance(image.get('sha256'), str) and (len(image['sha256']) == 64) and (not image.get('crop_bbox')) for image in entry.get('page_images', []))):
            full.append(identity)
    if not range_was_read(scope, read_units(task, full)):
        raise GoogleSourceError("Confirming saved rows requires this request's fresh full original page image.")
    return deepcopy(proposal['current_rows'])
