from __future__ import annotations
import base64
import csv
import http.client
import ipaddress
import math
import mimetypes
import posixpath
import re
import socket
import ssl
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import ParseResult, unquote, unquote_to_bytes, urljoin, urlparse
from xml.etree import ElementTree
from openclass_core.models import SourceIngestionRecord
from openclass_core.services.source_archive import SafeSourceArchive, SourceArchiveError
from openclass_core.services.source_markup_text import CanonicalMarkupText
from openclass_core.services.source_visual_extraction_budget import SourceVisualExtractionBudget, SourceVisualExtractionBudgetError
from openclass_core.services.source_visual_extraction_types import RawSourceVisual, SourceVisualAdapterResult
from openclass_core.services.source_visual_storage import MAX_SOURCE_VISUAL_BYTES
from openclass_core.services.source_xml import SourceXmlError, parse_untrusted_xml
_ALLOWED_IMAGE_MIMES = {'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/svg+xml', 'image/tiff', 'image/bmp'}
_MAX_SVG_EDGE = 4096
_MAX_SVG_PIXELS = 16000000
_MAX_SVG_NODES = 50000
_UNSAFE_SVG_ELEMENTS = {'script', 'foreignobject', 'iframe', 'object', 'embed'}

@dataclass
class _ImageReference:
    source: str
    caption: str
    text_offset: int
    document_order: int
    paragraph_index: int | None = None

@dataclass
class _TableReference:
    rows: list[list[str]]
    text_offset: int
    document_order: int
    has_merged_cells: bool = False

@dataclass
class _SvgReference:
    text_offset: int
    document_order: int
    raw_offset: int

class _MarkupVisualParser(HTMLParser):

    def __init__(self, source_text: str='', *, budget: SourceVisualExtractionBudget | None=None) -> None:
        super().__init__()
        self.images: list[_ImageReference] = []
        self.tables: list[_TableReference] = []
        self.svgs: list[_SvgReference] = []
        self._canonical_text = CanonicalMarkupText()
        self._document_order = 0
        self._paragraph_index = 0
        self._svg_depth = 0
        self._table_depth = 0
        self._table_rows: list[list[str]] | None = None
        self._table_offset = 0
        self._table_document_order = 0
        self._table_has_merged_cells = False
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None
        self._line_offsets = [0, *(match.end() for match in re.finditer('\\n', source_text))]
        self._budget = budget

    @property
    def text_length(self) -> int:
        return self._canonical_text.offset

    @property
    def canonical_text(self) -> str:
        return self._canonical_text.text

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered == 'svg':
            if self._svg_depth == 0:
                if self._budget is not None:
                    self._budget.reserve_visual_objects()
                text_offset = self._canonical_text.append_visual_anchor()
                self.svgs.append(_SvgReference(text_offset=text_offset, document_order=self._next_document_order(), raw_offset=self._current_raw_offset()))
            self._svg_depth += 1
            return
        if self._svg_depth:
            return
        values = {key.lower(): value or '' for key, value in attrs}
        if lowered in {'p', 'div', 'section', 'article', 'li'}:
            self._paragraph_index += 1
        if lowered == 'img' and values.get('src'):
            if self._budget is not None:
                self._budget.reserve_visual_objects()
            text_offset = self._canonical_text.append_visual_anchor()
            self.images.append(_ImageReference(source=values['src'], caption=(values.get('alt') or values.get('title') or '').strip(), text_offset=text_offset, document_order=self._next_document_order(), paragraph_index=self._paragraph_index))
        elif lowered == 'table':
            if self._table_depth == 0:
                if self._budget is not None:
                    self._budget.reserve_visual_objects()
                self._table_rows = []
                self._table_offset = self._canonical_text.append_visual_anchor()
                self._table_document_order = self._next_document_order()
                self._table_has_merged_cells = False
            self._table_depth += 1
        elif lowered == 'tr' and self._table_rows is not None:
            self._row = []
        elif lowered in {'td', 'th'} and self._row is not None:
            if _markup_cell_has_merged_span(values):
                self._table_has_merged_cells = True
            self._cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == 'svg' and self._svg_depth:
            self._svg_depth -= 1
            return
        if self._svg_depth:
            return
        if lowered in {'td', 'th'} and self._cell_parts is not None and (self._row is not None):
            cell_text = ' '.join(''.join(self._cell_parts).split())
            if self._budget is not None:
                self._budget.account_table([[cell_text]])
            self._row.append(cell_text)
            self._cell_parts = None
        elif lowered == 'tr' and self._row is not None and (self._table_rows is not None):
            if any(self._row):
                self._table_rows.append(self._row)
            self._row = None
        elif lowered == 'table' and self._table_depth:
            self._table_depth -= 1
            if self._table_depth == 0 and self._table_rows:
                self.tables.append(_TableReference(rows=self._table_rows, text_offset=self._table_offset, document_order=self._table_document_order, has_merged_cells=self._table_has_merged_cells))
            if self._table_depth == 0:
                self._table_rows = None
                self._table_has_merged_cells = False

    def handle_data(self, data: str) -> None:
        normalized = self._canonical_text.append_text(data)
        if not normalized:
            return
        if self._cell_parts is not None:
            self._cell_parts.append(normalized)

    def _next_document_order(self) -> int:
        current = self._document_order
        self._document_order += 1
        return current

    def _current_raw_offset(self) -> int:
        line, column = self.getpos()
        line_index = max(0, min(line - 1, len(self._line_offsets) - 1))
        return self._line_offsets[line_index] + column

