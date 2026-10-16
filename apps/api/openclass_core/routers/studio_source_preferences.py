from fastapi import APIRouter, Depends
from openclass_core.studio_contracts import WorkspacePrincipal
from openclass_core.studio_dependencies import current_user
from openclass_core.services.source_retrieval_preferences import SourcePreferenceView, SourcePreferenceUpdate, _preferences
from openclass_core.services.workspace_state import DATABASE_PATH
router = APIRouter()

@router.get('/api/source-preferences', response_model=SourcePreferenceView)
def source_preference(principal: WorkspacePrincipal=Depends(current_user)):
    return _preferences(str(DATABASE_PATH)).get(principal.id)

@router.put('/api/source-preferences', response_model=SourcePreferenceView)
def save_source_preference(payload: SourcePreferenceUpdate, principal: WorkspacePrincipal=Depends(current_user)):
    principal.require('sources.manage')
    return _preferences(str(DATABASE_PATH)).update(principal.id, payload)
