"""Shared backend-controlled location -> original reading, with bounded coverage."""
from __future__ import annotations
from collections import deque
from copy import deepcopy
from openclass_core.services.source_content_contract import content_digest, verified_read_evidence
from openclass_core.services.source_content_contract import SourceReadBudget
from openclass_core.services.source_content_reader import read_source_content
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_locations import locate_sources
from openclass_core.services.google_sources.search import resolve_ref_range
from openclass_core.services.source_retrieval_preferences import auto_source_retrieval_enabled
from openclass_core.services.source_catalog_readability import verified_catalog_range

def retrieve_original_content(service, *, owner_user_id, package_id, query, refs=None, authorization='automatic', grant_id='', role='chatbot', read_complete=False, is_cancelled=None, calculation_jobs=None):
    ordinary = bool(refs) and any((service.get(owner_user_id=owner_user_id, package_id=package_id, source_id=ref.source_ingestion_id, content_hash=ref.source_content_hash, ready=False).metadata.get('basic_content_artifact') for ref in refs))
    budget = SourceReadBudget.continuation() if ordinary or read_complete else SourceReadBudget()
    jobs = []
    explicit_range = bool(refs) and all((ref.source_chapter_id or ref.page_start is not None or getattr(ref, 'source_range', None) for ref in refs))
    direct = ordinary and authorization == 'explicit' or read_complete or explicit_range or (not auto_source_retrieval_enabled(owner_user_id, service.store.path))
    if calculation_jobs is not None:
        allowed = {ref.source_ingestion_id: ref.source_content_hash for ref in refs or []}
        if not calculation_jobs or (budget.max_reads is not None and len(calculation_jobs) > budget.max_reads):
            raise GoogleSourceError("The original-table calculation plan is empty or exceeds this turn's budget.")
        for job in calculation_jobs:
            requested = job.get('source_range', {})
            if job.get('source_id') not in allowed or job.get('content_hash') != allowed[job['source_id']] or requested.get('kind') != 'table_query' or (job.get('cursor') is not None):
                raise GoogleSourceError("The calculation job is outside this turn's authorized source identities.")
            jobs.append(dict(job))
    elif direct:
        if not refs:
            raise GoogleSourceError('Select a source before reading its complete original contents.')
        for ref in refs:
            source = service.get(owner_user_id=owner_user_id, package_id=package_id, source_id=ref.source_ingestion_id, content_hash=ref.source_content_hash, ready=False)
            requested = resolve_ref_range(service, ref, source)
            if ref.source_chapter_id and (not verified_catalog_range(source, requested)):
                raise GoogleSourceError('This chapter uses an older unverified directory boundary. Rebuild the source catalog before citing this chapter; explicit PDF page reading remains available.')
            jobs.append({'source_id': source.id, 'content_hash': source.metadata['content_hash'], 'source_range': requested, 'cursor': None})
    else:
        budget.search()
        locations = locate_sources(service, owner_user_id=owner_user_id, package_id=package_id, query=query, refs=refs, authorization=authorization)
        for location in locations:
            jobs.append({'source_id': location.source_id, 'content_hash': location.content_hash, 'locator': location, 'cursor': None})
    items, images, manifest, coverage = ([], [], [], [])
    seen_cursors = set()
    scope_coverage = {}
    while jobs and (budget.max_reads is None or budget.reads < budget.max_reads):
        job = jobs.pop(0)
        scope_key = (job['source_id'], str(job.get('source_range') or job.get('locator')))
        cursor_key = (*scope_key, job.get('cursor'))
        if cursor_key in seen_cursors:
            raise GoogleSourceError('The source reader repeated a cursor without progress.')
        seen_cursors.add(cursor_key)
        if calculation_jobs is not None:
            from openclass_core.services.source_table_planning import validate_calculation_job
            validate_calculation_job(service, owner_user_id=owner_user_id, package_id=package_id, job=job, refs=refs)
        reader_job = {key: value for key, value in job.items() if key != 'source_version'}
        result = read_source_content(service, owner_user_id=owner_user_id, package_id=package_id, authorization=authorization, grant_id=grant_id, role=role, budget=budget, is_cancelled=is_cancelled, **reader_job)
        if calculation_jobs is not None:
            validate_calculation_job(service, owner_user_id=owner_user_id, package_id=package_id, job=job, refs=refs)
        offset = len(images)
        images.extend(result.image_inputs)
        manifest.extend(({**item, 'image_index': offset + item['image_index']} for item in result.image_manifest))
        items.extend(result.evidence_items)
        retained = scope_coverage.get(scope_key, {})
        scope_coverage[scope_key] = {**result.source_identity, 'complete': result.complete, 'next_cursor': result.next_cursor, 'missing': [*retained.get('missing', []), *result.missing], 'read_id': result.receipt.get('read_id'), 'read_range': result.receipt.get('range'), 'read_history': [*retained.get('read_history', []), {'read_id': result.receipt.get('read_id'), 'read_range': result.receipt.get('range'), 'complete': result.complete, 'next_cursor': result.next_cursor}]}
        if result.next_cursor:
            jobs.append({**job, 'cursor': result.next_cursor})
        if budget.max_characters is not None and budget.characters >= budget.max_characters or (budget.max_visual_units is not None and budget.visual_units >= budget.max_visual_units):
            break
    coverage = list(scope_coverage.values())
    if jobs:
        coverage.append({'complete': False, 'reason': 'source_read_budget_exhausted', 'remaining_ranges': len(jobs), 'budget': budget.model_dump()})
    if not items:
        coverage.append({'complete': False, 'reason': 'no_verified_original_content'})
    unique = {item.id: item for item in items}
    seen, final_images, final_manifest = (set(), [], [])
    for image, item in zip(images, manifest, strict=True):
        if item['evidence_id'] in seen:
            continue
        seen.add(item['evidence_id'])
        final_manifest.append({**item, 'image_index': len(final_images)})
        final_images.append(image)
    return (list(unique.values()), final_images, final_manifest, coverage)

