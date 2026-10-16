"""Bounded original-content readers. Caller owns hash and access authorization."""
from __future__ import annotations
import json
import base64
import re
from pathlib import Path
from openclass_core.services.source_xml import parse_untrusted_xml

class SourceContentReadError(ValueError):
    pass

def text_part(text, locator, **provenance):
    return {'kind': 'text', 'text': text, 'locator': locator, 'provenance': {'input_method': 'original_native_object', **provenance}}

def interval(scope, kind, count, *, base=1):
    if scope and scope.get('kind') != kind:
        raise SourceContentReadError(f'This original format requires {kind} coordinates.')
    start, end = ((scope or {}).get('start'), (scope or {}).get('end'))
    start = base if start is None else start
    end = count + base - 1 if end is None else end
    if any((type(x) is not int for x in (start, end))) or not base <= start <= end < count + base:
        raise SourceContentReadError('The source range is outside the original content.')
    return (start, end)

def decode(raw):
    for encoding in ('utf-8-sig', 'utf-16'):
        try:
            return raw.decode(encoding)
        except UnicodeError:
            pass
    raise SourceContentReadError('The original text encoding is not supported.')

def _text_units(path, scope):
    raw = decode(path.read_bytes())
    kind = (scope or {}).get('kind', 'text_lines')
    if kind == 'structured_path':
        steps = (scope or {}).get('path') or []
        if path.suffix.lower() == '.json':
            node = json.loads(raw)
            for step in steps:
                try:
                    node = node[int(step)] if isinstance(node, list) else node[str(step)]
                except (KeyError, ValueError, IndexError, TypeError) as exc:
                    raise SourceContentReadError('The original JSON object does not exist.') from exc
            text = json.dumps(node, ensure_ascii=False)
        elif path.suffix.lower() == '.xml':
            from xml.etree import ElementTree as ET
            node = parse_untrusted_xml(raw.encode())
            for position, step in enumerate(steps):
                match = re.fullmatch('(\\{[^}]+\\}[^/\\[\\]]+|[A-Za-z_][\\w.:-]*)(?:\\[([1-9][0-9]*)\\])?', str(step))
                if match is None:
                    raise SourceContentReadError('The original XML path segment is invalid.')
                name, occurrence = (match[1], int(match[2] or 1))

                def named(element):
                    return element.tag == name if name.startswith('{') else str(element.tag).rsplit('}', 1)[-1] == name
                if position == 0 and named(node):
                    if occurrence != 1:
                        raise SourceContentReadError('The original XML root has no requested sibling.')
                    continue
                matches = [child for child in node if named(child)]
                if not name.startswith('{') and len({child.tag for child in matches}) > 1:
                    raise SourceContentReadError('The original XML path is ambiguous across namespaces.')
                if occurrence > len(matches) or (not match[2] and len(matches) != 1):
                    raise SourceContentReadError('The XML object path is missing or ambiguous.')
                node = matches[occurrence - 1]
            import copy
            node = copy.deepcopy(node)
            node.tail = None
            text = ET.tostring(node, encoding='unicode')
        else:
            raise SourceContentReadError('This original has no supported structured path.')
        return [text_part(text, {'kind': kind, 'path': steps})]
    lines = raw.splitlines()
    start, end = interval(scope, 'text_lines', len(lines))
    return [text_part(lines[i - 1], {'kind': 'text_lines', 'start': i, 'end': i}) for i in range(start, end + 1)]
NATIVE_UNITS_PER_READ = 512

