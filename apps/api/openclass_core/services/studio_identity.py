"""Local profile and loopback session; independent of platform accounts."""
from __future__ import annotations
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit
from uuid import uuid4
from fastapi import HTTPException, Request, WebSocket
from starlette.requests import HTTPConnection
from openclass_core.studio_contracts import WorkspacePrincipal
STUDIO_COOKIE = 'openclass.studio.session'
LOCAL_CAPABILITIES = frozenset({'courses.read', 'courses.write', 'models.manage', 'sources.manage'})

def _private_create(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 384)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())

class LocalStudioIdentity:

    def __init__(self, root: Path, *, launch_token: str, web_origin: str, api_origin: str):
        if len(launch_token) < 32:
            raise ValueError('Studio requires a private launch token of at least 32 characters.')
        for origin in (web_origin, api_origin):
            parsed = urlsplit(origin)
            if parsed.scheme != 'http' or parsed.hostname not in {'localhost', '127.0.0.1', '::1'} or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                raise ValueError('Local Studio origins must be loopback HTTP origins.')
        self.root = root.expanduser().absolute()
        if self.root.is_symlink():
            raise ValueError('The local Studio data directory cannot be a symlink.')
        self.root.mkdir(parents=True, exist_ok=True, mode=448)
        self.root.chmod(448)
        profile = self.root / 'profile.json'
        if profile.is_symlink():
            raise ValueError('The local Studio profile cannot be a symlink.')
        try:
            _private_create(profile, json.dumps({'version': 1, 'id': 'local_' + uuid4().hex}).encode())
        except FileExistsError:
            pass
        if profile.stat().st_mode & 63:
            raise ValueError('The local Studio profile must be private.')
        data = json.loads(profile.read_text())
        if data.get('version') != 1 or not isinstance(data.get('id'), str) or (not data['id'].startswith('local_')) or (len(data['id']) != 38):
            raise ValueError('The local Studio profile is invalid.')
        self.principal = WorkspacePrincipal(id=data['id'], kind='local', capabilities=LOCAL_CAPABILITIES)
        self.cookie_name = STUDIO_COOKIE + '.' + data['id'].removeprefix('local_')
        self.web_origin = web_origin
        self.authorities = {urlsplit(value).netloc for value in (api_origin, web_origin)}
        self._launch_digest = hashlib.sha256(launch_token.encode()).digest()
        self._session = secrets.token_urlsafe(48)

    def check_transport(self, request: Request | WebSocket, *, write: bool=False) -> None:
        if request.headers.get('host') not in self.authorities:
            raise HTTPException(403, 'Local Studio requires a loopback host.')
        try:
            address = ipaddress.ip_address(request.client.host) if request.client else None
        except ValueError:
            address = None
        if address is None or not address.is_loopback:
            raise HTTPException(403, 'Local Studio accepts loopback clients only.')
        origin = request.headers.get('origin')
        if origin is not None and origin != self.web_origin:
            raise HTTPException(403, 'The request does not belong to this Studio.')
        if write and origin != self.web_origin:
            raise HTTPException(403, 'Studio writes require the configured browser origin.')

    def open_session(self, request: Request) -> str:
        self.check_transport(request, write=True)
        digest = hashlib.sha256(request.headers.get('x-openclass-launch-token', '').encode()).digest()
        if not hmac.compare_digest(digest, self._launch_digest):
            raise HTTPException(401, 'Open Studio using its local launcher.')
        return self._session

    def resolve(self, request: HTTPConnection) -> WorkspacePrincipal:
        write = isinstance(request, WebSocket) or request.method not in {'GET', 'HEAD', 'OPTIONS'}
        self.check_transport(request, write=write)
        token = request.cookies.get(self.cookie_name, '')
        if not hmac.compare_digest(token, self._session):
            raise HTTPException(401, 'The local Studio session must be opened again.')
        return self.principal
