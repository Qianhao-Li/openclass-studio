from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Any
import httpx

class OpenNotebookAdapterError(RuntimeError):
    pass

@dataclass(frozen=True)
class OpenNotebookSourceResult:
    source_id: str
    command_id: str = ''
    status: str = 'queued'
    raw: dict[str, Any] | None = None

class OpenNotebookAdapter:
    """Small compatibility adapter for the Open Notebook REST API."""

    def __init__(self, *, api_url: str | None=None, password: str | None=None, api_prefix: str | None=None, timeout_seconds: float | None=None) -> None:
        self.api_url = (api_url or os.getenv('OPEN_NOTEBOOK_API_URL') or 'http://localhost:5055').rstrip('/')
        self.password = password if password is not None else os.getenv('OPEN_NOTEBOOK_PASSWORD', '')
        self.api_prefix = api_prefix if api_prefix is not None else os.getenv('OPEN_NOTEBOOK_API_PREFIX', '/api')
        self.timeout_seconds = timeout_seconds or float(os.getenv('OPEN_NOTEBOOK_TIMEOUT_SECONDS', '20'))

    def create_notebook(self, *, title: str, description: str='') -> str:
        payload = {'name': title, 'title': title, 'description': description}
        data = self._request_json('POST', '/notebooks', json=payload)
        notebook_id = _first_text(data, 'id', 'notebook_id', 'record_id')
        if not notebook_id:
            raise OpenNotebookAdapterError('Open Notebook did not return a notebook id.')
        return notebook_id

    def add_url_source(self, *, notebook_id: str, source_uri: str, title: str='') -> OpenNotebookSourceResult:
        payload = self._request_json('POST', '/sources', data={'type': 'link', 'notebook_id': notebook_id, 'url': source_uri, 'source_uri': source_uri, 'title': title, 'async_processing': 'true'})
        return _source_result(payload)

    def upload_file_source(self, *, notebook_id: str, file_name: str, content: bytes, mime_type: str, title: str='') -> OpenNotebookSourceResult:
        payload = self._request_json('POST', '/sources', data={'type': 'upload', 'notebook_id': notebook_id, 'title': title or file_name, 'async_processing': 'true'}, files={'file': (file_name, content, mime_type or 'application/octet-stream')})
        return _source_result(payload)

    def get_command(self, command_id: str) -> dict[str, Any]:
        if not command_id:
            return {}
        last_error: OpenNotebookAdapterError | None = None
        for path in (f'/commands/jobs/{command_id}', f'/commands/{command_id}'):
            try:
                return self._request_json('GET', path)
            except OpenNotebookAdapterError as exc:
                last_error = exc
        raise last_error or OpenNotebookAdapterError('Open Notebook command lookup failed.')

    def delete_source(self, source_id: str) -> None:
        if source_id:
            self._request_json('DELETE', f'/sources/{source_id}')

    def search(self, *, notebook_id: str, query: str, limit: int=8, source_ids: list[str] | None=None) -> list[dict[str, Any]]:
        data = self._request_json('POST', '/search', json={'notebook_id': notebook_id, 'query': query, 'question': query, 'type': 'text', 'limit': limit, 'source_ids': source_ids or [], 'search_sources': True, 'search_notes': False})
        return _search_items(data)

    def _request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        last_error: Exception | None = None
        for url in self._candidate_urls(path):
            try:
                response = httpx.request(method, url, headers=self._headers(), timeout=self.timeout_seconds, **kwargs)
                if response.status_code == 404 and self.api_prefix:
                    last_error = OpenNotebookAdapterError(f'Open Notebook returned 404 for {url}.')
                    continue
                response.raise_for_status()
                if not response.content:
                    return {}
                data = response.json()
                return data if isinstance(data, dict) else {'items': data}
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
        raise OpenNotebookAdapterError(str(last_error or 'Open Notebook request failed.'))

    def _candidate_urls(self, path: str) -> list[str]:
        normalized = path if path.startswith('/') else f'/{path}'
        urls: list[str] = []
        if self.api_prefix:
            prefix = self.api_prefix if self.api_prefix.startswith('/') else f'/{self.api_prefix}'
            urls.append(f'{self.api_url}{prefix}{normalized}')
        urls.append(f'{self.api_url}{normalized}')
        return list(dict.fromkeys(urls))

    def _headers(self) -> dict[str, str]:
        if not self.password:
            return {}
        return {'Authorization': f'Bearer {self.password}', 'X-Password': self.password}

def _first_text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = data.get('data')
    if isinstance(nested, dict):
        return _first_text(nested, *keys)
    return ''

def _source_result(data: dict[str, Any]) -> OpenNotebookSourceResult:
    source_id = _first_text(data, 'id', 'source_id', 'record_id')
    command_id = _first_text(data, 'command_id', 'command')
    status = _first_text(data, 'status', 'processing_status') or ('queued' if command_id else 'ready')
    if not source_id:
        raise OpenNotebookAdapterError('Open Notebook did not return a source id.')
    return OpenNotebookSourceResult(source_id=source_id, command_id=command_id, status=status, raw=data)

def _search_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ('results', 'items', 'sources', 'matches'):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    nested = data.get('data')
    if isinstance(nested, dict):
        return _search_items(nested)
    if isinstance(nested, list):
        return [item for item in nested if isinstance(item, dict)]
    return []
open_notebook_adapter = OpenNotebookAdapter()
