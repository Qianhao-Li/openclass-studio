from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from openclass_core.studio_dependencies import current_user
from openclass_core.studio_contracts import WorkspacePrincipal
from openclass_core.services import workspace_state
from openclass_core.services.ridoc_v3 import RidocV3Error
router = APIRouter()

class CourseMetadataUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_ridoc_revision: str = Field(min_length=64, max_length=64)
    title: str | None = None
    summary: str | None = None

@router.post('/api/packages/{package_id}')
def update_course(package_id: str, payload: CourseMetadataUpdate, principal: WorkspacePrincipal=Depends(current_user)):
    principal.require('courses.write')
    try:
        store = workspace_state.get_ridoc_workspace_store()
        store.update_package_metadata(owner_user_id=principal.id, package_id=package_id, expected_revision=payload.expected_ridoc_revision, title=payload.title, summary=payload.summary)
        return workspace_state.workspace_view(workspace_state.load_workspace_for_user(principal.id))
    except RidocV3Error as error:
        raise HTTPException(409, str(error)) from error
