"""GitHub compatibility access over the authoritative external connection store."""
from __future__ import annotations
import threading
import time
from openclass_core.services.external_connections import ExternalConnectionError, ExternalConnectionStore, capability_resource_scope
from openclass_core.services.github_user_client import GitHubUserClient
from openclass_core.services.repository_connection_http import RepositoryAccessError
from openclass_core.services.repository_oauth import GitHubOAuthClient
_refresh_lock = threading.RLock()

class GitHubConnectionAccess:

    def __init__(self, store=None, *, client_factory=GitHubUserClient, oauth_factory=GitHubOAuthClient.from_env):
        self._store = store
        self.client_factory = client_factory
        self.oauth_factory = oauth_factory

    @property
    def store(self):
        if self._store is not None:
            return self._store
        from openclass_core.services.studio_ports import host_service
        return host_service('connections')

    def connections(self, owner: str) -> list[dict]:
        return self.store.list_connections(owner, 'github')

    def connection(self, owner: str, connection_id: str | None=None) -> dict:
        rows = self.connections(owner)
        candidates = [item for item in rows if item['status'] == 'connected' and item.get('capabilities', {}).get('repositories', {}).get('granted') is True and (item.get('capabilities', {}).get('repositories', {}).get('enabled') is True) and (connection_id is None or item['id'] == connection_id)]
        if len(candidates) != 1:
            raise RepositoryAccessError('Choose one connected GitHub account before accessing repositories.', 409 if candidates else 401)
        return candidates[0]

    def client(self, owner: str, connection_id: str | None=None) -> tuple[dict, GitHubUserClient]:
        with _refresh_lock:
            connection = self.connection(owner, connection_id)
            credentials = self.store.get_credentials(owner, connection['id'], 'github')
            if float(credentials.get('expires_at') or float('inf')) <= time.time() + 60:
                update = getattr(self.store, 'update_credentials', None)
                if update is None:
                    raise RepositoryAccessError('GitHub authorization expired. Reconnect the account.', 401)
                refreshed = self.oauth_factory(str(credentials.get('redirect_uri') or 'https://localhost/unused-refresh')).refresh(credentials)
                connection = update(owner, connection['id'], connection['generation'], refreshed)
                credentials = refreshed
            token = str(credentials.get('access_token') or '')
            if not token.startswith('ghu_'):
                raise RepositoryAccessError('Reconnect GitHub with a GitHub App user authorization.', 401)
            client = self.client_factory(token)
            if client.identity()['subject'] != connection['subject']:
                raise RepositoryAccessError('GitHub account identity no longer matches this connection.', 403)
            self.store.mark_access(owner, connection['id'], connection['generation'])
            return (connection, client)

    def repositories(self, owner: str, connection_id: str | None=None) -> tuple[dict, GitHubUserClient, list[dict]]:
        connection, client = self.client(owner, connection_id)
        result: dict[int, dict] = {}
        cursor = None
        seen: set[str] = set()
        while True:
            installations = client.installations(cursor)
            for installation in installations['items']:
                permissions = installation.get('permissions', {})
                if installation.get('suspended_at') or permissions.get('contents') != 'read' or any((value != 'read' for value in permissions.values())):
                    continue
                repo_cursor = None
                repo_seen: set[str] = set()
                while True:
                    page = client.repositories(installation['id'], repo_cursor)
                    for item in page['items']:
                        scope = capability_resource_scope(connection, 'repositories')
                        if scope.get('mode') == 'selected' and str(item['id']) not in {str(value) for value in scope.get('resource_ids', [])}:
                            continue
                        result[int(item['id'])] = {**item, 'installation_id': installation['id']}
                    repo_cursor = page['next_cursor']
                    if repo_cursor is None:
                        break
                    if repo_cursor in repo_seen:
                        raise RepositoryAccessError('GitHub repeated a repository page; the list is incomplete.', 502)
                    repo_seen.add(repo_cursor)
            cursor = installations['next_cursor']
            if cursor is None:
                break
            if cursor in seen:
                raise RepositoryAccessError('GitHub repeated an installation page; the list is incomplete.', 502)
            seen.add(cursor)
        self.store.mark_access(owner, connection['id'], connection['generation'])
        return (connection, client, list(result.values()))

    def disconnect(self, owner: str, connection_id: str | None=None) -> None:
        rows = [row for row in self.connections(owner) if connection_id is None or row['id'] == connection_id]
        if len(rows) != 1:
            raise RepositoryAccessError('Choose one GitHub account to disconnect.', 409 if rows else 404)
        from openclass_core.services.external_connection_lifecycle import ExternalConnectionLifecycle
        ExternalConnectionLifecycle(self.store).disconnect(owner, rows[0]['id'], rows[0]['revision'])

    def invalidate(self, subject: str) -> None:
        self.store.invalidate_provider('github', subject=subject)
