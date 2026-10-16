"""Provider dispatch for navigation only; original readers remain authoritative."""
from openclass_core.models import SourceQueryRef

def locate_sources(service, *, owner_user_id, package_id, query, refs=None, limit=8, authorization='automatic'):
    if refs is None:
        refs = [SourceQueryRef(source_ingestion_id=source.id, source_content_hash=source.metadata['content_hash']) for source in service.store.list_sources(owner_user_id=owner_user_id, package_id=package_id) if source.status == 'ready' and source.metadata.get('source_backend') in {'google', 'local'} and (source.metadata.get('search_ready') or source.metadata.get('native_search_ready') or source.metadata.get('basic_content_artifact')) and (not source.metadata.get('external_turn_temporary'))]
    material, ordinary, enhanced = ([], [], [])
    for ref in refs:
        source = service.get(owner_user_id=owner_user_id, package_id=package_id, source_id=ref.source_ingestion_id, content_hash=ref.source_content_hash, ready=False)
        if source.metadata.get('source_processor') == 'anythingllm':
            material.append(ref)
        elif source.metadata.get('basic_content_artifact'):
            ordinary.append(ref)
        else:
            enhanced.append(ref)
    result = []
    if material:
        from openclass_core.services.anythingllm.provider import AnythingLLMProvider, refresh_statuses
        sources = [service.get(owner_user_id=owner_user_id, package_id=package_id, source_id=ref.source_ingestion_id, content_hash=ref.source_content_hash, ready=False) for ref in material]
        refresh_statuses(service, sources)
        result.extend(AnythingLLMProvider(service).locate(owner_user_id=owner_user_id, package_id=package_id, query=query, refs=material, limit=limit, authorization=authorization))
    if ordinary:
        from openclass_core.services.source_basic_search import locate_basic_sources
        result.extend(locate_basic_sources(service, owner_user_id=owner_user_id, package_id=package_id, query=query, refs=ordinary, limit=limit))
    if enhanced:
        from openclass_core.services.google_sources.locate import locate_sources as google_locate
        result.extend(google_locate(service, owner_user_id=owner_user_id, package_id=package_id, query=query, refs=enhanced, limit=limit))
    return result[:limit]
