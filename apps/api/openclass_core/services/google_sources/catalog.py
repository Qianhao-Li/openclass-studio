from __future__ import annotations
import hashlib
from openclass_core.models import SourceChapter, SourceRange, SourceStructure, SourceStructureQuality
from .catalog_structure import cloud_blocks, find_body_heading, heading_parts, page_marker, semantic_native_candidates, shared_boundary_page, toc_pages

def build_catalog(source, adapted, outputs, *, search_locator=None):
    """Publish authored navigation, never a list of numeric PDF page bookmarks."""
    if adapted.format == 'pdf':
        from .config import GoogleSourceError
        raise GoogleSourceError('PDF catalogs require scoped original-page reading after Layout Parser processing.')
    content_hash = source.metadata['content_hash']
    version = int(source.metadata.get('catalog_version', 1))
    hints = search_locator('table of contents 目录') if search_locator is not None else []
    blocks = cloud_blocks(outputs)
    total_pages = sum((part.page_count for part in adapted.parts)) if adapted.format == 'pdf' else 0
    directory_pages = toc_pages(blocks, adapted.catalog, hints) if adapted.format == 'pdf' else []
    candidates = semantic_native_candidates(adapted.catalog)
    complete, used_toc = (True, False)
    if directory_pages and source.metadata.get('catalog_model_selection'):
        from .catalog_toc import extract_toc
        candidates, complete = extract_toc(source, blocks, directory_pages, page_count=total_pages)
        used_toc = True
    elif not candidates:
        for block in blocks:
            if block['page_start'] in directory_pages or not block['type'].startswith('HEADING') or (not block['text'].strip()) or (page_marker(block['text']) is not None):
                continue
            level_text = block['type'].removeprefix('HEADING_')
            level = int(level_text) if level_text.isdigit() else 1
            ordinal, _ = heading_parts(block['text'])
            candidates.append({'title': block['text'], 'level': level, 'ordinal': ordinal, 'part_index': block['part_index'], 'page_start': block['page_start'], 'body_anchor': block['ref'], 'body_order': block['order'], 'block_id': block['id'], 'origin': 'layout_content_structure'})
        if not candidates and source.metadata.get('catalog_model_selection'):
            from .catalog_ai import cloud_heading_candidates
            candidates = cloud_heading_candidates(source, outputs)
    chapters = []
    by_ref = {block['ref']: block for block in blocks}
    for item in candidates:
        if total_pages and (not item.get('body_anchor')) and (type(item.get('page_start')) is int):
            heading = find_body_heading(item['title'], blocks, directory_pages, expected_page=item['page_start'])
            if heading:
                item.update(body_anchor=heading['ref'], body_order=heading['order'], block_id=heading['id'], part_index=heading['part_index'])
    stack = []
    for index, item in enumerate(candidates):
        start = item.get('page_start')
        if item.get('mapping_valid') is False or (total_pages and (type(start) is not int or not 1 <= start <= total_pages)):
            start = None
        end, end_anchor = (total_pages if total_pages else start, '')
        boundary_valid = True
        if start and total_pages:
            for later in candidates[index + 1:]:
                if later['level'] > item['level']:
                    continue
                next_page = later.get('page_start')
                if type(next_page) is int and next_page > total_pages:
                    break
                if later.get('mapping_valid') is False or type(next_page) is not int or (not start <= next_page <= total_pages):
                    boundary_valid = False
                    end = None
                    break
                if type(next_page) is int:
                    end_anchor = later.get('body_anchor', '')
                    next_heading = by_ref.get(end_anchor)
                    end = max(start, next_page if next_heading and shared_boundary_page(next_heading, blocks) else next_page - 1)
                    break
        source_range = None
        if start and end and boundary_valid and (adapted.format == 'pdf'):
            source_range = SourceRange(kind='pdf_pages', start=start, end=end, start_anchor=item.get('body_anchor', ''), end_anchor=end_anchor, metadata={'end_anchor_exclusive': bool(end_anchor), 'printed_page_start': item.get('printed_page'), 'mapping_method': item.get('mapping_method', 'native_or_layout_heading')})
        elif item.get('range'):
            source_range = SourceRange.model_validate(item['range'])
        while stack and stack[-1][0] >= item['level']:
            stack.pop()
        level = len(stack) + 1
        ordinal = item.get('ordinal')
        if ordinal is None:
            ordinal, _ = heading_parts(item['title'])
        _, display_title = heading_parts(item['title'])
        identity = 'sourcechapter_' + hashlib.sha256(f'{source.id}:{content_hash}:{version}:{index}'.encode()).hexdigest()[:24]
        parents = [entry[2] for entry in stack]
        numbers = [str(entry[3]) for entry in stack if entry[3] is not None]
        number = '.'.join([*numbers, str(ordinal)]) if ordinal is not None else ''
        chapter = SourceChapter(id=identity, owner_user_id=source.owner_user_id, package_id=source.package_id, source_ingestion_id=source.id, parent_id=stack[-1][1] if stack else None, title=display_title or item['title'], number=number, normalized_number=str(ordinal) if ordinal is not None else '', level=level, path=[*parents, display_title or item['title']], order_index=index, page_start=start, page_end=end if start is not None else None, range=source_range, anchor_status='verified' if source_range else 'unverified', mapping_status='verified' if source_range else 'unmapped', source_content_hash=content_hash, catalog_version=version, metadata={'origin': item['origin'], 'part_index': item.get('part_index'), 'block_id': item.get('block_id'), 'ordinal': ordinal, 'original_heading': item['title'], 'printed_page_start': item.get('printed_page'), 'toc_evidence_blocks': item.get('toc_evidence_blocks', []), 'mapping_method': item.get('mapping_method', 'native_or_layout_heading')})
        chapters.append(chapter)
        stack.append((item['level'], identity, chapter.title, ordinal))
    native = bool(candidates) and all((item['origin'] in {'native_outline', 'epub_spine'} for item in candidates))
    verified = bool(chapters) and all((chapter.range for chapter in chapters)) and complete
    catalog_status = 'ready' if verified else 'partial' if chapters else 'unavailable'
    structure = SourceStructure(owner_user_id=source.owner_user_id, package_id=source.package_id, source_ingestion_id=source.id, status='ready' if chapters else 'linear_only', strategy='epub_navigation' if adapted.format == 'epub' else 'pdf_outline' if native else 'linear_text', has_verified_toc=(native or used_toc) and verified, chapter_count=len(chapters), catalog_version=version, source_content_hash=content_hash, catalog_schema_version='google_layout_toc_v2', catalog_model=str(source.metadata.get('catalog_model_selection', {}).get('model', 'google_layout_parser')) if used_toc else 'google_layout_parser', quality=SourceStructureQuality(level='fully_verified' if verified else 'search_only'), metadata={'source_backend': 'google', 'catalog_mode': 'cloud', 'catalog_label': 'Book table of contents' if used_toc else 'Native table of contents' if native else 'Content structure', 'catalog_status': catalog_status, 'directory_status': 'complete' if complete and verified else 'uncertain', 'directory_pages': directory_pages, 'search_ready': True})
    return (structure, chapters)
