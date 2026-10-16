"""Read-only Google and Microsoft resource consent, separate from sign-in OAuth."""
from __future__ import annotations
from dataclasses import dataclass
import os
from pathlib import Path
import time
from urllib.parse import urlencode
import httpx
from openclass_core.services.external_connections import ExternalConnectionError
DRIVE_SELECTED_SCOPE = 'https://www.googleapis.com/auth/drive.file'
DRIVE_ALL_SCOPE = 'https://www.googleapis.com/auth/drive.readonly'
CALENDAR_SCOPES = ('https://www.googleapis.com/auth/calendar.calendarlist.readonly', 'https://www.googleapis.com/auth/calendar.events.readonly')

def _secret(file_name: str, direct_name: str='') -> str:
    path = os.getenv(file_name, '').strip()
    if path:
        try:
            return Path(path).expanduser().read_text().strip()
        except OSError as exc:
            raise ExternalConnectionError('External authorization credentials are unavailable.', 503) from exc
    return os.getenv(direct_name, '').strip() if direct_name else ''

@dataclass(frozen=True)
class ResourceOAuthConfig:
    provider: str
    capability: str
    client_id: str
    client_secret: str
    redirect_uri: str
    authorization_endpoint: str
    token_endpoint: str
    profile_endpoint: str
    scopes: tuple[str, ...]

