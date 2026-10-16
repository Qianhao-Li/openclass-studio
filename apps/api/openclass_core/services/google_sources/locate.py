"""Google retrieval supplies authenticated coordinates, never learner evidence."""
from __future__ import annotations
from openclass_core.models import SourceQueryRef
from openclass_core.services.source_content_contract import seal_locator
from openclass_core.services.source_retrieval_preferences import auto_source_retrieval_enabled
from .config import GoogleSourceError
from .search import source_search_filter, resolve_ref_range

def locate_sources(service, *, owner_user_id, package_id, query, refs=None, limit=8, authorization='automatic'):

    def check():
        if authorization not in {'automatic', 'explicit'} or (authorization == 'explicit' and (not refs)):
            raise GoogleSourceError('Explicit source search requires current file references.')
        if authorization == 'automatic' and (not auto_source_retrieval_enabled(owner_user_id, service.store.path)):
            raise GoogleSourceError('Automatic source search is off. Select a source range to read the original.')
    check()
    if not isinstance(query, str) or not query.strip() or type(limit) is not int or (not 1 <= limit <= 8):
        raise GoogleSourceError('Source location search needs a query and a limit between one and eight.')
    if refs == []:
        return []
    sources = service.store.list_sources(owner_user_id=owner_user_id, package_id=package_id) if refs is None else [service.get(owner_user_id=owner_user_id, package_id=package_id, source_id=ref.source_ingestion_id, content_hash=ref.source_content_hash, ready=False) for ref in refs]
    sources = list({s.id: s for s in sources if s.metadata.get('source_backend') in {'google', 'local'} and (s.metadata.get('search_ready') or s.metadata.get('native_search_ready'))}.values())
    if not sources:
        return []
    by_id = {s.id: s for s in sources}
    scopes = {}
    for ref in refs or []:
        scopes.setdefault(ref.source_ingestion_id, []).append(resolve_ref_range(service, ref, by_id[ref.source_ingestion_id]))
    from .native_locations import native_locations
    fast = []
    for source in sources:
        if not source.metadata.get('search_ready') and source.metadata.get('native_search_ready'):
            fast.extend(native_locations(service, source, query, scopes=scopes.get(source.id, ()), limit=limit))
    sources = [source for source in sources if source.metadata.get('search_ready')]
    if not sources:
        check()
        return fast[:limit]
    service.config.validate(write=True)
    search_filter = source_search_filter(owner_user_id, sources)
    response = service.client.json('POST', f'{service.config.endpoint}/v1/{service.config.data_store}/servingConfigs/{service.config.serving_config}:search', json={'query': query.strip()[:10000], 'pageSize': limit, 'filter': search_filter, 'contentSearchSpec': {'searchResultMode': 'CHUNKS'}})
    documents = {doc: s for s in sources for doc in s.metadata.get('google_documents', [])}
    artifacts, locations, seen = ({}, [], set())
    for rank, result in enumerate(response.get('results', []), 1):
        hit = result.get('chunk', {})
        name = str(hit.get('name', ''))
        if '/documents/' not in name or '/chunks/' not in name or name.rsplit('/chunks/', 1)[-1] != str(hit.get('id')):
            raise GoogleSourceError('The source location has no verified document and chunk identity.')
        document_name = name.split('/chunks/', 1)[0]
        document_id = document_name.rsplit('/', 1)[-1]
        source = documents.get(document_id)
        if source is None:
            raise GoogleSourceError('The source location belongs to another file or version.')
        if source.id not in artifacts:
            artifacts[source.id] = service.artifact(source)
        part = next((p for p in artifacts[source.id]['parts'] if p['document_id'] == document_id), None)
        if part is None or document_name != part.get('document_name', service.config.documents + '/' + document_id):
            raise GoogleSourceError('The search document is not in the verified source artifact.')
        chunk = next((c for c in part['chunks'] if str(c.get('id')) == str(hit['id'])), None)
        if chunk is None:
            raise GoogleSourceError('The search location is absent from the verified source artifact.')
        span = chunk.get('pageSpan', {})
        is_pdf = source.mime_type == 'application/pdf' or source.file_name.lower().endswith('.pdf')
        for scope in scopes.get(source.id, [None]):
            authorized = scope.model_dump(mode='json') if scope is not None else None
            if is_pdf:
                start, end = (span.get('pageStart'), span.get('pageEnd'))
                if type(start) is not int or type(end) is not int or (not 1 <= start <= end <= artifacts[source.id].get('page_count', 0)):
                    continue
                if scope is not None:
                    if scope.kind != 'pdf_pages':
                        continue
                    start, end = (max(start, scope.start), min(end, scope.end))
                    if start > end:
                        continue
                located = {'kind': 'pdf_pages', 'start': start, 'end': end}
                if scope is not None:
                    located.update({'start_anchor': scope.start_anchor, 'end_anchor': scope.end_anchor, 'metadata': dict(scope.metadata)})
            else:
                native = part.get('locator', {})
                if native.get('kind') == 'epub_spine':
                    located = {'kind': 'epub_spine', 'start': native['start'], 'end': native.get('end', native['start']), 'container': native.get('container', '')}
                    if scope is not None:
                        if scope.kind != 'epub_spine' or type(scope.start) is not int or type(scope.end) is not int:
                            continue
                        offset = 1 if scope.metadata.get('index_base', 1) == 0 else 0
                        scope_start, scope_end = (scope.start + offset, scope.end + offset)
                        if not scope_start <= located['start'] <= located['end'] <= scope_end:
                            continue
                        first, last = (located['start'] == scope_start, located['end'] == scope_end)
                        start_anchor = scope.start_anchor if first else ''
                        end_anchor = scope.end_anchor if last else ''
                        metadata = dict(scope.metadata)
                        if offset:
                            metadata['index_base'] = 1
                        metadata.pop('epub_boundary_mode', None)
                        if scope_start != scope_end:
                            if start_anchor and (not end_anchor):
                                metadata['epub_boundary_mode'] = 'suffix'
                            elif end_anchor and (not start_anchor):
                                metadata['epub_boundary_mode'] = 'prefix'
                        elif end_anchor and (not start_anchor):
                            metadata['epub_boundary_mode'] = 'prefix'
                        if end_anchor:
                            metadata.setdefault('end_anchor_exclusive', True)
                        located.update(start_anchor=start_anchor, end_anchor=end_anchor, metadata=metadata)
                elif native.get('kind') == 'image_frames':
                    start, end = (span.get('pageStart'), span.get('pageEnd'))
                    if type(start) is not int or type(end) is not int or (not 1 <= start <= end <= part.get('page_count', 0)):
                        continue
                    located = {'kind': 'image_frames', 'start': start, 'end': end}
                    if scope is not None:
                        if scope.kind != 'image_frames':
                            continue
                        located.update(start=max(start, scope.start), end=min(end, scope.end))
                        if located['start'] > located['end']:
                            continue
                else:
                    located = {'kind': 'cloud_location', 'document_id': document_id, 'chunk_id': str(chunk['id']), 'page_start': span.get('pageStart'), 'page_end': span.get('pageEnd')}
            item = seal_locator(source_id=source.id, source_title=source.title, source_version=source.metadata['source_version'], content_hash=source.metadata['content_hash'], coordinate_type=located['kind'], source_range=located, authorized_range=authorized, document_id=document_id, chunk_id=str(chunk['id']), rank=rank)
            if item.locator_id not in seen:
                locations.append(item)
                seen.add(item.locator_id)
    for source in sources:
        service.get(owner_user_id=owner_user_id, package_id=package_id, source_id=source.id, content_hash=source.metadata['content_hash'])
        service._check(source)
    check()
    return [*locations, *fast][:limit]
