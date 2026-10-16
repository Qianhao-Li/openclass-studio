from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from openclass_core.studio_dependencies import current_user
from openclass_core.studio_contracts import WorkspacePrincipal
from openclass_core.services.studio_platform_client import local_platform_client
from openclass_core.services.studio_sync import PushCourse, PullCourse, push, pull
from openclass_core.services.workspace_state import package_view
router = APIRouter(prefix='/api/workbench/v1/platform')

@router.get('/courses')
def courses(user: WorkspacePrincipal=Depends(current_user)):
    return local_platform_client(user.id).request('GET', 'courses').json()

@router.get('/courses/{package_id}/association')
def association(package_id: str, user: WorkspacePrincipal=Depends(current_user)):
    return local_platform_client(user.id).get('sync:' + package_id)

@router.post('/courses/{package_id}/push')
def push_course(package_id: str, payload: PushCourse, user: WorkspacePrincipal=Depends(current_user)):
    user.require('courses.write')
    return push(user.id, package_id, payload)

@router.post('/courses/{identity}/pull')
def pull_course(identity: str, payload: PullCourse, user: WorkspacePrincipal=Depends(current_user)):
    user.require('courses.write')
    return package_view(pull(user.id, identity, payload))

class SubmitPR(BaseModel):
    model_config = ConfigDict(extra='forbid')
    package_id: str
    source_lesson_id: str
    source_branch: str = 'main'
    expected_ridoc_revision: str = Field(pattern='^[0-9a-f]{64}$')
    target_address: str = Field(min_length=1, max_length=2000)
    target_branch: str = 'main'
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=20000)

@router.post('/course-prs')
def submit_pr(payload: SubmitPR, user: WorkspacePrincipal=Depends(current_user)):
    client = local_platform_client(user.id)
    baseline = client.get('sync:' + payload.package_id)
    if not baseline or baseline['local_revision'] != payload.expected_ridoc_revision:
        raise HTTPException(409, 'Push this exact course revision before submitting a pull request.')
    from openclass_core.services.workspace_state import get_ridoc_workspace_store
    archive = get_ridoc_workspace_store().load_archive(user.id, package_id=payload.package_id)
    if archive.revision.value != payload.expected_ridoc_revision:
        raise HTTPException(409, 'The course changed; Push again before submitting.')
    body = payload.model_dump(exclude={'package_id', 'expected_ridoc_revision'})
    body.update(source_repository_id=baseline['repository_id'], expected_source_revision=baseline['ridoc_revision'])
    return client.request('POST', 'course-prs', json=body).json()