def resource_oauth_config(provider: str, capability: str, redirect_uri: str) -> ResourceOAuthConfig:
    if provider == 'google':
        if capability == 'drive_selected':
            scopes = ('openid', 'email', DRIVE_SELECTED_SCOPE)
        elif capability == 'drive_all':
            scopes = ('openid', 'email', DRIVE_ALL_SCOPE)
        elif capability == 'calendar':
            scopes = ('openid', 'email', *CALENDAR_SCOPES)
        else:
            raise ExternalConnectionError('Unsupported Google account capability.', 422)
        data_id = os.getenv('OPENCLASS_GOOGLE_DRIVE_CLIENT_ID', '').strip()
        if data_id:
            client_id = data_id
            client_secret = _secret('OPENCLASS_GOOGLE_DRIVE_CLIENT_SECRET_FILE')
        else:
            client_id = os.getenv('OPENCLASS_OAUTH_GOOGLE_CLIENT_ID', '').strip()
            client_secret = os.getenv('OPENCLASS_OAUTH_GOOGLE_CLIENT_SECRET', '').strip()
        return ResourceOAuthConfig(provider, capability, client_id, client_secret, redirect_uri, 'https://accounts.google.com/o/oauth2/v2/auth', 'https://oauth2.googleapis.com/token', 'https://openidconnect.googleapis.com/v1/userinfo', scopes)
    if provider == 'microsoft':
        if capability == 'files_personal':
            scopes = ('openid', 'email', 'profile', 'offline_access', 'Files.Read')
        elif capability == 'files_work':
            scopes = ('openid', 'email', 'profile', 'offline_access', 'Files.Read.All')
        elif capability == 'sharepoint':
            scopes = ('openid', 'email', 'profile', 'offline_access', 'Files.Read.All', 'Sites.Read.All')
        else:
            raise ExternalConnectionError('Unsupported Microsoft account capability.', 422)
        return ResourceOAuthConfig(provider, capability, os.getenv('OPENCLASS_MICROSOFT_CONNECTION_CLIENT_ID', '').strip() or os.getenv('OPENCLASS_OAUTH_MICROSOFT_CLIENT_ID', '').strip(), _secret('OPENCLASS_MICROSOFT_CONNECTION_CLIENT_SECRET_FILE', 'OPENCLASS_OAUTH_MICROSOFT_CLIENT_SECRET'), redirect_uri, 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize', 'https://login.microsoftonline.com/common/oauth2/v2.0/token', 'https://graph.microsoft.com/oidc/userinfo', scopes)
    raise ExternalConnectionError('This account provider is not supported.', 404)

class ResourceOAuthClient:

    def __init__(self, config: ResourceOAuthConfig, http: httpx.Client | None=None):
        self.config = config
        self.http = http or httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=False)

    @property
    def configured(self) -> bool:
        return bool(self.config.client_id and self.config.client_secret and self.config.redirect_uri)

    def authorization_url(self, state: str, code_challenge: str) -> str:
        if not self.configured:
            raise ExternalConnectionError('External account authorization is not configured.', 503)
        params = {'client_id': self.config.client_id, 'redirect_uri': self.config.redirect_uri, 'response_type': 'code', 'scope': ' '.join(self.config.scopes), 'state': state, 'code_challenge': code_challenge, 'code_challenge_method': 'S256', 'prompt': 'select_account'}
        if self.config.provider == 'google':
            params.update({'access_type': 'offline', 'prompt': 'consent select_account', 'include_granted_scopes': 'false'})
        return self.config.authorization_endpoint + '?' + urlencode(params)

    def _token(self, data: dict) -> dict:
        if not self.configured:
            raise ExternalConnectionError('External account authorization is not configured.', 503)
        try:
            response = self.http.post(self.config.token_endpoint, data={'client_id': self.config.client_id, 'client_secret': self.config.client_secret, **data})
        except httpx.RequestError as exc:
            raise ExternalConnectionError('External authorization service is unavailable.', 503) from exc
        if response.status_code in {400, 401, 403}:
            raise ExternalConnectionError('Account authorization expired or was refused.', 401)
        if not response.is_success:
            raise ExternalConnectionError('External authorization service is unavailable.', 502)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExternalConnectionError('External authorization response is invalid.', 502) from exc
        if not isinstance(payload, dict) or not payload.get('access_token'):
            raise ExternalConnectionError('External authorization did not grant access.', 403)
        return payload

    def exchange_code(self, code: str, code_verifier: str) -> dict:
        payload = self._token({'grant_type': 'authorization_code', 'code': code, 'code_verifier': code_verifier, 'redirect_uri': self.config.redirect_uri})
        granted = set(str(payload.get('scope', '')).split())
        required = {scope for scope in self.config.scopes if scope not in {'openid', 'email', 'profile', 'offline_access'}}
        if not required.issubset(granted):
            raise ExternalConnectionError('The requested read permission was not granted.', 403)
        if not payload.get('refresh_token'):
            raise ExternalConnectionError('Offline access was not granted; reconnect the account.', 403)
        return {'access_token': payload['access_token'], 'refresh_token': payload['refresh_token'], 'scope': payload.get('scope', ''), 'expires_at': time.time() + int(payload.get('expires_in') or 3600)}

    def refresh(self, credentials: dict) -> dict:
        if not credentials.get('refresh_token'):
            raise ExternalConnectionError('Reconnect this account to renew access.', 401)
        payload = self._token({'grant_type': 'refresh_token', 'refresh_token': credentials['refresh_token']})
        granted = set(str(payload.get('scope') or credentials.get('scope') or '').split())
        required = {scope for scope in self.config.scopes if scope not in {'openid', 'email', 'profile', 'offline_access'}}
        if not required.issubset(granted):
            raise ExternalConnectionError('The connected account no longer grants this read permission.', 403)
        return {**credentials, 'access_token': payload['access_token'], 'refresh_token': payload.get('refresh_token') or credentials['refresh_token'], 'scope': payload.get('scope') or credentials.get('scope', ''), 'expires_at': time.time() + int(payload.get('expires_in') or 3600)}

    def profile(self, token: str) -> dict:
        try:
            response = self.http.get(self.config.profile_endpoint, headers={'Authorization': 'Bearer ' + token})
        except httpx.RequestError as exc:
            raise ExternalConnectionError('Account identity verification is unavailable.', 503) from exc
        if response.status_code in {401, 403}:
            raise ExternalConnectionError('Account identity could not be verified.', 403)
        if not response.is_success:
            raise ExternalConnectionError('Account identity verification is unavailable.', 502)
        try:
            profile = response.json()
        except ValueError as exc:
            raise ExternalConnectionError('Account identity is invalid.', 502) from exc
        if not isinstance(profile, dict) or not profile.get('sub'):
            raise ExternalConnectionError('Account identity is incomplete.', 403)
        return profile
