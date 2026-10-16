from fastapi import APIRouter, Depends, HTTPException, Query
from openclass_core.studio_dependencies import current_user
from openclass_core.studio_contracts import WorkspacePrincipal
from openclass_core.models import CodexLoginStartResponse, CodexLoginStatusResponse, CodexProviderStatus
from openclass_core.services.codex_app_server import CodexAppServerError, start_codex_device_login, codex_login_status, cancel_codex_login, codex_provider_status
from openclass_core.services.codex_connections import list_codex_connections, disconnect_codex_connection
from openclass_core.services.external_connections import ExternalConnectionError
from openclass_core.services.studio_personal_models import personal_model_catalog
router = APIRouter(prefix='/api/codex')

def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (CodexAppServerError, ExternalConnectionError) as error:
        raise HTTPException(getattr(error, 'status_code', 400), str(error)) from error

@router.get('/connections')
def accounts(principal: WorkspacePrincipal=Depends(current_user)):
    return _call(list_codex_connections, principal.id)

@router.get('/status', response_model=CodexProviderStatus)
def status(principal: WorkspacePrincipal=Depends(current_user), include_rate_limits: bool=False, connection_id: str | None=None):
    return _call(codex_provider_status, principal.id, include_rate_limits=include_rate_limits, connection_id=connection_id)

@router.post('/connections/login/device', response_model=CodexLoginStartResponse)
@router.post('/login/device', response_model=CodexLoginStartResponse)
def connect(principal: WorkspacePrincipal=Depends(current_user), connection_id: str | None=None):
    principal.require('models.manage')
    return _call(start_codex_device_login, principal.id, purpose='provider', connection_id=connection_id, new_connection=connection_id is None)

@router.get('/login/{login_id}', response_model=CodexLoginStatusResponse)
def login_status(login_id: str, principal: WorkspacePrincipal=Depends(current_user)):
    return _call(codex_login_status, login_id, principal.id)

@router.post('/login/{login_id}/cancel', response_model=CodexLoginStatusResponse)
def cancel(login_id: str, principal: WorkspacePrincipal=Depends(current_user)):
    principal.require('models.manage')
    return _call(cancel_codex_login, login_id, principal.id)

@router.get('/connections/{connection_id}/models')
def account_models(connection_id: str, principal: WorkspacePrincipal=Depends(current_user)):
    return _call(personal_model_catalog, principal.id, connection_id)

@router.get('/connections/{connection_id}/status', response_model=CodexProviderStatus)
def account_status(connection_id: str, include_rate_limits: bool=False, principal: WorkspacePrincipal=Depends(current_user)):
    return _call(codex_provider_status, principal.id, include_rate_limits=include_rate_limits, connection_id=connection_id)

@router.delete('/connections/{connection_id}')
def disconnect(connection_id: str, expected_revision: int=Query(ge=1), principal: WorkspacePrincipal=Depends(current_user)):
    principal.require('models.manage')
    return _call(disconnect_codex_connection, principal.id, connection_id, expected_revision)
