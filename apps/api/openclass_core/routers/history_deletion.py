from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, Response
from openclass_core.models import RidocMessageDeletionPreview, RidocMessageDeletionRequest, RidocMessageDeletionResponse, UserView
from openclass_core.studio_dependencies import current_user
from openclass_core.services import workspace_state
from openclass_core.services.ridoc_lock import ridoc_file_lock
from openclass_core.services.ridoc_v3 import RidocV3ConflictError, RidocV3Error
router = APIRouter()

def _store_for_lesson(user_id: str, lesson_id: str):
    if not workspace_state.is_ridoc_lesson_for_user(user_id, lesson_id):
        raise HTTPException(status_code=404, detail='Editable RIDOC lesson not found')
    return workspace_state.get_ridoc_workspace_store()

@router.get('/api/lessons/{lesson_id}/history/nodes/{node_id}/deletion-preview', response_model=RidocMessageDeletionPreview)
def preview_deletion(lesson_id: str, node_id: str, response: Response, user: UserView=Depends(current_user)):
    store = _store_for_lesson(user.id, lesson_id)
    try:
        result = store.preview_message_deletion(owner_user_id=user.id, lesson_id=lesson_id, node_id=node_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail='RIDOC history node is unavailable') from exc
    except RidocV3Error as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    scope = workspace_state.current_repository_workspace_scope()
    if scope:
        result['expected_version'] = scope.version
    response.headers['Cache-Control'] = 'no-store'
    return result

@router.post('/api/lessons/{lesson_id}/history/nodes/{node_id}/delete', response_model=RidocMessageDeletionResponse)
def delete_message(lesson_id: str, node_id: str, request: RidocMessageDeletionRequest, response: Response, user: UserView=Depends(current_user)):
    store = _store_for_lesson(user.id, lesson_id)
    package_id = store.package_id_for_lesson(user.id, lesson_id)
    assert package_id is not None
    scope = workspace_state.current_repository_workspace_scope()
    if scope:
        operation = store.deletion_journal.get(user.id, store.deletion_scope_id(package_id), request.mutation_id)
        if request.expected_version is None:
            raise HTTPException(status_code=422, detail='Repository version is required')
        if operation is None and request.expected_version != scope.version:
            raise HTTPException(status_code=409, detail='Repository changed; review the deletion again')
        store.expected_deletion_version = request.expected_version
    try:
        with store._lock, ridoc_file_lock(store.package_path(user.id, package_id)):
            result = store.delete_history_message(owner_user_id=user.id, package_id=package_id, lesson_id=lesson_id, node_id=node_id, expected_revision=request.expected_ridoc_revision, mutation_id=request.mutation_id)
            package = store.load_package(user.id, package_id=package_id)
    except RidocV3ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RidocV3Error as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response.headers['Cache-Control'] = 'no-store'
    return RidocMessageDeletionResponse(course_package=workspace_state.package_view(package, is_standalone=package.package_kind == 'standalone', resource_lesson_id=lesson_id, isolate_lesson_resources=package.package_kind == 'standalone'), **{key: result[key] for key in ('ridoc_revision', 'deleted_node_id', 'affected_branches', 'retained_document_node_ids', 'mutation_id')})
