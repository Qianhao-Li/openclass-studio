from __future__ import annotations
import hashlib
import json
import math
from openclass_core.models import RetrievalEvidence, SourceRange
from .config import GoogleSourceError

def page_coordinate_metadata(source, start, end):
    pdf = source.mime_type.split(';', 1)[0] == 'application/pdf' or source.file_name.lower().endswith('.pdf')
    coordinate = 'physical_pdf_pages' if pdf else 'processing_document_pages'
    span = str(start) if start == end else f'{start}–{end}'
    return {'page_coordinate_system': coordinate, 'page_label': f'PDF page {span}' if pdf and start is not None else f'Processing page {span}' if start is not None else ''}

def evidence_from_chunk(source, chunk: dict, *, document_id: str, locator: dict | None=None) -> RetrievalEvidence:
    text = str(chunk.get('content') or '').strip()
    span = chunk.get('pageSpan', {})
    start, end = (span.get('pageStart'), span.get('pageEnd'))
    return RetrievalEvidence(id='evidence_' + hashlib.sha256(f"{source.id}:{source.metadata['source_version']}:{document_id}:{chunk.get('id')}".encode()).hexdigest()[:24], source_ingestion_id=source.id, source_title=source.title, source_uri=source.source_uri, chunk_ids=[str(chunk.get('id', ''))], excerpt=text[:1600], expanded_text=text, page_range=f'{start}-{end}' if start else '', token_count=max(1, math.ceil(len(text) / 3)), reason='Google source search evidence', metadata={**page_coordinate_metadata(source, start, end), 'source_backend': 'google', 'source_version': source.metadata['source_version'], 'source_content_hash': source.metadata['content_hash'], 'page_start': start, 'page_end': end, 'google_document_id': document_id, 'parser_run_id': source.metadata.get('parser_run_id', ''), 'locator': locator or {}, 'source_transport': 'google_verified_evidence'})

def parsed_units(part: dict) -> list[dict]:
    """Positioned cloud layout blocks, carrying their own identity rather than chunk IDs."""
    from .processor import normalized_blocks
    result = []
    offset = int(part.get('page_offset', 0))
    for block in normalized_blocks(part.get('parsed', {})):
        start, end = (block.get('page_start'), block.get('page_end'))
        if not str(block.get('text') or '').strip() or type(start) is not int or type(end) is not int or (not 1 <= start <= end):
            continue
        result.append({'block_id': str(block['id']), 'content': block['text'], 'pageSpan': {'pageStart': start + offset, 'pageEnd': end + offset}, 'bbox': block.get('bbox', block.get('bounding_box', [])), 'block_type': block.get('type', ''), 'section_path': block.get('parents', []), 'layout': {key: block[key] for key in ('table_id', 'table_row', 'table_column', 'table_row_kind', 'list_id', 'list_item') if key in block}})
    return result

def evidence_from_unit(source, unit: dict, *, document_id: str, locator=None):
    if 'block_id' not in unit:
        return evidence_from_chunk(source, unit, document_id=document_id, locator=locator)
    span = unit['pageSpan']
    identity = hashlib.sha256(f"{source.id}:{source.metadata['source_version']}:{document_id}:block:{unit['block_id']}".encode()).hexdigest()[:24]
    text = unit['content']
    return RetrievalEvidence(id='evidence_' + identity, source_ingestion_id=source.id, source_title=source.title, source_uri=source.source_uri, section_path=unit.get('section_path', []), page_range=f"{span['pageStart']}-{span['pageEnd']}", excerpt=text[:1600], expanded_text=text, token_count=max(1, math.ceil(len(text) / 3)), reason='Verified Google layout range reading', metadata={**page_coordinate_metadata(source, span['pageStart'], span['pageEnd']), 'source_backend': 'google', 'source_version': source.metadata['source_version'], 'source_content_hash': source.metadata['content_hash'], 'page_start': span['pageStart'], 'page_end': span['pageEnd'], 'google_document_id': document_id, 'google_block_ids': [unit['block_id']], 'layout': unit.get('layout', {}), 'bbox': unit.get('bbox', []), 'locator': locator or {}, 'source_transport': 'google_verified_evidence', 'parser_run_id': source.metadata.get('parser_run_id', '')})

def authorized_units(part, requested):
    if requested is not None and requested.kind == 'pdf_pages':
        blocks = parsed_units(part)
        if blocks:
            return [block for block in blocks if in_range(block, requested, part['locator'])]
    return [chunk for chunk in part['chunks'] if in_range(chunk, requested, part['locator'])]