def _reading_jobs(service, *, owner_user_id, package_id, query, refs, budget, read_complete, calculation_jobs, authorization='automatic'):
    jobs = []
    explicit_range = bool(refs) and all((ref.source_chapter_id or ref.page_start is not None or getattr(ref, 'source_range', None) for ref in refs))
    ordinary = bool(refs) and any((service.get(owner_user_id=owner_user_id, package_id=package_id, source_id=ref.source_ingestion_id, content_hash=ref.source_content_hash, ready=False).metadata.get('basic_content_artifact') for ref in refs))
    direct = ordinary and authorization == 'explicit' or read_complete or explicit_range or (not auto_source_retrieval_enabled(owner_user_id, service.store.path))
    if calculation_jobs is not None:
        allowed = {ref.source_ingestion_id: ref.source_content_hash for ref in refs or []}
        if not calculation_jobs or len(calculation_jobs) > SourceReadBudget().max_reads:
            raise GoogleSourceError("The original-table calculation plan is empty or exceeds this turn's budget.")
        for job in calculation_jobs:
            requested = job.get('source_range', {})
            if job.get('source_id') not in allowed or job.get('content_hash') != allowed[job['source_id']] or requested.get('kind') != 'table_query' or (job.get('cursor') is not None):
                raise GoogleSourceError("The calculation job is outside this turn's authorized source identities.")
            jobs.append(dict(job))
    elif direct:
        if not refs:
            raise GoogleSourceError('Select a source before reading its complete original contents.')
        for ref in refs:
            source = service.get(owner_user_id=owner_user_id, package_id=package_id, source_id=ref.source_ingestion_id, content_hash=ref.source_content_hash, ready=False)
            requested = resolve_ref_range(service, ref, source)
            if ref.source_chapter_id and (not verified_catalog_range(source, requested)):
                raise GoogleSourceError('This chapter uses an older unverified directory boundary. Rebuild the source catalog before citing this chapter; explicit PDF page reading remains available.')
            jobs.append({'source_id': source.id, 'source_version': source.metadata['source_version'], 'content_hash': source.metadata['content_hash'], 'source_range': requested, 'cursor': None})
    else:
        budget.search()
        if ordinary:
            from openclass_core.services.source_basic_search import locate_basic_sources
            locations = locate_basic_sources(service, owner_user_id=owner_user_id, package_id=package_id, query=query, refs=refs)
        else:
            locations = locate_sources(service, owner_user_id=owner_user_id, package_id=package_id, query=query, refs=refs, authorization=authorization)
        for location in locations:
            jobs.append({'source_id': location.source_id, 'source_version': location.source_version, 'content_hash': location.content_hash, 'locator': location, 'cursor': None})
    return deepcopy(jobs)

