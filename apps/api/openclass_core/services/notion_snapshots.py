"""Immutable Notion API snapshots; a capture is not a Notion history revision."""
from __future__ import annotations
import hashlib
import json
import re
from urllib.parse import urlsplit
from uuid import UUID

class NotionSourceError(RuntimeError):

    def __init__(self, message: str, status_code: int=422, *, retry_after: int | None=None):
        super().__init__(message)
        self.status_code, self.retry_after = (status_code, retry_after)

def notion_id(value: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise NotionSourceError('A valid Notion object ID is required.') from None

def notion_page_id(value: str) -> str:
    """Accept an object ID or a page URL on a Notion-controlled host.

    The API, not the URL, still decides whether the connection may read it.
    Other object IDs (notably pagination cursors) remain UUID-only.
    """
    if not isinstance(value, str):
        return notion_id(value)
    if not value.startswith('https://'):
        return notion_id(value)
    try:
        if any((ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError()
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        if parsed.scheme != 'https' or parsed.username or parsed.password or (parsed.port is not None) or (not (host in {'notion.so', 'www.notion.so', 'notion.site'} or host.endswith('.notion.site'))):
            raise ValueError()
        last = parsed.path.rstrip('/').rsplit('/', 1)[-1]
        try:
            return notion_id(last)
        except NotionSourceError:
            pass
        match = re.fullmatch('(?:[^/]*-)?([0-9a-fA-F]{32})', last)
        if not match:
            raise ValueError()
        return notion_id(match.group(1))
    except (ValueError, TypeError):
        raise NotionSourceError('A valid Notion page URL or object ID is required.') from None

def json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()

def page_title(page: dict) -> str:
    for prop in page.get('properties', {}).values():
        if isinstance(prop, dict) and prop.get('type') == 'title':
            return ''.join((str(part.get('plain_text') or part.get('text', {}).get('content', '')) for part in prop.get('title', []) if isinstance(part, dict)))[:500]
    return 'Notion page'

def make_snapshot(*, identity: dict, version: str, responses: list[tuple[str, bytes]], coverage: dict, api_version: str, title: str) -> dict:
    entries = []
    for endpoint, body in responses:
        entries.append(b'{"endpoint":' + json_bytes(endpoint) + b',"sha256":' + json_bytes(hashlib.sha256(body).hexdigest()) + b',"body":' + body + b'}')
    header = json_bytes({'format': 'notion_api_snapshot_v1', 'identity': identity, 'provider_version': version, 'api_version': api_version, 'coverage': coverage})
    content = header[:-1] + b',"responses":[' + b','.join(entries) + b']}'
    digest = hashlib.sha256(content).hexdigest()
    return {'content': content, 'file_name': f"notion-{identity['page_id']}.json", 'mime_type': 'application/json', 'sha256': digest, 'identity': identity, 'provider_version': version, 'coverage': coverage, 'title': title, 'source_snapshot_kind': 'api_json_snapshot', 'origin_metadata': {'provider': 'notion', 'origin': 'notion', **identity, 'source_snapshot_kind': 'api_json_snapshot', 'api_version': api_version, 'provider_version': version, 'snapshot_sha256': digest, 'coverage': coverage}}
