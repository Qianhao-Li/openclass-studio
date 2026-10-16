"""Deterministic calculations over complete native table inputs, never RAG data."""
from __future__ import annotations
import copy
import csv
import hashlib
import io
import json
import math
import re
from pathlib import Path
from .source_archive import SafeSourceArchive
from .source_content_formats import SourceContentReadError, decode, text_part
from .source_content_office import _conversion, _relationships
from .source_xml import parse_untrusted_xml
from .google_sources.read import query_table
MAX_ROWS = 100000
MAX_COLUMNS = 1000
_NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}

def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SourceContentReadError('The original calculation contains unsupported or non-finite values.') from exc

def _native_tables(path, raw):
    if path.suffix.lower() in {'.csv', '.tsv'}:
        rows = []
        for row in csv.reader(io.StringIO(decode(raw)), delimiter='\t' if path.suffix.lower() == '.tsv' else ','):
            if len(rows) >= MAX_ROWS or len(row) > MAX_COLUMNS:
                raise SourceContentReadError('The complete original table exceeds the supported row or column limit.')
            rows.append(row)
        return {'Sheet1': rows}
    if path.suffix.lower() not in {'.xlsx', '.xlsm'}:
        raise SourceContentReadError('Original calculations require CSV, TSV, XLSX, XLSM, or a verified spreadsheet conversion.')
    with SafeSourceArchive(path) as archive:
        shared = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            shared = [''.join(node.itertext()) for node in parse_untrusted_xml(archive.read('xl/sharedStrings.xml'))]
        workbook = parse_untrusted_xml(archive.read('xl/workbook.xml'))
        relationships = _relationships(archive, 'xl/workbook.xml')
        tables = {}
        for sheet in workbook.findall('s:sheets/s:sheet', _NS):
            name = sheet.attrib.get('name')
            if not name or name in tables:
                raise SourceContentReadError('The original workbook sheet identity is missing or duplicated.')
            relation = sheet.attrib.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
            target, kind, external = relationships.get(relation, ('', '', True))
            if external or kind != 'worksheet':
                raise SourceContentReadError('The original worksheet relationship is unavailable.')
            root = parse_untrusted_xml(archive.read(target))
            sparse = {}
            for row in root.findall('s:sheetData/s:row', _NS):
                index = int(row.attrib.get('r', '0'))
                if not 1 <= index <= MAX_ROWS or index in sparse:
                    raise SourceContentReadError('The original row identity is invalid or duplicated.')
                values, seen = ([], set())
                for cell in row:
                    if not str(cell.tag).endswith('}c'):
                        continue
                    match = re.fullmatch('([A-Z]+)([1-9][0-9]*)', cell.attrib.get('r', ''))
                    if not match or int(match[2]) != index:
                        raise SourceContentReadError('The cell address does not match its original row.')
                    column = 0
                    for letter in match[1]:
                        column = column * 26 + ord(letter) - 64
                    if column > MAX_COLUMNS or column in seen:
                        raise SourceContentReadError('The original column identity is too large or duplicated.')
                    seen.add(column)
                    values.extend([None] * (column - len(values)))
                    stored, formula = (cell.find('s:v', _NS), cell.find('s:f', _NS))
                    value = stored.text if stored is not None else None
                    cell_type = cell.attrib.get('t', 'n')
                    if formula is not None and (stored is None or value is None):
                        value = {'error': 'formula_cache_unavailable'}
                    elif cell_type == 's' and value is not None:
                        try:
                            shared_index = int(value)
                            if not 0 <= shared_index < len(shared):
                                raise ValueError()
                            value = shared[shared_index]
                        except (IndexError, ValueError) as exc:
                            raise SourceContentReadError('A complete original shared string is unavailable.') from exc
                    elif cell_type == 'inlineStr':
                        inline = cell.find('s:is', _NS)
                        if inline is None:
                            raise SourceContentReadError('The original inline string is missing.')
                        value = ''.join(inline.itertext())
                    elif cell_type == 'b' and value is not None:
                        if value not in {'0', '1'}:
                            raise SourceContentReadError('The original Boolean cell is invalid.')
                        value = value == '1'
                    elif cell_type == 'e':
                        value = {'error': 'original_cell_error', 'value': value}
                    elif cell_type == 'n' and value is not None:
                        try:
                            value = float(value)
                        except ValueError as exc:
                            raise SourceContentReadError('The original numeric cell is invalid.') from exc
                        if not math.isfinite(value):
                            raise SourceContentReadError('The original cell contains a non-finite value.')
                    values[column - 1] = value
                sparse[index] = values
            tables[name] = [sparse.get(index, []) for index in range(1, max(sparse, default=0) + 1)]
        return tables

