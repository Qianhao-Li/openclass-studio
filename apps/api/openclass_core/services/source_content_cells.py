"""Read exact native cell rectangles without expanding column authorization."""
from __future__ import annotations
import csv
import io
import json
import re
from .source_archive import SafeSourceArchive
from .source_content_formats import SourceContentReadError, decode, text_part
from .source_content_office import _conversion, _relationships, _local, _linked_parts
from .source_xml import parse_untrusted_xml
_NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
MAX_SELECTED_CELLS = 100000

def cell_coordinate(value):
    match = re.fullmatch('([A-Z]+)([1-9][0-9]*)', str(value or ''))
    if not match:
        raise SourceContentReadError('Cell ranges require uppercase A1-style coordinates.')
    column = 0
    for letter in match[1]:
        column = column * 26 + ord(letter) - 64
    row = int(match[2])
    if column > 1000 or row > 100000:
        raise SourceContentReadError('The requested cell coordinate exceeds supported source limits.')
    return (row, column)

def cell_address(row, column):
    letters = ''
    while column:
        column, digit = divmod(column - 1, 26)
        letters = chr(65 + digit) + letters
    return letters + str(row)

def _rect(start, end):
    top, left = cell_coordinate(start)
    bottom, right = cell_coordinate(end)
    if top > bottom or left > right:
        raise SourceContentReadError('The original cell rectangle is reversed.')
    return (top, left, bottom, right)