def _markup_cell_has_merged_span(attributes: dict[str, str]) -> bool:
    for name in ('colspan', 'rowspan'):
        if name not in attributes:
            continue
        try:
            if int(attributes[name].strip()) != 1:
                return True
        except (TypeError, ValueError):
            return True
    return False

def _unrepresented_markup_table_merge_metadata(table: _TableReference) -> dict[str, object]:
    if not table.has_merged_cells:
        return {}
    return {'force_unverified': True, 'table_merge_semantics': 'unrepresented', 'table_merge_markers': ['colspan_or_rowspan']}

def _merged_markup_table_warnings(issue_count: int, *, source_format: str) -> list[str]:
    if issue_count <= 0:
        return []
    return [f'{issue_count} {source_format} table(s) contain merged cells whose span semantics cannot be represented by the current editable table matrix; they were kept as unverified visual records.']

def extract_markup_visuals(path: Path, record: SourceIngestionRecord) -> SourceVisualAdapterResult:
    suffix = path.suffix.lower()
    if suffix == '.epub' or record.mime_type == 'application/epub+zip':
        return _extract_epub(path)
    if suffix in {'.html', '.htm'} or record.mime_type == 'text/html':
        return _extract_html(path, record)
    if suffix in {'.md', '.markdown'} or record.mime_type in {'text/markdown', 'text/x-markdown'}:
        return _extract_markdown(path, record)
    if suffix == '.csv' or record.mime_type == 'text/csv':
        return _extract_csv(path)
    return SourceVisualAdapterResult(status='ready')

def extract_standalone_image(path: Path, record: SourceIngestionRecord) -> SourceVisualAdapterResult:
    budget = SourceVisualExtractionBudget()
    try:
        budget.reserve_visual_objects()
    except SourceVisualExtractionBudgetError as exc:
        return SourceVisualAdapterResult(status='failed', warnings=[str(exc)])
    try:
        if path.stat().st_size > MAX_SOURCE_VISUAL_BYTES:
            return SourceVisualAdapterResult(status='partial', warnings=['Image media type or size is unsupported.'])
        content = path.read_bytes()
    except OSError as exc:
        return SourceVisualAdapterResult(status='failed', warnings=[f'Image visual could not be read: {exc}'])
    try:
        budget.account_image_bytes(len(content))
    except SourceVisualExtractionBudgetError as exc:
        return SourceVisualAdapterResult(status='failed', warnings=[str(exc)])
    mime_type = _image_mime(path.name, content, declared=record.mime_type)
    if mime_type not in _ALLOWED_IMAGE_MIMES or len(content) > MAX_SOURCE_VISUAL_BYTES:
        return SourceVisualAdapterResult(status='partial', warnings=['Image media type or size is unsupported.'])
    return SourceVisualAdapterResult(visuals=[RawSourceVisual(kind='image', source_locator='image:whole', native_order=0, content=content, mime_type=mime_type, bbox=[0.0, 0.0, 1.0, 1.0], text_offset=0, caption=record.title, confidence=1.0, metadata={'standalone_image': True})], status='ready')

