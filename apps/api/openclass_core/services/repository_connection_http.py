"""Bounded, credential-safe transport shared by read-only repository adapters."""
from __future__ import annotations
import hashlib
import re
from urllib.parse import quote
import httpx

class RepositoryAccessError(RuntimeError):

    def __init__(self, message: str, status_code: int=422):
        super().__init__(message)
        self.status_code = status_code

def commit_id(value: str) -> str:
    if not re.fullmatch('[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', value):
        raise RepositoryAccessError('A complete immutable commit identity is required.')
    return value.lower()

def repository_path(value: str) -> str:
    if not value or value.startswith('/') or '\\' in value or ('\x00' in value) or any((part in {'', '.', '..'} for part in value.split('/'))):
        raise RepositoryAccessError('The repository path is invalid.')
    return value

def verify_blob(data: bytes, blob_id: str) -> str:
    blob_id = commit_id(blob_id)
    payload = f'blob {len(data)}\x00'.encode() + data
    actual = hashlib.sha1(payload, usedforsecurity=False).hexdigest() if len(blob_id) == 40 else hashlib.sha256(payload).hexdigest()
    if actual != blob_id:
        raise RepositoryAccessError('Repository bytes do not match the immutable blob identity.', 409)
    return hashlib.sha256(data).hexdigest()

class RepositoryReadClient:
    api_url: str

    def __init__(self, token: str, *, http: httpx.Client | None=None, max_bytes: int=32 * 1024 * 1024):
        if not token:
            raise RepositoryAccessError('Connect a repository account first.', 401)
        self._token = token
        self.http = http or httpx.Client(timeout=30, follow_redirects=False)
        self.max_bytes = max_bytes

    @property
    def headers(self) -> dict[str, str]:
        return {'Authorization': f'Bearer {self._token}'}

    @staticmethod
    def checked(response: httpx.Response) -> httpx.Response:
        if not response.is_success:
            status = response.status_code
            if status in {401, 403, 404}:
                raise RepositoryAccessError('Repository access is unavailable. Reconnect or choose an accessible repository.', status)
            if status == 429 or status >= 500:
                raise RepositoryAccessError('The repository provider is temporarily unavailable. Retry later.', 503)
            raise RepositoryAccessError('The repository provider rejected this read request.', 422)
        return response

    def get(self, path: str, params: dict | None=None) -> tuple[object, httpx.Headers]:
        try:
            response = self.checked(self.http.get(self.api_url + path, headers=self.headers, params=params, follow_redirects=False))
            return (response.json(), response.headers)
        except (httpx.HTTPError, ValueError) as exc:
            raise RepositoryAccessError('The repository provider returned an unavailable or invalid response.', 502) from exc

    def raw(self, path: str, params: dict | None=None, *, headers: dict | None=None) -> bytes:
        result = bytearray()
        try:
            with self.http.stream('GET', self.api_url + path, headers={**self.headers, **(headers or {})}, params=params, follow_redirects=False) as response:
                self.checked(response)
                for block in response.iter_bytes():
                    result.extend(block)
                    if len(result) > self.max_bytes:
                        raise RepositoryAccessError('The repository file exceeds the read limit.', 413)
        except httpx.HTTPError as exc:
            raise RepositoryAccessError('The repository file could not be read.', 502) from exc
        return bytes(result)

    @staticmethod
    def positive_id(value: str | int) -> str:
        if not str(value).isdigit() or int(value) <= 0:
            raise RepositoryAccessError('A stable numeric repository identity is required.')
        return quote(str(value), safe='')

    @staticmethod
    def page(value: str | int | None) -> int:
        if value is None:
            return 1
        if not str(value).isdigit() or not 1 <= int(value) <= 100000:
            raise RepositoryAccessError('The repository page cursor is invalid.')
        return int(value)
