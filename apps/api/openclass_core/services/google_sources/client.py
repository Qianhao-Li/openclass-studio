from __future__ import annotations
import threading
from collections.abc import Callable
from urllib.parse import urlsplit
import httpx
from .config import GoogleSourceConfig, GoogleSourceError, GoogleSourceUnavailableError

class GoogleCloudClient:
    """ADC-backed HTTP transport; tests inject a token provider and MockTransport."""

    def __init__(self, config: GoogleSourceConfig, *, transport=None, token_provider: Callable[[], str] | None=None):
        self.config = config
        self.http = httpx.Client(transport=transport, timeout=60, follow_redirects=False)
        self._token_provider = token_provider
        self._credentials = None
        self._lock = threading.Lock()

    def _token(self) -> str:
        if self._token_provider is not None:
            return self._token_provider()
        try:
            import google.auth
            from google.auth.transport.requests import Request
            with self._lock:
                if self._credentials is None or not self._credentials.valid:
                    from .federation import refresh_subject_token
                    refresh_subject_token()
                if self._credentials is None:
                    self._credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
                if not self._credentials.valid:
                    self._credentials.refresh(Request())
                return str(self._credentials.token)
        except Exception as exc:
            raise GoogleSourceUnavailableError('Google credentials are unavailable. Configure Application Default Credentials or a server service account.') from exc

    def request(self, method: str, url: str, *, allow_status: tuple[int, ...]=(), **kwargs) -> httpx.Response:
        self.config.validate()
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname not in {'storage.googleapis.com', 'discoveryengine.googleapis.com', 'us-discoveryengine.googleapis.com', 'eu-discoveryengine.googleapis.com', 'us-speech.googleapis.com', 'eu-speech.googleapis.com'}:
            raise GoogleSourceError('This URL is not an allowed Google source service endpoint.')
        headers = {'Authorization': f'Bearer {self._token()}', 'X-Goog-User-Project': self.config.project_id, **kwargs.pop('headers', {})}
        try:
            response = self.http.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise GoogleSourceUnavailableError('The Google source service network request failed. Retrying will resume the recorded operation.') from exc
        if response.is_error and response.status_code not in allow_status:
            hints = {401: 'credentials expired', 403: 'permissions, enabled APIs, or billing status do not meet requirements', 404: 'cloud object or configuration not found', 409: 'cloud version conflict', 429: 'quota exceeded or too many requests'}
            error = GoogleSourceUnavailableError if response.status_code == 429 or response.status_code >= 500 else GoogleSourceError
            raise error(f"Google source service request failed (HTTP {response.status_code}): {hints.get(response.status_code, 'cloud processing error')}.")
        return response

    def json(self, method: str, url: str, **kwargs) -> dict:
        response = self.request(method, url, **kwargs)
        try:
            result = response.json()
        except ValueError as exc:
            raise GoogleSourceError('The Google source service returned invalid JSON.') from exc
        if not isinstance(result, dict):
            raise GoogleSourceError('The Google source service returned an unexpected response format.')
        return result