def _extract_html(path: Path, record: SourceIngestionRecord) -> SourceVisualAdapterResult:
    budget = SourceVisualExtractionBudget()
    try:
        return _extract_html_with_budget(path, record, budget=budget)
    except SourceVisualExtractionBudgetError as exc:
        return SourceVisualAdapterResult(status='failed', warnings=[str(exc)])

def _extract_html_with_budget(path: Path, record: SourceIngestionRecord, *, budget: SourceVisualExtractionBudget) -> SourceVisualAdapterResult:
    content = path.read_text(encoding='utf-8', errors='replace')
    parser = _MarkupVisualParser(content, budget=budget)
    parser.feed(content)
    visuals: list[RawSourceVisual] = []
    warnings: list[str] = []
    merged_table_count = sum((table.has_merged_cells for table in parser.tables))
    for index, reference in enumerate(parser.images):
        resolved = _resolve_markup_image(reference.source, source_uri=_markup_source_uri(record), budget=budget)
        if resolved is None:
            warnings.append(f'HTML image {index + 1} was rejected or unavailable.')
            continue
        image_content, mime_type, resolved_source = resolved
        visuals.append(RawSourceVisual(kind='image', source_locator=f'html:{path.name}:image:{index}', native_order=reference.document_order, content=image_content, mime_type=mime_type, text_offset=reference.text_offset, paragraph_index=reference.paragraph_index, caption=reference.caption, confidence=0.88, metadata={'image_source': resolved_source}))
    for index, table in enumerate(parser.tables):
        visuals.append(RawSourceVisual(kind='table', source_locator=f'html:{path.name}:table:{index}', native_order=table.document_order, text_offset=table.text_offset, table_data=table.rows, confidence=0.92, metadata=_unrepresented_markup_table_merge_metadata(table)))
    for index, reference in enumerate(parser.svgs):
        svg = _inline_svg_at_offset(content, reference.raw_offset)
        if svg is None:
            warnings.append(f'Inline SVG {index + 1} could not be located safely.')
            continue
        budget.account_image_bytes(len(svg))
        rendered = _render_svg(svg)
        if rendered is None:
            warnings.append(f'Inline SVG {index + 1} could not be rendered.')
            continue
        image_content, mime_type = rendered
        visuals.append(RawSourceVisual(kind='diagram', source_locator=f'html:{path.name}:svg:{index}', native_order=reference.document_order, content=image_content, mime_type=mime_type, text_offset=reference.text_offset, confidence=0.9, metadata={'original_mime_type': 'image/svg+xml'}))
    warnings.extend(_merged_markup_table_warnings(merged_table_count, source_format='HTML'))
    return SourceVisualAdapterResult(visuals=visuals, warnings=warnings, status='partial' if warnings else 'ready')

def _extract_markdown(path: Path, record: SourceIngestionRecord) -> SourceVisualAdapterResult:
    budget = SourceVisualExtractionBudget()
    try:
        return _extract_markdown_with_budget(path, record, budget=budget)
    except SourceVisualExtractionBudgetError as exc:
        return SourceVisualAdapterResult(status='failed', warnings=[str(exc)])

def _extract_markdown_with_budget(path: Path, record: SourceIngestionRecord, *, budget: SourceVisualExtractionBudget) -> SourceVisualAdapterResult:
    text = path.read_text(encoding='utf-8', errors='replace')
    visuals: list[RawSourceVisual] = []
    warnings: list[str] = []
    native_order = 0
    image_pattern = re.compile('!\\[([^\\]]*)\\]\\((?:<([^>]+)>|([^\\s)]+))(?:\\s+[\'\\"][^\'\\"]*[\'\\"])?\\)')
    image_matches = list(image_pattern.finditer(text))
    markdown_tables = _markdown_tables(text)
    budget.reserve_visual_objects(len(image_matches) + len(markdown_tables))
    for _offset, rows in markdown_tables:
        budget.account_table(rows)
    for index, match in enumerate(image_matches):
        source = match.group(2) or match.group(3) or ''
        resolved = _resolve_markup_image(source, source_uri=_markup_source_uri(record), budget=budget)
        if resolved is None:
            warnings.append(f'Markdown image {index + 1} was rejected or unavailable.')
            continue
        content, mime_type, resolved_source = resolved
        visuals.append(RawSourceVisual(kind='image', source_locator=f'markdown:offset:{match.start()}:image:{index}', native_order=native_order, content=content, mime_type=mime_type, text_offset=match.start(), caption=match.group(1).strip(), confidence=0.94, metadata={'image_source': resolved_source}))
        native_order += 1
    for table_index, (offset, rows) in enumerate(markdown_tables):
        visuals.append(RawSourceVisual(kind='table', source_locator=f'markdown:offset:{offset}:table:{table_index}', native_order=native_order, text_offset=offset, table_data=rows, confidence=0.94))
        native_order += 1
    return SourceVisualAdapterResult(visuals=visuals, warnings=warnings, status='partial' if warnings else 'ready')

