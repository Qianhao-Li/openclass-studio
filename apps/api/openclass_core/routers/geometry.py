from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError
from openclass_core.models import AIModelSelection, ChatAttachmentRef, SelectionRef, UserView
from openclass_core.studio_dependencies import current_user
from openclass_core.services.ai_execution_adapter import build_ai_execution_adapter
from openclass_core.services.ai_model_catalog import build_model_catalog
from openclass_core.services.chat_attachments import prepare_chat_attachments, verify_chat_attachments
from openclass_core.services.codex_app_server import CodexAppServerError
from openclass_core.services.geometry_scene import GeometryScene, generate_geometry_scene
from openclass_core.services.workspace_state import find_lesson_package, load_workspace_for_user
router = APIRouter(prefix='/api')

class GeometryGenerationRequest(BaseModel):
    selection: SelectionRef
    instructions: str = Field(default='', max_length=2000)
    attachments: list[ChatAttachmentRef] = Field(default_factory=list, max_length=10)
    text_model: AIModelSelection | None = None

def _normalized_text(value: str) -> str:
    return ' '.join(value.split())

def _validated_board_excerpt(lesson_id: str, selection: SelectionRef, document_text: str) -> str:
    excerpt = selection.excerpt.strip()
    if selection.kind != 'board':
        raise HTTPException(status_code=422, detail='Select content from the current board to generate a diagram.')
    if selection.lesson_id and selection.lesson_id != lesson_id:
        raise HTTPException(status_code=409, detail='The selection does not belong to the current page.')
    if not excerpt:
        raise HTTPException(status_code=422, detail='Select a formula or problem from the board first.')
    if len(excerpt) > 6000:
        raise HTTPException(status_code=422, detail='The selection is too long. Select a smaller range and try again.')
    normalized_excerpt = _normalized_text(excerpt)
    if normalized_excerpt not in _normalized_text(document_text):
        raise HTTPException(status_code=409, detail='The selected content changed. Select it again from the board.')
    return excerpt

@router.post('/lessons/{lesson_id}/geometry/generate', response_model=GeometryScene)
def create_geometry_scene(lesson_id: str, payload: GeometryGenerationRequest, user: UserView=Depends(current_user)) -> GeometryScene:
    workspace = load_workspace_for_user(user.id)
    package, lesson = find_lesson_package(workspace, lesson_id)
    if payload.selection.document_id and payload.selection.document_id != lesson.board_document.id:
        raise HTTPException(status_code=409, detail='The selection does not belong to the current board version.')
    excerpt = _validated_board_excerpt(lesson_id, payload.selection, lesson.board_document.content_text)
    selected_model = payload.text_model or build_model_catalog(user.id).defaults['text']
    try:
        prepared_attachments = prepare_chat_attachments(attachments=verify_chat_attachments(owner_user_id=user.id, package_id=package.id, attachments=payload.attachments))
    except CodexAppServerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        return generate_geometry_scene(adapter=build_ai_execution_adapter(selected_model, owner_user_id=user.id), source_excerpt=excerpt, instructions=payload.instructions, attachment_context=prepared_attachments.prompt_context, image_inputs=prepared_attachments.image_inputs)
    except CodexAppServerError as exc:
        raise HTTPException(status_code=503, detail='The diagram model is unavailable. Check the model connection.') from exc
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=502, detail='The model did not return a valid diagram. Adjust the selection and try again.') from exc