def _overlaps(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and (a[1] <= b[3]) and (b[1] <= a[3])

def _contains(outer, inner):
    return outer[0] <= inner[0] <= inner[2] <= outer[2] and outer[1] <= inner[1] <= inner[3] <= outer[3]

def _xlsx_cells(archive, sheet_name):
    workbook = parse_untrusted_xml(archive.read('xl/workbook.xml'))
    sheets = [node for node in workbook.findall('s:sheets/s:sheet', _NS) if node.attrib.get('name') == sheet_name]
    if len(sheets) != 1:
        raise SourceContentReadError('Select an exact, unique original worksheet name.')
    relationship = sheets[0].attrib.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
    target, kind, external = _relationships(archive, 'xl/workbook.xml').get(relationship, ('', '', True))
    if external or kind != 'worksheet':
        raise SourceContentReadError('The selected original worksheet relationship is unavailable.')
    root = parse_untrusted_xml(archive.read(target))
    if any((_local(node.tag) in {'legacyDrawing', 'oleObjects'} for node in root)):
        raise SourceContentReadError('Legacy drawing objects have no verified bounds inside this cell selection.')
    shared = []
    if 'xl/sharedStrings.xml' in archive.namelist():
        shared = [''.join(node.itertext()) for node in parse_untrusted_xml(archive.read('xl/sharedStrings.xml'))]
    cells, maximum_row, maximum_column, row_ids = ({}, 0, 0, set())
    for row in root.findall('s:sheetData/s:row', _NS):
        row_index = int(row.attrib.get('r', '0'))
        if not 1 <= row_index <= 100000 or row_index in row_ids:
            raise SourceContentReadError('The original worksheet contains an invalid or duplicate row identity.')
        row_ids.add(row_index)
        for node in row:
            if _local(node.tag) != 'c':
                continue
            address = node.attrib.get('r', '')
            index, column = cell_coordinate(address)
            if index != row_index or address in cells:
                raise SourceContentReadError('The original cell identity is inconsistent or duplicated.')
            stored, formula = (node.find('s:v', _NS), node.find('s:f', _NS))
            raw = stored.text if stored is not None else None
            kind = node.attrib.get('t', 'n')
            value = raw
            if kind == 's' and raw is not None:
                try:
                    shared_index = int(raw)
                    if not 0 <= shared_index < len(shared):
                        raise ValueError()
                    value = shared[shared_index]
                except (ValueError, TypeError) as exc:
                    raise SourceContentReadError('The original shared-string index is invalid.') from exc
            elif kind == 'inlineStr':
                inline = node.find('s:is', _NS)
                if inline is None:
                    raise SourceContentReadError('The original inline cell string is missing.')
                value = ''.join(inline.itertext())
            cells[address] = {'address': address, 'value': value, 'type': kind, 'formula': formula.text if formula is not None else None, 'cached_value': raw if formula is not None else None, 'cache_available': stored is not None and raw is not None if formula is not None else None}
            maximum_row, maximum_column = (max(maximum_row, index), max(maximum_column, column))
    merged = []
    for node in root.findall('s:mergeCells/s:mergeCell', _NS):
        pieces = node.attrib.get('ref', '').split(':')
        if len(pieces) != 2:
            raise SourceContentReadError('The original merge range is invalid.')
        rectangle = _rect(*pieces)
        merged.append((node.attrib['ref'], rectangle))
        maximum_row, maximum_column = (max(maximum_row, rectangle[2]), max(maximum_column, rectangle[3]))
    dimension = root.find('s:dimension', _NS)
    if dimension is not None and dimension.attrib.get('ref'):
        endpoints = dimension.attrib['ref'].split(':')
        bounds = _rect(endpoints[0], endpoints[-1])
        maximum_row, maximum_column = (max(maximum_row, bounds[2]), max(maximum_column, bounds[3]))
    drawing_relationships = _relationships(archive, target)
    for node in root:
        if _local(node.tag) != 'drawing':
            continue
        relation = next((value for key, value in node.attrib.items() if 'relationships' in key), None)
        drawing_path, drawing_kind, external = drawing_relationships.get(relation, ('', '', True))
        if external or drawing_kind != 'drawing':
            raise SourceContentReadError('The worksheet drawing relationship is invalid.')
        for anchor in parse_untrusted_xml(archive.read(drawing_path)):
            for point in anchor:
                if _local(point.tag) not in {'from', 'to'}:
                    continue
                for coordinate in point:
                    if _local(coordinate.tag) in {'row', 'col'} and coordinate.text and coordinate.text.isdigit():
                        if _local(coordinate.tag) == 'row':
                            maximum_row = max(maximum_row, int(coordinate.text) + 1)
                        else:
                            maximum_column = max(maximum_column, int(coordinate.text) + 1)
    return (cells, merged, root, target, maximum_row, maximum_column)

def _drawing_parts(archive, root, worksheet, requested, locator, artifact):
    relationships = _relationships(archive, worksheet)
    result = []
    for node in root:
        if _local(node.tag) != 'drawing':
            continue
        relation = next((value for key, value in node.attrib.items() if 'relationships' in key), None)
        target, kind, external = relationships.get(relation, ('', '', True))
        if external or kind != 'drawing':
            raise SourceContentReadError('The worksheet drawing relationship is invalid.')
        for anchor in parse_untrusted_xml(archive.read(target)):

            def point(name):
                marker = next((child for child in anchor if _local(child.tag) == name), None)
                if marker is None:
                    return None
                coords = {}
                for child in marker:
                    if _local(child.tag) in {'row', 'col'}:
                        if child.text is None or not child.text.isdigit():
                            raise SourceContentReadError('The original drawing cell anchor is invalid.')
                        coords[_local(child.tag)] = int(child.text) + 1
                return (coords['row'], coords['col']) if set(coords) == {'row', 'col'} else None
            start, end = (point('from'), point('to'))
            if start and (start[0] > requested[2] or start[1] > requested[3]):
                continue
            if _local(anchor.tag) != 'twoCellAnchor' or start is None or end is None:
                raise SourceContentReadError('The drawing has no complete row-and-column bounds inside this cell selection.')
            rectangle = (start[0], start[1], end[0], end[1])
            if start[0] > end[0] or start[1] > end[1]:
                raise SourceContentReadError('The original drawing cell bounds are reversed.')
            if not _overlaps(requested, rectangle):
                continue
            if not _contains(requested, rectangle):
                raise SourceContentReadError('The selected cells cut through an original drawing; its columns or rows cannot be expanded automatically.')
            if any((_local(child.tag) == 'chart' for child in anchor.iter())):
                raise SourceContentReadError('This chart has no verified visual crop for the selected cell rectangle.')
            if not any((_local(child.tag) == 'pic' for child in anchor.iter())):
                raise SourceContentReadError('This drawing object has no verified original image representation.')
            at = {'kind': 'sheet_cells', 'container': locator['container'], 'start': cell_address(*start), 'end': cell_address(*end)}
            result.extend(_linked_parts(anchor, archive, target, artifact, at, ''))
    return result

def _parts(cells, merged, requested, scope, *, atomic_ranges=()):
    top, left, bottom, right = requested
    relevant = [(label, rect) for label, rect in merged if _overlaps(rect, requested)]
    if any((not _contains(requested, rect) for _, rect in relevant)):
        raise SourceContentReadError('The selected cells cut through a merged cell; select its full original rectangle.')
    bands = [rect for _, rect in relevant] + list(atomic_ranges)
    result, row = ([], top)
    while row <= bottom:
        last = row
        while True:
            extended = max([last, *(rect[2] for rect in bands if rect[0] <= last and rect[2] >= row)])
            if extended == last:
                break
            last = extended
        values = []
        for r in range(row, last + 1):
            for c in range(left, right + 1):
                address = cell_address(r, c)
                cell = dict(cells.get(address) or {'address': address, 'value': None, 'type': 'blank', 'formula': None, 'cached_value': None, 'cache_available': None})
                if cell.get('formula') is not None and cell.get('cache_available') is not True:
                    raise SourceContentReadError('The selected original formula has no cached result.')
                values.append(cell)
        data = {'cells': values, 'merged_ranges': [label for label, rect in relevant if row <= rect[0] <= rect[2] <= last]}
        at = {'kind': 'sheet_cells', 'container': scope['container'], 'start': cell_address(row, left), 'end': cell_address(last, right)}
        result.append(text_part(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')), at))
        row = last + 1
    return result

def read_cells_units(source, path, artifact, scope):
    if scope.get('kind') != 'sheet_cells' or not isinstance(scope.get('container'), str) or (not scope['container']):
        raise SourceContentReadError('Select a worksheet and an explicit native cell rectangle.')
    requested = _rect(scope.get('start'), scope.get('end'))
    if (requested[2] - requested[0] + 1) * (requested[3] - requested[1] + 1) > MAX_SELECTED_CELLS:
        raise SourceContentReadError('The selected cell rectangle exceeds 100,000 cells. Select a smaller range.')
    conversion = _conversion(source, path, artifact)
    object_only = bool(scope.get('object'))
    if path.suffix.lower() in {'.csv', '.tsv'}:
        if scope['container'] != 'Sheet1':
            raise SourceContentReadError('Delimited originals contain only Sheet1.')
        rows = list(csv.reader(io.StringIO(decode(path.read_bytes())), delimiter='\t' if path.suffix.lower() == '.tsv' else ','))
        if requested[2] > len(rows) or requested[3] > max(map(len, rows), default=0):
            raise SourceContentReadError('The requested cells are outside the original delimited table.')
        cells = {cell_address(r, c): {'address': cell_address(r, c), 'value': value, 'type': 'text', 'formula': None, 'cached_value': None, 'cache_available': None} for r, values in enumerate(rows, 1) for c, value in enumerate(values, 1) if requested[0] <= r <= requested[2] and requested[1] <= c <= requested[3]}
        result = [] if object_only else _parts(cells, [], requested, scope)
    elif path.suffix.lower() in {'.xlsx', '.xlsm'}:
        with SafeSourceArchive(path) as archive:
            cells, merged, root, worksheet, rows, columns = _xlsx_cells(archive, scope['container'])
            if not object_only and (requested[2] > rows or requested[3] > columns):
                raise SourceContentReadError('The requested cells are outside the original worksheet content.')
            visuals = _drawing_parts(archive, root, worksheet, requested, scope, artifact)
            bands = [_rect(unit['locator']['start'], unit['locator']['end']) for unit in visuals]
            result = [] if object_only else _parts(cells, merged, requested, scope, atomic_ranges=bands)
            result.extend(visuals)
    else:
        raise SourceContentReadError('Native cell rectangles require CSV, TSV, XLSX or XLSM originals.')
    if conversion:
        for unit in result:
            unit['provenance'].update(conversion)
    return result
