from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from openclass_core.studio_dependencies import current_user
from openclass_core.studio_contracts import WorkspacePrincipal
from openclass_core.services.studio_platform_client import local_platform_client
router = APIRouter(prefix='/api/workbench/v1/platform')

class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scopes: list[str] = Field(min_length=1, max_length=4)

@router.get('')
def status(user: WorkspacePrincipal=Depends(current_user)):
    return local_platform_client(user.id).status()

@router.post('/connect')
def connect(payload: ConnectRequest, user: WorkspacePrincipal=Depends(current_user)):
    user.require('models.manage')
    return local_platform_client(user.id).start(payload.scopes)

@router.get('/connect/{attempt}')
def poll(attempt: str, user: WorkspacePrincipal=Depends(current_user)):
    return local_platform_client(user.id).poll(attempt)

@router.delete('')
def disconnect(user: WorkspacePrincipal=Depends(current_user)):
    user.require('models.manage')
    return local_platform_client(user.id).disconnect()