def _extract_csv(path: Path) -> SourceVisualAdapterResult:
    text = path.read_text(encoding='utf-8-sig', errors='replace')
    rows = [[cell.strip() for cell in row] for row in csv.reader(text.splitlines())]
    rows = [row for row in rows if any(row)]
    if not rows:
        return SourceVisualAdapterResult(status='ready')
    budget = SourceVisualExtractionBudget()
    try:
        budget.reserve_visual_objects()
        budget.account_table(rows)
    except SourceVisualExtractionBudgetError as exc:
        return SourceVisualAdapterResult(status='failed', warnings=[str(exc)])
    return SourceVisualAdapterResult(visuals=[RawSourceVisual(kind='table', source_locator='csv:table:0', native_order=0, text_offset=0, table_data=rows, caption=path.stem, confidence=1.0)], status='ready')

def _extract_epub(path: Path) -> SourceVisualAdapterResult:
    visuals: list[RawSourceVisual] = []
    warnings: list[str] = []
    merged_table_count = 0
    budget = SourceVisualExtractionBudget()
    document_offset = 0
    included_document_count = 0
    try:
        with SafeSourceArchive(path) as archive:
            manifest, spine = _epub_manifest_and_spine(archive)
            for spine_index, item_name in enumerate(spine):
                content = _safe_zip_read(archive, item_name)
                if not content:
                    continue
                html_text = content.decode('utf-8', errors='replace')
                parser = _MarkupVisualParser(html_text, budget=budget)
                parser.feed(html_text)
                document_order_base = spine_index * 1000000
                canonical_text = parser.canonical_text
                document_included = bool(canonical_text.strip())
                separator = '\n\n' if included_document_count else ''
                prefix = f'{separator}[{item_name}]\n' if document_included else ''
                text_offset_base = document_offset + len(prefix) if document_included else None
                for image_index, reference in enumerate(parser.images):
                    target = _safe_posix_join(posixpath.dirname(item_name), unquote(reference.source))
                    image_content = _safe_zip_read(archive, target)
                    if not image_content:
                        warnings.append(f'EPUB image {reference.source} was rejected or unavailable.')
                        continue
                    budget.account_image_bytes(len(image_content))
                    mime_type = manifest.get(target) or _image_mime(target, image_content)
                    if mime_type not in _ALLOWED_IMAGE_MIMES:
                        warnings.append(f'EPUB image {reference.source} has an unsupported media type.')
                        continue
                    if mime_type == 'image/svg+xml':
                        rendered = _render_svg(image_content)
                        if rendered is None:
                            warnings.append(f'EPUB SVG image {reference.source} could not be rendered safely.')
                            continue
                        image_content, mime_type = rendered
                    visuals.append(RawSourceVisual(kind='image', source_locator=f'epub:{item_name}:image:{image_index}', native_order=document_order_base + reference.document_order, content=image_content, mime_type=mime_type, text_offset=text_offset_base + reference.text_offset if text_offset_base is not None else None, paragraph_index=reference.paragraph_index, caption=reference.caption, confidence=0.92, metadata={'epub_spine_index': spine_index, 'epub_asset': target, 'text_offset_anchor_safe': document_included}))
                for table_index, table in enumerate(parser.tables):
                    if table.has_merged_cells:
                        merged_table_count += 1
                    visuals.append(RawSourceVisual(kind='table', source_locator=f'epub:{item_name}:table:{table_index}', native_order=document_order_base + table.document_order, text_offset=text_offset_base + table.text_offset if text_offset_base is not None else None, table_data=table.rows, confidence=0.9, metadata={'epub_spine_index': spine_index, 'text_offset_anchor_safe': document_included, **_unrepresented_markup_table_merge_metadata(table)}))
                for svg_index, reference in enumerate(parser.svgs):
                    svg = _inline_svg_at_offset(html_text, reference.raw_offset)
                    if svg is None:
                        warnings.append(f'EPUB inline SVG {svg_index + 1} could not be located safely.')
                        continue
                    budget.account_image_bytes(len(svg))
                    rendered = _render_svg(svg)
                    if rendered is None:
                        warnings.append(f'EPUB inline SVG {svg_index + 1} could not be rendered.')
                        continue
                    image_content, mime_type = rendered
                    visuals.append(RawSourceVisual(kind='diagram', source_locator=f'epub:{item_name}:svg:{svg_index}', native_order=document_order_base + reference.document_order, content=image_content, mime_type=mime_type, text_offset=text_offset_base + reference.text_offset if text_offset_base is not None else None, confidence=0.9, metadata={'epub_spine_index': spine_index, 'original_mime_type': 'image/svg+xml', 'text_offset_anchor_safe': document_included}))
                if document_included:
                    document_offset += len(prefix) + len(canonical_text)
                    included_document_count += 1
    except (SourceArchiveError, SourceVisualExtractionBudgetError, SourceXmlError, KeyError, OSError) as exc:
        return SourceVisualAdapterResult(status='failed', warnings=[f'EPUB visual parsing failed: {exc}'])
    warnings.extend(_merged_markup_table_warnings(merged_table_count, source_format='EPUB'))
    return SourceVisualAdapterResult(visuals=visuals, warnings=warnings, status='partial' if warnings else 'ready')

