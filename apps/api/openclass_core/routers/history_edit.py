from __future__ import annotations
from contextlib import contextmanager
from typing import Literal
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from openclass_core.history_edit_models import BeginHistoryEditRequest, CommitHistoryEditRequest, CommitHistoryEditResponse, CompactHistoryEditSessionView, DiscardHistoryEditRequest, DiscardHistoryEditResponse, HistoryEditSessionView, UpdateHistoryEditRequest, HistoryEditVersionRequest, HistoryRemovalImpactRequest, HistoryRemovalImpactView, HistoryEditReviewView
from openclass_core.models import LessonMergeEventPlan, UserView
from openclass_core.studio_dependencies import current_user
from openclass_core.routers.history_deletion import _store_for_lesson
from openclass_core.services import workspace_state
from openclass_core.services.ridoc_lock import ridoc_file_lock
from openclass_core.services.ridoc_v3 import RidocV3ConflictError, RidocV3Error
router = APIRouter()

@contextmanager
def _context(user: UserView, lesson_id: str, response: Response):
    store = _store_for_lesson(user.id, lesson_id)
    package_id = store.package_id_for_lesson(user.id, lesson_id)
    if package_id is None:
        raise HTTPException(status_code=404, detail='Editable RIDOC lesson not found')
    response.headers['Cache-Control'] = 'no-store'
    try:
        with store._lock, ridoc_file_lock(store.package_path(user.id, package_id)):
            yield (store, {'owner_user_id': user.id, 'package_id': package_id, 'lesson_id': lesson_id})
    except KeyError as exc:
        raise HTTPException(status_code=404, detail='History edit session is unavailable') from exc
    except RidocV3ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RidocV3Error as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

def _package_view(package, lesson_id: str):
    return workspace_state.package_view(package, is_standalone=package.package_kind == 'standalone', resource_lesson_id=lesson_id, isolate_lesson_resources=package.package_kind == 'standalone')

def _view(value, *, compact: bool=False, accept_encoding: str=''):
    from openclass_core.services.ridoc_edit_transport import compact_history_response
    if value is None:
        return None
    if compact and value.get('reuse_base_revision'):
        model = CompactHistoryEditSessionView.model_validate(value)
        return compact_history_response(model.model_dump(mode='json', exclude_unset=True), accept_encoding)
    model = HistoryEditSessionView.model_validate({**value, 'draft_course_package': _package_view(value['draft_course_package'], value['lesson_id'])})
    if compact:
        from openclass_core.services.ridoc_edit_transport import compact_history_documents
        payload = compact_history_documents(model.model_dump(mode='json'))
        return compact_history_response(payload, accept_encoding)
    return model

@router.get('/api/lessons/{lesson_id}/history/edit-session', response_model=HistoryEditSessionView | CompactHistoryEditSessionView | None)
def get_edit_session(lesson_id: str, response: Response, user: UserView=Depends(current_user), history_view: Literal['full', 'compact']=Header(default='full', alias='X-OpenClass-History-View'), accept_encoding: str=Header(default='', alias='Accept-Encoding')):
    from openclass_core.services.ridoc_edit_sessions import get_active_edit_session
    with _context(user, lesson_id, response) as (store, scope):
        return _view(get_active_edit_session(store, **scope, compact=history_view == 'compact'), compact=history_view == 'compact', accept_encoding=accept_encoding)

@router.post('/api/lessons/{lesson_id}/history/edit-session', response_model=HistoryEditSessionView | CompactHistoryEditSessionView)
def begin_edit_session(lesson_id: str, request: BeginHistoryEditRequest, response: Response, user: UserView=Depends(current_user), history_view: Literal['full', 'compact']=Header(default='full', alias='X-OpenClass-History-View'), accept_encoding: str=Header(default='', alias='Accept-Encoding')):
    from openclass_core.services.ridoc_edit_sessions import begin_edit_session as begin
    if history_view != 'compact' and workspace_state.current_repository_workspace_scope() and (request.expected_version is None):
        raise HTTPException(status_code=422, detail='Repository version is required')
    with _context(user, lesson_id, response) as (store, scope):
        return _view(begin(store, **scope, expected_revision=request.expected_ridoc_revision, expected_version=request.expected_version, compact=history_view == 'compact'), compact=history_view == 'compact', accept_encoding=accept_encoding)

