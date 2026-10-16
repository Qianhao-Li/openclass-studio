"""Version-bound Sources artifacts. Original and basic text never depend on RAG."""
from __future__ import annotations
import hashlib
import json
from collections import OrderedDict
import os
import tempfile
import time
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.google_sources.storage import scope_prefix
from openclass_core.services.source_basic_formats import SCHEMA, extract_basic_content
from openclass_core.services.source_basic_index import BasicCacheEntry, indexed_content, position_index, retained_size
BASIC_CONFIGURATION = {'schema': SCHEMA, 'native_pdf': 'native_navigation_with_original_visual_carriers_v3', 'native_office': 'original_object_positions'}

def basic_content_available(source):
    return source.metadata.get('basic_read_status') == 'ready' and bool(source.metadata.get('basic_content_artifact'))

def prepare_basic_content(service, source):
    from openclass_core.services.google_sources.catalog_fallback import ensure_original_cache
    from openclass_core.services.source_ingestion_jobs import source_task_cancel_requested
    from openclass_core.services.source_processing_metrics import phase, parsing_slot, cache_hit
    with phase(service, source, 'original_verification'):
        source, path = ensure_original_cache(service, source)
    if source.metadata.get('basic_content_artifact') and source.metadata.get('basic_read_status') in {'ready', 'visual'}:
        cache_hit(service, source, 'parsed_content')
        return (source, load_basic_content(service, source))
    source = service.state.update(source, status='parsing', metadata={'basic_read_status': 'parsing', 'basic_read_progress': 25})
    service._job(source, 'basic_content_parsing', 25)
    started = time.monotonic()
    with parsing_slot(service, source, source_task_cancel_requested), phase(service, source, 'native_text_extraction'):
        data = extract_basic_content(path, source, is_cancelled=source_task_cancel_requested)
    data['configuration'] = BASIC_CONFIGURATION
    if data.get('visual_only') and data['format'] == 'native':
        from openclass_core.services.source_basic_formats import BasicContentError
        raise BasicContentError('No native text was extracted from this document. It cannot be marked as readable.')
    service._check(source)
    prefix = scope_prefix(source.owner_user_id, source.package_id, source.id, source.metadata['source_version'])
    content = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()
    ref = service.storage.put(bucket=service.config.artifact_bucket, name=f'{prefix}/basic/{hashlib.sha256(content).hexdigest()}.json', content=content, mime_type='application/json')
    _save_local_artifact(ref, content)
    visual = data.get('visual_only') or data['format'] == 'visual'
    source = service.state.update(source, status='ready', error='', metadata={'processing_mode': 'ordinary', 'basic_content_artifact': ref, 'basic_read_status': 'visual' if visual else 'ready', 'basic_read_error': '', 'basic_read_progress': 100, 'original_read_ready': True, 'basic_content_schema': SCHEMA, 'basic_content_configuration': BASIC_CONFIGURATION, 'basic_parse_elapsed_ms': round((time.monotonic() - started) * 1000), 'basic_unit_count': len(data['units']), 'google_objects': [*source.metadata.get('google_objects', []), ref], 'enhancement_status': source.metadata.get('enhancement_status', 'ready' if source.metadata.get('search_ready') else 'not_prepared')})
    return (source, indexed_content(data))

def _save_local_artifact(ref, raw):
    from openclass_core.services.google_sources.original_cache import original_cache_root
    if len(raw) != int(ref['size']) or hashlib.sha256(raw).hexdigest() != ref['sha256']:
        raise GoogleSourceError('The parsed source artifact hash is invalid.')
    root = original_cache_root() / 'basic-content-cache'
    root.mkdir(parents=True, exist_ok=True, mode=448)
    path = root / (ref['sha256'] + '.json')
    if root.is_symlink() or path.is_symlink():
        raise GoogleSourceError('The source cache is not a private regular file.')
    with tempfile.NamedTemporaryFile(dir=root, delete=False) as handle:
        temporary = handle.name
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(temporary, 384)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def load_basic_content(service, source):
    ref = source.metadata.get('basic_content_artifact')
    if not ref:
        raise GoogleSourceError('Basic source contents have not finished parsing.')
    service._verify_blob_binding(source, ref)
    key = (source.owner_user_id, source.id, source.metadata['source_version'], source.metadata['content_hash'], source.metadata.get('basic_content_schema', SCHEMA), json.dumps(source.metadata.get('basic_content_configuration', BASIC_CONFIGURATION), sort_keys=True), ref['sha256'], ref['generation'])
    cache = getattr(service, '_basic_content_cache', None)
    if cache is None:
        cache = OrderedDict()
        service._basic_content_cache = cache
    with service.store._lock:
        entry = cache.get(key)
        if isinstance(entry, BasicCacheEntry):
            cache.move_to_end(key)
            return entry.data
    if not isinstance(entry, BasicCacheEntry):
        from openclass_core.services.google_sources.original_cache import original_cache_root
        root = original_cache_root() / 'basic-content-cache'
        root.mkdir(parents=True, exist_ok=True, mode=448)
        path = root / (ref['sha256'] + '.json')
        if root.is_symlink() or path.is_symlink():
            raise GoogleSourceError('The source cache is not a private regular file.')
        raw = path.read_bytes() if path.is_file() else b''
        if len(raw) != int(ref['size']) or hashlib.sha256(raw).hexdigest() != ref['sha256']:
            raw = service.storage.read(ref)
            _save_local_artifact(ref, raw)
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise GoogleSourceError('The source cache has invalid JSON.') from None
    if any((data.get(key) != value for key, value in {'schema': SCHEMA, 'source_id': source.id, 'source_version': source.metadata['source_version'], 'content_hash': source.metadata['content_hash']}.items())):
        raise GoogleSourceError('The parsed source cache belongs to another file or version.')
    if data.get('configuration', BASIC_CONFIGURATION) != source.metadata.get('basic_content_configuration', BASIC_CONFIGURATION):
        raise GoogleSourceError('The parsed source cache uses a different parser configuration.')
    units = data.get('units')
    if not isinstance(units, list) or any((unit.get('id') != index for index, unit in enumerate(units, 1))):
        raise GoogleSourceError('The parsed source cache has invalid content positions.')
    data = indexed_content(data)
    weight = retained_size(data)
    if weight <= 64 * 1024 * 1024:
        with service.store._lock:
            cache[key] = BasicCacheEntry(data, weight)
            while len(cache) > 8 or sum((value.size if isinstance(value, BasicCacheEntry) else len(value) for value in cache.values())) > 64 * 1024 * 1024:
                cache.popitem(last=False)
    return data