def authorized_records(artifact, requested):
    """Keep a chapter's shared boundary page, but stop before its next heading."""
    if requested is None or not (requested.start_anchor or requested.end_anchor):
        return [(part, unit) for part in artifact['parts'] for unit in authorized_units(part, requested)]
    if requested.kind != 'pdf_pages' or (requested.end_anchor and requested.metadata.get('end_anchor_exclusive') is not True):
        raise GoogleSourceError('Exact source anchors must be verified exclusive PDF layout boundaries.')
    records = [(part, unit) for part in artifact['parts'] for unit in parsed_units(part)]
    identities = [f"{part['document_id']}/{unit['block_id']}" for part, unit in records]

    def position(anchor, default):
        if not anchor:
            return default
        matches = [index for index, identity in enumerate(identities) if identity == anchor]
        if len(matches) != 1:
            raise GoogleSourceError('An exact source anchor is missing, duplicated, or belongs to another file version.')
        return matches[0]
    start, end = (position(requested.start_anchor, 0), position(requested.end_anchor, len(records)))
    if start >= end:
        raise GoogleSourceError('The source chapter anchors are in an invalid order.')
    return [(part, unit) for part, unit in records[start:end] if in_range(unit, requested, part['locator'])]

def _spine_positions(locator):
    if locator.get('kind') == 'epub_spine':
        return [{'container': locator.get('container'), 'spine_index': locator.get('start')}]
    if locator.get('kind') == 'epub_image':
        return locator.get('chapters') or ([locator] if isinstance(locator.get('spine_index'), int) else [])
    return []

def validate_range(requested):
    if requested is None:
        return
    if requested.kind in {'pdf_pages', 'epub_spine'}:
        if type(requested.start) is not int or type(requested.end) is not int or (not 1 <= requested.start <= requested.end):
            raise GoogleSourceError('The source range must be a valid consecutive interval of positive integers.')
    elif requested.kind == 'structured_path':
        if not requested.container or requested.start is not None or requested.end is not None or requested.path or requested.start_anchor or requested.end_anchor:
            raise GoogleSourceError('This structural range must specify a complete container and cannot ignore a narrower authorized range.')
    else:
        raise GoogleSourceError('This range type does not support precise cloud reading yet. Select a file, PDF page range, or EPUB chapter.')

def in_range(chunk: dict, requested: SourceRange | None, locator: dict) -> bool:
    validate_range(requested)
    if requested is None:
        return True
    if requested.kind == 'pdf_pages':
        span = chunk.get('pageSpan', {})
        start, end = (span.get('pageStart'), span.get('pageEnd'))
        return type(start) is int and type(end) is int and (requested.start <= start <= end <= requested.end)
    if requested.kind == 'epub_spine':
        return any((type(position.get('spine_index')) is int and requested.start <= position['spine_index'] <= requested.end and (not requested.container or requested.container == position.get('container')) for position in _spine_positions(locator)))
    return requested.container == str(locator.get('container', ''))

def validate_page_bounds(artifact, requested):
    count = artifact.get('page_count')
    if requested is not None and requested.kind == 'pdf_pages' and (type(count) is int) and (requested.end > count):
        raise GoogleSourceError(f"The requested pages are outside this source's {count}-page range.")