@router.patch('/api/lessons/{lesson_id}/history/edit-session/{session_id}', response_model=HistoryEditSessionView | CompactHistoryEditSessionView)
def update_edit_session(lesson_id: str, session_id: str, request: UpdateHistoryEditRequest, response: Response, user: UserView=Depends(current_user), history_view: Literal['full', 'compact']=Header(default='full', alias='X-OpenClass-History-View'), accept_encoding: str=Header(default='', alias='Accept-Encoding')):
    from openclass_core.services.ridoc_edit_sessions import update_edit_session as update
    with _context(user, lesson_id, response) as (store, scope):
        return _view(update(store, **scope, session_id=session_id, expected_draft_version=request.expected_draft_version, operation_id=request.operation_id, operation=request.operation.model_dump(mode='json', exclude_none=True), impact_token=request.impact_token, compact=history_view == 'compact'), compact=history_view == 'compact', accept_encoding=accept_encoding)

@router.post('/api/lessons/{lesson_id}/history/edit-session/{session_id}/impact', response_model=HistoryRemovalImpactView)
def preview_edit_removal(lesson_id: str, session_id: str, request: HistoryRemovalImpactRequest, response: Response, user: UserView=Depends(current_user)):
    from openclass_core.services.ridoc_edit_sessions import preview_edit_removal as preview
    with _context(user, lesson_id, response) as (store, scope):
        return preview(store, **scope, session_id=session_id, node_id=request.node_id, node_ids=request.node_ids, expected_draft_version=request.expected_draft_version)

@router.post('/api/lessons/{lesson_id}/history/edit-session/{session_id}/review', response_model=HistoryEditReviewView)
def review_edit_session(lesson_id: str, session_id: str, request: HistoryEditVersionRequest, response: Response, user: UserView=Depends(current_user)):
    from openclass_core.services.ridoc_edit_sessions import review_edit_session as review
    with _context(user, lesson_id, response) as (store, scope):
        return review(store, **scope, session_id=session_id, expected_draft_version=request.expected_draft_version)

@router.post('/api/lessons/{lesson_id}/history/edit-session/{session_id}/pause', response_model=HistoryEditSessionView | CompactHistoryEditSessionView)
def pause_edit_session(lesson_id: str, session_id: str, request: HistoryEditVersionRequest, response: Response, user: UserView=Depends(current_user), history_view: Literal['full', 'compact']=Header(default='full', alias='X-OpenClass-History-View'), accept_encoding: str=Header(default='', alias='Accept-Encoding')):
    from openclass_core.services.ridoc_edit_sessions import pause_edit_session as pause
    with _context(user, lesson_id, response) as (store, scope):
        return _view(pause(store, **scope, session_id=session_id, expected_draft_version=request.expected_draft_version, compact=history_view == 'compact'), compact=history_view == 'compact', accept_encoding=accept_encoding)

@router.get('/api/lessons/{lesson_id}/history/edit-session/{session_id}/merge-plan', response_model=LessonMergeEventPlan)
def get_edit_merge_plan(lesson_id: str, session_id: str, source_branch_name: str, response: Response, user: UserView=Depends(current_user)):
    from openclass_core.services.ridoc_edit_sessions import get_edit_merge_plan as plan
    with _context(user, lesson_id, response) as (store, scope):
        return LessonMergeEventPlan.model_validate(plan(store, **scope, session_id=session_id, source_branch_name=source_branch_name))

@router.post('/api/lessons/{lesson_id}/history/edit-session/{session_id}/commit', response_model=CommitHistoryEditResponse)
def commit_edit_session(lesson_id: str, session_id: str, request: CommitHistoryEditRequest, response: Response, user: UserView=Depends(current_user)):
    from openclass_core.services.ridoc_edit_sessions import commit_edit_session as commit
    with _context(user, lesson_id, response) as (store, scope):
        result = commit(store, **scope, session_id=session_id, expected_draft_version=request.expected_draft_version, mutation_id=request.mutation_id, confirmation_token=request.confirmation_token)
        package = store.load_package(user.id, package_id=scope['package_id'])
        return CommitHistoryEditResponse(**result, course_package=_package_view(package, lesson_id))

@router.post('/api/lessons/{lesson_id}/history/edit-session/{session_id}/discard', response_model=DiscardHistoryEditResponse)
def discard_edit_session(lesson_id: str, session_id: str, request: DiscardHistoryEditRequest, response: Response, user: UserView=Depends(current_user)):
    from openclass_core.services.ridoc_edit_sessions import discard_edit_session as discard
    with _context(user, lesson_id, response) as (store, scope):
        discard(store, **scope, session_id=session_id, expected_draft_version=request.expected_draft_version)
        return DiscardHistoryEditResponse(session_id=session_id)
