"""Keep verified catalog work when a later model pass has local gaps."""
from __future__ import annotations
from collections import Counter
from openclass_core.models import SourceStructureQuality
from .catalog_structure import normalized, semantic_native_candidates

def _verified(chapter, content_hash):
    return chapter.mapping_status == 'verified' and chapter.range is not None and (chapter.source_content_hash == content_hash)

def _key(chapter):
    return (chapter.number, chapter.level) if chapter.number else (tuple((normalized(part) for part in chapter.path)), chapter.level)

def _same_range(left, right):
    if left is None or right is None:
        return left is right
    keys = ('kind', 'start', 'end', 'container', 'path', 'start_anchor', 'end_anchor')
    return all((getattr(left, key) == getattr(right, key) for key in keys)) and left.metadata.get('end_anchor_exclusive') == right.metadata.get('end_anchor_exclusive')

def merge_catalog(source, prior, old_chapters, structure, chapters, metadata):
    content_hash = source.metadata['content_hash']
    if structure.catalog_schema_version == 'source_original_catalog_v1':
        version = max(structure.catalog_version, prior.catalog_version + 1)
        status = structure.metadata.get('catalog_status', 'partial')
        verified = sum((_verified(chapter, content_hash) for chapter in chapters))
        if status == 'ready' and (not chapters or verified != len(chapters)):
            status = 'partial' if chapters else 'unavailable'
        quality = structure.quality.model_copy(update={'level': 'fully_verified' if status == 'ready' else 'partially_verified' if verified else 'unverified', 'total_chapter_count': len(chapters), 'verified_chapter_count': verified, 'unverified_chapter_count': len(chapters) - verified})
        return (structure.model_copy(update={'id': prior.id, 'catalog_version': version, 'quality': quality, 'metadata': {**structure.metadata, 'catalog_status': status}}), [chapter.model_copy(update={'catalog_version': version}) for chapter in chapters], {**metadata, 'catalog_version': version, 'catalog_status': status, 'catalog_ready': status == 'ready'})
    old_valid = [item for item in old_chapters if _verified(item, content_hash) and item.catalog_version == prior.catalog_version]
    old_valid = [item['chapter'] for item in semantic_native_candidates([{'title': item.title, 'chapter': item} for item in old_valid])]
    if not old_valid or prior.source_content_hash != content_hash:
        return (structure, chapters, metadata)
    old_counts, new_counts = (Counter(map(_key, old_valid)), Counter(map(_key, chapters)))
    old_by_key = {_key(item): item for item in old_valid if old_counts[_key(item)] == 1}
    mapped_new_keys = {_key(item) for item in chapters if _verified(item, content_hash) and new_counts[_key(item)] == 1}
    complete_new = metadata.get('catalog_status') == 'ready' and set(old_by_key) <= mapped_new_keys
    result, used, remap, issues = ([], set(), {}, [])
    for item in chapters:
        old = old_by_key.get(_key(item)) if new_counts[_key(item)] == 1 else None
        verified = _verified(item, content_hash)
        if old is not None:
            used.add(old.id)
            loses_anchor = verified and any((getattr(old.range, key) and (not getattr(item.range, key)) for key in ('start_anchor', 'end_anchor')))
            if not verified or loses_anchor or (not complete_new and (not _same_range(item.range, old.range))):
                chosen = old
                issues.append({'title': item.title, 'reason': 'kept_verified_position'})
            else:
                chosen = item.model_copy(update={'id': old.id}) if _same_range(item.range, old.range) else item
                chosen = chosen.model_copy(update={'metadata': {**old.metadata, **{key: value for key, value in chosen.metadata.items() if value is not None}}})
                if chosen.range is not None and old.range is not None:
                    chosen = chosen.model_copy(update={'range': chosen.range.model_copy(update={'metadata': {**old.range.metadata, **{key: value for key, value in chosen.range.metadata.items() if value is not None}}})})
            remap[item.id] = chosen.id
            remap[old.id] = chosen.id
            result.append(chosen)
        elif verified:
            remap[item.id] = item.id
            result.append(item)
        else:
            issues.append({'title': item.title, 'reason': 'position_unresolved'})
    for item in old_valid:
        if item.id not in used:
            result.append(item)
            issues.append({'title': item.title, 'reason': 'kept_missing_verified_entry'})
    version = max(structure.catalog_version, prior.catalog_version + 1)
    result.sort(key=lambda item: (item.range.start if item.range and type(item.range.start) is int else 10 ** 12, item.level, item.order_index))
    identities = {item.id for item in result}
    result = [item.model_copy(update={'catalog_version': version, 'order_index': index, 'parent_id': remap.get(item.parent_id, item.parent_id) if remap.get(item.parent_id, item.parent_id) in identities else None}) for index, item in enumerate(result)]
    has_toc = prior.has_verified_toc or structure.has_verified_toc
    warning = 'Some catalog entries could not be located. Previously verified locations have been preserved, and other chapters remain available for citation.' if issues else ''
    structure = structure.model_copy(update={'id': prior.id, 'catalog_version': version, 'chapter_count': len(result), 'has_verified_toc': has_toc, 'quality': SourceStructureQuality(level='fully_verified', total_chapter_count=len(result), verified_chapter_count=len(result)), 'warnings': [*structure.warnings, *([warning] if warning else [])], 'metadata': {**structure.metadata, 'catalog_status': 'ready', 'directory_status': 'complete' if has_toc else 'uncertain', 'catalog_local_issues': issues, 'catalog_retained_verified_count': len(old_valid), 'directory_gaps': [f"{issue['title']}: pending individual correction" for issue in issues]}})
    metadata = {**metadata, 'catalog_version': version, 'catalog_ready': True, 'catalog_status': 'ready', 'catalog_warning': warning}
    return (structure, result, metadata)
