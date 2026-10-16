"""Formal publication accepts complete, version-bound original-content catalogs."""
from __future__ import annotations
from .config import GoogleSourceError

def catalog_publication_issue(source, structure, chapters, metadata=None):
    count = len(chapters)
    if not count or structure.status != 'ready':
        return 'The catalog is empty or its verification has not completed.'
    for values in (structure.metadata, metadata or {}):
        if values.get('catalog_status', 'ready') != 'ready' or values.get('catalog_ready') is False:
            return 'The catalog still has unfinished verification.'
        if values.get('directory_status', 'complete') != 'complete':
            return 'The catalog discovery is incomplete.'
        if any((values.get(key) for key in ('directory_gaps', 'remaining_work', 'unresolved_node_count'))):
            return 'The catalog has unresolved entries or discovery gaps.'
    quality = structure.quality
    if quality.level != 'fully_verified' or quality.total_chapter_count != count or quality.verified_chapter_count != count or quality.unverified_chapter_count:
        return 'The catalog verification counts do not describe a complete catalog.'
    if structure.chapter_count != count or not structure.source_content_hash:
        return 'The catalog identity or entry count is inconsistent.'
    if (structure.owner_user_id, structure.package_id, structure.source_ingestion_id, structure.source_content_hash) != (source.owner_user_id, source.package_id, source.id, source.metadata.get('content_hash')):
        return 'The catalog does not belong to the current original.'
    by_id = {chapter.id: chapter for chapter in chapters}
    if len(by_id) != count:
        return 'The catalog contains duplicate entry identities.'
    pdf = source.mime_type == 'application/pdf' or source.file_name.lower().endswith('.pdf')
    page_count = source.metadata.get('page_count')
    for chapter in chapters:
        if (chapter.owner_user_id, chapter.package_id, chapter.source_ingestion_id, chapter.source_content_hash, chapter.catalog_version) != (structure.owner_user_id, structure.package_id, structure.source_ingestion_id, structure.source_content_hash, structure.catalog_version):
            return 'A catalog entry belongs to a different owner or version.'
        value = chapter.range
        if not chapter.title.strip() or chapter.mapping_status != 'verified' or chapter.anchor_status != 'verified' or (value is None):
            return 'A catalog entry has no verified original-content range.'
        if value.metadata.get('source_content_hash', structure.source_content_hash) != structure.source_content_hash or value.metadata.get('source_version', source.metadata.get('source_version')) != source.metadata.get('source_version'):
            return 'A catalog range belongs to an older original.'
        if pdf and (value.kind != 'pdf_pages' or type(page_count) is not int or type(value.start) is not int or (type(value.end) is not int) or (not 1 <= value.start <= value.end <= page_count)):
            return 'A catalog range is outside the original PDF pages.'
    for chapter in chapters:
        visited, parent_id = ({chapter.id}, chapter.parent_id)
        while parent_id:
            if parent_id in visited or parent_id not in by_id:
                return 'The catalog hierarchy has a cycle or a missing parent.'
            visited.add(parent_id)
            parent = by_id[parent_id]
            if parent.level >= chapter.level:
                return 'The catalog hierarchy has inconsistent levels.'
            if pdf and (not parent.range.start <= chapter.range.start <= chapter.range.end <= parent.range.end):
                return 'A catalog entry extends outside its parent range.'
            parent_id = parent.parent_id
    return None

def require_complete_catalog(source, structure, chapters, metadata=None):
    issue = catalog_publication_issue(source, structure, chapters, metadata)
    if issue:
        raise GoogleSourceError(issue)

def public_catalog_view(source, structure, chapters, view):
    """Quarantine historical drafts without deleting resumable observations."""
    if source.metadata.get('source_backend') not in {'google', 'local'}:
        return view
    issue = catalog_publication_issue(source, structure, chapters) if structure else 'The catalog has not been published.'
    if issue is None:
        return view
    from openclass_core.models import SourceStructureQuality
    working = source.metadata.get('catalog_status') in {'pending', 'running'} or source.metadata.get('update_status') == 'parsing'
    return view.model_copy(update={'structure_id': None, 'status': 'building' if working else 'failed', 'has_verified_toc': False, 'catalog_version': 0, 'catalog_updated_at': None, 'revision': 0, 'chapters': [], 'chapter_count': 0, 'verified_chapter_count': 0, 'unresolved_node_count': 0, 'directory_status': 'incomplete', 'index_status': 'pending', 'locator_method': '', 'work_state': 'working' if working else 'paused', 'phase': 'range_mapping', 'completion_reason': '', 'can_refine': True, 'background_refine_active': working, 'stop_reason': '' if working else source.metadata.get('catalog_warning') or issue, 'error': '' if working else source.metadata.get('catalog_warning') or issue, 'quality': SourceStructureQuality(level='unverified')})
