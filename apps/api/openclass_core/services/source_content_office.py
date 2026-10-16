"""Read native Office objects and delimited rows without executing document code."""
from __future__ import annotations
import csv
import hashlib
import io
import json
import posixpath
from xml.etree import ElementTree as ET
from .source_archive import SafeSourceArchive
from .source_xml import parse_untrusted_xml
from .source_ooxml_navigation import ordered_pptx_slide_parts
from .source_content_formats import SourceContentReadError, decode, interval, text_part
from .source_content_media import image_part

def _local(tag):
    return str(tag).rsplit('}', 1)[-1]

def _relationships(archive, container):
    directory, name = posixpath.split(container)
    rel = posixpath.join(directory, '_rels', name + '.rels')
    if rel not in archive.namelist():
        return {}
    result = {}
    for node in parse_untrusted_xml(archive.read(rel)):
        target = node.attrib.get('Target', '')
        external = node.attrib.get('TargetMode', '').lower() == 'external'
        resolved = target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join(directory, target))
        if not external and (resolved.startswith('../') or resolved not in archive.namelist()):
            raise SourceContentReadError('An original Office object has an invalid relationship.')
        result[node.attrib.get('Id')] = (resolved, node.attrib.get('Type', '').rsplit('/', 1)[-1], external)
    return result

def _linked_parts(node, archive, container, artifact, locator, source_hash):
    relationships = _relationships(archive, container)
    result, seen = ([], set())
    for child in node.iter():
        for key, value in child.attrib.items():
            if 'relationships' not in key or value in seen:
                continue
            seen.add(value)
            if value not in relationships:
                raise SourceContentReadError('The native object references a missing Office relationship.')
            target, kind, external = relationships[value]
            if kind == 'hyperlink':
                continue
            if external:
                raise SourceContentReadError('The native object depends on an external resource.')
            if kind == 'image':
                result.append(image_part(archive.read(target), locator={**locator, 'object': target}, artifact=artifact, original_name=target, source_hash=source_hash, native_container=container.startswith(('word/', 'ppt/'))))
            elif kind == 'chart':
                chart = parse_untrusted_xml(archive.read(target))
                result.append(text_part(ET.tostring(chart, encoding='unicode'), {**locator, 'object': target}, input_method='original_chart_xml'))
            else:
                raise SourceContentReadError('This native Office object requires an unsupported embedded-object reader.')
    return result

def _conversion(source, path, artifact):
    extension = path.suffix.lower()
    original_extension = source.file_name.rsplit('.', 1)[-1].lower()
    if extension == '.' + original_extension:
        return {}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    for part in artifact.get('parts', []):
        mapping = part.get('locator', {})
        if mapping.get('converted_from') == original_extension and mapping.get('original_sha256') == source.metadata.get('content_hash') and (part.get('input', {}).get('sha256') == digest):
            return {'input_method': 'verified_conversion_snapshot', 'conversion': mapping.get('conversion'), 'snapshot_sha256': digest, 'original_sha256': mapping['original_sha256']}
    raise SourceContentReadError('The converted Office snapshot has no verified original mapping.')

def read_office_units(source, path, artifact, scope):
    suffix = path.suffix.lower()
    provenance = _conversion(source, path, artifact)
    result = []
    if suffix in {'.csv', '.tsv'}:
        rows = list(csv.reader(io.StringIO(decode(path.read_bytes())), delimiter='\t' if suffix == '.tsv' else ','))
        start, end = interval(scope, 'sheet_rows', len(rows))
        if scope and scope.get('container') not in {None, '', 'Sheet1'}:
            raise SourceContentReadError('This delimited file has only Sheet1.')
        result = [text_part(json.dumps(rows[i - 1], ensure_ascii=False), {'kind': 'sheet_rows', 'container': 'Sheet1', 'start': i, 'end': i}) for i in range(start, end + 1)]
    else:
        with SafeSourceArchive(path) as archive:
            if suffix == '.docx':
                container = 'word/document.xml'
                document = parse_untrusted_xml(archive.read(container))
                body = next((x for x in document if _local(x.tag) == 'body'), None)
                if body is None:
                    raise SourceContentReadError('The DOCX body is unavailable.')
                if scope and scope.get('container') not in {None, '', container}:
                    raise SourceContentReadError('The DOCX range belongs to another native container.')
                nodes = [x for x in body.iter() if _local(x.tag) == 'p']
                base = 0 if (scope or {}).get('metadata', {}).get('index_base') == 0 else 1
                start, end = interval(scope, 'docx_paragraphs', len(nodes), base=base)
                for index in range(start, end + 1):
                    locator = {'kind': 'docx_paragraphs', 'start': index, 'end': index, 'container': container, 'index_base': base}
                    result.append(text_part(ET.tostring(nodes[index - base], encoding='unicode'), locator))
                    result.extend(_linked_parts(nodes[index - base], archive, container, artifact, locator, provenance.get('snapshot_sha256', source.metadata.get('content_hash', ''))))
            elif suffix == '.pptx':
                names = ordered_pptx_slide_parts(archive)
                start, end = interval(scope, 'ppt_slides', len(names))
                for index in range(start, end + 1):
                    node = parse_untrusted_xml(archive.read(names[index - 1]))
                    locator = {'kind': 'ppt_slides', 'start': index, 'end': index, 'container': names[index - 1]}
                    result.append(text_part(ET.tostring(node, encoding='unicode'), locator))
                    result.extend(_linked_parts(node, archive, names[index - 1], artifact, locator, provenance.get('snapshot_sha256', source.metadata.get('content_hash', ''))))
            else:
                result = _sheet_units(archive, scope, artifact=artifact)
    if provenance:
        for unit in result:
            unit['provenance'].update(provenance)
    return result