def cached_content_parts(data, requested, offset, *, max_chars=18000, max_visual_units=6, include_visuals=False):
    """Return exact positioned text and a cursor; never truncate a fragment."""
    from openclass_core.services.source_content_scope import ensure_unit_authorized
    units = []
    bounds = (requested or {}).get('metadata', {})
    for unit in position_index(data).candidates(data, requested):
        if not bounds.get('basic_unit_start', 1) <= unit['id'] <= bounds.get('basic_unit_end', len(data['units'])):
            continue
        if requested:
            coordinate = unit['locator']
            if coordinate['kind'] in {'pdf_pages', 'epub_spine'} and bounds.get('basic_unit_start') is not None:
                coordinate = {**coordinate, 'metadata': {**bounds, **coordinate.get('metadata', {})}}
            if coordinate['kind'] == 'epub_spine' and bounds.get('basic_unit_start') is not None:
                coordinate = {**coordinate, **({'start_anchor': requested.get('start_anchor', '')} if coordinate['start'] == requested.get('start') else {}), **({'end_anchor': requested.get('end_anchor', '')} if coordinate['end'] == requested.get('end') else {})}
            try:
                ensure_unit_authorized(coordinate, requested)
            except GoogleSourceError:
                continue
        if include_visuals and unit.get('original_images'):
            from openclass_core.services.anythingllm.native_images import positioned_image_units
            units.extend(positioned_image_units(unit, bounds))
        else:
            units.append(unit)
    if not units:
        raise GoogleSourceError('The selected native content range has no readable units.')
    if not 0 <= offset < len(units):
        raise GoogleSourceError('The source cursor is outside its native content range.')
    parts, used, visuals, consumed = ([], 0, 0, 0)
    rendered = set()
    for unit in units[offset:]:
        text = unit['text']
        visual = include_visuals and (bool(unit.get('_original_asset')) or (data['format'] == 'pdf' and unit['locator'].get('metadata', {}).get('requires_original_image') is True))
        pdf_visual = visual and (not unit.get('_original_asset'))
        if pdf_visual and unit['locator']['start'] in rendered:
            consumed += 1
            continue
        if visual and visuals >= max_visual_units:
            break
        if pdf_visual:
            text = ''
        start_char = bounds.get('basic_start_char', 0) if not unit.get('_scope_applied') and unit['id'] == bounds.get('basic_unit_start') else 0
        if unit['id'] == bounds.get('basic_unit_start'):
            text = text[start_char:]
        if not unit.get('_scope_applied') and unit['id'] == bounds.get('basic_unit_end') and (bounds.get('basic_end_char') is not None):
            end_char = bounds['basic_end_char'] - (bounds.get('basic_start_char', 0) if unit['id'] == bounds.get('basic_unit_start') else 0)
            text = text[:end_char]
        key = tuple((unit['locator'].get(name) for name in ('kind', 'start', 'end', 'container')))
        previous = parts[-1] if parts else None
        from openclass_core.services.source_basic_transport import native_separator
        same = bool(previous and (not visual) and (previous['kind'] == 'text') and (key == tuple((previous['locator'].get(name) for name in ('kind', 'start', 'end', 'container')))))
        separator = len(native_separator(previous, text, {'text_start': unit['text_start'] + start_char})) if same else 0
        if used + len(text) + separator > max_chars:
            if not parts:
                raise GoogleSourceError('One source fragment exceeds this reading batch capacity.')
            break
        used += len(text) + separator
        visuals += int(visual)
        parts.append({'kind': 'original_archive_image' if unit.get('_original_asset') else 'original_pdf_page' if visual else 'text', **({'asset': unit['_original_asset']} if unit.get('_original_asset') else {}), 'text': text, 'locator': {**unit['locator'], 'metadata': {**bounds, 'basic_unit_id': unit['id'], 'text_start': unit['text_start'] + start_char, 'text_end': unit['text_start'] + start_char + len(text)}}, 'provenance': {'representation': 'converted_native_text' if data.get('conversion') else 'faithful_parsed_text', 'input_method': 'basic_source_text', 'parser_schema': SCHEMA, **({'conversion': data['conversion'], 'original_sha256': data['content_hash'], 'converted_sha256': data['conversion']['converted_sha256']} if data.get('conversion') else {})}})
        consumed += 1
        if pdf_visual:
            rendered.add(unit['locator']['start'])
    end = offset + consumed
    from openclass_core.services.source_basic_transport import coalesce_native_parts
    parts = coalesce_native_parts(parts)
    return {'parts': parts, 'range': requested or {'kind': 'structured_path'}, 'complete': end == len(units), 'next_cursor': end if end < len(units) else None, 'missing': []}