def _epub_manifest_and_spine(archive: SafeSourceArchive) -> tuple[dict[str, str], list[str]]:
    opf_name = ''
    if 'META-INF/container.xml' in archive.namelist():
        container = parse_untrusted_xml(archive.read('META-INF/container.xml'))
        opf_name = next((_safe_posix_join('', unquote(str(node.attrib.get('full-path') or ''))) for node in container.iter() if node.tag.endswith('}rootfile')), '')
    if not opf_name:
        opf_name = next((name for name in archive.namelist() if name.lower().endswith('.opf')), '')
    if not opf_name:
        html_names = [name for name in archive.namelist() if name.lower().endswith(('.xhtml', '.html', '.htm'))]
        return ({}, html_names)
    root = parse_untrusted_xml(archive.read(opf_name))
    base = posixpath.dirname(opf_name)
    by_id: dict[str, str] = {}
    manifest: dict[str, str] = {}
    for node in root.iter():
        if not node.tag.endswith('}item'):
            continue
        href = str(node.attrib.get('href') or '')
        item_id = str(node.attrib.get('id') or '')
        target = _safe_posix_join(base, unquote(href))
        if not target:
            continue
        by_id[item_id] = target
        manifest[target] = str(node.attrib.get('media-type') or '')
    spine = [by_id.get(str(node.attrib.get('idref') or ''), '') for node in root.iter() if node.tag.endswith('}itemref')]
    return (manifest, [name for name in spine if name])

def _resolve_markup_image(source: str, *, source_uri: str | None, budget: SourceVisualExtractionBudget) -> tuple[bytes, str, str] | None:
    normalized = source.strip()
    if not normalized:
        return None
    if normalized.startswith('data:'):
        return _decode_data_image(normalized, budget=budget)
    parsed = urlparse(normalized)
    if parsed.scheme in {'http', 'https'}:
        return _fetch_public_image(normalized, budget=budget)
    if parsed.scheme:
        return None
    if source_uri:
        remote = urljoin(source_uri, normalized)
        if urlparse(remote).scheme in {'http', 'https'}:
            return _fetch_public_image(remote, budget=budget)
    return None