def _sheet_units(archive, scope, *, artifact=None, whole_workbook=False):
    artifact = artifact or {}
    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    workbook = parse_untrusted_xml(archive.read('xl/workbook.xml'))
    rels = _relationships(archive, 'xl/workbook.xml')
    sheets = workbook.findall('s:sheets/s:sheet', ns)
    wanted = (scope or {}).get('container')
    matches = [sheet for sheet in sheets if sheet.attrib['name'] == wanted] if wanted else sheets
    if not wanted and len(matches) > 1:
        result = []
        for sheet in matches:
            result.extend(_sheet_units(archive, {'kind': 'sheet_rows', 'container': sheet.attrib['name']}, artifact=artifact, whole_workbook=True))
        seen = set()
        unique = []
        for unit in result:
            if unit['locator'].get('kind') == 'workbook_visual':
                key = unit['locator'].get('snapshot_page')
                if key in seen:
                    continue
                seen.add(key)
            unique.append(unit)
        return unique
    if len(matches) != 1:
        raise SourceContentReadError('Select one original worksheet by its exact name.')
    sheet = matches[0]
    relation = next((v for k, v in sheet.attrib.items() if 'relationships' in k), None)
    target, kind, external = rels.get(relation, ('', '', True))
    if external or kind != 'worksheet':
        raise SourceContentReadError('The worksheet relationship cannot be verified.')
    root = parse_untrusted_xml(archive.read(target))
    if any((_local(node.tag) in {'legacyDrawing', 'oleObjects'} for node in root)):
        raise SourceContentReadError('Legacy drawings and embedded objects require a separately mapped worksheet snapshot.')
    drawings, visual_extent = _sheet_drawings(archive, root, target)
    rows = {int(row.attrib['r']): row for row in root.findall('s:sheetData/s:row', ns)}
    extent = max(max(rows, default=0), visual_extent)
    start, end = interval(scope, 'sheet_rows', extent)
    shared = []
    if 'xl/sharedStrings.xml' in archive.namelist():
        shared = [''.join(node.itertext()) for node in parse_untrusted_xml(archive.read('xl/sharedStrings.xml'))]
    merged = [node.attrib.get('ref') for node in root.findall('s:mergeCells/s:mergeCell', ns)]
    result = []
    for index in range(start, end + 1):
        cells = []
        for cell in rows.get(index, []):
            value_node, formula = (cell.find('s:v', ns), cell.find('s:f', ns))
            raw = value_node.text if value_node is not None else None
            if formula is not None and raw is None:
                raise SourceContentReadError('An original formula has no cached result; this reader does not execute formulas.')
            if cell.attrib.get('t') == 's':
                try:
                    shared_index = int(raw)
                    if not 0 <= shared_index < len(shared):
                        raise ValueError()
                    raw = shared[shared_index]
                except (IndexError, TypeError, ValueError) as exc:
                    raise SourceContentReadError("The cell's shared string is unavailable.") from exc
            if cell.attrib.get('t') == 'inlineStr':
                raw = ''.join(cell.find('s:is', ns).itertext())
            cells.append({'address': cell.attrib.get('r'), 'value': raw, 'formula': formula.text if formula is not None else None, 'type': cell.attrib.get('t', 'n')})
        result.append(text_part(json.dumps({'cells': cells, 'merged_ranges': merged}, ensure_ascii=False), {'kind': 'sheet_rows', 'container': sheet.attrib['name'], 'start': index, 'end': index}))
    full_sheet = start == 1 and end == extent
    requires_snapshot = False
    for container, anchor, first_row, last_row in drawings:
        if first_row is not None and last_row is not None and (last_row < start or first_row > end):
            continue
        if first_row is None or last_row is None:
            if not full_sheet:
                raise SourceContentReadError('The drawing has no reliable row boundary for this narrow selection.')
            first_row, last_row = (start, end)
        elif first_row < start or last_row > end:
            raise SourceContentReadError('The selected rows cut through an original drawing; select its complete row range.')
        locator = {'kind': 'sheet_rows', 'container': sheet.attrib['name'], 'start': first_row, 'end': last_row, 'object': container}
        has_chart = any((_local(node.tag) == 'chart' for node in anchor.iter()))
        if has_chart:
            requires_snapshot = True
            if not full_sheet or (len(sheets) != 1 and (not whole_workbook)):
                raise SourceContentReadError("The chart's workbook snapshot cannot be mapped inside this narrower worksheet range.")
        result.extend(_linked_parts(anchor, archive, container, artifact, locator, ''))
        if not any((_local(node.tag) in {'pic', 'chart'} for node in anchor.iter())):
            requires_snapshot = True
    if requires_snapshot:
        if not full_sheet or (len(sheets) != 1 and (not whole_workbook)):
            raise SourceContentReadError("The worksheet's visual snapshot exceeds this source range.")
        result.extend(_workbook_visual_parts(artifact, {'kind': 'sheet_rows', 'container': sheet.attrib['name'], 'start': start, 'end': end}, whole_workbook=len(sheets) > 1))
    return result

