"""Ordinary lexical location lookup; returned positions have no citation authority."""
import json
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.google_sources.search import _query_terms, resolve_ref_range
from openclass_core.services.source_basic_cache import load_basic_content
from openclass_core.services.source_content_contract import seal_locator
from openclass_core.services.source_content_scope import ensure_unit_authorized
from openclass_core.services.source_native_search_boundaries import verified_native_sections, native_section_for_hit

def locate_basic_sources(service, *, owner_user_id, package_id, query, refs, limit=8):
    scored = []
    enhanced = []
    terms = _query_terms(query)
    for ref in refs:
        source = service.get(owner_user_id=owner_user_id, package_id=package_id, source_id=ref.source_ingestion_id, content_hash=ref.source_content_hash, ready=False)
        if source.metadata.get('search_ready') and (source.metadata.get('enhancement_status') == 'ready' or source.metadata.get('processing_mode') != 'ordinary'):
            enhanced.append(ref)
            continue
        if not source.metadata.get('basic_content_artifact'):
            continue
        data = load_basic_content(service, source)
        scope = resolve_ref_range(service, ref, source)
        chapters = verified_native_sections(service, source) if any((unit['locator'].get('kind') == 'epub_spine' for unit in data['units'])) else []
        authorized = scope.model_dump(mode='json') if scope else None
        for unit in data['units']:
            if authorized:
                try:
                    ensure_unit_authorized(unit['locator'], authorized)
                except GoogleSourceError:
                    continue
            counts = _query_terms(unit['text'])
            score = sum((min(count, counts.get(term, 0)) for term, count in terms.items()))
            if score:
                coordinate = native_section_for_hit(unit, terms, chapters) or {**unit['locator'], 'metadata': {**unit['locator'].get('metadata', {}), 'basic_unit_start': unit['id'], 'basic_unit_end': unit['id']}}
                if authorized:
                    try:
                        ensure_unit_authorized(coordinate, authorized)
                    except GoogleSourceError:
                        continue
                scored.append((score, source, coordinate, authorized))
    scored.sort(key=lambda row: -row[0])
    locations = []
    seen = set()
    for _, source, coordinate, authorized in scored:
        identity = (source.id, json.dumps(coordinate, sort_keys=True))
        if identity in seen:
            continue
        seen.add(identity)
        rank = len(locations) + 1
        locations.append(seal_locator(source_id=source.id, source_title=source.title, source_version=source.metadata['source_version'], content_hash=source.metadata['content_hash'], coordinate_type=coordinate['kind'], source_range=coordinate, authorized_range=authorized, rank=rank))
        if len(locations) >= limit:
            break
    if enhanced:
        from openclass_core.services.google_sources.locate import locate_sources
        locations.extend(locate_sources(service, owner_user_id=owner_user_id, package_id=package_id, query=query, refs=enhanced, limit=limit))
    return locations[:limit]
