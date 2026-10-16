"""Read-only Drive operations; drive.file itself also grants upstream write permission."""
from __future__ import annotations
from urllib.parse import quote, urlencode
import httpx
import hashlib
from openclass_core.services.google_drive_sources import DriveConfig, DriveSourceError
EXPORT_FORMATS = {'application/vnd.google-apps.document': ('application/vnd.openxmlformats-officedocument.wordprocessingml.document', '.docx'), 'application/vnd.google-apps.presentation': ('application/vnd.openxmlformats-officedocument.presentationml.presentation', '.pptx'), 'application/vnd.google-apps.spreadsheet': ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', '.xlsx')}
DRIVE_FILE_SCOPE = 'https://www.googleapis.com/auth/drive.file'
DRIVE_READONLY_SCOPE = 'https://www.googleapis.com/auth/drive.readonly'
DRIVE_SCOPES = 'openid email ' + DRIVE_FILE_SCOPE
DRIVE_READONLY_SCOPES = 'openid email ' + DRIVE_READONLY_SCOPE

class GoogleDriveClient:

    def __init__(self, config: DriveConfig, http: httpx.Client | None=None):
        self.config = config
        self.http = http or httpx.Client(timeout=httpx.Timeout(60, connect=15), follow_redirects=False)

    def authorization_url(self, state: str, *, access_mode: str='selected_files') -> str:
        if access_mode not in {'selected_files', 'readonly'}:
            raise DriveSourceError('Unknown Drive access mode.')
        return 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({'client_id': self.config.client_id, 'redirect_uri': self.config.redirect_uri, 'response_type': 'code', 'scope': DRIVE_READONLY_SCOPES if access_mode == 'readonly' else DRIVE_SCOPES, 'access_type': 'offline', 'prompt': 'consent select_account', 'include_granted_scopes': 'false', 'state': state})

    @staticmethod
    def _checked(response: httpx.Response) -> httpx.Response:
        if response.is_success:
            return response
        if response.status_code in {401, 403, 404}:
            raise DriveSourceError('Google Drive access is unavailable; reconnect or choose an accessible file.', response.status_code)
        if response.status_code == 429 or response.status_code >= 500:
            raise DriveSourceError('Google Drive is temporarily unavailable; retry later.', 503)
        raise DriveSourceError('Google Drive could not process this request (possibly an export size limit).', 422)

    def exchange_code(self, code: str, *, access_mode: str='selected_files') -> dict:
        if access_mode not in {'selected_files', 'readonly'}:
            raise DriveSourceError('Unknown Drive access mode.')
        response = self._checked(self.http.post('https://oauth2.googleapis.com/token', data={'code': code, 'client_id': self.config.client_id, 'client_secret': self.config.client_secret, 'redirect_uri': self.config.redirect_uri, 'grant_type': 'authorization_code'}))
        payload = response.json()
        if not payload.get('access_token'):
            raise DriveSourceError('Google did not grant Drive access. Reconnect and grant the requested permission.', 403)
        scopes = set(str(payload.get('scope', '')).split())
        required = DRIVE_READONLY_SCOPE if access_mode == 'readonly' else DRIVE_FILE_SCOPE
        if required not in scopes:
            raise DriveSourceError('The requested Drive permission was not granted.', 403)
        return payload

    def refresh(self, credentials: dict) -> str:
        response = self.http.post('https://oauth2.googleapis.com/token', data={'refresh_token': credentials.get('refresh_token', ''), 'client_id': self.config.client_id, 'client_secret': self.config.client_secret, 'grant_type': 'refresh_token'})
        if response.status_code == 400:
            raise DriveSourceError('Google Drive authorization expired. Reconnect the account.', 401)
        payload = self._checked(response).json()
        if not payload.get('access_token'):
            raise DriveSourceError('Google Drive did not return an access token.', 503)
        return str(payload['access_token'])

    def profile(self, token: str) -> dict:
        payload = self._checked(self.http.get('https://openidconnect.googleapis.com/v1/userinfo', headers={'Authorization': 'Bearer ' + token})).json()
        if not payload.get('sub'):
            raise DriveSourceError('Google account identity could not be verified.', 403)
        return payload

    def _get(self, token: str, path: str, params: dict | None=None) -> dict:
        return self._checked(self.http.get('https://www.googleapis.com/drive/v3/' + path, headers={'Authorization': 'Bearer ' + token}, params=params)).json()

    def metadata(self, token: str, file_id: str) -> dict:
        return self._get(token, 'files/' + quote(file_id, safe=''), {'fields': 'id,name,mimeType,version,modifiedTime,trashed,size,parents,driveId,webViewLink,capabilities(canDownload)', 'supportsAllDrives': 'true'})

    def search_files(self, token: str, query: str='', *, folder_id: str | None=None, drive_id: str | None=None, page_token: str='', page_size: int=50) -> dict:
        """A metadata page only; folder_id denotes direct children, not an OAuth boundary."""

        def literal(value):
            return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"
        if len(query) > 1000 or len(page_token) > 8192 or (not 1 <= page_size <= 100):
            raise DriveSourceError('Invalid Drive search parameters.')
        terms = ['trashed = false']
        if query:
            terms.append('fullText contains ' + literal(query))
        if folder_id:
            terms.append(literal(folder_id) + ' in parents')
        params = {'q': ' and '.join(terms), 'pageSize': page_size, 'fields': 'nextPageToken,incompleteSearch,files(id,name,mimeType,version,modifiedTime,size,parents,driveId,webViewLink)', 'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true'}
        if page_token:
            params['pageToken'] = page_token
        if drive_id:
            params.update({'corpora': 'drive', 'driveId': drive_id})
        else:
            params['corpora'] = 'user'
        return self._get(token, 'files', params)

    def within_folder(self, token: str, file_id: str, folder_id: str) -> bool:
        """Recheck a bounded ancestry graph; selected folders are application scope, not OAuth scope."""
        folder = self.metadata(token, folder_id)
        if folder.get('id') != folder_id or folder.get('trashed') or folder.get('mimeType') != 'application/vnd.google-apps.folder':
            return False
        pending, visited = ([(file_id, 0)], set())
        while pending:
            current_id, depth = pending.pop()
            if depth > 32 or len(visited) >= 128:
                return False
            if current_id in visited:
                continue
            visited.add(current_id)
            if current_id == folder_id:
                return True
            current = self.metadata(token, current_id)
            if current.get('id') != current_id or current.get('trashed'):
                return False
            parents = current.get('parents', [])
            if not isinstance(parents, list) or any((not isinstance(value, str) or not value for value in parents)):
                return False
            pending.extend(((value, depth + 1) for value in parents))
        return False

    def snapshot(self, token: str, file_id: str) -> dict:
        metadata = self.metadata(token, file_id)
        version = str(metadata.get('version') or '')
        if not version:
            raise DriveSourceError('Google Drive returned no stable file version.', 503)
        content, name, mime = self.download(token, metadata)
        after = self.metadata(token, file_id)
        if str(after.get('version') or '') != version or after.get('trashed') or after.get('capabilities', {}).get('canDownload') is False:
            raise DriveSourceError('The Drive document changed during download; retry.', 409)
        return {'content': content, 'file_name': name, 'mime_type': mime, 'content_hash': hashlib.sha256(content).hexdigest(), 'provider_version': version, 'origin_mime_type': metadata.get('mimeType', ''), 'metadata': after, 'source_snapshot_kind': 'workspace_export' if metadata.get('mimeType') in EXPORT_FORMATS else 'original_file'}

    def revoke_project_authorization(self, token: str) -> None:
        """Explicit whole-account unlink only: Google revokes every capability in this project."""
        self._checked(self.http.post('https://oauth2.googleapis.com/revoke', data={'token': token}))

    def download(self, token: str, metadata: dict) -> tuple[bytes, str, str]:
        mime = str(metadata.get('mimeType', ''))
        if metadata.get('trashed') or metadata.get('capabilities', {}).get('canDownload') is False:
            raise DriveSourceError('This Drive file is unavailable for download.', 403)
        if mime.startswith('video/'):
            raise DriveSourceError('Video sources are not supported by this integration.', 415)
        if mime.startswith('application/vnd.google-apps.') and mime not in EXPORT_FORMATS:
            raise DriveSourceError('Choose a document, slide deck, spreadsheet or supported file, not a Drive folder or shortcut.', 415)
        if int(metadata.get('size', '0')) > self.config.max_file_bytes:
            raise DriveSourceError('The selected Drive file exceeds the configured upload limit.', 413)
        name = str(metadata.get('name', 'Drive source'))
        path = 'files/' + quote(str(metadata['id']), safe='')
        if mime in EXPORT_FORMATS:
            mime, extension = EXPORT_FORMATS[mime]
            if not name.lower().endswith(extension):
                name += extension
            path += '/export'
            params = {'mimeType': mime}
        else:
            params = {'alt': 'media', 'supportsAllDrives': 'true'}
        data = bytearray()
        with self.http.stream('GET', 'https://www.googleapis.com/drive/v3/' + path, headers={'Authorization': 'Bearer ' + token}, params=params) as response:
            self._checked(response)
            for part in response.iter_bytes():
                data.extend(part)
                if len(data) > self.config.max_file_bytes:
                    raise DriveSourceError('The Drive export exceeds the configured upload limit; upload the file manually.', 413)
        if not data:
            raise DriveSourceError('Google Drive returned an empty file.', 422)
        return (bytes(data), name, mime)

    def start_cursor(self, token: str) -> str:
        return str(self._get(token, 'changes/startPageToken', {'supportsAllDrives': 'true'})['startPageToken'])

    def changes(self, token: str, cursor: str) -> tuple[list[dict], str]:
        all_changes: list[dict] = []
        while True:
            payload = self._get(token, 'changes', {'pageToken': cursor, 'includeRemoved': 'true', 'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true', 'pageSize': 1000, 'fields': 'nextPageToken,newStartPageToken,changes(fileId,removed,file(id,version,trashed))'})
            all_changes.extend(payload.get('changes', []))
            if payload.get('nextPageToken'):
                cursor = str(payload['nextPageToken'])
            else:
                return (all_changes, str(payload.get('newStartPageToken') or cursor))
