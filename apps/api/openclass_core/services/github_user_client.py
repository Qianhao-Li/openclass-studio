"""Read repositories as an authorized GitHub App user, never as the installation bot."""
from __future__ import annotations
from urllib.parse import parse_qs, quote, urlsplit
import httpx
from openclass_core.services.repository_connection_http import RepositoryAccessError, RepositoryReadClient, commit_id, repository_path, verify_blob

class GitHubUserClient(RepositoryReadClient):
    api_url = 'https://api.github.com'

    @property
    def headers(self) -> dict[str, str]:
        return {**super().headers, 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2026-03-10', 'User-Agent': 'OpenClassRepositorySource/1.0'}

    @staticmethod
    def _next(headers: httpx.Headers) -> str | None:
        link = httpx.Response(200, headers=headers).links.get('next', {}).get('url', '')
        page = parse_qs(urlsplit(link).query).get('page', [None])[0]
        if link and (not page):
            raise RepositoryAccessError('GitHub returned an invalid continuation cursor.', 502)
        return page

    def identity(self) -> dict:
        raw, _ = self.get('/user')
        if not isinstance(raw, dict) or not raw.get('id'):
            raise RepositoryAccessError('GitHub account identity could not be verified.', 502)
        return {'subject': str(raw['id']), 'display_name': str(raw.get('login') or raw['id']), 'issuer': 'https://github.com'}

    def installations(self, cursor: str | None=None) -> dict:
        raw, headers = self.get('/user/installations', {'per_page': 100, 'page': self.page(cursor)})
        if not isinstance(raw, dict) or not isinstance(raw.get('installations'), list):
            raise RepositoryAccessError('GitHub returned an invalid installation list.', 502)
        cursor = self._next(headers)
        return {'items': raw['installations'], 'next_cursor': cursor, 'complete': cursor is None}

    def installation(self, installation_id: int) -> dict:
        target = int(self.positive_id(installation_id))
        cursor = None
        seen: set[str] = set()
        while True:
            page = self.installations(cursor)
            for item in page['items']:
                if item.get('id') == target:
                    permissions = item.get('permissions', {})
                    if item.get('suspended_at') or permissions.get('contents') != 'read' or any((value != 'read' for value in permissions.values())):
                        raise RepositoryAccessError('This installation is suspended or does not have strictly read-only access.', 403)
                    return item
            cursor = page['next_cursor']
            if cursor is None:
                break
            if cursor in seen:
                raise RepositoryAccessError('GitHub returned a repeated installation cursor.', 502)
            seen.add(cursor)
        raise RepositoryAccessError('This GitHub user cannot access the selected installation.', 403)

    def repositories(self, installation_id: int, cursor: str | None=None) -> dict:
        installation_id = self.positive_id(installation_id)
        raw, headers = self.get(f'/user/installations/{installation_id}/repositories', {'per_page': 100, 'page': self.page(cursor)})
        if not isinstance(raw, dict) or not isinstance(raw.get('repositories'), list):
            raise RepositoryAccessError('GitHub returned an invalid repository list.', 502)
        cursor = self._next(headers)
        return {'items': raw['repositories'], 'next_cursor': cursor, 'complete': cursor is None}

    def resolve(self, repository_id: str | int, ref: str | None=None) -> dict:
        repo = self.positive_id(repository_id)
        self.require_repository(int(repo))
        raw, _ = self.get(f'/repositories/{repo}')
        if not isinstance(raw, dict) or str(raw.get('id')) != repo:
            raise RepositoryAccessError('GitHub repository identity changed.', 409)
        selected = ref or str(raw.get('default_branch') or '')
        if not selected:
            raise RepositoryAccessError('This repository has no readable branch.', 409)
        commit, _ = self.get(f"/repositories/{repo}/commits/{quote(selected, safe='')}")
        if not isinstance(commit, dict):
            raise RepositoryAccessError('GitHub returned an invalid commit.', 502)
        return {'provider': 'github', 'issuer': 'https://github.com', 'repository_id': int(repo), 'name': raw.get('full_name', ''), 'requested_ref': selected, 'commit_sha': commit_id(str(commit.get('sha') or '')), 'tree_sha': commit_id(str(commit.get('commit', {}).get('tree', {}).get('sha') or ''))}

    def require_repository(self, repository_id: int) -> None:
        """Public API readability alone must not bypass the selected App installation."""
        cursor, seen = (None, set())
        while True:
            installations = self.installations(cursor)
            for installation in installations['items']:
                permissions = installation.get('permissions', {})
                if installation.get('suspended_at') or permissions.get('contents') != 'read' or any((value != 'read' for value in permissions.values())):
                    continue
                repo_cursor, repo_seen = (None, set())
                while True:
                    page = self.repositories(installation['id'], repo_cursor)
                    if any((item.get('id') == repository_id for item in page['items'])):
                        return
                    repo_cursor = page['next_cursor']
                    if repo_cursor is None:
                        break
                    if repo_cursor in repo_seen:
                        raise RepositoryAccessError('GitHub repeated a repository page.', 502)
                    repo_seen.add(repo_cursor)
            cursor = installations['next_cursor']
            if cursor is None:
                break
            if cursor in seen:
                raise RepositoryAccessError('GitHub repeated an installation page.', 502)
            seen.add(cursor)
        raise RepositoryAccessError('The GitHub user and App installation do not grant access to this repository.', 403)

    def tree(self, repository_id: str | int, commit_sha: str, path: str='') -> dict:
        repo, sha = (self.positive_id(repository_id), commit_id(commit_sha))
        resolved = self.resolve(repo, sha)
        tree_sha = resolved['tree_sha']
        parts = repository_path(path).split('/') if path else []
        for part in [*parts, None]:
            raw, _ = self.get(f'/repositories/{repo}/git/trees/{tree_sha}')
            if not isinstance(raw, dict) or not isinstance(raw.get('tree'), list) or raw.get('truncated'):
                raise RepositoryAccessError('This directory cannot be enumerated completely within the provider limit.', 413)
            if part is None:
                return {'items': raw['tree'], 'commit_sha': sha, 'next_cursor': None, 'complete': True}
            child = next((item for item in raw['tree'] if item.get('path') == part and item.get('type') == 'tree'), None)
            if child is None:
                raise RepositoryAccessError('The selected directory is unavailable at this commit.', 404)
            tree_sha = commit_id(str(child.get('sha') or ''))
        raise RepositoryAccessError('The repository directory is unavailable.', 404)

    def read_raw(self, repository_id: str | int, commit_sha: str, path: str) -> dict:
        repo, sha, path = (self.positive_id(repository_id), commit_id(commit_sha), repository_path(path))
        parent, _, basename = path.rpartition('/')
        entries = self.tree(repo, sha, parent)['items']
        entry = next((item for item in entries if item.get('path') == basename), None)
        if entry is None or entry.get('type') != 'blob' or entry.get('mode') not in {'100644', '100755'}:
            raise RepositoryAccessError('Choose a regular repository file; links and submodules are not followed.', 415)
        if int(entry.get('size') or 0) > self.max_bytes:
            raise RepositoryAccessError('The repository file exceeds the read limit.', 413)
        blob = commit_id(str(entry.get('sha') or ''))
        data = self.raw(f'/repositories/{repo}/git/blobs/{blob}', headers={'Accept': 'application/vnd.github.raw+json'})
        digest = verify_blob(data, blob)
        if data.startswith(b'version https://git-lfs.github.com/spec/v1\n'):
            raise RepositoryAccessError('This file is a Git LFS pointer. Import the actual attachment through Sources.', 415)
        return {'provider': 'github', 'issuer': 'https://github.com', 'repository_id': int(repo), 'commit_sha': sha, 'path': path, 'blob_sha': blob, 'sha256': digest, 'size_bytes': len(data), 'content': data}

    def search_candidates(self, repository_id: str | int, query: str, cursor: str | None=None) -> dict:
        resolved = self.resolve(repository_id)
        if not query.strip() or any((character in query for character in ('\n', '\r'))):
            raise RepositoryAccessError('A repository search term is required.')
        term = query.replace('\\', '\\\\').replace('"', '\\"')
        raw, headers = self.get('/search/code', {'q': f'''"{term}" repo:{resolved['name']}''', 'per_page': 100, 'page': self.page(cursor)})
        if not isinstance(raw, dict) or not isinstance(raw.get('items'), list):
            raise RepositoryAccessError('GitHub returned invalid search candidates.', 502)
        items = [item for item in raw['items'] if str(item.get('repository', {}).get('id')) == str(repository_id)]
        return {'items': items, 'next_cursor': self._next(headers), 'complete': False, 'evidence_ready': False, 'incomplete_results': bool(raw.get('incomplete_results')), 'limitations': ['Default branch only; files below 384 KB; at most 1000 results. Reread each candidate at a fixed commit.']}