def read_original_units(*, source, original_path, artifact, source_range=None, cursor=0, max_chars=18000, max_units=NATIVE_UNITS_PER_READ, max_images=6, max_image_bytes=40 * 1024 * 1024):
    """Return faithful original objects or position-bound continuation fragments.

    Cursor is a unit offset in the immutable requested range. The outer service
    must bind it to source hash, version, range and current authorization.
    """
    if type(cursor) is not int or cursor < 0 or type(max_chars) is not int or (max_chars < 1) or (type(max_units) is not int) or (max_units < 1) or (type(max_images) is not int) or (max_images < 0) or (type(max_image_bytes) is not int) or (max_image_bytes < 1):
        raise SourceContentReadError('Invalid original-content reading budget or cursor.')
    path = Path(original_path)
    suffix = path.suffix.lower()
    scope = source_range.model_dump(mode='json') if hasattr(source_range, 'model_dump') else source_range
    from .source_content_office import read_office_units
    from .source_content_web import read_web_units, read_notebook_units
    from .source_content_media import read_image_units, read_transcript_units
    metadata = source.metadata
    cloud_scope = scope if scope and scope.get('kind') == 'cloud_location' else None
    if cloud_scope:
        scope = None
    missing = []
    if scope and scope.get('kind') == 'sheet_cells':
        from .source_content_cells import read_cells_units
        units = read_cells_units(source, path, artifact, scope)
    elif scope and scope.get('kind') == 'workbook_visual':
        from .source_content_office import _workbook_visual_parts
        units = _workbook_visual_parts(artifact, {}, whole_workbook=True)
    elif metadata.get('media_kind') in {'caption_transcript', 'audio_transcript'} or metadata.get('origin', {}).get('snapshot_kind') in {'caption_transcript', 'public_audio'} or source.mime_type.startswith('audio/'):
        units = read_transcript_units(source, path, artifact, scope)
    elif suffix in {'.docx', '.pptx', '.xlsx', '.xlsm', '.csv', '.tsv'}:
        units = read_office_units(source, path, artifact, scope)
    elif suffix in {'.doc', '.ppt', '.xls', '.odt', '.odp', '.ods', '.rtf'}:
        raise SourceContentReadError('This format requires its verified native conversion snapshot.')
    elif suffix == '.epub' or suffix in {'.html', '.htm'} or source.file_name.endswith('.webbundle.zip'):
        units = read_web_units(source, path, artifact, scope)
    elif suffix == '.ipynb':
        units = read_notebook_units(source, path, artifact, scope)
    elif source.mime_type.startswith('image/') or suffix in {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif', '.tif', '.tiff', '.heic', '.svg'}:
        units = read_image_units(source, path, artifact, scope)
    elif suffix == '.pdf' or source.mime_type.startswith('video/'):
        raise SourceContentReadError('This format requires a dedicated original-page or media reader.')
    else:
        from .google_sources.adapters import TEXT_EXTENSIONS
        if suffix not in TEXT_EXTENSIONS and (not source.mime_type.startswith('text/')):
            raise SourceContentReadError('This original format has no supported content reader.')
        units = _text_units(path, scope)
    if cloud_scope:
        units = _locate_original_units(units, artifact, cloud_scope)
        scope = cloud_scope
    if scope:
        for field in ('object', 'snapshot_page', 'segment'):
            if scope.get(field) is not None:
                units = [unit for unit in units if unit['locator'].get(field) == scope[field]]
                if not units:
                    raise SourceContentReadError('The requested original content object is missing from its native range.')
    from .source_native_text_fragments import decode_cursor, encode_cursor, text_fragment, requested_text_fragment
    units = requested_text_fragment(units, scope)
    start_index, text_offset, fragment_cursor = decode_cursor(cursor)
    if start_index > len(units) or (text_offset and (start_index == len(units) or units[start_index]['kind'] != 'text' or text_offset >= len(units[start_index].get('text', '')))):
        raise SourceContentReadError('The source reading cursor exceeds this range.')
    selected, chars, image_bytes, images, end = ([], 0, 0, 0, start_index)
    pending_fragment = None
    for unit in units[start_index:]:
        size = len(unit.get('text', ''))
        if unit['kind'] == 'text' and (text_offset or size > max_chars):
            if selected:
                break
            stop = min(size, text_offset + max_chars)
            selected.append(text_fragment(unit, text_offset, stop))
            chars += stop - text_offset
            if stop < size:
                pending_fragment = encode_cursor(end, stop)
            else:
                end += 1
            fragment_cursor = True
            break
        if len(selected) >= max_units or chars + size > max_chars:
            if not selected:
                raise SourceContentReadError('One complete source object exceeds the reading budget; select a smaller native range.')
            break
        if unit['kind'] == 'image' and images >= max_images:
            if not selected:
                raise SourceContentReadError('The original image reading budget is exhausted; this range still has unread images.')
            break
        if '_load' in unit:
            loaded = unit['_load']()
            loaded['provenance'].update(unit.get('provenance', {}))
            loaded['locator'] = dict(unit['locator'])
            unit = loaded
        if unit['kind'] == 'image':
            try:
                payload = unit['data_url'].partition(',')[2]
                image_size = len(base64.b64decode(payload, validate=True))
            except (KeyError, ValueError) as exc:
                raise SourceContentReadError('The original image payload is invalid.') from exc
            if image_size > max_image_bytes:
                raise SourceContentReadError("One complete original image exceeds this transport's byte budget; image quality cannot be reduced automatically.")
            if image_bytes + image_size > max_image_bytes:
                break
            image_bytes += image_size
            images += 1
        selected.append(unit)
        chars += size
        end += 1
    complete = pending_fragment is None and end == len(units) and bool(units)
    if not units:
        missing.append({'reason': 'No original objects in the requested range.'})
    next_cursor = pending_fragment if pending_fragment is not None else (encode_cursor(end, 0) if fragment_cursor else end) if end < len(units) else None
    return {'parts': selected, 'next_cursor': next_cursor, 'complete': complete, 'missing': missing, 'range': scope or {'kind': 'whole_source'}, 'unit_count': len(selected)}

def _matching_text(part):
    """Recover native object text for matching; never return the retrieval body."""
    if part['kind'] != 'text':
        return ''
    text = part['text']
    kind = part['locator'].get('kind')
    if kind in {'docx_paragraphs', 'ppt_slides'}:
        root = parse_untrusted_xml(text.encode())
        return ''.join((node.text or '' for node in root.iter() if str(node.tag).rsplit('}', 1)[-1] == 't'))
    if kind in {'dom_anchor', 'web_snapshot', 'epub_spine'}:

        def walk(node):
            if isinstance(node, str):
                return node
            return ''.join((walk(child) for child in node.get('children', [])))
        return walk(json.loads(text))
    if kind == 'sheet_rows':
        data = json.loads(text)
        return ''.join((str(value) for value in data)) if isinstance(data, list) else ''.join((str(cell.get('value') or '') for cell in data['cells']))
    if kind == 'notebook_cells':
        data = json.loads(text)
        return ''.join(data.get('source', [])) + ''.join((''.join(output.get('text', [])) + ''.join(output.get('data', {}).get('text/plain', [])) for output in data.get('outputs', [])))
    return text

def _locate_original_units(units, artifact, scope):
    parts = [part for part in artifact.get('parts', []) if part.get('document_id') == scope.get('document_id')]
    if len(parts) != 1 or not scope.get('chunk_id'):
        raise SourceContentReadError('The cloud locator has no unique verified processing document.')
    chunks = [chunk for chunk in parts[0].get('chunks', []) if str(chunk.get('id')) == str(scope['chunk_id'])]
    if len(chunks) != 1:
        raise SourceContentReadError('The cloud locator has no unique saved search chunk.')
    mapping = parts[0].get('locator', {})
    native_asset = mapping.get('asset_path') or mapping.get('original')
    image_matches = [unit for unit in units if unit['kind'] == 'image' and native_asset and (unit['locator'].get('object') == native_asset or unit.get('provenance', {}).get('original_object') == native_asset)]
    if len(image_matches) == 1:
        image_matches[0]['provenance']['location_method'] = 'original_asset_relationship'
        return image_matches
    if units and all((unit['locator'].get('kind') == 'media_time' for unit in units)):
        from .source_content_media import _seconds
        timestamps = re.findall('\\[([0-9:.]+s?)\\s*(?:–|-->)\\s*([0-9:.]+s?)\\]', str(chunks[0].get('content') or ''))
        if timestamps:
            selected = set()
            for raw_start, raw_end in timestamps:
                start, end = (_seconds(raw_start), _seconds(raw_end))
                matches = [index for index, unit in enumerate(units) if unit['locator']['start'] == start and unit['locator']['end'] == end]
                if len(matches) != 1:
                    raise SourceContentReadError('The search timestamp does not uniquely map to a complete original transcript segment.')
                selected.add(matches[0])
            result = [unit for index, unit in enumerate(units) if index in selected]
            for unit in result:
                unit['provenance']['location_method'] = 'original_transcript_timestamps'
            return result
    normalize = lambda value: re.sub('\\s+', '', value)
    needle = normalize(str(chunks[0].get('content') or ''))
    if not needle:
        raise SourceContentReadError('The cloud locator contains no usable native text anchor.')
    spans, whole = ([], '')
    for index, unit in enumerate(units):
        native = normalize(_matching_text(unit))
        if not native:
            continue
        spans.append((index, len(whole), len(whole) + len(native)))
        whole += native
    start = whole.find(needle)
    if start < 0 or whole.find(needle, start + 1) >= 0:
        raise SourceContentReadError('The search locator does not uniquely map to original native objects.')
    selected = [index for index, lo, hi in spans if lo < start + len(needle) and hi > start]
    if len(needle) < 16 and (not any((normalize(_matching_text(units[index])) == needle for index in selected))):
        raise SourceContentReadError('The search anchor is too short to reliably map to an original object.')
    indices = set(selected)
    for index in selected:
        for following in range(index + 1, len(units)):
            if units[following]['kind'] != 'image':
                break
            indices.add(following)
    result = [unit for index, unit in enumerate(units) if index in indices]
    for unit in result:
        unit['provenance']['location_method'] = 'exact_original_text_match'
        unit['provenance']['location_document_id'] = scope['document_id']
    return result
