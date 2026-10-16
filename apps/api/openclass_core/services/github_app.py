from __future__ import annotations
import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode
import httpx
from openclass_core.models import GitHubConnectionView, GitHubInstallationView, GitHubRepositoryView, now_iso
from openclass_core.services.ai_logging import ai_usage_logger
from openclass_core.services.repository_store import RepositoryStore, repository_store
from openclass_core.services.github_connection_access import GitHubConnectionAccess
from openclass_core.services.repository_connection_http import RepositoryAccessError
from openclass_core.services.external_connections import ExternalConnectionError
GITHUB_API = 'https://api.github.com'
GITHUB_API_VERSION = '2026-03-10'

class GitHubAppError(RuntimeError):
    pass

@dataclass(frozen=True)
class GitHubInstallationToken:
    value: str
    expires_at: float

class GitHubAppService:

    def __init__(self, *, store: RepositoryStore=repository_store, connections: GitHubConnectionAccess | None=None) -> None:
        self.store = store
        self.connections = connections or GitHubConnectionAccess()
        self._tokens: dict[int, GitHubInstallationToken] = {}
        self._token_lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return os.getenv('OPENCLASS_GITHUB_SOURCE_ENABLED', '1').strip().lower() not in {'0', 'false', 'off'}

    @property
    def configured(self) -> bool:
        return bool(self.slug and self.app_id and self.private_key)

    @property
    def slug(self) -> str:
        return os.getenv('OPENCLASS_GITHUB_APP_SLUG', '').strip()

    @property
    def app_id(self) -> str:
        return os.getenv('OPENCLASS_GITHUB_APP_ID', '').strip()

    @property
    def private_key(self) -> str:
        raw = os.getenv('OPENCLASS_GITHUB_APP_PRIVATE_KEY', '')
        return raw.replace('\\n', '\n').strip()

    def status(self, owner_user_id: str) -> GitHubConnectionView:
        installations = self.store.list_installations(owner_user_id)
        connected = self.configured and any((item['status'] == 'connected' for item in self.connections.connections(owner_user_id)))
        if not connected:
            installations = [item.model_copy(update={'status': 'disconnected'}) for item in installations]
        if not self.enabled:
            message = 'GitHub repository sources are disabled.'
        elif not self.configured:
            message = 'GitHub App is not configured; public repository URLs remain available.'
        elif connected:
            message = 'GitHub repositories are connected.'
        else:
            message = 'Connect GitHub to import private repositories.'
        return GitHubConnectionView(enabled=self.enabled, configured=self.configured, connected=connected, installations=installations, message=message)

    def start_install(self, *, owner_user_id: str, next_path: str='/studio', connection_id: str | None=None) -> str:
        if not self.enabled or not self.configured:
            raise GitHubAppError('GitHub App is not configured.')
        try:
            connection, _client = self.connections.client(owner_user_id, connection_id)
        except (RepositoryAccessError, ExternalConnectionError) as exc:
            raise GitHubAppError(str(exc)) from exc
        state = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        self.store.create_connection_state(state=state, owner_user_id=owner_user_id, next_path=next_path if next_path.startswith('/') else '/studio', created_at=now.isoformat(), expires_at=(now + timedelta(minutes=20)).isoformat(), connection_id=connection['id'])
        return f"https://github.com/apps/{quote(self.slug, safe='')}/installations/new?{urlencode({'state': state})}"

    def complete_install(self, *, state: str, installation_id: int, owner_user_id: str) -> tuple[str, GitHubInstallationView]:
        state_row = self.store.consume_connection_state(state, now=now_iso())
        if state_row is None or str(state_row['owner_user_id']) != owner_user_id or (not state_row['connection_id']):
            raise GitHubAppError('GitHub connection state is invalid or expired.')
        try:
            _connection, client = self.connections.client(owner_user_id, str(state_row['connection_id']))
            raw = client.installation(installation_id)
        except (RepositoryAccessError, ExternalConnectionError) as exc:
            raise GitHubAppError(str(exc)) from exc
        account = raw.get('account') if isinstance(raw.get('account'), dict) else {}
        permissions = raw.get('permissions') if isinstance(raw.get('permissions'), dict) else {}
        if permissions.get('contents') != 'read' or any((value != 'read' for value in permissions.values())):
            raise GitHubAppError('GitHub App installation does not have the required read-only permissions.')
        installation = GitHubInstallationView(installation_id=installation_id, account_id=_int_or_none(account.get('id')), account_login=str(account.get('login') or ''), account_type=str(account.get('type') or ''), repository_selection=str(raw.get('repository_selection') or 'selected'), status='suspended' if raw.get('suspended_at') else 'connected', permissions={str(key): str(value) for key, value in permissions.items()}, updated_at=now_iso())
        try:
            _connection, _client, repositories = self.connections.repositories(owner_user_id, str(state_row['connection_id']))
            installation.repository_count = sum((item['installation_id'] == installation_id for item in repositories))
        except (RepositoryAccessError, ExternalConnectionError) as exc:
            raise GitHubAppError(str(exc)) from exc
        self.store.save_installation(owner_user_id=str(state_row['owner_user_id']), installation=installation)
        ai_usage_logger.log_event('github_app_connection_completed', owner_user_id=str(state_row['owner_user_id']), installation_id=installation_id, account_type=installation.account_type, repository_selection=installation.repository_selection, repository_count=installation.repository_count)
        return (str(state_row['next_path']), installation)

    def cancel_install(self, *, state: str, owner_user_id: str) -> str:
        state_row = self.store.consume_connection_state(state, now=now_iso())
        if state_row is None or str(state_row['owner_user_id']) != owner_user_id:
            raise GitHubAppError('GitHub connection state is invalid or expired.')
        return str(state_row['next_path'])

    def repositories(self, owner_user_id: str, connection_id: str | None=None) -> list[GitHubRepositoryView]:
        try:
            _connection, _client, rows = self.connections.repositories(owner_user_id, connection_id)
        except (RepositoryAccessError, ExternalConnectionError) as exc:
            raise GitHubAppError(str(exc)) from exc
        result = [_repository_view(row).model_copy(update={'installation_id': row['installation_id']}) for row in rows]
        return sorted(result, key=lambda item: item.full_name.casefold())

    def token_for_repository(self, *, owner_user_id: str, owner: str, name: str, connection_id: str | None=None) -> str | None:
        target = f'{owner}/{name}'.casefold()
        try:
            if not self.connections.connections(owner_user_id):
                if self.store.list_installations(owner_user_id):
                    raise GitHubAppError("Reconnect GitHub to verify the repository user's access.")
                return None
            _connection, client, repositories = self.connections.repositories(owner_user_id, connection_id)
            if any((str(item.get('full_name', '')).casefold() == target for item in repositories)):
                return client._token
        except (RepositoryAccessError, ExternalConnectionError) as exc:
            raise GitHubAppError(str(exc)) from exc
        raise GitHubAppError('The selected GitHub account and installation do not grant access to this repository.')

    def assert_user_token_active(self, owner_user_id: str, token: str, repository_id: int | None=None) -> None:
        for connection in self.connections.connections(owner_user_id):
            if connection['status'] != 'connected':
                continue
            credentials = self.connections.store.get_credentials(owner_user_id, connection['id'], 'github')
            if secrets.compare_digest(str(credentials.get('access_token') or ''), token):
                scope = connection['resource_scope']
                if repository_id is not None and scope.get('mode') == 'selected' and (str(repository_id) not in {str(value) for value in scope.get('resource_ids', [])}):
                    continue
                try:
                    current, client, repositories = self.connections.repositories(owner_user_id, connection['id'])
                except (RepositoryAccessError, ExternalConnectionError) as exc:
                    raise GitHubAppError(str(exc)) from exc
                if current['generation'] != connection['generation'] or not secrets.compare_digest(client._token, token):
                    break
                if repository_id is not None and (not any((int(item['id']) == repository_id for item in repositories))):
                    break
                return
        raise GitHubAppError('GitHub account access changed during this repository request.')

    def installation_token(self, installation_id: int) -> str:
        with self._token_lock:
            cached = self._tokens.get(installation_id)
            if cached is not None and cached.expires_at > time.time() + 60:
                return cached.value
        raw = self._request_app('POST', f'/app/installations/{installation_id}/access_tokens')
        token = str(raw.get('token') or '')
        expires_raw = str(raw.get('expires_at') or '')
        if not token:
            raise GitHubAppError('GitHub did not return an installation token.')
        try:
            expires_at = datetime.fromisoformat(expires_raw.replace('Z', '+00:00')).timestamp()
        except ValueError:
            expires_at = time.time() + 3000
        with self._token_lock:
            self._tokens[installation_id] = GitHubInstallationToken(value=token, expires_at=expires_at)
        return token

    def disconnect(self, owner_user_id: str, connection_id: str | None=None) -> None:
        try:
            self.connections.disconnect(owner_user_id, connection_id)
        except (RepositoryAccessError, ExternalConnectionError) as exc:
            raise GitHubAppError(str(exc)) from exc
        ai_usage_logger.log_event('github_app_connection_disconnected', owner_user_id=owner_user_id)

    def verify_webhook(self, body: bytes, signature: str) -> None:
        secret = os.getenv('OPENCLASS_GITHUB_APP_WEBHOOK_SECRET', '').encode('utf-8')
        if not secret:
            raise GitHubAppError('GitHub webhook secret is not configured.')
        expected = 'sha256=' + hmac.new(secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise GitHubAppError('GitHub webhook signature is invalid.')

    def handle_webhook(self, *, event: str, payload: dict[str, Any]) -> None:
        if event == 'github_app_authorization' and payload.get('action') == 'revoked':
            sender = payload.get('sender')
            if isinstance(sender, dict) and sender.get('id'):
                self.connections.invalidate(str(sender['id']))
            return
        installation_raw = payload.get('installation')
        installation = installation_raw if isinstance(installation_raw, dict) else {}
        installation_id = _int_or_none(installation.get('id'))
        if installation_id is None:
            return
        action = str(payload.get('action') or '')
        if event == 'installation' and action in {'deleted', 'unsuspend', 'suspend'}:
            status = 'revoked' if action == 'deleted' else 'connected' if action == 'unsuspend' else 'suspended'
            self.store.set_installation_status(installation_id=installation_id, status=status)
            with self._token_lock:
                self._tokens.pop(installation_id, None)
        elif event == 'installation_repositories' and action == 'removed':
            with self._token_lock:
                self._tokens.pop(installation_id, None)
        if event in {'installation', 'installation_repositories'}:
            for owner in self.store.owners_for_installation(installation_id):
                for connection in self.connections.connections(owner):
                    if connection['status'] == 'connected':
                        self.connections.store.update_connection(owner, connection['id'], connection['revision'])

    def _list_repositories_for_installation(self, installation_id: int) -> list[GitHubRepositoryView]:
        token = self.installation_token(installation_id)
        page = 1
        result: list[GitHubRepositoryView] = []
        while page <= 100:
            raw = self._request('GET', '/installation/repositories', token=token, params={'per_page': '100', 'page': str(page)})
            repositories = raw.get('repositories') if isinstance(raw, dict) else None
            if not isinstance(repositories, list):
                raise GitHubAppError('GitHub returned an invalid repository list.')
            result.extend((_repository_view(item) for item in repositories if isinstance(item, dict)))
            if len(repositories) < 100:
                break
            page += 1
        return result

    def _app_jwt(self) -> str:
        if not self.configured:
            raise GitHubAppError('GitHub App is not configured.')
        try:
            import jwt
        except ImportError as exc:
            raise GitHubAppError('PyJWT is required for GitHub App authentication.') from exc
        now = int(time.time())
        return str(jwt.encode({'iat': now - 60, 'exp': now + 540, 'iss': self.app_id}, self.private_key, algorithm='RS256'))

    def _request_app(self, method: str, path: str) -> dict[str, Any]:
        raw = self._request(method, path, token=self._app_jwt())
        if not isinstance(raw, dict):
            raise GitHubAppError('GitHub returned an invalid app response.')
        return raw

    @staticmethod
    def _request(method: str, path: str, *, token: str | None=None, params: dict[str, str] | None=None) -> Any:
        headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'OpenClassGitHubSource/1.0', 'X-GitHub-Api-Version': GITHUB_API_VERSION}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        try:
            response = httpx.request(method, f'{GITHUB_API}{path}', headers=headers, params=params, timeout=30)
            if response.status_code in {403, 429}:
                ai_usage_logger.log_event('github_api_rate_limited', endpoint=path, status_code=response.status_code, rate_limit_remaining=response.headers.get('x-ratelimit-remaining'), rate_limit_reset=response.headers.get('x-ratelimit-reset'), retry_after=response.headers.get('retry-after'))
            response.raise_for_status()
            return response.json() if response.content else {}
        except (httpx.HTTPError, ValueError) as exc:
            raise GitHubAppError(str(exc)) from exc

def _repository_view(raw: dict[str, Any]) -> GitHubRepositoryView:
    owner_raw = raw.get('owner') if isinstance(raw.get('owner'), dict) else {}
    owner = str(owner_raw.get('login') or '')
    name = str(raw.get('name') or '')
    return GitHubRepositoryView(id=int(raw.get('id') or 0), full_name=str(raw.get('full_name') or f'{owner}/{name}'), owner=owner, name=name, private=bool(raw.get('private')), default_branch=str(raw.get('default_branch') or ''), html_url=str(raw.get('html_url') or f'https://github.com/{owner}/{name}'), description=str(raw.get('description') or ''))

def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
github_app_service = GitHubAppService()
