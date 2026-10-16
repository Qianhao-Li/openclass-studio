from fastapi import APIRouter, Depends, Request, Response
from openclass_core.studio_contracts import StudioBootstrap, WorkspacePrincipal
from openclass_core.studio_dependencies import current_user
router = APIRouter(prefix='/api/workbench/v1')

@router.post('/session', response_model=StudioBootstrap)
def open_local_session(request: Request, response: Response) -> StudioBootstrap:
    identity = request.app.state.local_studio_identity
    token = identity.open_session(request)
    response.set_cookie(identity.cookie_name, token, httponly=True, samesite='strict', path='/')
    response.headers['Cache-Control'] = 'no-store'
    return StudioBootstrap(mode='local', principal=identity.principal)

@router.get('/bootstrap', response_model=StudioBootstrap)
def bootstrap(response: Response, principal: WorkspacePrincipal=Depends(current_user)) -> StudioBootstrap:
    response.headers['Cache-Control'] = 'no-store'
    return StudioBootstrap(mode=principal.kind, principal=principal)
