"""GitLab.com-only repository access; credentials and connection ownership are injected."""
from __future__ import annotations
import re
from urllib.parse import parse_qs, quote, urlencode, urlsplit
import httpx
from openclass_core.services.repository_connection_http import RepositoryAccessError, RepositoryReadClient, commit_id, repository_path, verify_blob
GITLAB_SCOPES = 'read_api'

class GitLabRepositoryAdapter(RepositoryReadClient):
    api_url = 'https://gitlab.com/api/v4'

    def identity(self) -> dict:
        raw, _ = self.get('/user')
        if not isinstance(raw, dict) or not raw.get('id'):
            raise RepositoryAccessError('GitLab account identity could not be verified.', 502)
        return {'subject': str(raw['id']), 'display_name': str(raw.get('username') or raw['id']), 'issuer': 'https://gitlab.com'}

    def repositories(self, cursor: str | None=None, query: str='') -> dict:
        page = self.page(cursor)
        raw, headers = self.get('/projects', {'membership': 'true', 'simple': 'true', 'per_page': 100, 'page': page, 'search': query})
        if not isinstance(raw, list):
            raise RepositoryAccessError('GitLab returned an invalid project list.', 502)
        next_cursor = headers.get('x-next-page') or (str(page + 1) if len(raw) == 100 else None)
        return {'items': raw, 'next_cursor': next_cursor, 'complete': not bool(next_cursor)}

    def resolve(self, repository_id: str | int, ref: str | None=None) -> dict:
        project = self.positive_id(repository_id)
        raw, _ = self.get(f'/projects/{project}')
        if not isinstance(raw, dict) or str(raw.get('id')) != project:
            raise RepositoryAccessError('GitLab repository identity changed.', 409)
        selected = ref or str(raw.get('default_branch') or '')
        if not selected:
            raise RepositoryAccessError('This repository has no readable branch.', 409)
        commit, _ = self.get(f"/projects/{project}/repository/commits/{quote(selected, safe='')}")
        if not isinstance(commit, dict):
            raise RepositoryAccessError('GitLab returned an invalid commit.', 502)
        return {'provider': 'gitlab', 'issuer': 'https://gitlab.com', 'repository_id': int(project), 'name': raw.get('path_with_namespace', ''), 'requested_ref': selected, 'commit_sha': commit_id(str(commit.get('id') or ''))}

    def tree(self, repository_id: str | int, commit_sha: str, path: str='', cursor: str | None=None) -> dict:
        project, sha = (self.positive_id(repository_id), commit_id(commit_sha))
        params = {'ref': sha, 'per_page': 100, 'pagination': 'keyset', 'recursive': 'false'}
        if path:
            params['path'] = repository_path(path)
        if cursor:
            if len(cursor) > 1024:
                raise RepositoryAccessError('The repository page cursor is invalid.')
            params['page_token'] = cursor
        raw, headers = self.get(f'/projects/{project}/repository/tree', params)
        if not isinstance(raw, list):
            raise RepositoryAccessError('GitLab returned an invalid repository tree.', 502)
        response = httpx.Response(200, headers=headers)
        next_url = response.links.get('next', {}).get('url', '')
        next_cursor = parse_qs(urlsplit(next_url).query).get('page_token', [None])[0]
        if next_url and (not next_cursor):
            raise RepositoryAccessError('GitLab returned an invalid continuation cursor.', 502)
        return {'items': raw, 'commit_sha': sha, 'next_cursor': next_cursor, 'complete': not bool(next_cursor)}

    def read_raw(self, repository_id: str | int, commit_sha: str, path: str) -> dict:
        project, sha, path = (self.positive_id(repository_id), commit_id(commit_sha), repository_path(path))
        parent, _, basename = path.rpartition('/')
        cursor, seen, entry = (None, set(), None)
        while True:
            page = self.tree(project, sha, parent, cursor)
            entry = next((item for item in page['items'] if item.get('name') == basename and item.get('path') == path), None)
            cursor = page['next_cursor']
            if entry or cursor is None:
                break
            if cursor in seen:
                raise RepositoryAccessError('GitLab repeated a directory cursor.', 502)
            seen.add(cursor)
        if not entry or entry.get('type') != 'blob' or entry.get('mode') not in {'100644', '100755'}:
            raise RepositoryAccessError('Choose a regular repository file; links and submodules are not followed.', 415)
        endpoint = f"/projects/{project}/repository/files/{quote(path, safe='')}"
        try:
            response = self.checked(self.http.head(self.api_url + endpoint, params={'ref': sha}, headers=self.headers, follow_redirects=False))
        except httpx.HTTPError as exc:
            raise RepositoryAccessError('GitLab file metadata is unavailable.', 502) from exc
        blob = str(response.headers.get('x-gitlab-blob-id') or '')
        if blob != entry.get('id'):
            raise RepositoryAccessError('GitLab file identity does not match the commit tree.', 409)
        if response.headers.get('x-gitlab-commit-id', '').lower() != sha:
            raise RepositoryAccessError('GitLab returned metadata for another commit.', 409)
        try:
            declared_size = int(response.headers['x-gitlab-size'])
        except (KeyError, ValueError) as exc:
            raise RepositoryAccessError('GitLab file metadata is invalid.', 502) from exc
        if declared_size < 0:
            raise RepositoryAccessError('GitLab file metadata is invalid.', 502)
        if declared_size > self.max_bytes:
            raise RepositoryAccessError('The repository file exceeds the read limit.', 413)
        data = self.raw(endpoint + '/raw', {'ref': sha, 'lfs': 'false'})
        if len(data) != declared_size:
            raise RepositoryAccessError('GitLab raw bytes do not match file metadata.', 409)
        digest = verify_blob(data, blob)
        if data.startswith(b'version https://git-lfs.github.com/spec/v1\n'):
            raise RepositoryAccessError('This file is a Git LFS pointer. Import the actual attachment through Sources.', 415)
        return {'provider': 'gitlab', 'issuer': 'https://gitlab.com', 'repository_id': int(project), 'commit_sha': sha, 'path': path, 'blob_sha': blob, 'sha256': digest, 'size_bytes': len(data), 'content': data}

    def search_candidates(self, repository_id: str | int, query: str, ref: str | None=None, cursor: str | None=None) -> dict:
        params = {'scope': 'blobs', 'search': query, 'search_type': 'basic', 'per_page': 100, 'page': self.page(cursor)}
        if ref and (not re.fullmatch('[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', ref)):
            params['ref'] = ref
        raw, headers = self.get(f'/projects/{self.positive_id(repository_id)}/search', params)
        if not isinstance(raw, list):
            raise RepositoryAccessError('GitLab returned invalid search candidates.', 502)
        next_cursor = headers.get('x-next-page') or (str(params['page'] + 1) if len(raw) == 100 else None)
        return {'items': raw, 'next_cursor': next_cursor, 'complete': False, 'evidence_ready': False, 'limitations': ['Project search returns candidates from a branch index and may omit files; each candidate must be reread at the selected commit. It does not cover all projects or prove complete coverage.']}

def gitlab_authorization_url(*, client_id: str, redirect_uri: str, state: str, code_challenge: str) -> str:
    return 'https://gitlab.com/oauth/authorize?' + urlencode({'client_id': client_id, 'redirect_uri': redirect_uri, 'state': state, 'response_type': 'code', 'scope': GITLAB_SCOPES, 'code_challenge': code_challenge, 'code_challenge_method': 'S256'})
