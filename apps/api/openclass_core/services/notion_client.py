"""Read-only Notion public-connection client. No account or credential storage.

The caller owns OAuth state, connection isolation, refresh serialization and the
current turn's resource grant. Search results are locators, never evidence.
"""
from __future__ import annotations
import hashlib
import json
import mimetypes
import re
from urllib.parse import urlencode
import httpx
from openclass_core.services.notion_snapshots import NotionSourceError, make_snapshot, notion_id, notion_page_id, page_title
API = 'https://api.notion.com/v1'
MAX_JSON_BYTES = 8 * 1024 * 1024
MEDIA_TYPES = {'image', 'file', 'pdf', 'audio', 'video'}

class NotionClient:

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, *, http: httpx.Client | None=None, api_version: str='2026-03-11', file_fetch=None):
        self.client_id, self.client_secret, self.redirect_uri = (client_id, client_secret, redirect_uri)
        self.api_version = api_version
        self.http = http or httpx.Client(timeout=httpx.Timeout(40, connect=10), follow_redirects=False)
        if file_fetch is None:
            from openclass_core.services.google_sources.web_snapshot import fetch_public
            file_fetch = fetch_public
        self.file_fetch = file_fetch

    def authorization_url(self, state: str) -> str:
        if not state or not self.client_id or (not self.redirect_uri):
            raise NotionSourceError('Notion authorization requires configured credentials and state.')
        return API + '/oauth/authorize?' + urlencode({'client_id': self.client_id, 'redirect_uri': self.redirect_uri, 'response_type': 'code', 'owner': 'user', 'state': state})

    def _request(self, method: str, path: str, *, token: str='', body=None, params=None, oauth=False):
        query_only = method == 'POST' and re.fullmatch('/data_sources/[a-f0-9-]{36}/query', path)
        if method != 'GET' and (not query_only) and ((method, path) not in {('POST', endpoint) for endpoint in ('/search', '/oauth/token', '/oauth/introspect', '/oauth/revoke')}):
            raise NotionSourceError('This Notion adapter permits only read operations.', 403)
        headers = {'Notion-Version': self.api_version}
        auth = httpx.BasicAuth(self.client_id, self.client_secret) if oauth else None
        if not oauth:
            if not token:
                raise NotionSourceError('Connect Notion before reading its content.', 401)
            headers['Authorization'] = 'Bearer ' + token
        try:
            with self.http.stream(method, API + path, headers=headers, auth=auth, json=body, params=params, follow_redirects=False) as response:
                if not response.is_success:
                    retry = response.headers.get('Retry-After', '')
                    raise NotionSourceError('Notion rejected this request; check authorization or retry later.', response.status_code, retry_after=int(retry) if retry.isdigit() else None)
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_JSON_BYTES:
                        raise NotionSourceError('Notion returned more content than this request can read.', 413)
                payload = json.loads(raw) if raw else {}
                if not isinstance(payload, dict):
                    raise ValueError()
                return (payload, bytes(raw))
        except (httpx.HTTPError, ValueError):
            raise NotionSourceError('Notion returned an unavailable or invalid response.', 502) from None

    def exchange_code(self, code: str) -> dict:
        if not code:
            raise NotionSourceError('Notion did not return an authorization code.')
        data, _ = self._request('POST', '/oauth/token', oauth=True, body={'grant_type': 'authorization_code', 'code': code, 'redirect_uri': self.redirect_uri})
        if not all((data.get(field) for field in ('access_token', 'refresh_token', 'workspace_id', 'bot_id'))):
            raise NotionSourceError('Notion did not return a complete public authorization.', 502)
        return data

    def refresh(self, credentials: dict) -> dict:
        if not credentials.get('refresh_token'):
            raise NotionSourceError('Reconnect Notion to renew this authorization.', 401)
        data, _ = self._request('POST', '/oauth/token', oauth=True, body={'grant_type': 'refresh_token', 'refresh_token': credentials['refresh_token']})
        if not data.get('access_token') or not data.get('refresh_token'):
            raise NotionSourceError('Notion did not return a rotated token pair.', 502)
        return data

    def inspect_access(self, access_token: str) -> dict:
        introspection, _ = self._request('POST', '/oauth/introspect', oauth=True, body={'token': access_token})
        if introspection.get('active') is not True:
            raise NotionSourceError('Notion authorization is no longer active.', 401)
        profile, _ = self._request('GET', '/users/me', token=access_token)
        bot = profile.get('bot', {})
        if profile.get('type') != 'bot' or not profile.get('id') or (not isinstance(bot, dict)):
            raise NotionSourceError('A Notion public connection identity could not be verified.', 403)
        self._request('POST', '/search', token=access_token, body={'page_size': 1})
        owner = bot.get('owner', {})
        return {'bot_id': profile['id'], 'workspace_name': bot.get('workspace_name', ''), 'user_id': owner.get('user', {}).get('id', ''), 'grants': {'read_content': True, 'scope': introspection.get('scope', ''), 'write_enabled': False, 'resource_scope': 'shared_pages', 'enumeration_complete': False}}

    def search(self, access_token: str, *, query: str='', cursor: str | None=None, page_size: int=50) -> dict:
        if type(page_size) is not int or not 1 <= page_size <= 100 or len(query) > 1000:
            raise NotionSourceError('Invalid Notion search size or query.')
        body = {'query': query, 'page_size': page_size}
        if cursor:
            body['start_cursor'] = notion_id(cursor)
        data, _ = self._request('POST', '/search', token=access_token, body=body)
        return self._search_page(data)

    @staticmethod
    def _search_page(data):
        if not isinstance(data.get('results'), list) or type(data.get('has_more')) is not bool:
            raise NotionSourceError('Notion returned an invalid result page.', 502)
        items = []
        for item in data.get('results', []):
            if not isinstance(item, dict) or item.get('object') not in {'page', 'data_source', 'database'}:
                continue
            items.append({'id': notion_id(item.get('id')), 'kind': item['object'], 'title': page_title(item) if item['object'] == 'page' else ''.join((part.get('plain_text', '') for part in item.get('title', []))), 'url': item.get('url', ''), 'provider_version': item.get('last_edited_time', ''), 'citable': False})
        has_more = data.get('has_more') is True
        if has_more and (not data.get('next_cursor')):
            raise NotionSourceError('Notion search pagination is incomplete.', 502)
        return {'items': items, 'next_cursor': data.get('next_cursor') if has_more else None, 'complete': False, 'page_complete': not has_more, 'coverage': {'search_kind': 'title', 'exhaustive': False, 'reason': 'Notion search is eventually consistent and is not a workspace inventory.'}}

    def query_data_source(self, access_token: str, data_source_id: str, *, cursor: str | None=None, page_size: int=50) -> dict:
        data_source_id = notion_id(data_source_id)
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise NotionSourceError('Invalid Notion page size.')
        body = {'page_size': page_size, **({'start_cursor': notion_id(cursor)} if cursor else {})}
        data, _ = self._request('POST', f'/data_sources/{data_source_id}/query', token=access_token, body=body)
        result = self._search_page(data)
        result['coverage'] = {'scope': 'selected_data_source', 'data_source_id': data_source_id, 'snapshot_consistent': False, 'search_kind': 'browse'}
        return result

    def locate_page(self, access_token: str, page_url: str) -> dict:
        """Resolve a pasted Notion page link through the caller's access token."""
        page_id = notion_page_id(page_url)
        page, _ = self._request('GET', '/pages/' + page_id, token=access_token)
        self._check_object(page, page_id, expected_object='page')
        return {'id': page_id, 'kind': 'page', 'title': page_title(page), 'url': page.get('url', ''), 'provider_version': page['last_edited_time'], 'citable': False}

    def read_page(self, access_token: str, page_id: str, *, max_requests: int=30, max_bytes: int=16 * 1024 * 1024) -> dict:
        page_id = notion_page_id(page_id)
        if type(max_requests) is not int or not 3 <= max_requests <= 200 or (not 1 <= max_bytes <= 64 * 1024 * 1024):
            raise NotionSourceError('Invalid Notion capture budget.')
        endpoint = '/pages/' + page_id
        page, raw = self._request('GET', endpoint, token=access_token)
        self._check_object(page, page_id, expected_object='page')
        responses, size, requests = ([(endpoint, raw)], len(raw), 1)
        pending, seen, missing, assets = ([(page_id, None)], set(), [], [])
        if isinstance(page.get('cover'), dict):
            missing.append({'reason': 'page_cover_not_downloaded'})
        for name, prop in page.get('properties', {}).items():
            value = prop.get(prop.get('type')) if isinstance(prop, dict) else None
            if isinstance(prop, dict) and (prop.get('has_more') or (prop.get('type') in {'relation', 'rollup', 'people', 'rich_text', 'title'} and isinstance(value, list) and (len(value) >= 25))):
                missing.append({'reason': 'property_may_be_truncated', 'property': name})
            if isinstance(prop, dict) and prop.get('type') == 'files' and prop.get('files'):
                missing.append({'reason': 'property_files_not_downloaded', 'property': name})
        while pending and requests < max_requests - 1 and (size <= max_bytes):
            parent, cursor = pending.pop(0)
            key = (parent, cursor)
            if key in seen:
                raise NotionSourceError('Notion returned a repeated content cursor.', 502)
            seen.add(key)
            path = '/blocks/' + parent + '/children'
            params = {'page_size': 100, **({'start_cursor': cursor} if cursor else {})}
            batch, body = self._request('GET', path, token=access_token, params=params)
            if not isinstance(batch.get('results'), list) or type(batch.get('has_more')) is not bool:
                raise NotionSourceError('Notion returned an invalid block page.', 502)
            if size + len(body) > max_bytes:
                pending.insert(0, (parent, cursor))
                break
            responses.append((path + ('?start_cursor=' + cursor if cursor else ''), body))
            requests += 1
            size += len(body)
            for block in batch.get('results', []):
                if not isinstance(block, dict) or block.get('object') not in {None, 'block'}:
                    raise NotionSourceError('Notion returned an invalid block.', 502)
                try:
                    block_id = notion_id(block.get('id'))
                except NotionSourceError:
                    raise NotionSourceError('Notion returned an invalid block ID.', 502) from None
                kind = block.get('type', 'unsupported')
                if kind == 'unsupported':
                    missing.append({'reason': 'unsupported_block', 'block_id': block_id})
                if kind in {'embed', 'link_preview'}:
                    missing.append({'reason': 'embedded_content_not_read', 'block_id': block_id})
                if kind in MEDIA_TYPES:
                    assets.append({'block_id': block_id, 'kind': kind})
                    missing.append({'reason': 'media_not_downloaded', 'block_id': block_id})
                linked_sync = kind == 'synced_block' and isinstance(block.get(kind), dict) and (block[kind].get('synced_from') is not None)
                if kind in {'child_page', 'child_database', 'link_to_page'} or linked_sync:
                    missing.append({'reason': 'linked_content_not_selected', 'block_id': block_id})
                elif block.get('has_children'):
                    pending.append((block_id, None))
            if batch.get('has_more'):
                if not batch.get('next_cursor'):
                    raise NotionSourceError('Notion page pagination is incomplete.', 502)
                pending.insert(0, (parent, notion_id(batch['next_cursor'])))
        after, _ = self._request('GET', endpoint, token=access_token)
        self._check_object(after, page_id, expected_object='page')
        if page.get('last_edited_time') != after.get('last_edited_time'):
            raise NotionSourceError('The Notion page changed during reading. Select it again.', 409)
        if pending or size > max_bytes:
            missing.append({'reason': 'read_budget_exhausted'})
        coverage = {'complete': not missing, 'structure_complete': not pending and size <= max_bytes, 'atomic_snapshot': False, 'scope': 'selected_page_blocks', 'missing': missing}
        result = make_snapshot(identity={'page_id': page_id}, version=page['last_edited_time'], responses=responses, coverage=coverage, api_version=self.api_version, title=page_title(page))
        result['assets'] = assets
        return result

    @staticmethod
    def _check_object(data, expected_id, *, expected_object=None):
        if not isinstance(data, dict) or (expected_object and data.get('object') != expected_object):
            raise NotionSourceError('Notion returned an invalid object.', 502)
        if expected_object == 'page' and (not isinstance(data.get('properties'), dict)):
            raise NotionSourceError('Notion returned invalid page properties.', 502)
        try:
            actual_id = notion_id(data.get('id', ''))
        except NotionSourceError:
            raise NotionSourceError('Notion returned an invalid object ID.', 502) from None
        if actual_id != expected_id or data.get('in_trash') or data.get('archived'):
            raise NotionSourceError('The selected Notion object is unavailable.', 404)
        if not data.get('last_edited_time'):
            raise NotionSourceError('Notion returned no object version marker.', 502)

    def download_file(self, access_token: str, block_id: str, *, max_bytes: int=32 * 1024 * 1024) -> dict:
        block_id = notion_id(block_id)
        block, _ = self._request('GET', '/blocks/' + block_id, token=access_token)
        self._check_object(block, block_id)
        kind = block.get('type')
        file = block.get(kind, {})
        if kind not in MEDIA_TYPES or file.get('type') not in {'file', 'external'}:
            raise NotionSourceError('This Notion block has no downloadable file.', 415)
        url = file.get(file['type'], {}).get('url', '')
        if not url.startswith('https://'):
            raise NotionSourceError('Notion files require a public HTTPS download URL.')
        try:
            content, mime, _ = self.file_fetch(url)
        except Exception:
            raise NotionSourceError('The Notion attachment could not be downloaded safely.', 502) from None
        if not content or len(content) > max_bytes:
            raise NotionSourceError('The Notion attachment is empty or exceeds the download limit.', 413)
        after, _ = self._request('GET', '/blocks/' + block_id, token=access_token)
        self._check_object(after, block_id)
        if block['last_edited_time'] != after['last_edited_time']:
            raise NotionSourceError('The Notion attachment changed during download.', 409)
        digest = hashlib.sha256(content).hexdigest()
        extension = mimetypes.guess_extension(mime) or ''
        return {'content': content, 'file_name': f'notion-{block_id}{extension}', 'mime_type': mime, 'sha256': digest, 'identity': {'block_id': block_id}, 'provider_version': block['last_edited_time'], 'source_snapshot_kind': 'original_file', 'coverage': {'complete': True, 'scope': 'selected_attachment'}, 'origin_metadata': {'origin': 'notion', 'provider': 'notion', 'block_id': block_id, 'provider_version': block['last_edited_time'], 'snapshot_sha256': digest, 'source_snapshot_kind': 'original_file'}}

    def revoke(self, access_token: str) -> None:
        self._request('POST', '/oauth/revoke', oauth=True, body={'token': access_token})
