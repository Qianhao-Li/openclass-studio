"""Map EPUB lexical hits to current verified authored section boundaries."""
from openclass_core.services.google_sources.search import _query_terms

def verified_native_sections(service, source):
    structures = getattr(service, 'structure_store', None)
    if structures is None:
        return []
    catalog = structures.get_catalog_view(source=source)
    if catalog.status != 'ready' or catalog.directory_status != 'complete' or catalog.unresolved_node_count:
        return []
    if catalog.source_content_hash != source.metadata.get('content_hash'):
        return []
    return [chapter for chapter in catalog.chapters if chapter.mapping_status == 'verified' and chapter.source_content_hash == source.metadata.get('content_hash') and (chapter.catalog_version == source.metadata.get('catalog_version', 1)) and (chapter.range is not None) and (chapter.range.kind == 'epub_spine') and (chapter.range.metadata.get('source_version') == source.metadata.get('source_version'))]

def native_section_for_hit(unit, terms, chapters):
    if unit['locator'].get('kind') != 'epub_spine':
        return None
    candidates = []
    for chapter in chapters:
        location = chapter.range.model_dump(mode='json')
        metadata = location.get('metadata', {})
        start, end = (metadata.get('basic_unit_start'), metadata.get('basic_unit_end'))
        if type(start) is not int or type(end) is not int or (not start <= unit['id'] <= end):
            continue
        begin = metadata.get('basic_start_char', 0) if unit['id'] == start else 0
        stop = metadata.get('basic_end_char') if unit['id'] == end else None
        if type(begin) is not int or (stop is not None and type(stop) is not int):
            continue
        counts = _query_terms(unit['text'][begin:stop])
        score = sum((min(count, counts.get(term, 0)) for term, count in terms.items()))
        if score:
            candidates.append((chapter.level, score, -(end - start), location))
    return max(candidates, key=lambda row: row[:3])[3] if candidates else None
