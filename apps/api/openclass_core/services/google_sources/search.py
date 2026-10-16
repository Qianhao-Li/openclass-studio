from __future__ import annotations
import json
import math
import re
import unicodedata
from collections import Counter
from openclass_core.models import SourceRange
from .config import GoogleSourceError
from .read import evidence_from_unit, authorized_records, in_range, parsed_units, validate_page_bounds
from openclass_core.services.source_retrieval_preferences import auto_source_retrieval_enabled

def _term(field, value):
    return f'{field}: ANY({json.dumps(value, ensure_ascii=True)})'

def source_search_filter(owner_user_id, sources):
    scopes = ['(' + ' AND '.join((_term('package_id', source.package_id), _term('source_id', source.id), _term('source_version', source.metadata['source_version']))) + ')' for source in sources]
    return _term('owner_user_id', owner_user_id) + ' AND (' + ' OR '.join(scopes) + ')'

def _compact(text):
    return ''.join(unicodedata.normalize('NFKC', str(text or '')).split())

def _query_terms(text):
    terms = []
    for word in re.findall('[\\u3400-\\u9fff]+|[^\\W_\\u3400-\\u9fff]+', unicodedata.normalize('NFKC', text).casefold()):
        terms.extend((word[index:index + 2] for index in range(len(word) - 1)) if '㐀' <= word[0] <= '鿿' and len(word) > 1 else [word])
    return Counter(terms)

def _rank_units(units, query):
    """Refine one Google hit using complete, verbatim blocks, without a model call."""
    query_terms = set(_query_terms(query))
    terms = [_query_terms(str(unit.get('content') or '')) for unit in units]
    frequencies = Counter((term for candidate in terms for term in query_terms.intersection(candidate)))
    average = sum((sum(candidate.values()) for candidate in terms)) / max(1, len(terms))

    def score(index):
        candidate = terms[index]
        length = sum(candidate.values())
        return sum((math.log(1 + (len(units) + 0.5) / (frequencies[term] + 0.5)) * candidate[term] * 2.2 / (candidate[term] + 1.2 * (0.25 + 0.75 * length / max(1, average))) for term in query_terms.intersection(candidate)))
    return [units[index] for index in sorted(range(len(units)), key=lambda index: -score(index))]

def _bounded_groups(groups, limit, budget=6000):
    """Reserve evidence for later Google hits before spending remaining slots."""
    groups = [group for group in groups if group]
    quota = budget // max(1, min(limit, len(groups)))
    result, seen, used = ([], set(), 0)

    def take(group, allowance, *, reserve=False):
        nonlocal used
        selected = next((item for item in group if item.id not in seen and item.token_count <= (budget if reserve else allowance) and (reserve or used + item.token_count <= budget)), None)
        if selected is None or selected.token_count > allowance or used + selected.token_count > budget:
            return False
        result.append(selected)
        seen.add(selected.id)
        used += selected.token_count
        return True
    for group in groups:
        if len(result) >= limit:
            break
        take(group, quota, reserve=True)
    while len(result) < limit:
        added = False
        for group in groups:
            added = take(group, budget - used) or added
            if len(result) >= limit:
                break
        if not added:
            break
    return result

