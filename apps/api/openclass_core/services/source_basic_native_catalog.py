"""Observed complete native catalogs and printed-directory reconciliation."""
from __future__ import annotations
from dataclasses import dataclass, field
from openclass_core.services.source_basic_toc import BodyHeadingIndex, discover_printed_directory, title_key
ALGORITHM_VERSION = 'ordinary_catalog_speed_v2'

@dataclass
class NativeCatalogObservation:
    rows: list = field(default_factory=list)
    complete: bool = False
    authored: bool = False
    unresolved: list = field(default_factory=list)
    toc_units: list[int] = field(default_factory=list)
    candidate_units: list[int] = field(default_factory=list)

def observe_native_catalog(data):
    from openclass_core.services.source_basic_catalog import BasicCatalogRow, _native_rows, _heading_position
    from openclass_core.services.source_basic_navigation import normalized_native_navigation
    from openclass_core.services.source_directory_extractor import _heading_number_and_level
    data = normalized_native_navigation(data)
    native = data.get('native_entries', [])
    result = NativeCatalogObservation(authored=bool(data.get('authored')))
    if data['format'] != 'pdf':
        if not native:
            return result
        result.rows = _native_rows(data)
        result.complete = True
        return result
    printed = discover_printed_directory(data)
    result.toc_units = printed.unit_ids
    if not printed.rows:
        return result
    index = BodyHeadingIndex(data)
    last_toc = max(printed.pages)
    native_rows = _native_rows(data) if native else []
    native_by_key = {}
    for entry, row in zip(native, native_rows, strict=True):
        native_by_key.setdefault(title_key(entry['title']), []).append((entry, row))
    printed_keys = {title_key(row.title) for row in printed.rows}
    folded_toc = title_key('\n'.join(printed.page_text.values()))
    rows = []
    seen = set()
    previous_level = 1
    for printed_row in printed.rows:
        matches = native_by_key.get(title_key(printed_row.title), [])
        chosen = None
        for entry, row in matches:
            if entry['range']['start'] <= last_toc:
                continue
            offset = _heading_position(data, row.start_unit, row.title)
            if offset is not None:
                chosen = row.model_copy(update={'start_char': offset, 'printed_page_label': str(printed_row.printed_page or '')})
                break
        if chosen is None:
            candidates = index.find(printed_row.title, after=last_toc)
            if len(candidates) == 1:
                number, level = _heading_number_and_level(printed_row.title)
                if not number:
                    level = previous_level + 1
                chosen = BasicCatalogRow(title=printed_row.title, level=level, start_unit=candidates[0][0], start_char=candidates[0][1], printed_page_label=str(printed_row.printed_page or ''))
                result.candidate_units.append(chosen.start_unit)
            else:
                result.unresolved.append({'toc_page': printed_row.toc_page, 'title': printed_row.title, 'candidate_units': [unit for unit, _ in candidates]})
        if chosen is not None:
            _, numbered_level = _heading_number_and_level(chosen.title)
            if _heading_number_and_level(chosen.title)[0]:
                previous_level = numbered_level
            key = (chosen.start_unit, chosen.start_char, chosen.title)
            if key not in seen:
                rows.append(chosen)
                seen.add(key)
    for entry, row in zip(native, native_rows, strict=True):
        key = title_key(entry['title'])
        if key in printed_keys:
            continue
        offset = _heading_position(data, row.start_unit, row.title)
        if entry['range']['start'] > last_toc and (key not in folded_toc or offset is None):
            result.unresolved.append({'title': entry['title'], 'candidate_units': [row.start_unit]})
            continue
        rows.append(row.model_copy(update={'start_char': offset}))
    result.rows = sorted(rows, key=lambda row: (row.start_unit, row.start_char or 0, row.level))
    result.complete = not result.unresolved and bool(result.rows)
    result.authored = True
    return result

def native_catalog(service, source, data):
    from openclass_core.services.source_basic_catalog import materialize_basic_catalog
    from openclass_core.services.google_sources.config import GoogleSourceError
    service._check(source)
    service._job(source.model_copy(update={'status': 'parsing'}), 'basic_native_directory', 40)
    observation = observe_native_catalog(data)
    if not observation.complete:
        return (None, observation)
    materialized = data
    visual_units = [row.start_unit for row in observation.rows if not data['units'][row.start_unit - 1]['text'].strip()]
    if visual_units:
        from openclass_core.services.google_sources.original_cache import original_path
        from openclass_core.services.source_content_reader import _pdf_parts
        for unit_id in visual_units:
            location = data['units'][unit_id - 1]['locator']
            if location.get('kind') != 'pdf_pages' or not location.get('metadata', {}).get('requires_original_image'):
                observation.complete = False
                return (None, observation)
            page = location['start']
            result = _pdf_parts(original_path(source), source, {}, {'kind': 'pdf_pages', 'start': page, 'end': page}, 0, 1, 40 * 1024 * 1024)
            if len(result['parts']) != 1:
                observation.complete = False
                return (None, observation)
        materialized = {**data, 'visual_verified_units': visual_units}
    try:
        structure, chapters = materialize_basic_catalog(source, materialized, observation.rows, authored=observation.authored, model_calls=0)
    except GoogleSourceError:
        observation.complete = False
        return (None, observation)
    structure = structure.model_copy(update={'catalog_model': 'native_original_reader', 'metadata': {**structure.metadata, 'catalog_strategy': 'native', 'catalog_algorithm_version': ALGORITHM_VERSION}})
    return ((structure, chapters), observation)
