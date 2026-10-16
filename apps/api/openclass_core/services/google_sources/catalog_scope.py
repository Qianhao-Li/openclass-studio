"""Resolve only verified structural ordinals; never guess a chapter from body keywords."""
from __future__ import annotations
import unicodedata
from pathlib import PurePosixPath
from openclass_core.models import SourceQueryRef

def _number(chapter):
    for raw in (chapter.metadata.get('ordinal'), chapter.normalized_number, chapter.number):
        value = unicodedata.normalize('NFKC', str(raw or '')).strip()
        parts = value.split('.')
        if parts and all((part.isdigit() for part in parts)):
            return int(parts[-1])
    return None

def resolve_catalog_refs(*, sources, structure_store, source_hint='', chapter_number=None, section_number=None):
    candidates = sources
    if source_hint:

        def identity(value):
            value = unicodedata.normalize('NFKC', value).strip().casefold()
            if PurePosixPath(value).suffix in {'.pdf', '.epub', '.docx', '.pptx', '.xlsx', '.txt', '.md'}:
                value = str(PurePosixPath(value).with_suffix(''))
            return ''.join((char for char in value if not char.isspace() and (not unicodedata.category(char).startswith('P'))))
        hint = identity(source_hint)
        candidates = [source for source in sources if source.id.casefold() == source_hint.strip().casefold() or (hint and any((hint in identity(value) for value in (source.title, source.file_name))))]
    if len(candidates) != 1:
        return ([], 'Please specify the source first. A chapter number cannot be applied to every file in the course.')
    source = candidates[0]
    if chapter_number is None and section_number is None:
        return ([SourceQueryRef(source_ingestion_id=source.id, source_content_hash=source.metadata['content_hash'])], '')
    view = structure_store.get_catalog_view(source=source)
    chapters = view.chapters
    structure = structure_store.get_structure(owner_user_id=source.owner_user_id, package_id=source.package_id, source_id=source.id)
    if structure is None:
        return ([], 'The source catalog has not been verified. Please select a specific range manually.')
    matches = [chapter for chapter in chapters if _number(chapter) == (chapter_number if chapter_number is not None else section_number)]
    if chapter_number is None:
        matches = [chapter for chapter in matches if chapter.parent_id is not None]
    if matches and chapter_number is not None:
        chapter_nodes = [chapter for chapter in chapters if _number(chapter) is not None]
        level = min((chapter.level for chapter in chapter_nodes))
        matches = [chapter for chapter in matches if chapter.level == level]
    if len(matches) != 1:
        return ([], 'The chapter cannot be uniquely identified. Please select it from the verified catalog.')
    target = matches[0]
    if section_number is not None and chapter_number is not None:
        matches = [chapter for chapter in chapters if chapter.parent_id == target.id and _number(chapter) == section_number]
        if len(matches) != 1:
            return ([], 'The section cannot be uniquely identified. Please select a range from the verified catalog.')
        target = matches[0]
    if target.range is None or target.mapping_status != 'verified' or target.source_content_hash != source.metadata.get('content_hash') or (structure.source_content_hash != source.metadata.get('content_hash')) or (target.catalog_version != structure.catalog_version):
        return ([], 'The chapter range or catalog version is unverified. Please select an available range.')
    return ([SourceQueryRef(source_ingestion_id=source.id, source_content_hash=source.metadata['content_hash'], source_chapter_id=target.id, catalog_version=structure.catalog_version)], '')
