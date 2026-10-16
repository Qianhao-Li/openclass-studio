from __future__ import annotations
import re
from html.parser import HTMLParser
from pathlib import Path
import httpx
from openclass_core.models import SourceIngestionRecord
from openclass_core.services import workspace_state

class SourceUrlSnapshotError(RuntimeError):
    pass

def fetch_url_source_snapshot(record: SourceIngestionRecord, source_uri: str) -> dict[str, str]:
    try:
        response = httpx.get(source_uri, headers={'User-Agent': 'OpenClassSourceIngestion/1.0'}, follow_redirects=True, timeout=20)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise SourceUrlSnapshotError(str(exc)) from exc
    content_type = response.headers.get('content-type', '').lower()
    if 'text/html' in content_type:
        text = _html_to_text(response.text)
    elif content_type.startswith('text/') or not content_type:
        text = response.text
    else:
        raise SourceUrlSnapshotError('Only text and HTML URLs are supported by the native URL importer.')
    text = '\n'.join((line.strip() for line in text.splitlines() if line.strip()))
    if not text:
        raise SourceUrlSnapshotError('URL did not return readable text.')
    return {**_save_local_source_text(record, text), 'local_source_snapshot_mime_type': content_type or 'text/plain'}

def _save_local_source_text(record: SourceIngestionRecord, text: str) -> dict[str, str]:
    safe_name = _safe_file_name(record.file_name or record.title or record.id)
    source_dir = workspace_state.UPLOAD_DIR / 'sources'
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / f'{record.id}_{safe_name}.txt'
    path.write_text(text, encoding='utf-8')
    return {'local_source_path': str(path)}

class _HtmlTextExtractor(HTMLParser):

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._heading_level = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {'script', 'style', 'noscript'}:
            self._skip_depth += 1
        if re.fullmatch('h[1-6]', tag):
            self._heading_level = int(tag[1])
        if tag in {'p', 'br', 'div', 'section', 'article', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            self.parts.append('\n')

    def handle_endtag(self, tag: str) -> None:
        if tag in {'script', 'style', 'noscript'} and self._skip_depth:
            self._skip_depth -= 1
        if re.fullmatch('h[1-6]', tag):
            self._heading_level = 0
        if tag in {'p', 'div', 'section', 'article', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
            self.parts.append('\n')

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            prefix = f"{'#' * self._heading_level} " if self._heading_level else ''
            self.parts.append(f'{prefix}{data.strip()}')

def _html_to_text(html: str) -> str:
    parser = _HtmlTextExtractor()
    parser.feed(html)
    return '\n'.join((part.strip() for part in parser.parts if part.strip()))

def _safe_file_name(file_name: str) -> str:
    name = Path(file_name).name.strip() or 'source'
    return re.sub('[^A-Za-z0-9._ -]+', '_', name)[:180]
