"""Lossless positioned catalog transport; cached unit identities never change."""
from dataclasses import dataclass
import json
from openclass_core.services.google_sources.config import GoogleSourceError

@dataclass
class CatalogTextChunk:
    units: list
    text: str

    @property
    def id(self):
        return self.units[0]['id']

    @property
    def unit_ids(self):
        return [unit['id'] for unit in self.units]

    def pair(self):
        return [self.id, self.text]

def catalog_chunks(units, capacity, *, reserved=0):
    current = []
    text = ''
    key = None
    limit = capacity.limit - reserved
    for unit in units:
        location = unit['locator']
        identity = (location['kind'], location.get('start'), location.get('container', ''))
        separator = '' if current and (current[-1].get('text_end') == unit.get('text_start') and unit.get('text_start') is not None or text.endswith('\n') or unit['text'].startswith('\n')) else '\n'
        joined = text + separator + unit['text'] if current else unit['text']
        if current and (identity != key or unit['id'] != current[-1]['id'] + 1 or len(joined) > 16000 or (capacity.measure([current[0]['id'], joined]) > limit)):
            yield CatalogTextChunk(current, text)
            current = []
            joined = unit['text']
        if capacity.measure([unit['id'], joined]) > limit:
            raise GoogleSourceError('One original catalog unit exceeds the selected model capacity.')
        current.append(unit)
        text = joined
        key = identity
    if current:
        yield CatalogTextChunk(current, text)

def native_hints(data, chunks):
    lookup = {}
    for chunk in chunks:
        for unit in chunk.units:
            loc = unit['locator']
            lookup.setdefault((loc['kind'], loc.get('start'), loc.get('container', '')), set()).add(chunk.id)
    result = []
    for entry in data.get('native_entries', []):
        loc = entry['range']
        ids = lookup.get((loc['kind'], loc.get('start'), loc.get('container', '')))
        if ids:
            result.append({'title': entry['title'], 'level': entry['level'], 'possible_start_units': sorted(ids)})
    return result

def chunk_payload(source, data, chunks, *, mode='full_text', unresolved=None):
    return {'source_title': source.title, 'native_format': data.get('native_format', data['format']), 'units': [chunk.pair() for chunk in chunks], 'native_navigation': native_hints(data, chunks), 'catalog_mode': mode, **({'unresolved_titles': unresolved} if unresolved else {})}

def catalog_request_batches(source, data, chunks, capacity, prompt, schema, *, mode='full_text', unresolved=None):
    fixed = capacity.measure(prompt) + capacity.measure(schema.model_json_schema()) + 2048
    fixed += capacity.measure([{'title': e['title'], 'level': e['level'], 'possible_start_units': [1]} for e in data.get('native_entries', [])])
    groups = []
    batch = []
    used = 0
    for chunk in chunks:
        amount = capacity.measure(chunk.pair())
        if batch and used + amount > capacity.limit - fixed:
            groups.append(batch)
            batch = []
            used = 0
        batch.append(chunk)
        used += amount
    if batch:
        groups.append(batch)
    pending = list(groups)
    while pending:
        current = pending.pop(0)
        payload = chunk_payload(source, data, current, mode=mode, unresolved=unresolved)
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        if capacity.fits_request(prompt, body, schema):
            yield (current, payload)
        elif len(current) > 1:
            middle = len(current) // 2
            pending = [current[:middle], current[middle:], *pending]
        else:
            raise GoogleSourceError('The catalog instructions and one source chunk exceed the selected model transport capacity.')

def resolve_chunk_rows(data, chunks, rows, *, authored):
    from openclass_core.services.source_basic_catalog import _heading_position
    members = {unit['id']: chunk for chunk in chunks for unit in chunk.units}
    result = []
    for row in rows:
        chunk = members.get(row.start_unit)
        if chunk is None:
            raise GoogleSourceError('The directory role returned a position outside its delivered original chunks.')
        bounded = {**data, 'units': data['units'][:chunk.units[-1]['id']]}
        matches = [(unit['id'], offset) for unit in chunk.units if (offset := _heading_position(bounded, unit['id'], row.title)) is not None]
        if not matches:
            delivered = {unit['id'] for item in chunks for unit in item.units}
            matches = []
            for item in chunks:
                window = {**data, 'units': data['units'][:item.units[-1]['id']]}
                matches.extend(((unit['id'], offset) for unit in item.units if (offset := _heading_position(window, unit['id'], row.title)) is not None))
            candidates = []
            for entry in data.get('native_entries', []):
                if entry['title'].strip().casefold() != row.title.strip().casefold():
                    continue
                loc = entry['range']
                candidates.extend((pair for pair in matches if data['units'][pair[0] - 1]['locator'].get('start') == loc.get('start') and data['units'][pair[0] - 1]['locator'].get('container', '') == loc.get('container', '')))
            if len(candidates) == 1:
                matches = candidates
        if len(matches) > 1 and row.start_char is not None:
            matches = [pair for pair in matches if pair == (row.start_unit, row.start_char)]
        if len(matches) == 1:
            result.append(row.model_copy(update={'start_unit': matches[0][0], 'start_char': matches[0][1], 'end_unit': None}))
        elif not matches and (not authored):
            result.append(row.model_copy(update={'start_unit': chunk.id, 'start_char': None, 'end_unit': None}))
        else:
            raise GoogleSourceError('The directory heading has no unique readable position in its delivered original chunk.')
    return result
