"""Materialize Pi's displayable directory; citation ranges require actual reads."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from openclass_core.models import SourceChapter, SourceRange, SourceStructure, SourceStructureQuality, new_id
from openclass_core.services.source_content_contract import verified_read_receipt
from openclass_core.services.source_catalog_refinement import PIPELINE
from openclass_core.services.source_catalog_quality import current_range_proof, is_page_level_range, precise_native_boundary, same_range_scope, verified_range_metadata

class RefinementNode(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str
    title: str
    level: int = Field(default=1, ge=1)
    printed_page_label: str = ''
    directory_printed_page_label: str = ''
    page_label_reviewed: bool = False
    source_range: SourceRange | None = None
    range_complete: bool = False
    read_ids: list[str] = Field(default_factory=list)
    directory_read_ids: list[str] = Field(default_factory=list)

class RefinementReport(BaseModel):
    model_config = ConfigDict(extra='forbid')
    nodes: list[RefinementNode] = Field(default_factory=list)
    origin: Literal['authored', 'ai_organized'] = 'authored'
    directory_complete: bool = False
    directory_read_ids: list[str] = Field(default_factory=list)
    summary: str = ''
    next_plan: str = ''

def read_units(task, ids):
    return [unit for identity in ids for entry in [task.get('reads', {}).get(identity, {})] if verified_read_receipt(entry.get('receipt', {})) and entry['receipt'].get('content_hash') == task['content_hash'] and (entry['receipt'].get('source_version') == task['source_version']) for unit in entry.get('units', [])]

def current_boundary_read_ids(source, task, ids):
    expected = {'grant_id': task['request_id'], 'role': 'source_catalog_refinement', 'authorization': 'explicit', 'owner_user_id': source.owner_user_id, 'package_id': source.package_id, 'source_id': source.id, 'content_hash': task['content_hash'], 'source_version': task['source_version']}
    return [identity for identity in ids for entry in [task.get('reads', {}).get(identity, {})] if entry.get('request_id') == task['request_id'] and entry.get('receipt', {}).get('read_id') == identity and all((entry['receipt'].get(key) == value for key, value in expected.items())) and verified_read_receipt(entry['receipt'])]

def range_was_read(value, units):
    if value is None or value.start_anchor or value.end_anchor:
        return False

    def contains(endpoint):
        for unit in units:
            loc = unit['locator']
            if loc.get('kind') != value.kind or loc.get('container', '') != value.container:
                continue
            if type(endpoint) is int and type(loc.get('start')) is int and (loc['start'] <= endpoint <= loc.get('end', loc['start'])):
                return True
            if endpoint is None and loc.get('start') is None or endpoint == loc.get('start'):
                return True
        return False
    return contains(value.start) and contains(value.end)

def directory_units(task, ids):
    from openclass_core.services.source_catalog_transcription import transcription_view
    units = read_units(task, ids)
    if not transcription_view(task)['complete']:
        return units
    planned = [{'locator': unit['source_range']} for unit in task['directory_transcription']['units']]
    return [unit for unit in units if range_was_read(SourceRange.model_validate(unit['locator']), planned)]

def materialize(source, task, report):
    from openclass_core.services.source_catalog_pagination import mapped_positions, mapped_ranges, mapped_range_limits
    from openclass_core.services.source_catalog_scope_boundaries import verified_scopes
    from openclass_core.services.source_catalog_transcription import transcription_view
    directory_pages = {page for unit in directory_units(task, report.directory_read_ids) if unit['locator'].get('kind') == 'pdf_pages' for page in range(unit['locator']['start'], unit['locator']['end'] + 1)}
    page_count = int(source.metadata.get('catalog_pdf_page_count') or source.metadata.get('page_count') or 0)
    calibrated = mapped_ranges(task, report.nodes, page_count=page_count, directory_pages=directory_pages)
    positions = mapped_positions(task, report.nodes, page_count=page_count, directory_pages=directory_pages)
    position_limits = mapped_range_limits(task, report.nodes, page_count=page_count, directory_pages=directory_pages)
    visual_scopes = verified_scopes(task, report.nodes, page_count=page_count, directory_pages=directory_pages)
    seed = {row['id']: row for row in task['seed']}
    stack, chapters = ([], [])
    version = task['published_version'] + 1
    for index, node in enumerate(report.nodes):
        while stack and stack[-1].level >= node.level:
            stack.pop()
        value = node.source_range
        old = seed.get(node.key) or seed.get(task.get('node_ids', {}).get(node.key), {})
        saved_range = SourceRange.model_validate(old['range']) if old.get('range') else None
        same_row = old.get('title') == node.title and old.get('level') == node.level and (old.get('source_content_hash') == task['content_hash']) and (old.get('owner_user_id') == source.owner_user_id) and (old.get('package_id') == source.package_id) and (old.get('source_ingestion_id') == source.id) and ('printed_page' not in old.get('metadata', {}) or old['metadata']['printed_page'] == node.printed_page_label)
        saved_native = saved_range is not None and saved_range.metadata.get('source_pipeline') in {'source_original_catalog_v1', 'anythingllm_source_catalog_v2'}
        saved_proof = saved_range.metadata.get('range_verification') if saved_range else None
        saved_visual = isinstance(saved_proof, dict) and saved_proof.get('method') == 'original_pdf_visual_boundaries'
        retained = same_row and old.get('mapping_status') == 'verified' and current_range_proof(source, saved_range) and (same_range_scope(saved_range, value) or (value is None and (saved_native or saved_visual or precise_native_boundary(saved_range))))
        fresh_ids = current_boundary_read_ids(source, task, node.read_ids)
        units = read_units(task, fresh_ids)
        verified = retained or (node.range_complete and range_was_read(value, units))
        if retained:
            value = saved_range
        retained_exact = retained and (not (isinstance(saved_proof, dict) and saved_proof.get('method') in {'original_pdf_pagination', 'original_pdf_pagination_pages'}))
        if task.get('pdf_pagination', {}).get('regimes') and value and (value.kind == 'pdf_pages') and node.printed_page_label.strip().isdigit():
            verified = retained_exact
        method = 'verified_original_read' if verified else ''
        location, reason = ('unresolved', 'untrusted_previous_range' if old else 'no_verified_range')
        scope_reads = old.get('metadata', {}).get('source_read_ids') or value.metadata.get('range_verification', {}).get('read_ids', []) if retained else fresh_ids
        unsupported = value and (value.start_anchor or value.end_anchor or any((key in value.metadata for key in ('page_clips', 'boundary_precision', 'basic_start_char', 'basic_end_char')))) and (not retained)
        if unsupported:
            verified, reason = (False, 'unsupported_boundary_proof')
        if node.key in calibrated and (not retained_exact):
            value, scope_reads, regime_id = calibrated[node.key]
            verified, method = (True, 'verified_original_pagination_pages:' + regime_id)
        elif node.key in positions and (not retained_exact):
            verified = False
            if not 1 <= positions[node.key] <= page_count:
                location, reason = ('outside_uploaded_file', 'outside_uploaded_file')
            else:
                location, reason = ('position_only', position_limits.get(node.key) or 'chapter_boundary_unverified')
                method = 'pagination_position_pending_boundary'
        if value and value.kind == 'pdf_pages' and (not page_count or not 1 <= value.start <= value.end <= page_count):
            verified = False
        if verified:
            location, reason = ('verified', 'original_range_verified')
        if value and verified and (not retained_exact) and (not retained or node.key in calibrated):
            proof = verified_range_metadata(source, value, method='original_pdf_pagination_pages' if node.key in calibrated else 'original_boundary_reads', read_ids=scope_reads)
            value = value.model_copy(update={'metadata': {**proof, 'printed_page_label': node.printed_page_label}})
        if node.key in visual_scopes:
            visual_range, visual_reads, visual_proof = visual_scopes[node.key]
            visual_range = visual_range.model_copy(update={'metadata': {**verified_range_metadata(source, visual_range, method='original_pdf_visual_boundaries', read_ids=visual_reads, scope_proof=visual_proof), 'printed_page_label': node.printed_page_label}})
            if not current_range_proof(source, visual_range):
                raise ValueError('The host chapter crop does not match its signed source scope.')
            value, scope_reads = (visual_range, visual_reads)
            verified, location, reason = (True, 'verified', 'original_range_verified')
            method = 'verified_original_visual_boundaries'
        identity = old['id'] if same_row else task.setdefault('node_ids', {}).setdefault(node.key, 'sourcechapter_' + new_id('pi'))
        if same_row:
            task.setdefault('node_ids', {})[node.key] = identity
        directory = directory_units(task, node.directory_read_ids)
        chapter = SourceChapter(id=identity, owner_user_id=source.owner_user_id, package_id=source.package_id, source_ingestion_id=source.id, title=node.title, level=node.level, order_index=index, parent_id=stack[-1].id if stack else None, path=[item.title for item in stack] + [node.title], range=value if verified else None, mapping_status='verified' if verified else 'unverified', anchor_status='verified' if verified else 'unverified', catalog_version=version, source_content_hash=task['content_hash'], page_start=value.start if verified and value.kind == 'pdf_pages' else None, page_end=value.end if verified and value.kind == 'pdf_pages' else None, metadata={'catalog_engine': PIPELINE, 'printed_page': node.printed_page_label, 'mapped_pdf_page': positions.get(node.key), 'location_state': location, 'location_reason': reason, 'directory_printed_page': node.directory_printed_page_label, 'locator_source': method, 'directory_evidence': [{'locator': unit['locator'], 'status': 'model_observed', 'source_version': task['source_version'], 'content_hash': task['content_hash']} for unit in directory], 'source_read_ids': scope_reads})
        chapters.append(chapter)
        stack.append(chapter)
    directory = directory_units(task, report.directory_read_ids)
    evidence = [{'locator': item['locator'], 'status': 'model_observed', 'source_version': task['source_version'], 'content_hash': task['content_hash']} for item in directory]
    complete = bool(directory) and transcription_view(task)['complete']
    if report.origin == 'ai_organized':
        total = int(source.metadata.get('basic_unit_count', 0))
        covered = {identity for item in read_units(task, list(task.get('reads', {}))) for identity in item.get('unit_ids', [item.get('unit_id')])}
        complete = report.directory_complete and total > 0 and (set(range(1, total + 1)) <= covered)
    metadata = {'catalog_executor': 'pi', 'catalog_engine': PIPELINE, 'catalog_pipeline': PIPELINE, 'catalog_task_contract': PIPELINE, 'catalog_display_mode': 'direct', 'catalog_quality_gates': 'disabled', 'catalog_origin': report.origin, 'catalog_label': 'Table of contents' if report.origin == 'authored' else 'AI-organized content structure', 'catalog_result_returned': True, 'catalog_ready': True, 'catalog_version': version, 'directory_status': 'complete' if complete else 'uncertain', 'catalog_directory_evidence': evidence, 'catalog_read_ranges': task.get('coverage', []), 'catalog_directory_origin': 'original_pages' if evidence else 'location_pending', 'catalog_status': 'running', 'summary': report.summary, 'next_plan': report.next_plan}
    ambiguous = set()
    for index, chapter in enumerate(chapters):
        value = chapter.range
        if not value or value.kind != 'pdf_pages':
            continue
        if any((other.parent_id == chapter.parent_id and other.level == chapter.level and other.range and (other.range.kind == value.kind) and (other.range.start <= value.end) and (value.start <= other.range.end) and (not (is_page_level_range(source, value) or is_page_level_range(source, other.range) or (precise_native_boundary(value) and precise_native_boundary(other.range)))) for other in chapters[:index] + chapters[index + 1:])):
            ambiguous.add(index)
    for index in ambiguous:
        chapters[index] = chapters[index].model_copy(update={'range': None, 'page_start': None, 'page_end': None, 'mapping_status': 'unverified', 'anchor_status': 'unverified', 'metadata': {**chapters[index].metadata, 'mapped_pdf_page': chapters[index].range.start, 'location_state': 'position_only', 'location_reason': 'overlapping_sibling_ranges'}})
    counts = {state: sum((chapter.metadata['location_state'] == state for chapter in chapters)) for state in ('verified', 'position_only', 'unresolved', 'outside_uploaded_file')}
    metadata.update(catalog_location_summary={**counts, 'total': len(chapters)}, catalog_location_complete=bool(chapters) and (not counts['unresolved']))
    verified = sum((chapter.mapping_status == 'verified' for chapter in chapters))
    structure = SourceStructure(owner_user_id=source.owner_user_id, package_id=source.package_id, source_ingestion_id=source.id, status='ready', strategy='ai_directory_v1', chapter_count=len(chapters), catalog_version=version, source_content_hash=task['content_hash'], catalog_schema_version=PIPELINE, catalog_model=task['model']['model'], has_verified_toc=complete, quality=SourceStructureQuality(level='unassessed', total_chapter_count=len(chapters), verified_chapter_count=verified, unverified_chapter_count=len(chapters) - verified), metadata=metadata)
    return (structure, chapters, metadata)