def read_artifact(source, artifact: dict, requested: SourceRange | None, cursor: str | None, max_chars: int=18000) -> dict:
    validate_range(requested)
    validate_page_bounds(artifact, requested)
    scope_hash = hashlib.sha256(json.dumps({'source_id': source.id, 'range': requested.model_dump(mode='json') if requested else None}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:32]
    prefix = source.metadata['source_version'] + ':' + scope_hash + ':'
    if cursor and (not cursor.startswith(prefix)):
        raise GoogleSourceError('The source reading cursor has expired.')
    try:
        offset = int(cursor[len(prefix):]) if cursor else 0
    except ValueError as exc:
        raise GoogleSourceError('The source reading cursor is invalid.') from exc
    records = authorized_records(artifact, requested)
    if offset < 0 or offset > len(records):
        raise GoogleSourceError('The source reading cursor is out of range.')
    items, size, next_offset = ([], 0, offset)
    for part, chunk in records[offset:]:
        text = str(chunk.get('content') or '')
        if size + len(text) > max_chars:
            if not items:
                raise GoogleSourceError('A source chunk exceeds the reading limit and cannot be safely truncated while preserving complete content.')
            break
        items.append(evidence_from_unit(source, chunk, document_id=part['document_id'], locator=part['locator']).model_dump(mode='json'))
        size += len(text)
        next_offset += 1
    coverage, missing_pages = (True, [])
    paginated = requested is not None and requested.kind == 'pdf_pages'
    if paginated or (requested is None and type(artifact.get('page_count')) is int):
        covered = set()
        for _, chunk in records:
            span = chunk.get('pageSpan', {})
            if type(span.get('pageStart')) is int and type(span.get('pageEnd')) is int and str(chunk.get('content') or '').strip():
                end = span['pageStart'] if str(chunk.get('block_type', '')).startswith('HEADING') else span['pageEnd']
                covered.update(range(span['pageStart'], end + 1))
        expected = range(requested.start, requested.end + 1) if paginated else range(1, artifact['page_count'] + 1)
        missing_pages = sorted(set(expected) - covered)
        coverage = not missing_pages
    if requested and requested.kind == 'epub_spine':
        seen = {part['locator'].get('start') for part, _ in records if part['locator'].get('kind') == 'epub_spine'}
        coverage = all((index in seen for index in range(requested.start, requested.end + 1)))
        if any((part.get('locator', {}).get('kind') == 'epub_image' and (not _spine_positions(part['locator'])) for part in artifact['parts'])):
            coverage = False
    complete = next_offset == len(records) and coverage and bool(records)
    warning = ''
    if not coverage:
        warning = 'Some content was excluded because it falls outside the authorized range or has no reliable location. These results do not cover the complete range.'
        if missing_pages:
            visible = ', '.join((str(page) for page in missing_pages[:20])) + (' and more' if len(missing_pages) > 20 else '')
            warning = f'Pages {visible} have no searchable chunks within the authorized range. They may be blank, unrecognized, or span multiple ranges; these pages have not been read completely.'
    return {'source_id': source.id, 'source_version': source.metadata['source_version'], 'source_content_hash': source.metadata['content_hash'], 'items': items, 'complete': complete, 'range_coverage_verified': coverage, 'unmapped_pages': missing_pages, 'next_cursor': prefix + str(next_offset) if next_offset < len(records) else None, 'warning': warning}

def query_table(tables: dict, plan: dict) -> dict:
    allowed = {'sheet', 'start_row', 'end_row', 'columns', 'filters', 'operation', 'column'}
    if set(plan) - allowed:
        raise GoogleSourceError('The spreadsheet query contains an unsupported operation.')
    sheet = plan.get('sheet')
    if sheet not in tables:
        raise GoogleSourceError('The requested worksheet was not found.')
    rows = tables[sheet]
    start, end = (int(plan.get('start_row', 1)), int(plan.get('end_row', len(rows))))
    if start < 1 or end < start or end > len(rows):
        raise GoogleSourceError('The spreadsheet row range is invalid.')
    records = [(i + 1, row) for i, row in enumerate(rows) if start <= i + 1 <= end]
    for rule in plan.get('filters', []):
        if set(rule) != {'column', 'op', 'value'} or rule['op'] not in {'eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'contains'}:
            raise GoogleSourceError('The spreadsheet filter is unsupported.')
        column = int(rule['column'])
        if column < 1:
            raise GoogleSourceError('Spreadsheet column numbers start at 1.')

        def matches(row):
            value = row[column - 1] if len(row) >= column else None
            expected = rule['value']
            if isinstance(value, dict):
                raise GoogleSourceError('The spreadsheet contains formulas with no cached results. A complete calculation is not possible.')
            try:
                op = rule['op']
                return value == expected if op == 'eq' else value != expected if op == 'ne' else str(expected) in str(value) if op == 'contains' else value > expected if op == 'gt' else value >= expected if op == 'gte' else value < expected if op == 'lt' else value <= expected
            except TypeError:
                return False
        records = [(i, row) for i, row in records if matches(row)]
    operation = plan.get('operation', 'rows')
    if operation == 'count':
        return {'sheet': sheet, 'operation': operation, 'value': len(records), 'complete': True}
    if operation in {'sum', 'mean', 'min', 'max'}:
        column = int(plan.get('column', 0))
        if column < 1:
            raise GoogleSourceError('Specify the column to calculate.')
        values = []
        for _, row in records:
            value = row[column - 1] if len(row) >= column else None
            if isinstance(value, dict):
                raise GoogleSourceError('Formula results are unavailable. Missing results cannot be treated as zero.')
            if value in (None, ''):
                continue
            try:
                number = float(value)
            except (ValueError, TypeError) as exc:
                raise GoogleSourceError('The calculation range contains non-numeric values.') from exc
            if not math.isfinite(number):
                raise GoogleSourceError('The calculation range contains non-finite numbers.')
            values.append(number)
        result = sum(values) if operation == 'sum' else sum(values) / len(values) if operation == 'mean' and values else min(values) if operation == 'min' and values else max(values) if operation == 'max' and values else None
        return {'sheet': sheet, 'operation': operation, 'value': result, 'numeric_count': len(values), 'complete': True}
    if operation != 'rows':
        raise GoogleSourceError('This spreadsheet operation is not supported yet.')
    columns = [int(column) for column in plan.get('columns', [])]
    if any((column < 1 for column in columns)):
        raise GoogleSourceError('Spreadsheet column numbers start at 1.')
    return {'sheet': sheet, 'rows': [{'row': i, 'values': [row[column - 1] if column <= len(row) else None for column in columns] if columns else row} for i, row in records[:1000]], 'complete': len(records) <= 1000, 'matched_rows': len(records)}
