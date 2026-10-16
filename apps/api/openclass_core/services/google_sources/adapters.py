from __future__ import annotations
import csv
import hashlib
import html
import io
import json
import mimetypes
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET
from urllib.parse import unquote, urlsplit
from .config import GoogleSourceError

@dataclass
class ProcessingPart:
    name: str
    mime_type: str
    content: bytes
    layout_required: bool = True
    page_count: int = 0
    page_offset: int = 0
    locator: dict = field(default_factory=dict)

@dataclass
class AdaptedSource:
    parts: list[ProcessingPart]
    format: str
    catalog: list[dict] = field(default_factory=list)
    tables: dict = field(default_factory=dict)
TEXT_EXTENSIONS = {'.txt', '.md', '.markdown', '.json', '.xml', '.py', '.js', '.ts', '.tsx', '.jsx', '.css', '.sql', '.yaml', '.yml', '.ipynb', '.rst', '.tex', '.log'}
AUDIO_EXTENSIONS = {'.mp3', '.wav', '.flac', '.ogg', '.m4a', '.aac', '.opus'}
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif', '.tif', '.tiff', '.heic'}
NATIVE_MIMES = {'.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation', '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.xlsm': 'application/vnd.ms-excel.sheet.macroEnabled.12'}

def image_pdf(content: bytes, *, name: str) -> ProcessingPart:
    if content.lstrip().startswith(b'<') and b'<svg' in content[:1000].lower():
        if any((not reference.startswith((b'#', b'data:')) for reference in re.findall(b'(?:href|src)\\s*=\\s*["\\\']([^"\\\']+)', content, re.I))) or b'<!ENTITY' in content.upper() or b'<script' in content.lower():
            raise GoogleSourceError('The SVG contains external resources or scripts and cannot be converted safely.')
        try:
            from openclass_core.services import pdf_backend as fitz
            with fitz.open(stream=content, filetype='svg') as document:
                pdf = document.convert_to_pdf()
            return ProcessingPart(name + '.pdf', 'application/pdf', pdf, page_count=1, locator={'kind': 'image_frames', 'original': name})
        except Exception as exc:
            raise GoogleSourceError('The SVG image could not be converted to Layout Parser input.') from exc
    from PIL import Image, ImageSequence, ImageOps
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    output = io.BytesIO()
    writer = canvas.Canvas(output)
    count = 0
    try:
        with Image.open(io.BytesIO(content)) as image:
            for frame in ImageSequence.Iterator(image):
                count += 1
                if count > 500:
                    raise GoogleSourceError('The image exceeds 500 frames. Split it before importing again.')
                rgb = ImageOps.exif_transpose(frame.copy()).convert('RGB')
                width, height = rgb.size
                writer.setPageSize((width, height))
                writer.drawImage(ImageReader(rgb), 0, 0, width, height)
                writer.showPage()
        writer.save()
    except GoogleSourceError:
        raise
    except Exception as exc:
        raise GoogleSourceError('The image format cannot be read losslessly. Convert it to PNG or PDF.') from exc
    return ProcessingPart(name + '.pdf', 'application/pdf', output.getvalue(), page_count=count, locator={'kind': 'image_frames', 'original': name})