def iter_original_content_batches(service, *, owner_user_id, package_id, query, refs=None, authorization='automatic', grant_id='', role='chatbot', read_complete=False, is_cancelled=None, calculation_jobs=None, budget=None):
    """Read a finite authorized scope completely, yielding bounded model inputs.

    No complete-range page/read ceiling and no accumulated image list. Consumers
    must process each batch before requesting the next. Authorization and source
    identity are rechecked by the original reader on every call. An explicitly
    supplied budget can enforce a real caller/cost limit; it is never reset here.
    """
    budget = budget or SourceReadBudget.continuation()

    def check_current():
        if is_cancelled and is_cancelled():
            raise GoogleSourceError('Original source reading was cancelled.')
        if authorization == 'automatic' and (not auto_source_retrieval_enabled(owner_user_id, service.store.path)):
            raise GoogleSourceError('Automatic source search was switched off.')
    check_current()
    jobs = deque(_reading_jobs(service, owner_user_id=owner_user_id, package_id=package_id, query=query, refs=refs, budget=budget, read_complete=read_complete, calculation_jobs=calculation_jobs, authorization=authorization))
    seen_cursors, delivered = ({}, {})
    while jobs:
        check_current()
        job = jobs.popleft()
        key = content_digest({name: value.model_dump(mode='json') if hasattr(value, 'model_dump') else value for name, value in job.items() if name != 'cursor'})
        prior = seen_cursors.setdefault(key, set())
        cursor = job.get('cursor')
        if cursor in prior:
            raise GoogleSourceError('The original reader repeated a cursor without progress.')
        prior.add(cursor)
        if calculation_jobs is not None:
            from openclass_core.services.source_table_planning import validate_calculation_job
            validate_calculation_job(service, owner_user_id=owner_user_id, package_id=package_id, job=job, refs=refs)
        result = read_source_content(service, owner_user_id=owner_user_id, package_id=package_id, authorization=authorization, grant_id=grant_id, role=role, budget=budget, is_cancelled=is_cancelled, **{name: value for name, value in job.items() if name != 'source_version'})
        check_current()
        if calculation_jobs is not None:
            validate_calculation_job(service, owner_user_id=owner_user_id, package_id=package_id, job=job, refs=refs)
        if result.source_identity.get('source_id') != job['source_id'] or result.source_identity.get('source_content_hash') != job['content_hash'] or (job.get('source_version') is not None and result.source_identity.get('source_version') != job['source_version']):
            raise GoogleSourceError('The original source identity changed during batch reading.')
        if result.missing:
            raise GoogleSourceError('The authorized source range contains unreadable units; reading is incomplete.')
        if result.receipt.get('owner_user_id') != owner_user_id or result.receipt.get('package_id') != package_id or any((not verified_read_evidence(item) for item in result.evidence_items)):
            raise GoogleSourceError('The original batch has no valid receipt for this authorization.')
        received = {item.id for item in result.evidence_items}
        known = delivered.setdefault(key, set())
        if not received or not received - known:
            raise GoogleSourceError('The original reader produced no new evidence for its continuation.')
        known.update(received)
        if result.next_cursor is not None:
            try:
                next_prefix, next_offset = result.next_cursor.rsplit(':', 1)
                old_prefix, old_offset = cursor.rsplit(':', 1) if cursor else (next_prefix, '0')
                if result.complete or result.next_cursor in prior or next_prefix != old_prefix or (int(next_offset) <= int(old_offset)):
                    raise ValueError()
            except (ValueError, AttributeError):
                raise GoogleSourceError('The original reader returned a repeated or non-progressing cursor.') from None
            jobs.append({**job, 'cursor': result.next_cursor})
        elif not result.complete:
            raise GoogleSourceError('The original range is incomplete and has no valid continuation cursor.')
        if not result.coverage:
            result.coverage = [{**result.source_identity, 'complete': result.complete, 'next_cursor': result.next_cursor, 'missing': result.missing, 'read_id': result.receipt.get('read_id'), 'read_range': result.receipt.get('range')}]
        yield result
        del result