def _decode_data_image(value: str, *, budget: SourceVisualExtractionBudget) -> tuple[bytes, str, str] | None:
    match = re.match('data:([^;,]+)(;base64)?,(.*)$', value, flags=re.S | re.I)
    if not match:
        return None
    mime_type = match.group(1).lower()
    if mime_type not in _ALLOWED_IMAGE_MIMES:
        return None
    try:
        content = base64.b64decode(match.group(3), validate=True) if match.group(2) else unquote_to_bytes(match.group(3))
    except (ValueError, TypeError):
        return None
    if not content or len(content) > MAX_SOURCE_VISUAL_BYTES:
        return None
    budget.account_image_bytes(len(content))
    return (content, mime_type, 'data:image')

def _fetch_public_image(url: str, *, budget: SourceVisualExtractionBudget | None=None) -> tuple[bytes, str, str] | None:
    extraction_budget = budget or SourceVisualExtractionBudget()
    current = url
    for _ in range(4):
        resolved = _resolve_public_http_url(current)
        if resolved is None:
            return None
        parsed, addresses = resolved
        redirect_url = ''
        for address in addresses:
            connection: http.client.HTTPConnection | None = None
            try:
                extraction_budget.reserve_remote_request()
                connection = _open_pinned_http_connection(parsed, address, timeout=10.0)
                connection.request('GET', _http_request_target(parsed), headers={'Accept': 'image/*', 'Accept-Encoding': 'identity', 'Host': _http_host_header(parsed), 'User-Agent': 'OpenClassSourceVisual/1.0'})
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = str(response.getheader('location') or '').strip()
                    if not location:
                        return None
                    redirect_url = urljoin(current, location)
                    break
                if response.status < 200 or response.status >= 300:
                    return None
                declared_length = str(response.getheader('content-length') or '').strip()
                if declared_length:
                    try:
                        if int(declared_length) < 0 or int(declared_length) > MAX_SOURCE_VISUAL_BYTES:
                            return None
                    except ValueError:
                        return None
                content = _read_limited_http_body(response, budget=extraction_budget)
                if not content:
                    return None
                mime_type = str(response.getheader('content-type') or '').split(';', 1)[0].strip().lower()
                if mime_type not in _ALLOWED_IMAGE_MIMES:
                    mime_type = _image_mime(current, content)
                return (content, mime_type, current) if mime_type in _ALLOWED_IMAGE_MIMES else None
            except (OSError, ssl.SSLError, http.client.HTTPException):
                continue
            finally:
                if connection is not None:
                    connection.close()
        if not redirect_url:
            return None
        current = redirect_url
    return None

def _is_public_http_url(url: str) -> bool:
    return _resolve_public_http_url(url) is not None

def _resolve_public_http_url(url: str) -> tuple[ParseResult, tuple[str, ...]] | None:
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        return None
    try:
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        addresses = tuple(dict.fromkeys((str(ipaddress.ip_address(item[4][0])) for item in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM))))
    except (OSError, ValueError):
        return None
    if not addresses or not all((ipaddress.ip_address(address).is_global for address in addresses)):
        return None
    return (parsed, addresses)

class _PinnedHTTPConnection(http.client.HTTPConnection):

    def __init__(self, origin_host: str, port: int, *, connect_host: str, timeout: float) -> None:
        super().__init__(origin_host, port=port, timeout=timeout)
        self._connect_host = connect_host

    def connect(self) -> None:
        self.sock = socket.create_connection((self._connect_host, self.port), self.timeout, self.source_address)

