"""Zotero OAuth 1.0a key exchange and bounded, read-only Web API access.

Credentials, OAuth attempts, local connection scopes and persistence belong to
the caller. A cloud library entry does not imply that its attachment is online.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
import httpx
API = 'https://api.zotero.org'
OAUTH = 'https://www.zotero.org/oauth'
MAX_JSON_BYTES = 8 * 1024 * 1024

class ZoteroSourceError(RuntimeError):

    def __init__(self, message: str, status_code: int=422, *, retry_after: int | None=None):
        super().__init__(message)
        self.status_code, self.retry_after = (status_code, retry_after)

def _escape(value):
    return quote(str(value), safe='~-._')

def _key(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Z0-9]{8}', value):
        raise ZoteroSourceError('A valid Zotero item or collection key is required.')
    return value

class ZoteroClient:

    def __init__(self, client_key: str, client_secret: str, redirect_uri: str, *, http: httpx.Client | None=None, file_fetch=None, clock=time.time, nonce=None):
        self.client_key, self.client_secret, self.redirect_uri = (client_key, client_secret, redirect_uri)
        self.http = http or httpx.Client(timeout=httpx.Timeout(40, connect=10), follow_redirects=False)
        self.clock, self.nonce = (clock, nonce or (lambda: secrets.token_hex(20)))
        if file_fetch is None:
            from openclass_core.services.google_sources.web_snapshot import fetch_public
            file_fetch = fetch_public
        self.file_fetch = file_fetch

    def _oauth_header(self, method, url, *, token='', secret='', **extras):
        params = {'oauth_consumer_key': self.client_key, 'oauth_nonce': self.nonce(), 'oauth_signature_method': 'HMAC-SHA1', 'oauth_timestamp': str(int(self.clock())), 'oauth_version': '1.0', **extras}
        if token:
            params['oauth_token'] = token
        parsed = urlsplit(url)
        base_url = parsed._replace(query='', fragment='').geturl()
        normalized = '&'.join((f'{key}={value}' for key, value in sorted(((_escape(k), _escape(v)) for k, v in [*params.items(), *parse_qsl(parsed.query)]))))
        base = '&'.join((_escape(method), _escape(base_url), _escape(normalized)))
        signing_key = _escape(self.client_secret) + '&' + _escape(secret)
        params['oauth_signature'] = base64.b64encode(hmac.new(signing_key.encode(), base.encode(), hashlib.sha1).digest()).decode()
        return 'OAuth ' + ', '.join((f'{_escape(k)}="{_escape(v)}"' for k, v in sorted(params.items())))

    def _response(self, method, url, *, headers, params=None, max_bytes=MAX_JSON_BYTES, allow_redirect=False, allow_empty=False):
        permitted = method == 'GET' and url.startswith(API + '/') or (method == 'POST' and url in {OAUTH + '/request', OAUTH + '/access'}) or (method == 'DELETE' and url.startswith(API + '/keys/'))
        if not permitted:
            raise ZoteroSourceError('This Zotero adapter permits only reads and credential lifecycle operations.', 403)
        try:
            with self.http.stream(method, url, headers=headers, params=params, follow_redirects=False) as response:
                if allow_redirect and response.status_code in {301, 302, 303, 307, 308}:
                    return (response.status_code, dict(response.headers), b'')
                if not response.is_success:
                    retry = response.headers.get('Retry-After') or response.headers.get('Backoff', '')
                    raise ZoteroSourceError('Zotero rejected this request; check library access or retry later.', response.status_code, retry_after=int(retry) if retry.isdigit() else None)
                content = bytearray()
                for part in response.iter_bytes():
                    content.extend(part)
                    if len(content) > max_bytes:
                        raise ZoteroSourceError("Zotero content exceeds this read's size limit.", 413)
                if not content and (not allow_empty):
                    raise ZoteroSourceError('Zotero returned an empty response.', 502)
                return (response.status_code, dict(response.headers), bytes(content))
        except httpx.HTTPError:
            raise ZoteroSourceError('Zotero could not be reached.', 502) from None

    def request_token(self) -> dict:
        if not self.client_key or not self.client_secret or (not self.redirect_uri):
            raise ZoteroSourceError('Configure the Zotero OAuth application before connecting.', 503)
        url = OAUTH + '/request'
        _, _, raw = self._response('POST', url, headers={'Authorization': self._oauth_header('POST', url, oauth_callback=self.redirect_uri)})
        data = self._form(raw)
        if data.get('oauth_callback_confirmed') != 'true' or not data.get('oauth_token') or (not data.get('oauth_token_secret')):
            raise ZoteroSourceError('Zotero did not confirm the OAuth callback.', 502)
        return data

    @staticmethod
    def _form(raw):
        try:
            pairs = parse_qsl(raw.decode('utf-8'), strict_parsing=True)
            if len(dict(pairs)) != len(pairs):
                raise ValueError()
            return dict(pairs)
        except (ValueError, UnicodeError):
            raise ZoteroSourceError('Zotero returned an invalid OAuth response.', 502) from None

    def authorization_url(self, request_token: str, *, notes: bool=True, groups: bool=False) -> str:
        if not request_token:
            raise ZoteroSourceError('A Zotero temporary request token is required.')
        return OAUTH + '/authorize?' + urlencode({'oauth_token': request_token, 'name': 'OpenClass', 'library_access': 1, 'notes_access': int(notes), 'write_access': 0, 'all_groups': 'read' if groups else 'none'})

    def exchange_token(self, request_token: str, request_secret: str, verifier: str) -> dict:
        if not all((request_token, request_secret, verifier)):
            raise ZoteroSourceError('The Zotero OAuth callback is incomplete.')
        url = OAUTH + '/access'
        _, _, raw = self._response('POST', url, headers={'Authorization': self._oauth_header('POST', url, token=request_token, secret=request_secret, oauth_verifier=verifier)})
        data = self._form(raw)
        api_key = data.get('oauth_token_secret', '')
        user_id = data.get('userID', '')
        if not api_key or not user_id.isdigit():
            raise ZoteroSourceError('Zotero did not return a key and account identity.', 502)
        access = self.inspect_access(api_key)
        if str(access['user_id']) != user_id:
            raise ZoteroSourceError('Zotero authorization returned a different account identity.', 403)
        return {'api_key': api_key, 'user_id': user_id, 'username': access.get('username', ''), 'grants': access['grants']}

    def _json(self, api_key, path, *, params=None):
        _, headers, raw = self._response('GET', API + path, params=params, headers={'Zotero-API-Key': api_key, 'Zotero-API-Version': '3'})
        try:
            value = json.loads(raw)
        except ValueError:
            raise ZoteroSourceError('Zotero returned invalid library data.', 502) from None
        if not isinstance(value, (dict, list)):
            raise ZoteroSourceError('Zotero returned invalid library data.', 502)
        return (value, raw, headers)

    def inspect_access(self, api_key: str) -> dict:
        if not api_key:
            raise ZoteroSourceError('Connect Zotero before reading a library.', 401)
        data, _, _ = self._json(api_key, '/keys/current')
        grants = data.get('access', {}) if isinstance(data, dict) else {}
        if not isinstance(data, dict) or not str(data.get('userID', '')).isdigit() or (not isinstance(grants, dict)):
            raise ZoteroSourceError('Zotero key identity and permissions could not be verified.', 403)
        user, groups = (grants.get('user', {}), grants.get('groups', {}))
        if not isinstance(user, dict) or not isinstance(groups, dict):
            raise ZoteroSourceError('Zotero returned invalid key permissions.', 502)
        if user.get('write') or any((isinstance(grant, dict) and grant.get('write') for grant in groups.values())):
            raise ZoteroSourceError('Authorize Zotero with read-only permissions before connecting.', 403)
        return {'user_id': str(data['userID']), 'username': data.get('username', ''), 'grants': grants}

    def _library(self, api_key, library_type, library_id, *, notes=False, files=False):
        library_id = str(library_id)
        if library_type not in {'user', 'group'} or not library_id.isdigit() or int(library_id) < 1:
            raise ZoteroSourceError('Select a valid Zotero user or group library.')
        access = self.inspect_access(api_key)
        if library_type == 'user':
            if library_id != access['user_id']:
                raise ZoteroSourceError('The requested Zotero library belongs to another account.', 403)
            grant = access['grants'].get('user', {})
            allowed = grant.get('library') is True and (not notes or grant.get('notes') is True) and (not files or grant.get('files') is True)
        else:
            groups = access['grants'].get('groups', {})
            grant = groups.get(library_id, groups.get('all', {}))
            allowed = isinstance(grant, dict) and grant.get('library') is True
        if not allowed:
            raise ZoteroSourceError('This Zotero key does not grant the requested library, note, or file access.', 403)
        return (f'/{library_type}s/{library_id}', access)

    @staticmethod
    def _pagination(cursor, page_size):
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ZoteroSourceError('Invalid Zotero page size.')
        start = int(cursor) if cursor is not None and str(cursor).isdigit() else 0
        if cursor is not None and str(start) != str(cursor):
            raise ZoteroSourceError('Invalid Zotero pagination cursor.')
        return start

    @staticmethod
    def _page_end(headers, start, count):
        more = 'rel="next"' in headers.get('link', '')
        total_header = headers.get('total-results', '')
        if not total_header.isdigit():
            raise ZoteroSourceError('Zotero did not provide a verifiable result count.', 502)
        total = int(total_header)
        if start + count > total or (more and start + count >= total):
            raise ZoteroSourceError('Zotero returned inconsistent pagination metadata.', 502)
        next_cursor = str(start + count) if more or start + count < total else None
        if next_cursor == str(start):
            raise ZoteroSourceError('Zotero returned an empty page before the end of the result set.', 502)
        return (total, next_cursor)

    def groups(self, api_key: str, *, cursor: str | None=None, page_size: int=50) -> dict:
        access = self.inspect_access(api_key)
        start = self._pagination(cursor, page_size)
        data, _, headers = self._json(api_key, f"/users/{access['user_id']}/groups", params={'limit': page_size, 'start': start})
        if not isinstance(data, list):
            raise ZoteroSourceError('Zotero returned an invalid group list.', 502)
        grants = access['grants'].get('groups', {})
        items = []
        for item in data:
            if not isinstance(item, dict) or not str(item.get('id', '')).isdigit() or (not isinstance(item.get('data'), dict)):
                raise ZoteroSourceError('Zotero returned an invalid group entry.', 502)
            group_id = str(item['id'])
            grant = grants.get(group_id, grants.get('all', {}))
            if not isinstance(grant, dict):
                raise ZoteroSourceError('Zotero returned invalid group permissions.', 502)
            items.append({'id': group_id, 'title': item['data'].get('name', ''), 'readable': grant.get('library') is True})
        total, next_cursor = self._page_end(headers, start, len(data))
        return {'items': items, 'next_cursor': next_cursor, 'complete': start == 0 and total == len(data), 'page_complete': next_cursor is None, 'coverage': {'scope': 'key_visible_groups', 'grants_checked': True, 'total_results': total}}

    def children(self, api_key: str, *, library_type: str='user', library_id, item_key: str, cursor: str | None=None, page_size: int=50) -> dict:
        prefix, access = self._library(api_key, library_type, library_id)
        start = self._pagination(cursor, page_size)
        data, _, headers = self._json(api_key, prefix + '/items/' + _key(item_key) + '/children', params={'start': start, 'limit': page_size})
        if not isinstance(data, list):
            raise ZoteroSourceError('Zotero returned an invalid child item list.', 502)
        allow_notes = library_type == 'group' or access['grants'].get('user', {}).get('notes') is True
        items = []
        for entry in data:
            if not isinstance(entry, dict):
                raise ZoteroSourceError('Zotero returned an invalid child item.', 502)
            fields = self._check_item(entry, _key(entry.get('key')), notes_granted=allow_notes)
            items.append({'id': entry['key'], 'kind': fields.get('itemType'), 'title': fields.get('title', ''), 'provider_version': str(entry['version']), 'library_type': library_type, 'library_id': str(library_id), 'link_mode': fields.get('linkMode'), 'citable': False})
        total, next_cursor = self._page_end(headers, start, len(data))
        return {'items': items, 'next_cursor': next_cursor, 'complete': start == 0 and total == len(data), 'page_complete': next_cursor is None, 'provider_version': headers.get('last-modified-version', ''), 'coverage': {'scope': 'selected_item_children', 'total_results': total, 'snapshot_consistent': False}}

    def search(self, api_key: str, *, library_type: str='user', library_id, query: str='', cursor: str | None=None, collection_key: str | None=None, page_size: int=50, fulltext: bool=False) -> dict:
        prefix, access = self._library(api_key, library_type, library_id)
        if len(query) > 1000:
            raise ZoteroSourceError('Invalid Zotero search size or query.')
        start = self._pagination(cursor, page_size)
        path = prefix + ('/collections/' + _key(collection_key) if collection_key else '') + '/items/top'
        data, _, headers = self._json(api_key, path, params={'q': query, 'qmode': 'everything' if fulltext else 'titleCreatorYear', 'limit': page_size, 'start': start, 'sort': 'title', 'direction': 'asc'})
        if not isinstance(data, list):
            raise ZoteroSourceError('Zotero returned an invalid item list.', 502)
        allow_notes = library_type == 'group' or access['grants'].get('user', {}).get('notes') is True
        items = []
        for item in data:
            if not isinstance(item, dict):
                raise ZoteroSourceError('Zotero returned an invalid item entry.', 502)
            item_key = _key(item.get('key'))
            fields = self._check_item(item, item_key, notes_granted=allow_notes)
            items.append({'id': item_key, 'kind': fields.get('itemType', 'item'), 'title': fields.get('title', ''), 'provider_version': str(item['version']), 'library_type': library_type, 'library_id': str(library_id), 'citable': False})
        total, next_cursor = self._page_end(headers, start, len(data))
        return {'items': items, 'next_cursor': next_cursor, 'complete': start == 0 and total == len(data), 'page_complete': next_cursor is None, 'provider_version': headers.get('last-modified-version', ''), 'coverage': {'search_kind': 'synced_fulltext_index' if fulltext else 'title_creator_year', 'total_results': total, 'returned': len(data), 'start': start, 'snapshot_consistent': False}, 'backoff': headers.get('backoff')}

    @staticmethod
    def _check_item(data, item_key, *, notes_granted=True):
        if not isinstance(data, dict) or data.get('key') != item_key or type(data.get('version')) is not int:
            raise ZoteroSourceError('Zotero returned an invalid item identity or version.', 502)
        fields = data.get('data', {})
        if not isinstance(fields, dict):
            raise ZoteroSourceError('Zotero returned invalid item data.', 502)
        if fields.get('deleted'):
            raise ZoteroSourceError('The Zotero item was deleted.', 404)
        if fields.get('itemType') == 'note' and (not notes_granted):
            raise ZoteroSourceError('This Zotero key does not allow reading notes.', 403)
        return fields

    def read_item(self, api_key: str, *, library_type: str='user', library_id, item_key: str) -> dict:
        item_key = _key(item_key)
        prefix, access = self._library(api_key, library_type, library_id)
        data, raw, _ = self._json(api_key, prefix + '/items/' + item_key)
        fields = self._check_item(data, item_key, notes_granted=library_type == 'group' or access['grants'].get('user', {}).get('notes') is True)
        coverage = {'complete': True, 'scope': 'selected_item_metadata', 'attachments_read': False, 'children_read': False, 'missing': []}
        if fields.get('itemType') == 'note':
            coverage['scope'] = 'selected_note'
            if '<img' in str(fields.get('note', '')).lower():
                coverage.update(complete=False, missing=[{'reason': 'embedded_note_image_not_read'}])
        return self._snapshot(raw, f'zotero-{item_key}.json', 'application/json', library_type, library_id, item_key, data['version'], coverage, 'api_json_snapshot')

    @staticmethod
    def _snapshot(content, name, mime, library_type, library_id, item_key, version, coverage, kind):
        digest = hashlib.sha256(content).hexdigest()
        identity = {'library_type': library_type, 'library_id': str(library_id), 'item_key': item_key}
        return {'content': content, 'file_name': name, 'mime_type': mime, 'sha256': digest, 'identity': identity, 'provider_version': str(version), 'coverage': coverage, 'source_snapshot_kind': kind, 'origin_metadata': {'origin': 'zotero', 'provider': 'zotero', **identity, 'provider_version': str(version), 'snapshot_sha256': digest, 'source_snapshot_kind': kind, 'api_version': '3', 'coverage': coverage}}

    def download_attachment(self, api_key: str, *, library_type: str='user', library_id, item_key: str, max_bytes: int=32 * 1024 * 1024) -> dict:
        item_key = _key(item_key)
        prefix, _ = self._library(api_key, library_type, library_id, files=True)
        path = prefix + '/items/' + item_key
        before, _, _ = self._json(api_key, path)
        fields = self._check_item(before, item_key)
        md5 = str(fields.get('md5', '')).lower()
        if fields.get('itemType') != 'attachment' or fields.get('linkMode') not in {'imported_file', 'imported_url'} or (not re.fullmatch('[a-f0-9]{32}', md5)):
            raise ZoteroSourceError('This attachment is linked locally, external, or has no verifiable cloud file.', 415)
        status, headers, content = self._response('GET', API + path + '/file', max_bytes=max_bytes, headers={'Zotero-API-Key': api_key, 'Zotero-API-Version': '3'}, allow_redirect=True)
        mime = headers.get('content-type', fields.get('contentType', 'application/octet-stream')).split(';', 1)[0]
        if status in {301, 302, 303, 307, 308}:
            url = headers.get('location', '')
            if not url.startswith('https://'):
                raise ZoteroSourceError('Zotero did not return a secure cloud download URL.', 502)
            try:
                content, mime, _ = self.file_fetch(url)
            except Exception:
                raise ZoteroSourceError('The attachment is not accessible from Zotero cloud storage.', 502) from None
        if not content or len(content) > max_bytes:
            raise ZoteroSourceError('The Zotero attachment is empty or exceeds the download limit.', 413)
        if hashlib.md5(content).hexdigest() != md5:
            raise ZoteroSourceError('The downloaded file is not the current attachment; it may exist only on WebDAV.', 409)
        after, _, _ = self._json(api_key, path)
        self._check_item(after, item_key)
        if before['version'] != after['version'] or after['data'].get('md5', '').lower() != md5:
            raise ZoteroSourceError('The Zotero attachment changed during download.', 409)
        self._library(api_key, library_type, library_id, files=True)
        name = str(fields.get('filename') or f'zotero-{item_key}').replace('\\', '/').rsplit('/', 1)[-1]
        if name in {'', '.', '..'}:
            name = f'zotero-{item_key}'
        return self._snapshot(content, name, mime, library_type, library_id, item_key, before['version'], {'complete': True, 'scope': 'selected_attachment'}, 'original_file')

    def revoke(self, api_key: str) -> None:
        self._response('DELETE', API + '/keys/' + quote(api_key, safe=''), headers={'Zotero-API-Key': api_key, 'Zotero-API-Version': '3'}, allow_empty=True)