def _integer(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise SourceContentReadError(f'The calculation {label} must be an integer coordinate.')
    try:
        result = int(value)
    except ValueError as exc:
        raise SourceContentReadError(f'The calculation {label} is invalid.') from exc
    if result < 1:
        raise SourceContentReadError(f'The calculation {label} must be positive.')
    return result

def read_original_calculation(*, source, original_path, artifact, query_plan):
    """Return one complete calculation object; outer service issues its receipt."""
    path = Path(original_path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    conversion = _conversion(source, path, artifact)
    if not conversion and digest != source.metadata.get('content_hash'):
        raise SourceContentReadError('The original table hash no longer matches this source version.')
    tables = _native_tables(path, raw)
    if not isinstance(query_plan, dict):
        raise SourceContentReadError('The original calculation requires a structured query plan.')
    plan = copy.deepcopy(query_plan)
    sheet = plan.get('sheet')
    if not isinstance(sheet, str) or sheet not in tables:
        raise SourceContentReadError('The requested original worksheet does not exist.')
    rows = tables[sheet]
    plan['start_row'] = _integer(plan.get('start_row', 1), 'start row')
    plan['end_row'] = _integer(plan.get('end_row', len(rows)), 'end row')
    if plan['end_row'] < plan['start_row'] or plan['end_row'] > len(rows):
        raise SourceContentReadError('The calculation range is outside the complete original table.')
    if 'column' in plan:
        plan['column'] = _integer(plan['column'], 'column')
    if 'columns' in plan:
        if not isinstance(plan['columns'], list):
            raise SourceContentReadError('The selected columns must be a list.')
        plan['columns'] = [_integer(column, 'column') for column in plan['columns']]
    if not isinstance(plan.get('filters', []), list):
        raise SourceContentReadError('The original calculation filters must be a list.')
    for rule in plan.get('filters', []):
        if not isinstance(rule, dict):
            raise SourceContentReadError('The original calculation filter is invalid.')
        rule['column'] = _integer(rule.get('column'), 'filter column')
    selected_rows = rows[plan['start_row'] - 1:plan['end_row']]
    if any((isinstance(value, dict) for row in selected_rows for value in row)):
        raise SourceContentReadError('The original input range has a formula without a cached result or an error cell. It cannot support a complete calculation.')
    _json(plan)
    result = query_table(tables, plan)
    result_text = _json(result)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise SourceContentReadError('The original table changed during calculation.')
    locator = {'kind': 'table_query', 'container': sheet, 'query_plan': copy.deepcopy(query_plan)}
    provenance = {'input_method': 'original_native_calculation', 'source_content_hash': source.metadata['content_hash'], 'source_version': source.metadata.get('source_version', ''), 'input_snapshot_sha256': digest, 'input_range': {'kind': 'sheet_rows', 'container': sheet, 'start': plan['start_row'], 'end': plan['end_row']}, 'input_values_sha256': hashlib.sha256(_json(selected_rows).encode()).hexdigest(), 'input_row_count': len(selected_rows), 'calculation_engine': 'native_table_query_v1', 'operation': plan.get('operation', 'rows'), 'formulas_executed': False, **({'conversion': conversion['conversion'], 'snapshot_sha256': conversion['snapshot_sha256']} if conversion else {})}
    complete = result.get('complete') is True
    missing = [] if complete else [{'reason': 'row_result_limit', 'matched_rows': result.get('matched_rows'), 'message': 'The complete original was queried, but the row listing is limited. Select a smaller row range.'}]
    return {'parts': [text_part(result_text, locator, **provenance)], 'range': locator, 'complete': complete, 'missing': missing, 'next_cursor': None, 'unit_count': 1}