class _PinnedHTTPSConnection(http.client.HTTPSConnection):

    def __init__(self, origin_host: str, port: int, *, connect_host: str, timeout: float) -> None:
        super().__init__(origin_host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._connect_host = connect_host

    def connect(self) -> None:
        raw_socket = socket.create_connection((self._connect_host, self.port), self.timeout, self.source_address)
        try:
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise

def _open_pinned_http_connection(parsed: ParseResult, resolved_address: str, *, timeout: float) -> http.client.HTTPConnection:
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    connection_type = _PinnedHTTPSConnection if parsed.scheme == 'https' else _PinnedHTTPConnection
    return connection_type(parsed.hostname, port, connect_host=resolved_address, timeout=timeout)

def _http_request_target(parsed: ParseResult) -> str:
    target = parsed.path or '/'
    return f'{target}?{parsed.query}' if parsed.query else target

def _http_host_header(parsed: ParseResult) -> str:
    hostname = parsed.hostname or ''
    if ':' in hostname and (not hostname.startswith('[')):
        hostname = f'[{hostname}]'
    default_port = 443 if parsed.scheme == 'https' else 80
    return f'{hostname}:{parsed.port}' if parsed.port and parsed.port != default_port else hostname

def _read_limited_http_body(response: http.client.HTTPResponse, *, budget: SourceVisualExtractionBudget) -> bytes | None:
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = MAX_SOURCE_VISUAL_BYTES - total
        chunk = response.read(min(64 * 1024, remaining + 1))
        if not chunk:
            break
        total += len(chunk)
        budget.account_remote_download(len(chunk))
        budget.account_image_bytes(len(chunk))
        if total > MAX_SOURCE_VISUAL_BYTES:
            return None
        chunks.append(chunk)
    return b''.join(chunks)

def _markup_source_uri(record: SourceIngestionRecord) -> str | None:
    resolved_source_uri = record.metadata.get('resolved_source_uri')
    if isinstance(resolved_source_uri, str) and resolved_source_uri.strip():
        return resolved_source_uri.strip()
    return record.source_uri

def _markdown_tables(text: str) -> list[tuple[int, list[list[str]]]]:
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    tables: list[tuple[int, list[list[str]]]] = []
    index = 0
    while index + 1 < len(lines):
        header = _pipe_row(lines[index])
        separator = _pipe_row(lines[index + 1])
        if not header or not separator or (not all((re.fullmatch(':?-{3,}:?', cell.replace(' ', '')) for cell in separator))):
            index += 1
            continue
        rows = [header]
        cursor_index = index + 2
        while cursor_index < len(lines):
            row = _pipe_row(lines[cursor_index])
            if not row:
                break
            rows.append(row)
            cursor_index += 1
        tables.append((offsets[index], rows))
        index = cursor_index
    return tables

def _pipe_row(line: str) -> list[str]:
    stripped = line.strip()
    if '|' not in stripped:
        return []
    return [cell.strip() for cell in stripped.strip('|').split('|')]

def _inline_svg_at_offset(text: str, start: int) -> bytes | None:
    opening = re.compile('<(/?)svg\\b[^>]*>', flags=re.I | re.S)
    first = opening.match(text, pos=max(0, start))
    if first is None or first.group(1):
        return None
    depth = 0
    for match in opening.finditer(text, pos=first.start()):
        token = match.group(0)
        if match.group(1):
            depth -= 1
            if depth == 0:
                content = text[first.start():match.end()].encode('utf-8')
                return content if len(content) <= MAX_SOURCE_VISUAL_BYTES else None
            continue
        if token.rstrip().endswith('/>'):
            if depth == 0:
                content = text[first.start():match.end()].encode('utf-8')
                return content if len(content) <= MAX_SOURCE_VISUAL_BYTES else None
            continue
        depth += 1
    return None

def _render_svg(content: bytes) -> tuple[bytes, str] | None:
    sanitized = _sanitize_svg(content)
    if sanitized is None:
        return None
    svg_content, width, height = sanitized
    try:
        import cairosvg
        rendered = cairosvg.svg2png(bytestring=svg_content, output_width=width, output_height=height)
        return (rendered, 'image/png')
    except Exception:
        pass
    try:
        from openclass_core.services import pdf_backend as fitz
        with fitz.open(stream=svg_content, filetype='svg') as document:
            page = document[0]
            page_width = max(float(page.rect.width), 1.0)
            page_height = max(float(page.rect.height), 1.0)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(width / page_width, height / page_height), alpha=True)
            return (pixmap.tobytes('png'), 'image/png')
    except Exception:
        return None

def _sanitize_svg(content: bytes) -> tuple[bytes, int, int] | None:
    if not content or len(content) > MAX_SOURCE_VISUAL_BYTES:
        return None
    try:
        root = parse_untrusted_xml(content)
    except SourceXmlError:
        return None
    if _xml_local_name(root.tag) != 'svg':
        return None
    node_count = 0
    for element in root.iter():
        node_count += 1
        if node_count > _MAX_SVG_NODES or _xml_local_name(element.tag) in _UNSAFE_SVG_ELEMENTS:
            return None
        if element.text and _has_unsafe_svg_reference(element.text):
            return None
        for raw_name, raw_value in element.attrib.items():
            name = _xml_local_name(raw_name)
            value = str(raw_value or '').strip()
            if name.startswith('on'):
                return None
            if name == 'href' and value and (not value.startswith('#')):
                return None
            if _has_unsafe_svg_reference(value):
                return None
    dimensions = _safe_svg_dimensions(root)
    if dimensions is None:
        return None
    return (ElementTree.tostring(root, encoding='utf-8'), dimensions[0], dimensions[1])

