"""Authorization and original receipts stay outside the AnythingLLM search engine."""
from __future__ import annotations
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.google_sources.search import resolve_ref_range, _query_terms
from openclass_core.services.source_basic_cache import load_basic_content
from openclass_core.services.source_content_scope import ensure_unit_authorized
from openclass_core.services.source_content_contract import seal_locator
from openclass_core.services.source_retrieval_preferences import auto_source_retrieval_enabled
from .client import AnythingLLMClient, source_identity
from .legacy_content import positioned_content

def authorized_units(data, scope):
    bounds = (scope or {}).get('metadata', {})
    result = []
    for unit in data['units']:
        if not bounds.get('basic_unit_start', 1) <= unit['id'] <= bounds.get('basic_unit_end', len(data['units'])):
            continue
        if scope:
            try:
                ensure_unit_authorized(unit['locator'], scope)
            except GoogleSourceError:
                continue
        start = bounds.get('basic_start_char', 0) if unit['id'] == bounds.get('basic_unit_start') else 0
        end = bounds.get('basic_end_char') if unit['id'] == bounds.get('basic_unit_end') else None
        end = len(unit['text']) if end is None else end
        if not 0 <= start <= end <= len(unit['text']):
            raise GoogleSourceError('The authorized source character boundary is invalid.')
        result.append({'id': unit['id'], 'start': start, 'end': end})
    return result

class AnythingLLMProvider:

    def __init__(self, service, client=None):
        self.service, self.client = (service, client or AnythingLLMClient())

    def locate(self, *, owner_user_id, package_id, query, refs, limit=8, authorization='automatic'):

        def check():
            if authorization != 'explicit' and (not auto_source_retrieval_enabled(owner_user_id, self.service.store.path)):
                raise GoogleSourceError('Automatic source search is off. Reference a source explicitly.')
        check()
        if not refs or not isinstance(query, str) or (not query.strip()) or (type(limit) is not int) or (not 1 <= limit <= 8):
            raise GoogleSourceError('Source search requires current references, a query, and a valid result limit.')
        grants, local, contexts = ([], [], {})
        for ref in refs:
            source = self.service.get(owner_user_id=owner_user_id, package_id=package_id, source_id=ref.source_ingestion_id, content_hash=ref.source_content_hash, ready=False)
            if source.metadata.get('source_processor') != 'anythingllm':
                raise GoogleSourceError('The source has not been imported into AnythingLLM.')
            data = positioned_content(load_basic_content(self.service, source))
            scope = resolve_ref_range(self.service, ref, source)
            scope = scope.model_dump(mode='json') if scope else None
            units = authorized_units(data, scope)
            contexts.setdefault(source.id, []).append((source, data, scope))
            grant = {**source_identity(source), 'units': units, 'whole_source': scope is None}
            if source.metadata.get('anythingllm_index_status') == 'ready':
                grants.append(grant)
            else:
                terms = _query_terms(query)
                for bounds in units:
                    text = data['units'][bounds['id'] - 1]['text'][bounds['start']:bounds['end']]
                    score = sum((min(count, _query_terms(text).get(term, 0)) for term, count in terms.items()))
                    if score:
                        local.append({'source_id': source.id, 'source_version': source.metadata['source_version'], 'content_hash': source.metadata['content_hash'], 'unit_id': bounds['id'], 'char_start': bounds['start'], 'char_end': bounds['end'], 'score': score})
        locations = self.client.request('/sources/search', {'owner_user_id': owner_user_id, 'grants': grants, 'query': query, 'limit': limit}).get('locations', []) if grants else []
        locations.extend(sorted(local, key=lambda item: -item['score'])[:limit])
        check()
        result = []
        for rank, item in enumerate(locations[:limit], 1):
            if item.get('source_id') not in contexts:
                raise GoogleSourceError("AnythingLLM returned a source outside this turn's grant.")
            source, data, _ = contexts[item['source_id']][0]
            self.service.get(owner_user_id=owner_user_id, package_id=package_id, source_id=source.id, content_hash=source.metadata['content_hash'], ready=False)
            if item.get('source_version') != source.metadata['source_version'] or item.get('content_hash') != source.metadata['content_hash']:
                raise GoogleSourceError('AnythingLLM returned a stale source version.')
            unit_id = item.get('unit_id')
            if type(unit_id) is not int or not 1 <= unit_id <= len(data['units']):
                raise GoogleSourceError('AnythingLLM returned an invalid original location.')
            matching = [(bound, scope) for _, _, scope in contexts[item['source_id']] for bound in authorized_units(data, scope) if bound['id'] == unit_id and bound['start'] <= item.get('char_start', -1) <= item.get('char_end', -1) <= bound['end']]
            if not matching:
                raise GoogleSourceError('AnythingLLM returned content outside the authorized chapter.')
            allowed, scope = matching[0]
            unit = data['units'][unit_id - 1]
            located = {**unit['locator'], 'metadata': {**unit['locator'].get('metadata', {}), 'basic_unit_start': unit_id, 'basic_unit_end': unit_id, 'basic_start_char': allowed['start'], 'basic_end_char': allowed['end']}}
            result.append(seal_locator(source_id=source.id, source_title=source.title, source_version=source.metadata['source_version'], content_hash=source.metadata['content_hash'], coordinate_type=located['kind'], source_range=located, authorized_range=scope, rank=rank))
        return result

def refresh_statuses(service, sources, *, client=None):
    targets = [source for source in sources if source.metadata.get('source_processor') == 'anythingllm' and source.metadata.get('anythingllm_document') and (source.metadata.get('anythingllm_index_status') in {'queued', 'indexing'})]
    if not targets:
        return sources
    try:
        rows = (client or AnythingLLMClient()).statuses(targets)
    except GoogleSourceError:
        return sources
    by_id = {source.id: source for source in targets}
    updated = {}
    for row in rows:
        source = by_id.get(row.get('source_id'))
        if source is None or row.get('source_version') != source.metadata['source_version'] or row.get('content_hash') != source.metadata['content_hash']:
            continue
        if row.get('index_status') not in {'queued', 'indexing', 'ready', 'failed'}:
            continue
        if source.metadata.get('anythingllm_index_status') != row['index_status'] or source.metadata.get('anythingllm_index_error', '') != row.get('error', ''):
            updated[source.id] = service.state.update(source, metadata={'anythingllm_index_status': row['index_status'], 'anythingllm_index_error': row.get('error', '')})
    return [updated.get(source.id, source) for source in sources]
