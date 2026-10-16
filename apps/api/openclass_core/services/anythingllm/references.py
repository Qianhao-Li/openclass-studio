"""An explicit compatibility reader for mechanically verified old chapter references."""
import hashlib
import json
from openclass_core.services.google_sources.config import GoogleSourceError

def range_digest(value):
    value = value.model_dump(mode='json') if hasattr(value, 'model_dump') else dict(value)
    metadata = value.pop('metadata', {})
    value['boundaries'] = {key: metadata[key] for key in ('basic_unit_start', 'basic_unit_end', 'basic_start_char', 'basic_end_char', 'page_clips', 'end_anchor_exclusive', 'boundary_precision') if key in metadata}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def aliases(old_chapters, new_chapters, content_hash):
    result = {}
    for old in old_chapters:
        if old.mapping_status != 'verified' or old.range is None or old.source_content_hash != content_hash:
            continue
        matches = [chapter for chapter in new_chapters if chapter.title == old.title and chapter.level == old.level and (chapter.range is not None) and (range_digest(chapter.range) == range_digest(old.range))]
        if len(matches) != 1:
            raise GoogleSourceError('An existing chapter reference cannot be uniquely mapped to the same verified original range. Migration remains staged.')
        result[old.id] = {'catalog_version': old.catalog_version, 'chapter_id': matches[0].id, 'range_digest': range_digest(matches[0].range), 'content_hash': content_hash}
    return result

def compatible_pair(service, source, ref):
    if source.metadata.get('source_processor') != 'anythingllm':
        return None
    alias = source.metadata.get('anythingllm_reference_aliases', {}).get(ref.source_chapter_id)
    if not isinstance(alias, dict) or alias.get('content_hash') != ref.source_content_hash or alias.get('content_hash') != source.metadata.get('content_hash'):
        return None
    if ref.catalog_version is not None and ref.catalog_version != alias.get('catalog_version'):
        return None
    pair = service.structure_store.get_catalog_chapter(owner_user_id=source.owner_user_id, package_id=source.package_id, source_id=source.id, chapter_id=alias.get('chapter_id'))
    if pair is None or pair[1].range is None or range_digest(pair[1].range) != alias.get('range_digest'):
        return None
    return pair
