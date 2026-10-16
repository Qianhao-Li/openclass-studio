"""Format adapters for faithful, positioned text; never OCR or execute input."""
from __future__ import annotations
import hashlib
import csv
import io
import json
import mailbox
from email.header import decode_header, make_header
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from openclass_core.services.source_archive import SafeSourceArchive
from openclass_core.services.source_xml import parse_untrusted_xml
SCHEMA = 'source_basic_content_v1'
MEDIA = {'.mp3', '.wav', '.mp4', '.mpeg', '.ogg', '.oga', '.opus', '.m4a', '.webm'}
IMAGES = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.tiff', '.tif', '.bmp', '.svg', '.heic'}

class BasicContentError(ValueError):
    pass

class _HTMLText(HTMLParser):

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = ([], 0)
        self.anchors = {}

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden += 1
        elif not self.hidden and tag in {'p', 'div', 'br', 'li', 'tr', 'section', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            self.parts.append('\n')
        if not self.hidden:
            for key, value in attrs:
                if key in {'id', 'name'} and value:
                    self.anchors[value] = None if value in self.anchors else sum(map(len, self.parts))

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

def html_text(value):
    parser = _HTMLText()
    parser.feed(value)
    return ''.join(parser.parts)

def decode_text(raw):
    for encoding in ('utf-8-sig', 'utf-16'):
        try:
            return raw.decode(encoding)
        except UnicodeError:
            pass
    raise BasicContentError('This file has no supported native text encoding.')

def _local(tag):
    return str(tag).rsplit('}', 1)[-1]

def _office(path):
    from openclass_core.services.source_content_catalog import _office as native_catalog
    from openclass_core.services.source_content_office import _relationships
    from openclass_core.services.source_ooxml_navigation import ordered_pptx_slide_parts
    units = []
    suffix = path.suffix.lower()
    with SafeSourceArchive(path) as archive:
        if suffix == '.docx':
            root = parse_untrusted_xml(archive.read('word/document.xml'))
            body = next((n for n in root if _local(n.tag) == 'body'), None)
            if body is None:
                raise BasicContentError('The document has no readable body.')
            for index, node in enumerate((n for n in body.iter() if _local(n.tag) == 'p'), 1):
                units.append((''.join((n.text or '' for n in node.iter() if _local(n.tag) == 't')), {'kind': 'docx_paragraphs', 'start': index, 'end': index}))
        elif suffix == '.pptx':
            for index, name in enumerate(ordered_pptx_slide_parts(archive), 1):
                root = parse_untrusted_xml(archive.read(name))
                units.append(('\n'.join((n.text or '' for n in root.iter() if _local(n.tag) == 't')), {'kind': 'ppt_slides', 'start': index, 'end': index}))
        else:
            shared = []
            if 'xl/sharedStrings.xml' in archive.namelist():
                shared = [''.join(n.itertext()) for n in parse_untrusted_xml(archive.read('xl/sharedStrings.xml'))]
            root = parse_untrusted_xml(archive.read('xl/workbook.xml'))
            rels = _relationships(archive, 'xl/workbook.xml')
            for sheet in (n for n in root.iter() if _local(n.tag) == 'sheet'):
                relation = next((v for k, v in sheet.attrib.items() if 'relationships' in k), '')
                name, kind, external = rels.get(relation, ('', '', True))
                if external or kind != 'worksheet':
                    raise BasicContentError('The worksheet relationship is invalid.')
                for row in (n for n in parse_untrusted_xml(archive.read(name)).iter() if _local(n.tag) == 'row'):
                    cells = {}
                    for cell in (n for n in row if _local(n.tag) == 'c'):
                        value = next((n.text or '' for n in cell if _local(n.tag) == 'v'), '')
                        if cell.attrib.get('t') == 's':
                            value = shared[int(value)] if value else ''
                        elif cell.attrib.get('t') == 'inlineStr':
                            value = ''.join((n.text or '' for n in cell.iter() if _local(n.tag) == 't'))
                        cells[cell.attrib.get('r', '')] = value
                    position = int(row.attrib['r'])
                    units.append((json.dumps(cells, ensure_ascii=False), {'kind': 'sheet_rows', 'container': sheet.attrib['name'], 'start': position, 'end': position}))
    entries, authored = native_catalog(path)
    return (units, entries, authored)

def extract_basic_content(path, source, *, is_cancelled=lambda: False):
    """Keep coordinates and text fragments, including an explicit visual-only carrier."""
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != source.metadata.get('content_hash'):
        raise BasicContentError('The original file hash changed.')
    suffix = Path(source.file_name).suffix.lower()
    units, entries, authored = ([], [], False)
    conversion = None
    if suffix in MEDIA or source.mime_type.startswith(('audio/', 'video/')):
        raise BasicContentError('Audio and video retain their existing source processing path.')
    if suffix in {'.odt', '.odp'}:
        from openclass_core.services.source_basic_odf import extract_odf
        units, entries, authored = extract_odf(path, suffix)
    elif suffix in {'.doc', '.ppt', '.xls', '.ods', '.rtf'}:
        from openclass_core.services.google_sources.office import convert_office
        target = 'pptx' if suffix in {'.odp', '.ppt'} else 'xlsx' if suffix in {'.xls', '.ods'} else 'docx'
        converted = convert_office(raw, source.file_name, target=target)
        conversion = {'original_sha256': digest, 'converted_sha256': hashlib.sha256(converted).hexdigest(), 'adapter': 'isolated_libreoffice_v1', 'target': target, 'position_mapping': 'converted_native_units'}
        with tempfile.TemporaryDirectory() as directory:
            converted_path = Path(directory) / ('converted.' + target)
            converted_path.write_bytes(converted)
            units, entries, authored = _office(converted_path)
    elif suffix == '.pdf':
        from openclass_core.services.source_basic_pdf import extract_pdf_units
        units, entries, authored = extract_pdf_units(path, is_cancelled)
    elif suffix in {'.docx', '.pptx', '.xlsx', '.xlsm'}:
        units, entries, authored = _office(path)
    elif suffix == '.epub':
        from openclass_core.services.source_range_reader import _epub_spine_items
        from openclass_core.services.source_directory_extractor import _epub_nav_entries
        with SafeSourceArchive(path) as archive:
            spine = _epub_spine_items(archive)
            anchors = {}
            for index, name in enumerate(spine, 1):
                parsed = _HTMLText()
                parsed.feed(decode_text(archive.read(name)))
                anchors[name] = parsed.anchors
                units.append((''.join(parsed.parts), {'kind': 'epub_spine', 'start': index, 'end': index, 'container': name}))
                entries.append({'title': name, 'level': 1, 'range': units[-1][1]})
            navigation = _epub_nav_entries(archive, archive.namelist())
            if navigation:
                entries = []
                for target, fragment, title, level in navigation:
                    if target not in spine or (fragment and anchors[target].get(fragment) is None):
                        raise BasicContentError('An authored EPUB navigation destination cannot be resolved uniquely.')
                    index = spine.index(target) + 1
                    entries.append({'title': title, 'level': level, 'range': {'kind': 'epub_spine', 'start': index, 'end': index, 'container': target, 'metadata': {'native_char_offset': anchors[target][fragment] if fragment else 0, 'native_anchor': fragment}}})
                authored = True
    elif suffix in IMAGES or source.mime_type.startswith('image/'):
        from openclass_core.services.source_content_media import read_image_units
        frames = read_image_units(source, path, {'_original_read_exception': 'ordinary_source'}, None)
        for frame in frames:
            if is_cancelled():
                raise BasicContentError('Source parsing was cancelled.')
            frame['_load']()
        return {'schema': SCHEMA, 'source_id': source.id, 'source_version': source.metadata['source_version'], 'content_hash': digest, 'format': 'visual', 'units': [], 'native_entries': [], 'authored': False, 'frame_count': len(frames)}
    elif suffix == '.mbox':
        box = mailbox.mbox(path, create=False)
        try:
            for index, message in enumerate(box, 1):
                bodies = []
                for part in message.walk():
                    if part.get_content_type() not in {'text/plain', 'text/html'}:
                        continue
                    content = (part.get_payload(decode=True) or b'').decode(part.get_content_charset() or 'utf-8', errors='replace')
                    bodies.append(html_text(content) if part.get_content_type() == 'text/html' else content)
                headers = '\n'.join((f'{name}: {make_header(decode_header(value))}' for name, value in message.items()))
                units.append((headers + '\n\n' + '\n'.join(bodies), {'kind': 'text_lines', 'start': index, 'end': index, 'metadata': {'position_kind': 'mail_message', 'message_index': index}}))
                entries.append({'title': str(message.get('Subject', '')) or f'Message {index}', 'level': 1, 'range': units[-1][1]})
        finally:
            box.close()
    elif suffix in {'.csv', '.tsv'}:
        rows = list(csv.reader(io.StringIO(decode_text(raw)), delimiter='\t' if suffix == '.tsv' else ','))
        units = [(json.dumps(row, ensure_ascii=False), {'kind': 'sheet_rows', 'start': index, 'end': index, 'container': 'Sheet1'}) for index, row in enumerate(rows, 1)]
        if units:
            entries = [{'title': 'Sheet1', 'level': 1, 'range': {'kind': 'sheet_rows', 'start': 1, 'end': len(units), 'container': 'Sheet1'}}]
    else:
        text = decode_text(raw)
        if '\x00' in text:
            raise BasicContentError('This binary file cannot be parsed as text.')
        if suffix in {'.html', '.htm'}:
            text = html_text(text)
        units = [(line, {'kind': 'text_lines', 'start': index, 'end': index}) for index, line in enumerate(text.splitlines(), 1)]
    positioned = []
    for text, locator in units:
        if is_cancelled():
            raise BasicContentError('Source parsing was cancelled.')
        start = 0
        while start < len(text) or (not text and start == 0):
            end = min(start + 1500, len(text))
            while end > start and len(text[start:end].encode()) > 2500:
                end -= 1
            positioned.append({'id': len(positioned) + 1, 'text': text[start:end], 'locator': locator, 'text_start': start, 'text_end': end})
            if not text:
                break
            start = end
    return {'schema': SCHEMA, 'source_id': source.id, 'source_version': source.metadata['source_version'], 'content_hash': digest, 'format': 'pdf' if suffix == '.pdf' else 'native', 'native_format': suffix.lstrip('.'), 'units': positioned, 'native_entries': entries, 'authored': authored, 'visual_only': not any((unit['text'].strip() and (suffix != '.pdf' or unit['locator'].get('metadata', {}).get('native_visibility_verified')) for unit in positioned)), 'visual_pages': sorted({unit['locator']['start'] for unit in positioned if suffix == '.pdf' and unit['locator'].get('metadata', {}).get('requires_original_image')}), **({'conversion': conversion} if conversion else {})}
