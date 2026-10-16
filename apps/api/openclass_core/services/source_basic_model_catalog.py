"""Selected-model repair/full-source catalog work, outside the publication boundary."""
from openclass_core.models import AIModelSelection
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_model_capacity import SourceInputCapacity
from openclass_core.services.source_catalog_transport import catalog_chunks, catalog_request_batches, resolve_chunk_rows
from openclass_core.services.source_catalog_observations import CatalogObservations
from openclass_core.services.source_basic_native_catalog import ALGORITHM_VERSION
from openclass_core.services.source_basic_toc import title_key

def _repair_units(data, observation):
    if not observation.toc_units or not observation.unresolved:
        return None
    ids = set(observation.toc_units)
    positions = set()
    for item in observation.unresolved:
        candidates = item.get('candidate_units', [])
        if not candidates:
            return None
        for identifier in candidates:
            if not 1 <= identifier <= len(data['units']):
                return None
            loc = data['units'][identifier - 1]['locator']
            positions.add((loc['kind'], loc.get('start'), loc.get('container', '')))
    for unit in data['units']:
        loc = unit['locator']
        if (loc['kind'], loc.get('start'), loc.get('container', '')) in positions:
            ids.add(unit['id'])
    return [unit for unit in data['units'] if unit['id'] in ids]

def build_model_catalog(service, source, data, native_observation, *, adapter=None):
    from openclass_core.services.source_basic_catalog import PROMPT, BasicCatalogObservation, BasicCatalogRow, materialize_basic_catalog, _reconcile_native_rows
    from openclass_core.services.ai_execution_adapter import build_ai_execution_adapter
    from openclass_core.services.ai_model_catalog import default_text_selection, resolve_text_model_selection
    selection = AIModelSelection.model_validate(source.metadata.get('catalog_model_selection') or default_text_selection().model_dump(mode='json'))
    selection = resolve_text_model_selection(selection, user_id=source.owner_user_id)
    adapter = adapter or build_ai_execution_adapter(selection, owner_user_id=source.owner_user_id)
    capacity = SourceInputCapacity(selection, adapter)
    units = _repair_units(data, native_observation)
    mode = 'toc_repaired' if units is not None else 'full_text'
    units = units if units is not None else data['units']
    unresolved = [item['title'] for item in native_observation.unresolved]
    prompt = PROMPT + '\nThe text pairs are lossless CHUNKS, possibly containing multiple native text units. start_unit is a supplied CHUNK id. The backend resolves the actual heading inside that chunk; do not guess character offsets. Set start_char and end_unit null.'
    if mode == 'toc_repaired':
        prompt += '\nOnly repair unresolved_titles. The complete original printed directory is included. Return no unrelated headings. If the supplied body candidates cannot establish a position, report unresolved instead of guessing.'
    observations = CatalogObservations(service, source, selection, prompt, mode)
    rows, covered = observations.retained(BasicCatalogRow)
    pending = [unit for unit in units if unit['id'] not in covered]
    reserved = capacity.measure(prompt) + capacity.measure(BasicCatalogObservation.model_json_schema()) + 2048
    chunks = list(catalog_chunks(pending, capacity, reserved=reserved))
    batches = list(catalog_request_batches(source, data, chunks, capacity, prompt, BasicCatalogObservation, mode=mode, unresolved=unresolved if mode == 'toc_repaired' else None))
    authored = bool(data.get('authored'))
    for index, (batch, payload) in enumerate(batches):
        service._check(source)
        service._job(source.model_copy(update={'status': 'parsing'}), 'basic_catalog_repair' if mode == 'toc_repaired' else 'basic_catalog_reading', 40 + int(40 * index / max(1, len(batches))))
        result = observations.saved_result(batch, BasicCatalogObservation)
        if result is None:
            result = observations.observe(adapter, prompt, payload, BasicCatalogObservation)
            observations.raw_observation(batch, result)
        if mode == 'toc_repaired' and result.unresolved:
            raise GoogleSourceError('The original directory still has unresolved body locations.')
        resolved = resolve_chunk_rows(data, batch, result.rows, authored=authored or result.authored_directory)
        rows.extend(resolved)
        authored |= result.authored_directory
        observations.add(batch, resolved)
        covered.update((unit['id'] for chunk in batch for unit in chunk.units))
        service.state.update(source, metadata={'catalog_strategy': mode, 'catalog_algorithm_version': ALGORITHM_VERSION, 'catalog_progress': 40 + int(40 * (index + 1) / max(1, len(batches))), 'catalog_read_batches': index + 1, 'catalog_total_batches': len(batches), 'catalog_read_unit_count': len(covered), 'catalog_total_unit_count': len(units), 'catalog_budget_measurement': capacity.measurement})
    if any((unit['id'] not in covered for unit in units)):
        raise GoogleSourceError('The catalog model did not receive every required original text unit.')
    from openclass_core.services.source_basic_catalog_visuals import verify_catalog_visuals
    visual_base = observations.calls
    previous_extra = list(observations.extra)

    def save_visuals(extra, verified, calls):
        observations.extra = [*previous_extra, *[row.model_dump(mode='json') for row in extra]]
        observations.verified = verified
        observations.calls = visual_base + calls
        observations.save()
    extra, verified, added = verify_catalog_visuals(service, source, data, adapter, checkpoint={'visual_verified_units': observations.verified}, save=save_visuals, check_budget=observations.check_budget)
    rows.extend((BasicCatalogRow.model_validate(row) for row in observations.extra))
    rows.extend(extra)
    data = {**data, 'visual_verified_units': verified}
    if native_observation.toc_units:
        required = {title_key(title) for title in unresolved}
        located = {}
        for row in rows:
            if title_key(row.title) in required:
                located[title_key(row.title)] = row
        if required - set(located):
            raise GoogleSourceError('The complete original printed directory could not be located in readable body contents.')
        rows = [*native_observation.rows, *located.values()]
    elif data.get('authored'):
        original = {title_key(entry['title']) for entry in data.get('native_entries', [])}
        rows = [*_reconcile_native_rows(rows, data), *(row for row in rows if title_key(row.title) not in original)]
    structure, chapters = materialize_basic_catalog(source, data, rows, authored=authored, model_calls=observations.calls)
    return (structure.model_copy(update={'metadata': {**structure.metadata, 'catalog_strategy': mode, 'catalog_algorithm_version': ALGORITHM_VERSION, 'catalog_budget_measurement': capacity.measurement}}), chapters)