def search_sources(service, *, owner_user_id, package_id, query, refs=None, limit=8):

    def ensure_enabled():
        if not auto_source_retrieval_enabled(owner_user_id, service.store.path):
            raise GoogleSourceError('Automatic source search is off. Select a source chapter manually to read its original file.')
    ensure_enabled()
    service.config.validate(write=True)
    if type(limit) is not int or not 1 <= limit <= 8:
        raise GoogleSourceError('The source search result limit must be between 1 and 8.')
    if not query.strip():
        raise GoogleSourceError('Enter a specific question to search sources.')
    if refs is None:
        sources = [source for source in service.store.list_sources(owner_user_id=owner_user_id, package_id=package_id) if source.metadata.get('source_backend') in {'google', 'local'} and source.status == 'ready' and source.metadata.get('search_ready')]
    else:
        if not refs:
            return []
        sources = [service.get(owner_user_id=owner_user_id, package_id=package_id, source_id=ref.source_ingestion_id, content_hash=ref.source_content_hash) for ref in refs]
    sources = list({source.id: source for source in sources}.values())
    sources = [service.get(owner_user_id=owner_user_id, package_id=package_id, source_id=source.id, content_hash=source.metadata['content_hash']) for source in sources]
    if not sources:
        return []
    scopes = {}
    by_id = {source.id: source for source in sources}
    for ref in refs or []:
        scopes.setdefault(ref.source_ingestion_id, []).append(resolve_ref_range(service, ref, by_id[ref.source_ingestion_id]))
    search_filter = source_search_filter(owner_user_id, sources)
    ensure_enabled()
    service.config.validate(write=True)
    result = service.client.json('POST', f'{service.config.endpoint}/v1/{service.config.data_store}/servingConfigs/{service.config.serving_config}:search', json={'query': query[:10000], 'pageSize': min(8, max(1, limit)), 'filter': search_filter, 'contentSearchSpec': {'searchResultMode': 'CHUNKS'}})
    allowed = {document_id: source for source in sources for document_id in source.metadata.get('google_documents', [])}
    groups = []
    artifacts = {}
    hits = result.get('results', [])
    share_chars = 18000 // max(1, min(limit, len(hits)))
    for rank, item in enumerate(hits, 1):
        chunk = item.get('chunk', {})
        name = str(chunk.get('name', ''))
        if '/documents/' not in name or '/chunks/' not in name or name.rsplit('/chunks/', 1)[-1] != str(chunk.get('id')):
            raise GoogleSourceError('Google Search returned results outside the current source scope.')
        document_name = name.split('/chunks/', 1)[0]
        document_id = document_name.rsplit('/', 1)[-1]
        if document_id not in allowed:
            raise GoogleSourceError('Google Search returned an unauthorized or outdated source version.')
        source = allowed[document_id]
        service.get(owner_user_id=owner_user_id, package_id=package_id, source_id=source.id, content_hash=source.metadata['content_hash'])
        service._check(source)
        if source.id not in artifacts:
            artifacts[source.id] = service.artifact(source)
            for scope in scopes.get(source.id, []):
                validate_page_bounds(artifacts[source.id], scope)
        part = next((part for part in artifacts[source.id]['parts'] if part['document_id'] == document_id), None)
        if part is None or document_name != part.get('document_name', service.config.documents + '/' + document_id):
            raise GoogleSourceError('The Google Search document is not in the current verified artifact.')
        saved_chunk = next((saved for saved in part['chunks'] if str(saved.get('id')) == str(chunk.get('id'))), None)
        if saved_chunk is None:
            raise GoogleSourceError('The Google Search chunk version does not match the verified artifact.')
        requested_scopes = scopes.get(source.id, [])
        units = [saved_chunk]
        bounded_scope = requested_scopes and (any((scope is not None and (scope.start_anchor or scope.end_anchor) for scope in requested_scopes)) or not any((in_range(saved_chunk, scope, part['locator']) for scope in requested_scopes)))
        hit_span = saved_chunk.get('pageSpan', {})
        hit_start, hit_end = (hit_span.get('pageStart'), hit_span.get('pageEnd'))
        split_hit = len(str(saved_chunk.get('content') or '')) > share_chars or (type(hit_start) is int and type(hit_end) is int and (hit_start != hit_end))
        if bounded_scope or split_hit:
            blocks = [unit for scope in requested_scopes for candidate_part, unit in authorized_records(artifacts[source.id], scope) if candidate_part['document_id'] == document_id] if requested_scopes else parsed_units(part)
            if type(hit_start) is int and type(hit_end) is int:
                blocks = [unit for unit in blocks if hit_start <= unit['pageSpan']['pageStart'] <= unit['pageSpan']['pageEnd'] <= hit_end]
            hit_text = _compact(saved_chunk.get('content'))
            matched = [unit for unit in blocks if _compact(unit.get('content')) and _compact(unit.get('content')) in hit_text]
            if matched or bounded_scope:
                units = matched
        candidates = []
        for unit in _rank_units(units, query):
            if not str(unit.get('content') or '').strip():
                continue
            selected = evidence_from_unit(source, unit, document_id=document_id, locator=part['locator'])
            selected = selected.model_copy(update={'metadata': {**selected.metadata, 'google_search_hit': {'document_id': document_id, 'chunk_id': str(saved_chunk['id']), 'rank': rank}}})
            candidates.append(selected)
        groups.append(candidates)
    evidence = _bounded_groups(groups, limit)
    for source in sources:
        service.get(owner_user_id=owner_user_id, package_id=package_id, source_id=source.id, content_hash=source.metadata['content_hash'])
        service._check(source)
    ensure_enabled()
    return evidence

