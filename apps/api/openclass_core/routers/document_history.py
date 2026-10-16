from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, Response
from openclass_core.models import RidocDocumentHistoryStepRequest, RidocDocumentHistoryStepResponse, UserView
from openclass_core.studio_dependencies import current_user
from openclass_core.routers.history_deletion import _store_for_lesson
from openclass_core.services import workspace_state
from openclass_core.services.ridoc_document_history import step_document_history
from openclass_core.services.ridoc_lock import ridoc_file_lock
from openclass_core.services.ridoc_v3 import RidocV3ConflictError, RidocV3Error
router = APIRouter()

@router.post('/api/lessons/{lesson_id}/history/document/step', response_model=RidocDocumentHistoryStepResponse)
def step_history_document(lesson_id: str, request: RidocDocumentHistoryStepRequest, response: Response, user: UserView=Depends(current_user)):
    store = _store_for_lesson(user.id, lesson_id)
    package_id = store.package_id_for_lesson(user.id, lesson_id)
    assert package_id is not None
    if workspace_state.current_repository_workspace_scope() and request.expected_version is None:
        raise HTTPException(status_code=422, detail='Repository version is required')
    try:
        with store._lock, ridoc_file_lock(store.package_path(user.id, package_id)):
            result = step_document_history(store, owner_user_id=user.id, package_id=package_id, lesson_id=lesson_id, request=request)
            package = store.load_package(user.id, package_id=package_id)
    except RidocV3ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RidocV3Error as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response.headers['Cache-Control'] = 'no-store'
    return RidocDocumentHistoryStepResponse(course_package=workspace_state.package_view(package, is_standalone=package.package_kind == 'standalone', resource_lesson_id=lesson_id, isolate_lesson_resources=package.package_kind == 'standalone'), **result)