def _sheet_drawings(archive, root, worksheet):
    relationships = _relationships(archive, worksheet)
    result, extent = ([], 0)
    for node in root:
        if _local(node.tag) != 'drawing':
            continue
        relation = next((value for key, value in node.attrib.items() if 'relationships' in key), None)
        target, kind, external = relationships.get(relation, ('', '', True))
        if external or kind != 'drawing':
            raise SourceContentReadError('The worksheet drawing relationship is invalid.')
        drawing = parse_untrusted_xml(archive.read(target))
        for anchor in drawing:
            if _local(anchor.tag) not in {'twoCellAnchor', 'oneCellAnchor', 'absoluteAnchor'}:
                raise SourceContentReadError('The original drawing uses unsupported cell coordinates.')

            def row(edge):
                parent = next((child for child in anchor if _local(child.tag) == edge), None)
                child = next((child for child in parent if _local(child.tag) == 'row'), None) if parent is not None else None
                if child is None or child.text is None or (not child.text.isdigit()):
                    return None
                return int(child.text) + 1
            first, last = (row('from'), row('to'))
            if first is not None and last is not None and (first > last):
                raise SourceContentReadError('The original drawing has reversed cell anchors.')
            extent = max(extent, first or 0, last or 0)
            result.append((target, anchor, first, last))
    return (result, extent)

def _workbook_visual_parts(artifact, locator, *, whole_workbook=False):
    from pathlib import Path
    from .pdf_page_visual_input import PdfPageVisualReader
    result = []
    snapshots = artifact.get('_read_snapshots', [])
    visual_parts = [part for part in artifact.get('parts', []) if part.get('locator', {}).get('kind') == 'workbook_visual_pages']
    if not visual_parts:
        raise SourceContentReadError('The original workbook has no verified visual conversion snapshot.')
    for part in visual_parts:
        snapshots_for_part = [snapshot for snapshot in snapshots if snapshot.get('document_id') == part.get('document_id')]
        if len(snapshots_for_part) != 1 or not part.get('layout_verified') or (not part.get('processing_complete')):
            raise SourceContentReadError('The workbook visual snapshot is unavailable or has not completed Layout Parser.')
        snapshot = snapshots_for_part[0]
        digest = part.get('input', {}).get('sha256')
        if snapshot.get('sha256') != digest:
            raise SourceContentReadError('The workbook visual snapshot identity does not match its processed input.')
        with PdfPageVisualReader(Path(snapshot['path']), expected_sha256=digest) as reader:
            count = reader.page_count
        if count != part.get('page_count'):
            raise SourceContentReadError('The workbook visual snapshot page count does not match.')
        for page in range(1, count + 1):
            at = {**locator, 'object': 'workbook_visual', 'snapshot_page': page + int(part.get('page_offset', 0))}
            if whole_workbook:
                at = {'kind': 'workbook_visual', 'snapshot_page': at['snapshot_page']}

            def load(snapshot=snapshot, digest=digest, page=page, at=at, document_id=part.get('document_id')):
                with PdfPageVisualReader(Path(snapshot['path']), expected_sha256=digest) as reader:
                    image = reader.render_pages([page])[0]
                return {'kind': 'image', 'data_url': image.data_url, 'locator': at, 'provenance': {'input_method': 'verified_conversion_snapshot', 'conversion': 'libreoffice', 'snapshot_sha256': digest, 'layout_document_ids': [document_id], 'snapshot_page': page}}
            result.append({'kind': 'image', 'locator': at, 'provenance': {}, '_load': load})
    return result
