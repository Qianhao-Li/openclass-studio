"""A course router asks its host for identity; it never imports platform auth."""
from __future__ import annotations
from fastapi import HTTPException, Request, WebSocket
from openclass_core.studio_contracts import WorkspacePrincipal
from openclass_core.services.studio_ports import host_service

def current_user(request: Request) -> WorkspacePrincipal:
    resolver = getattr(request.app.state, 'studio_identity', None)
    if resolver is None:
        return host_service('http_identity', request)
    return resolver(request)

def optional_current_user(request: Request) -> WorkspacePrincipal | None:
    try:
        return current_user(request)
    except HTTPException as error:
        if error.status_code == 401:
            return None
        raise

def current_websocket_user(socket: WebSocket) -> WorkspacePrincipal:
    resolver = getattr(socket.app.state, 'studio_websocket_identity', None)
    if resolver is None:
        return host_service('websocket_identity', socket)
    return resolver(socket)
