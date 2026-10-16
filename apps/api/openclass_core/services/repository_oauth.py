"""OAuth clients for repository connections; no OpenClass sign-in or account mutation."""
from __future__ import annotations
import os
import time
from urllib.parse import urlencode
import httpx
from openclass_core.services.github_user_client import GitHubUserClient
from openclass_core.services.gitlab_repository import GitLabRepositoryAdapter
from openclass_core.services.repository_connection_http import RepositoryAccessError, RepositoryReadClient

class RepositoryOAuthClient:
    provider: str
    origin: str
    authorization_path: str
    token_path: str
    env_prefix: str

    def __init__(self, *, client_id: str, client_secret: str, redirect_uri: str, http: httpx.Client | None=None):
        self.client_id, self.client_secret, self.redirect_uri = (client_id, client_secret, redirect_uri)
        self.http = http or httpx.Client(timeout=30, follow_redirects=False)

    @classmethod
    def from_env(cls, redirect_uri: str, *, http: httpx.Client | None=None):
        return cls(client_id=os.getenv(cls.env_prefix + 'CLIENT_ID', ''), client_secret=os.getenv(cls.env_prefix + 'CLIENT_SECRET', ''), redirect_uri=redirect_uri, http=http)

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        if not self.configured or not state or (not code_challenge):
            raise RepositoryAccessError('Repository account authorization is not configured.', 503)
        params = {'client_id': self.client_id, 'redirect_uri': self.redirect_uri, 'state': state, 'response_type': 'code', 'code_challenge': code_challenge, 'code_challenge_method': 'S256'}
        if self.provider == 'github':
            params['prompt'] = 'select_account'
        else:
            params['scope'] = 'read_api'
        return self.origin + self.authorization_path + '?' + urlencode(params)

    def _exchange(self, data: dict) -> dict:
        if not self.configured:
            raise RepositoryAccessError('Repository account authorization is not configured.', 503)
        try:
            response = self.http.post(self.origin + self.token_path, data={'client_id': self.client_id, 'client_secret': self.client_secret, **data}, headers={'Accept': 'application/json'}, follow_redirects=False)
            if response.status_code in {400, 401}:
                raise RepositoryAccessError('Repository authorization expired or was refused. Reconnect the account.', 401)
            payload = RepositoryReadClient.checked(response).json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RepositoryAccessError('The repository authorization provider is unavailable.', 502) from exc
        if not isinstance(payload, dict) or payload.get('error') or (not payload.get('access_token')):
            raise RepositoryAccessError('The repository provider did not grant account access.', 401)
        if self.provider == 'gitlab':
            granted = payload.get('scope')
            if granted is None:
                try:
                    info_response = self.http.get(self.origin + '/oauth/token/info', headers={'Authorization': f"Bearer {payload['access_token']}", 'Accept': 'application/json'}, follow_redirects=False)
                    info = RepositoryReadClient.checked(info_response).json()
                except (httpx.HTTPError, ValueError) as exc:
                    raise RepositoryAccessError('GitLab token permissions could not be verified.', 502) from exc
                if not isinstance(info, dict):
                    raise RepositoryAccessError('GitLab token permissions could not be verified.', 502)
                granted = info.get('scope')
            scopes = set(granted.split()) if isinstance(granted, str) else set(granted) if isinstance(granted, list) and all((isinstance(item, str) for item in granted)) else set()
            if 'read_api' not in scopes or scopes - {'read_api', 'read_user'}:
                raise RepositoryAccessError('GitLab read-only API permission was not granted.', 403)
            payload['scope'] = ' '.join(sorted(scopes))
        if self.provider == 'github' and (not str(payload['access_token']).startswith('ghu_')):
            raise RepositoryAccessError('A GitHub App user token is required for repository access.', 403)
        safe = {key: payload[key] for key in ('access_token', 'refresh_token', 'expires_in', 'refresh_token_expires_in', 'scope', 'token_type') if key in payload}
        if payload.get('expires_in'):
            safe['expires_at'] = time.time() + int(payload['expires_in'])
        return safe

    def exchange_code(self, code: str, code_verifier: str) -> dict:
        if not code or not code_verifier:
            raise RepositoryAccessError('The repository authorization response is incomplete.', 400)
        return self._exchange({'code': code, 'code_verifier': code_verifier, 'redirect_uri': self.redirect_uri, 'grant_type': 'authorization_code'})

    def refresh(self, credentials: dict) -> dict:
        if not credentials.get('refresh_token'):
            raise RepositoryAccessError('Reconnect the repository account to renew access.', 401)
        return {**credentials, **self._exchange({'refresh_token': credentials['refresh_token'], 'grant_type': 'refresh_token'})}

    def identity(self, token: str) -> dict:
        adapter = GitHubUserClient if self.provider == 'github' else GitLabRepositoryAdapter
        return adapter(token, http=self.http).identity()

class GitHubOAuthClient(RepositoryOAuthClient):
    provider = 'github'
    origin = 'https://github.com'
    authorization_path = '/login/oauth/authorize'
    token_path = '/login/oauth/access_token'
    env_prefix = 'OPENCLASS_GITHUB_APP_'

class GitLabOAuthClient(RepositoryOAuthClient):
    provider = 'gitlab'
    origin = 'https://gitlab.com'
    authorization_path = '/oauth/authorize'
    token_path = '/oauth/token'
    env_prefix = 'OPENCLASS_GITLAB_'