def resolve_ref_range(service, ref, source):
    requested = None
    if ref.source_chapter_id:
        from openclass_core.services.anythingllm.references import compatible_pair
        compatibility = compatible_pair(service, source, ref)
        pair = service.structure_store.get_catalog_chapter(owner_user_id=source.owner_user_id, package_id=source.package_id, source_id=source.id, chapter_id=ref.source_chapter_id)
        pair = compatibility or pair
        if pair is None:
            raise GoogleSourceError('The catalog range is outdated or has not been verified.')
        structure, chapter = pair
        progressive = structure.catalog_schema_version in {'pi_catalog_refinement_v1', 'anythingllm_source_catalog_v2', 'anythingllm_direct_catalog_v1'}
        if not progressive:
            view = service.structure_store.get_catalog_view(source=source)
            if not view.chapters or view.verified_chapter_count != view.chapter_count or view.directory_status != 'complete':
                raise GoogleSourceError('The catalog is not complete and published. Retry catalog verification before selecting a chapter.')
        if source.metadata.get('processing_mode') == 'ordinary' and structure.metadata.get('directory_status') != 'complete' and (not progressive):
            raise GoogleSourceError('The complete catalog and all chapter ranges have not finished verification.')
        if compatibility is None and ref.catalog_version is not None and (ref.catalog_version != structure.catalog_version):
            raise GoogleSourceError('The catalog version has changed. Please select the chapter again.')
        if chapter.range is None or chapter.mapping_status != 'verified' or chapter.source_content_hash != ref.source_content_hash or (structure.source_content_hash != ref.source_content_hash) or (chapter.catalog_version != structure.catalog_version):
            raise GoogleSourceError('The catalog range is outdated or has not been verified.')
        from openclass_core.services.source_catalog_quality import chapter_range_issue
        issue = chapter_range_issue(source, structure, chapter)
        if issue:
            raise GoogleSourceError(issue)
        requested = chapter.range
    native_range = getattr(ref, 'source_range', None)
    if native_range is not None:
        if requested is not None:
            from openclass_core.services.source_content_scope import ensure_unit_authorized
            ensure_unit_authorized(native_range.model_dump(mode='json'), requested.model_dump(mode='json'))
            requested = requested.model_copy(update={'start': native_range.start, 'end': native_range.end})
        else:
            requested = native_range
    if ref.page_start is not None:
        if requested is not None and (requested.kind != 'pdf_pages' or not requested.start <= ref.page_start <= ref.page_end <= requested.end):
            raise GoogleSourceError('The requested pages are outside the authorized chapter.')
        requested = requested.model_copy(update={'start': ref.page_start, 'end': ref.page_end}) if requested is not None else SourceRange(kind='pdf_pages', start=ref.page_start, end=ref.page_end)
    validate_page_bounds(source.metadata, requested)
    return requested