def _has_unsafe_svg_reference(value: str) -> bool:
    lowered = value.strip().lower()
    if any((token in lowered for token in ('javascript:', 'file:', 'http:', 'https:', 'ftp:', 'data:', '@import', 'expression('))):
        return True
    for match in re.finditer('url\\(\\s*([\'\\"]?)(.*?)\\1\\s*\\)', value, flags=re.I | re.S):
        if not match.group(2).strip().startswith('#'):
            return True
    return False

def _safe_svg_dimensions(root: ElementTree.Element) -> tuple[int, int] | None:
    width = _svg_length(root.attrib.get('width'))
    height = _svg_length(root.attrib.get('height'))
    viewbox = str(root.attrib.get('viewBox') or root.attrib.get('viewbox') or '').strip()
    viewbox_width: float | None = None
    viewbox_height: float | None = None
    if viewbox:
        try:
            parts = [float(item) for item in re.split('[\\s,]+', viewbox) if item]
        except ValueError:
            return None
        if len(parts) != 4 or parts[2] <= 0 or parts[3] <= 0 or (not all((math.isfinite(item) for item in parts))):
            return None
        viewbox_width, viewbox_height = (parts[2], parts[3])
    if width is None and height is None:
        width, height = (viewbox_width or 300.0, viewbox_height or 150.0)
    elif width is None:
        ratio = viewbox_width / viewbox_height if viewbox_width and viewbox_height else 2.0
        width = height * ratio if height is not None else None
    elif height is None:
        ratio = viewbox_height / viewbox_width if viewbox_width and viewbox_height else 0.5
        height = width * ratio
    if width is None or height is None or width <= 0 or (height <= 0):
        return None
    if not math.isfinite(width) or not math.isfinite(height):
        return None
    scale = min(1.0, _MAX_SVG_EDGE / width, _MAX_SVG_EDGE / height, math.sqrt(_MAX_SVG_PIXELS / (width * height)))
    return (max(1, round(width * scale)), max(1, round(height * scale)))

def _svg_length(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.fullmatch('\\s*([+-]?(?:\\d+(?:\\.\\d*)?|\\.\\d+)(?:[eE][+-]?\\d+)?)\\s*(?:px|pt|pc|mm|cm|in)?\\s*', value, flags=re.I)
    if not match:
        return None
    number = float(match.group(1))
    return number if math.isfinite(number) else None

def _xml_local_name(value: str) -> str:
    return value.rsplit('}', 1)[-1].lower()

def render_svg_to_png(content: bytes) -> bytes | None:
    rendered = _render_svg(content)
    return rendered[0] if rendered is not None else None

def _safe_posix_join(base: str, target: str) -> str:
    if not target or target.startswith('/'):
        return ''
    normalized = posixpath.normpath(posixpath.join(base, target)).lstrip('/')
    return '' if normalized.startswith('../') else normalized

def _safe_zip_read(archive: SafeSourceArchive, name: str) -> bytes:
    if not name or name.startswith('/') or '..' in Path(name).parts:
        return b''
    try:
        return archive.read(name, max_bytes=MAX_SOURCE_VISUAL_BYTES)
    except KeyError:
        return b''

def _image_mime(name: str, content: bytes, *, declared: str='') -> str:
    normalized_declared = declared.split(';', 1)[0].strip().lower()
    if normalized_declared in _ALLOWED_IMAGE_MIMES:
        return normalized_declared
    guessed = mimetypes.guess_type(name)[0] or ''
    if guessed in _ALLOWED_IMAGE_MIMES:
        return guessed
    if content.startswith(b'\x89PNG'):
        return 'image/png'
    if content.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if content.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if content.startswith(b'RIFF') and content[8:12] == b'WEBP':
        return 'image/webp'
    if b'<svg' in content[:2048].lower():
        return 'image/svg+xml'
    return 'application/octet-stream'