def _safe_zip(content: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
        total = 0
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            total += entry.file_size
            if path.is_absolute() or '..' in path.parts or entry.flag_bits & 1 or (total > 512 * 1024 * 1024):
                raise GoogleSourceError('The source archive contains unsafe paths, encrypted entries, or exceeds extraction limits.')
        if len(archive.infolist()) > 10000:
            raise GoogleSourceError('The source archive exceeds the file count limit.')
        return archive
    except zipfile.BadZipFile as exc:
        raise GoogleSourceError('The source archive is corrupt.') from exc

def _xml(content: bytes):
    if b'<!DOCTYPE' in content.upper() or b'<!ENTITY' in content.upper():
        raise GoogleSourceError('The source contains unsupported XML entities.')
    return ET.fromstring(content)

def adapt_source(content: bytes, file_name: str, mime_type: str, pdf_batch_pages: int=400) -> AdaptedSource:
    if type(pdf_batch_pages) is not int or not 25 <= pdf_batch_pages <= 400:
        raise GoogleSourceError('PDF cloud batches must contain between 25 and 400 pages.')
    extension = PurePosixPath(file_name).suffix.lower()
    if file_name.endswith('.webbundle.zip'):
        with _safe_zip(content) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            parts = [ProcessingPart('content.html', 'text/html', archive.read('content.html'), locator={'kind': 'web_snapshot', 'container': manifest['url']})]
            for asset in manifest['assets']:
                raw = archive.read(asset['name'])
                if hashlib.sha256(raw).hexdigest() != asset['sha256']:
                    raise GoogleSourceError('A webpage snapshot resource hash does not match.')
                if asset['kind'] == 'image':
                    part = image_pdf(raw, name=asset['name'])
                    part.locator = {'kind': 'web_image', 'container': asset['url']}
                    parts.append(part)
            return AdaptedSource(parts, 'web_snapshot')
    if extension in {'.doc', '.ppt', '.xls', '.odt', '.odp', '.ods', '.rtf'}:
        from .office import convert_office
        target = 'xlsx' if extension in {'.xls', '.ods'} else 'pptx' if extension in {'.ppt', '.odp'} else 'docx'
        converted = convert_office(content, file_name, target=target)
        adapted = adapt_source(converted, 'converted.' + target, NATIVE_MIMES['.' + target])
        for part in adapted.parts:
            part.locator = {**part.locator, 'converted_from': extension[1:], 'conversion': 'libreoffice', 'original_sha256': hashlib.sha256(content).hexdigest()}
        return adapted
    if extension in AUDIO_EXTENSIONS or mime_type.startswith('audio/'):
        return AdaptedSource([], 'audio')
    if extension == '.pdf' or mime_type == 'application/pdf':
        from pypdf import PdfReader, PdfWriter
        try:
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted:
                raise GoogleSourceError('Decrypt the PDF before uploading it again.')
            count = len(reader.pages)
            parts = []
            for start in range(0, count, pdf_batch_pages):
                end = min(start + pdf_batch_pages, count)
                if start == 0 and end == count:
                    data = content
                else:
                    writer = PdfWriter()
                    for page in reader.pages[start:end]:
                        writer.add_page(page)
                    out = io.BytesIO()
                    writer.write(out)
                    data = out.getvalue()
                parts.append(ProcessingPart(f'pages-{start + 1}-{end}.pdf', 'application/pdf', data, page_count=end - start, page_offset=start, locator={'kind': 'pdf_pages', 'start': start + 1, 'end': end}))
            catalog = []

            def visit(outline, level=1):
                for item in outline:
                    if isinstance(item, list):
                        visit(item, level + 1)
                    else:
                        page = reader.get_destination_page_number(item) + 1
                        if page > 0:
                            catalog.append({'title': str(item.title), 'level': level, 'page_start': page, 'origin': 'native_outline'})
            visit(reader.outline)
            return AdaptedSource(parts, 'pdf', catalog)
        except GoogleSourceError:
            raise
        except Exception as exc:
            raise GoogleSourceError('The PDF structure could not be read.') from exc
    if extension in IMAGE_EXTENSIONS or mime_type.startswith('image/'):
        return AdaptedSource([image_pdf(content, name=file_name)], 'image')
    if extension in NATIVE_MIMES:
        with _safe_zip(content) as archive:
            for name in archive.namelist():
                if name.endswith('.rels'):
                    for relation in _xml(archive.read(name)):
                        if relation.attrib.get('TargetMode') == 'External' and any((kind in relation.attrib.get('Type', '') for kind in ('/image', '/oleObject'))):
                            raise GoogleSourceError('The document contains external images or objects. Embed them before uploading.')
            parts = [ProcessingPart(file_name, NATIVE_MIMES[extension], content)]
            if extension in {'.xlsx', '.xlsm'}:
                for name in archive.namelist():
                    if name.startswith('xl/media/') and (not name.endswith('/')):
                        parts.append(image_pdf(archive.read(name), name=name))
            if extension in {'.xlsx', '.xlsm'} and any((name.startswith('xl/charts/') and name.endswith('.xml') for name in archive.namelist())):
                from .office import convert_office
                visual_pdf = convert_office(content, file_name)
                rendered = adapt_source(visual_pdf, 'workbook-visuals.pdf', 'application/pdf')
                for part in rendered.parts:
                    part.locator = {'kind': 'workbook_visual_pages', 'original': file_name}
                    parts.append(part)
            tables = _xlsx_tables(archive) if extension in {'.xlsx', '.xlsm'} else {}
        return AdaptedSource(parts, extension[1:], tables=tables)
    if extension == '.epub':
        return _epub(content)
    if extension in {'.csv', '.tsv'}:
        text = _text(content)
        rows = list(csv.reader(io.StringIO(text), delimiter='\t' if extension == '.tsv' else ','))
        if len(rows) > 100000 or any((len(row) > 1000 for row in rows)):
            raise GoogleSourceError('The spreadsheet exceeds the current row or column limit.')
        return AdaptedSource([ProcessingPart(file_name + '.txt', 'text/plain', text.encode(), False)], 'table', tables={'Sheet1': rows})
    if extension in {'.html', '.htm'} or mime_type == 'text/html':
        text = _text(content)
        if re.search('<(img|svg|canvas|iframe|object|video)\\b|url\\s*\\(', text, re.I):
            raise GoogleSourceError('This webpage contains visual resources. Upload a complete PDF snapshot so all images can pass through Layout Parser.')
        return AdaptedSource([ProcessingPart(file_name, 'text/html', content)], 'html')
    if extension in TEXT_EXTENSIONS or mime_type.startswith('text/'):
        text = _text(content)
        if extension in {'.md', '.markdown'} and re.search('!\\[[^\\]]*\\]\\(|<img\\b', text, re.I):
            raise GoogleSourceError('The Markdown references images. Upload a PDF containing the images or upload the original images separately.')
        parts = []
        if extension == '.ipynb':
            notebook = json.loads(text)
            cells = []
            for index, cell in enumerate(notebook.get('cells', [])):
                cell_source = ''.join(cell.get('source', []))
                cells.append(f"Cell {index + 1} ({cell.get('cell_type', '')})\n" + cell_source)
                attachments = cell.get('attachments', {})
                if cell.get('cell_type') == 'markdown':
                    image_links = re.findall('!\\[[^\\]]*\\]\\(([^)]+)\\)', cell_source)
                    if re.search('<img\\b|<svg\\b', cell_source, re.I) or any((not link.startswith('attachment:') or link.removeprefix('attachment:') not in attachments for link in image_links)):
                        raise GoogleSourceError('The notebook references external or missing images. Embed the attachments or upload a complete PDF.')
                for output in [*cell.get('outputs', []), *({'data': data} for data in attachments.values())]:
                    data = output.get('data', {})
                    cells.extend([''.join(output.get('text', [])), ''.join(data.get('text/plain', []))])
                    for key, encoded in data.items():
                        if key.startswith('image/'):
                            import base64
                            value = ''.join(encoded) if isinstance(encoded, list) else encoded
                            raw = value.encode('utf-8') if key == 'image/svg+xml' else base64.b64decode(value, validate=True)
                            part = image_pdf(raw, name=f'cell-{index + 1}-{len(parts)}')
                            part.locator = {'kind': 'notebook_image', 'container': str(index + 1)}
                            parts.append(part)
            text = '\n\n'.join(cells)
        parts.insert(0, ProcessingPart(file_name + '.txt', 'text/plain', text.encode(), False))
        return AdaptedSource(parts, 'text')
    raise GoogleSourceError('This format is not supported by Google processing yet. Convert it to PDF, DOCX, PPTX, XLSX, EPUB, text, or a supported image format.')

def _text(content: bytes) -> str:
    for encoding in ('utf-8-sig', 'utf-16'):
        try:
            return content.decode(encoding)
        except UnicodeError:
            pass
    raise GoogleSourceError('The text encoding could not be reliably identified. Save the file as UTF-8.')

def _xlsx_tables(archive: zipfile.ZipFile) -> dict:
    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    strings = []
    if 'xl/sharedStrings.xml' in archive.namelist():
        for item in _xml(archive.read('xl/sharedStrings.xml')):
            strings.append(''.join(item.itertext()))
    relationships = {r.attrib['Id']: r.attrib['Target'] for r in _xml(archive.read('xl/_rels/workbook.xml.rels'))}
    tables = {}
    for sheet in _xml(archive.read('xl/workbook.xml')).findall('s:sheets/s:sheet', ns):
        relationship = sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
        target = relationships[relationship]
        path = target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join('xl', target))
        rows = []
        for row in _xml(archive.read(path)).findall('s:sheetData/s:row', ns):
            row_index = int(row.attrib.get('r', len(rows) + 1))
            if row_index > 100000:
                raise GoogleSourceError('The spreadsheet exceeds the current limit of 100,000 rows.')
            while len(rows) < row_index:
                rows.append([])
            values = rows[row_index - 1]
            for cell in row:
                address = cell.attrib.get('r', '')
                column = 0
                for char in re.match('[A-Z]+', address).group():
                    column = column * 26 + ord(char) - 64
                if column > 1000:
                    raise GoogleSourceError('The spreadsheet exceeds the current limit of 1,000 columns.')
                while len(values) < column:
                    values.append(None)
                value = cell.find('s:v', ns)
                raw = value.text if value is not None else None
                if cell.attrib.get('t') == 's' and raw is not None:
                    raw = strings[int(raw)]
                elif cell.attrib.get('t') == 'inlineStr':
                    raw = ''.join(cell.find('s:is', ns).itertext())
                elif raw is not None and cell.attrib.get('t') not in {'str', 'e'}:
                    try:
                        raw = float(raw)
                    except ValueError:
                        pass
                if cell.find('s:f', ns) is not None and value is None:
                    raw = {'error': 'formula_result_unavailable'}
                values[column - 1] = raw
        tables[sheet.attrib['name']] = rows
    return tables

def _epub_resource_path(base: str, href: str) -> str:
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc or parsed.path.startswith('/'):
        raise GoogleSourceError('The EPUB contains external or absolute resource URLs. Embed all resources before uploading.')
    path = posixpath.normpath(posixpath.join(base, unquote(parsed.path)))
    if path.startswith('../') or path == '..':
        raise GoogleSourceError('An EPUB resource path is outside the archive.')
    return path

def _epub(content: bytes) -> AdaptedSource:
    with _safe_zip(content) as archive:
        container = _xml(archive.read('META-INF/container.xml'))
        package_path = next((item.attrib['full-path'] for item in container.iter() if item.tag.endswith('rootfile')))
        root = _xml(archive.read(package_path))
        base = posixpath.dirname(package_path)
        manifest = {item.attrib['id']: item.attrib for item in root.iter() if item.tag.endswith('}item')}
        spine = [item.attrib['idref'] for item in root.iter() if item.tag.endswith('}itemref')]
        images = {_epub_resource_path(base, entry['href']): entry for entry in manifest.values() if entry.get('media-type', '').startswith('image/')}
        image_chapters: dict[str, list[dict]] = {path: [] for path in images}
        parts, catalog = ([], [])
        for index, item_id in enumerate(spine):
            entry = manifest[item_id]
            path = _epub_resource_path(base, entry['href'])
            text = _text(archive.read(path))
            title = re.search('<title[^>]*>(.*?)</title>', text, re.S | re.I)
            label = html.unescape(re.sub('<[^>]+>', '', title.group(1))) if title else PurePosixPath(path).stem
            locator = {'kind': 'epub_spine', 'container': path, 'start': index + 1, 'end': index + 1}
            chapter = {'container': path, 'spine_index': index + 1}
            from .web_snapshot import SnapshotHTML
            sanitizer = SnapshotHTML()
            sanitizer.feed(text)
            chapter_part_index = len(parts)
            parts.append(ProcessingPart(f'chapter-{index + 1}.html', 'text/html', ''.join(sanitizer.parts).encode(), locator=locator))
            pending_images = [(link, posixpath.dirname(path)) for link in sanitizer.images]
            styles = [_epub_resource_path(posixpath.dirname(path), link) for link in sanitizer.styles]
            css_parts = [(css, posixpath.dirname(path)) for css in sanitizer.inline_styles]
            seen_styles = set()
            while styles:
                stylesheet = styles.pop()
                if stylesheet in seen_styles:
                    continue
                seen_styles.add(stylesheet)
                if len(seen_styles) > 100:
                    raise GoogleSourceError('The EPUB chapter exceeds the stylesheet resource limit.')
                css = _text(archive.read(stylesheet))
                css_base = posixpath.dirname(stylesheet)
                css_parts.append((css, css_base))
                for imported in re.findall('@import\\s+(?:url\\()?\\s*[\\"\']([^\\"\']+)', css, re.I):
                    styles.append(_epub_resource_path(css_base, imported))
            for css, css_base in css_parts:
                pending_images.extend(((link, css_base) for link in re.findall('url\\(\\s*[\\"\']?([^\\)\\"\']+)', css, re.I) if not link.startswith('#')))
            for image_index, (link, relative_base) in enumerate(pending_images):
                if link.startswith('data:image/'):
                    import base64
                    header, encoded = link.split(',', 1)
                    if not header.endswith(';base64'):
                        raise GoogleSourceError('Embedded EPUB images must use verifiable base64 encoding.')
                    try:
                        image_content = base64.b64decode(encoded, validate=True)
                    except ValueError as exc:
                        raise GoogleSourceError('An embedded EPUB image has invalid encoding.') from exc
                    part = image_pdf(image_content, name=f'chapter-{index + 1}-image-{image_index}')
                    part.locator = {'kind': 'epub_image', **chapter, 'chapters': [chapter]}
                    parts.append(part)
                    continue
                asset_path = _epub_resource_path(relative_base, link)
                if asset_path in images:
                    if chapter not in image_chapters[asset_path]:
                        image_chapters[asset_path].append(chapter)
                elif asset_path not in archive.namelist():
                    raise GoogleSourceError('An image or stylesheet referenced by an EPUB chapter is missing.')
                elif not asset_path.lower().endswith(('.css', '.woff', '.woff2', '.ttf', '.otf')):
                    raise GoogleSourceError('An EPUB chapter image is missing from the manifest, so complete processing cannot be verified.')
            for svg_index, svg in enumerate(sanitizer.inline_svgs):
                part = image_pdf(svg, name=f'chapter-{index + 1}-svg-{svg_index}')
                part.locator = {'kind': 'epub_image', **chapter, 'chapters': [chapter]}
                parts.append(part)
            catalog.append({'title': label, 'level': 1, 'part_index': chapter_part_index, 'origin': 'epub_spine', 'range': locator})
        for path in images:
            part = image_pdf(archive.read(path), name=path)
            chapters = image_chapters[path]
            part.locator = {'kind': 'epub_image', 'asset_path': path, 'chapters': chapters, **(chapters[0] if len(chapters) == 1 else {})}
            parts.append(part)
        if not parts:
            raise GoogleSourceError('The EPUB has no readable chapters.')
        return AdaptedSource(parts, 'epub', catalog)
