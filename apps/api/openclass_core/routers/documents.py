from __future__ import annotations
import json
import shutil
import uuid
from pathlib import Path
from typing import Annotated, Literal
from zipfile import BadZipFile
from docx.opc.exceptions import PackageNotFoundError
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from lxml.etree import XMLSyntaxError
from starlette.background import BackgroundTask
from openclass_core.models import BoardDocument, BoardSegmentKind, ChatCommitVisibilityRequest, ChatResponse, CoursePackageView, CreateBranchRequest, DeleteBranchRequest, DocumentAIEditRequest, DocumentHistoryStepRequest, EnsureDocumentHistoryBranchRequest, DocumentSaveRequest, DocumentSaveDelta, DocumentSegmentSearchResponse, ManualCommitRequest, RestoreCommitRequest, RenameBranchRequest, RidocHistoryDocumentView, RidocHistoryPageView, SwitchBranchRequest, UserView, WorkspaceState
from openclass_core.studio_dependencies import current_user
from openclass_core.services.chat_service import document_ai_edit_request
from openclass_core.services.board_asset_store import get_board_asset_store
from openclass_core.services.chat_commit_visibility import ChatCommitVisibilityError, set_chat_commit_visibility
from openclass_core.services.history import create_branch, current_document_commit, current_head_commit, ensure_document_divergence_branch, restore_commit, step_document_history, switch_branch
from openclass_core.services.html_document_export import HtmlExportBudgetError, export_html
from openclass_core.services.lesson_package_format import RIDOC_MEDIA_TYPE, RidocFormatError
from openclass_core.services.lesson_package_export import export_lesson_ridoc
from openclass_core.services.rich_document import document_changed, export_docx, import_docx, is_document_empty, rich_structure_counts, rich_structure_score, would_flatten_rich_document
from openclass_core.services.route_context import bind_ai_request_context
from openclass_core.services.ridoc_history import history_document, history_page
from openclass_core.services.ridoc_v3 import RIDOC_V3_MEDIA_TYPE, RidocV3ConflictError, RidocV3Error
from openclass_core.services.ridoc_workspace_store import document_at_history_node, resolve_history_branch_parent
from openclass_core.services.workspace_state import EXPORT_DIR, UPLOAD_DIR, commit_document_snapshot, find_lesson_package, load_workspace_for_user, load_workspace_for_user_with_revision, package_view_for_lesson, get_course_store, get_ridoc_workspace_store, is_ridoc_lesson_for_user, save_workspace_for_user_if_revision, search_document_segments_for_user
router = APIRouter()
_DOCX_NO_STORE_HEADERS = {'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0', 'Pragma': 'no-cache', 'Expires': '0'}
_BOARD_ASSET_CACHE_CONTROL = 'private, max-age=86400, immutable'

@router.get('/api/lessons/{lesson_id}/history/nodes', response_model=RidocHistoryPageView)
def get_ridoc_history_nodes(lesson_id: str, response: Response, branch: str | None=None, scope: Literal['branch', 'all']=Query('branch'), cursor: str | None=None, limit: int=Query(100, ge=1, le=300), q: str='', user: UserView=Depends(current_user), include_replay_documents: bool=True) -> RidocHistoryPageView:
    if not is_ridoc_lesson_for_user(user.id, lesson_id):
        raise HTTPException(status_code=404, detail='RIDOC lesson not found')
    from openclass_core.services.ridoc_lock import ridoc_file_lock
    store = get_ridoc_workspace_store()
    package_id = store.package_id_for_lesson(user.id, lesson_id)
    assert package_id is not None
    try:
        with store._lock, ridoc_file_lock(store.package_path(user.id, package_id)):
            archive = store.load_archive(user.id, lesson_id=lesson_id)
            result = history_page(archive, lesson_id, branch_name=branch, scope=scope, cursor=cursor, limit=limit, query=q, store=store, owner_user_id=user.id, include_replay_documents=include_replay_documents)
    except RidocV3Error as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response.headers['Cache-Control'] = 'no-store'
    return result

@router.get('/api/lessons/{lesson_id}/history/nodes/{node_id}/document', response_model=RidocHistoryDocumentView)
def get_ridoc_history_document(lesson_id: str, node_id: str, response: Response, user: UserView=Depends(current_user)) -> RidocHistoryDocumentView:
    if not is_ridoc_lesson_for_user(user.id, lesson_id):
        raise HTTPException(status_code=404, detail='RIDOC lesson not found')
    try:
        archive = get_ridoc_workspace_store().load_archive(user.id, lesson_id=lesson_id)
        result = history_document(archive, lesson_id, node_id)
    except RidocV3Error as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    response.headers['Cache-Control'] = 'no-store'
    return result

def _clear_transient_ai_state(lesson) -> None:
    lesson.board_teaching_guide = None
    lesson.board_teaching_progress = None
    lesson.learning_requirements = None
    lesson.board_task_requirements = None

@router.get('/api/documents/search', response_model=DocumentSegmentSearchResponse)
def search_documents(q: str='', kind: BoardSegmentKind | None=None, limit: int=Query(20, ge=1, le=100), user: UserView=Depends(current_user)) -> DocumentSegmentSearchResponse:
    return DocumentSegmentSearchResponse(query=q, kind=kind, results=search_document_segments_for_user(user.id, q, kind=kind, limit=limit))

@router.get('/api/board-assets/{asset_id}/content')
def get_board_asset_content(asset_id: str, request: Request, user: UserView=Depends(current_user)) -> Response:
    from openclass_core.services.ridoc_board_assets import read_owned_board_asset
    stored = read_owned_board_asset(asset_id, user.id, asset_store=get_board_asset_store())
    if stored is None:
        raise HTTPException(status_code=404, detail='The board image does not exist.')
    record, content = stored
    etag = f'"{record.content_hash}"'
    headers = {'Cache-Control': _BOARD_ASSET_CACHE_CONTROL, 'Content-Disposition': f'inline; filename="{record.id}{_board_asset_extension(record.mime_type)}"', 'ETag': etag, 'X-Content-Type-Options': 'nosniff'}
    if _etag_matches(request.headers.get('if-none-match', ''), etag):
        return Response(status_code=304, headers=headers)
    return Response(content=content, media_type=record.mime_type, headers=headers)

def _etag_matches(raw_header: str, etag: str) -> bool:
    if not raw_header:
        return False
    expected = etag.removeprefix('W/')
    for candidate in raw_header.split(','):
        candidate = candidate.strip()
        if candidate == '*' or candidate.removeprefix('W/') == expected:
            return True
    return False

def _board_asset_extension(mime_type: str) -> str:
    return {'image/gif': '.gif', 'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}.get(mime_type, '.bin')

def _document_save_delta(*, package_id: str, lesson, workspace_revision: int, changed: bool) -> DocumentSaveDelta:
    head = current_head_commit(lesson)
    branch = lesson.history_graph.branches[lesson.history_graph.current_branch]
    return DocumentSaveDelta(lesson_id=lesson.id, package_id=package_id, workspace_revision=workspace_revision, document=lesson.board_document, latest_commit=head, current_branch=lesson.history_graph.current_branch, branch_head_commit_id=head.id, branch_base_commit_id=branch.base_commit_id, updated_at=lesson.updated_at, changed=changed)

def _legacy_document_package(user_id: str, lesson_id: str) -> CoursePackageView:
    workspace = load_workspace_for_user(user_id)
    package, lesson = find_lesson_package(workspace, lesson_id)
    return package_view_for_lesson(workspace, package, lesson.id)

def _save_document_request(lesson_id: str, request: DocumentSaveRequest, user_id: str, *, response_mode: Literal['full', 'delta']='full') -> CoursePackageView | DocumentSaveDelta:
    if is_ridoc_lesson_for_user(user_id, lesson_id):
        expected_revision = (request.expected_ridoc_revision or '').strip()
        transaction_id = (request.transaction_id or '').strip()
        if not expected_revision or not transaction_id:
            raise HTTPException(status_code=409, detail='RIDOC document saves require revision and transaction identity')
        store = get_ridoc_workspace_store()
        package_id = store.package_id_for_lesson(user_id, lesson_id)
        if package_id is None:
            raise HTTPException(status_code=404, detail=f'Unknown lesson {lesson_id}')
        try:
            before = store.load_package(user_id, package_id=package_id)
            _package, before_lesson = find_lesson_package(WorkspaceState(packages=[before], active_package_id=package_id), lesson_id)
            base_document = before_lesson.board_document
            branch_action = request.history_branch_action
            if branch_action is not None:
                archive = store.load_archive(user_id, package_id=package_id)
                _source_node, parent_id = resolve_history_branch_parent(archive, lesson_id, branch_action.kind, branch_action.source_node_id)
                base_document = document_at_history_node(archive, lesson_id, parent_id)
            changed = document_changed(base_document, request.document.model_copy(update={'title': base_document.title}))
            record = store.commit_document(owner_user_id=user_id, package_id=package_id, lesson_id=lesson_id, expected_revision=expected_revision, transaction_id=transaction_id, document=request.document, source='manual', label=request.label, branch_action_kind=branch_action.kind if branch_action else None, branch_source_node_id=branch_action.source_node_id if branch_action else '')
        except RidocV3ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RidocV3Error as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        package = store.load_package(user_id, package_id=package_id)
        _package, lesson = find_lesson_package(WorkspaceState(packages=[package], active_package_id=package_id), lesson_id)
        _clear_transient_ai_state(lesson)
        store.save_lesson_runtime(owner_user_id=user_id, package_id=package_id, lesson=lesson)
        if response_mode == 'delta':
            head = current_head_commit(lesson)
            branch = lesson.history_graph.branches[lesson.history_graph.current_branch]
            return DocumentSaveDelta(lesson_id=lesson.id, package_id=package_id, workspace_revision=0, document=lesson.board_document, latest_commit=head, current_branch=lesson.history_graph.current_branch, branch_head_commit_id=head.id, branch_base_commit_id=branch.base_commit_id, updated_at=lesson.updated_at, changed=changed, storage_kind='ridoc', ridoc_revision=record.revision)
        workspace = WorkspaceState(packages=[package], active_package_id=package_id)
        return package_view_for_lesson(workspace, package, lesson_id)
    if request.history_branch_action is not None:
        raise HTTPException(status_code=409, detail='History branch actions require a RIDOC lesson')
    context = get_course_store().load_lesson_document_context_for_user(user_id, lesson_id)
    if context is None:
        raise HTTPException(status_code=404, detail=f'Unknown lesson {lesson_id}')
    lesson = context.lesson
    current_head = current_head_commit(lesson)
    is_autosave = request.metadata.get('autosave') is True or request.metadata.get('kind') == 'auto_document_save'
    allow_structure_removal = request.metadata.get('structure_removal_intent') is True
    guard_document: BoardDocument | None = None
    if request.base_commit_id and request.base_commit_id != current_head.id:
        if is_autosave:
            if response_mode == 'delta':
                return _document_save_delta(package_id=context.package_id, lesson=lesson, workspace_revision=context.workspace_revision, changed=False)
            return _legacy_document_package(user_id, lesson_id)
        raise HTTPException(status_code=409, detail='The document changed before this save. Refresh before saving again.')
    if not document_changed(lesson.board_document, request.document):
        if response_mode == 'delta':
            return _document_save_delta(package_id=context.package_id, lesson=lesson, workspace_revision=context.workspace_revision, changed=False)
        return _legacy_document_package(user_id, lesson_id)
    if is_autosave:
        guard_document = _recent_structured_snapshot_for_autosave(lesson, current_head, request.document) or current_head.snapshot
        if would_flatten_rich_document(current_document=guard_document, new_document=request.document, allow_structure_removal=allow_structure_removal):
            if response_mode == 'delta':
                return _document_save_delta(package_id=context.package_id, lesson=lesson, workspace_revision=context.workspace_revision, changed=False)
            return _legacy_document_package(user_id, lesson_id)
    with bind_ai_request_context('/api/lessons/{lesson_id}/document/save', lesson=lesson, trace_prefix='document_save'):
        previous_updated_at = lesson.updated_at
        lesson.board_document = request.document
        _clear_transient_ai_state(lesson)
        commit_metadata: dict[str, object] = {'kind': 'manual_document_save', **request.metadata, **_document_save_structure_metadata(base_commit_id=request.base_commit_id or current_head.id, current_head_commit_id=current_head.id, before_document=current_head.snapshot, after_document=request.document, flatten_guard_evaluated=is_autosave)}
        commit_document_snapshot(lesson, label=request.label, message=request.message, metadata=commit_metadata)
        committed_head = current_head_commit(lesson)
        if is_autosave and guard_document and would_flatten_rich_document(current_document=guard_document, new_document=committed_head.snapshot, allow_structure_removal=allow_structure_removal):
            _rollback_unpersisted_commit(lesson, commit_id=committed_head.id, restore_head=current_head, updated_at=previous_updated_at)
            if response_mode == 'delta':
                return _document_save_delta(package_id=context.package_id, lesson=lesson, workspace_revision=context.workspace_revision, changed=False)
            return _legacy_document_package(user_id, lesson_id)
        committed_head.metadata.update(_document_save_structure_metadata(base_commit_id=request.base_commit_id or current_head.id, current_head_commit_id=current_head.id, before_document=current_head.snapshot, after_document=committed_head.snapshot, flatten_guard_evaluated=is_autosave))
        saved_revision = get_course_store().save_document_for_user_if_head(user_id, lesson, expected_branch_name=lesson.history_graph.current_branch, expected_head_commit_id=current_head.id)
        if saved_revision is None:
            raise HTTPException(status_code=409, detail='The document changed before this save. Refresh before saving again.')
    if response_mode == 'delta':
        return _document_save_delta(package_id=context.package_id, lesson=lesson, workspace_revision=saved_revision, changed=True)
    return _legacy_document_package(user_id, lesson_id)

def _visible_document_text(document: BoardDocument) -> str:
    return ' '.join((document.content_text or '').split())

def _structured_document_score(document: BoardDocument) -> int:
    return rich_structure_score(rich_structure_counts(document))

def _recent_structured_snapshot_for_autosave(lesson, current_head, new_document: BoardDocument) -> BoardDocument | None:
    target_text = _visible_document_text(new_document)
    if not target_text:
        return None
    commits_by_id = {commit.id: commit for commit in lesson.history_graph.commits}
    cursor = current_head
    seen: set[str] = set()
    while cursor and cursor.id not in seen:
        seen.add(cursor.id)
        if _structured_document_score(cursor.snapshot) >= 8 and _visible_document_text(cursor.snapshot) == target_text:
            return cursor.snapshot
        parent_id = cursor.parent_ids[0] if cursor.parent_ids else None
        cursor = commits_by_id.get(parent_id) if parent_id else None
    return None

def _rollback_unpersisted_commit(lesson, *, commit_id: str, restore_head, updated_at: str) -> None:
    branch = lesson.history_graph.branches[lesson.history_graph.current_branch]
    branch.head_commit_id = restore_head.id
    lesson.board_document = restore_head.snapshot
    lesson.updated_at = updated_at
    lesson.history_graph.commits = [commit for commit in lesson.history_graph.commits if commit.id != commit_id]

def _document_save_structure_metadata(*, base_commit_id: str, current_head_commit_id: str, before_document: BoardDocument, after_document: BoardDocument, flatten_guard_evaluated: bool) -> dict[str, object]:
    before_counts = rich_structure_counts(before_document)
    after_counts = rich_structure_counts(after_document)
    return {'base_commit_id': base_commit_id, 'current_head_commit_id': current_head_commit_id, 'structure_before': before_counts, 'structure_after': after_counts, 'structure_score_before': rich_structure_score(before_counts), 'structure_score_after': rich_structure_score(after_counts), 'flatten_guard_evaluated': flatten_guard_evaluated, 'flatten_guard_triggered': False}

@router.post('/api/lessons/{lesson_id}/manual-commit', response_model=CoursePackageView)
def manual_commit(lesson_id: str, request: ManualCommitRequest, user: UserView=Depends(current_user)) -> CoursePackageView:
    workspace, revision = load_workspace_for_user_with_revision(user.id)
    package, lesson = find_lesson_package(workspace, lesson_id)
    package.active_lesson_id = lesson.id
    with bind_ai_request_context('/api/lessons/{lesson_id}/manual-commit', lesson=lesson, trace_prefix='manual_commit', commit_label=request.label):
        if request.document is not None:
            lesson.board_document = request.document
        _clear_transient_ai_state(lesson)
        commit_document_snapshot(lesson, label=request.label, message=request.message, metadata={'kind': 'manual_document_edit'})
        save_workspace_for_user_if_revision(user.id, workspace, expected_revision=revision)
    return package_view_for_lesson(workspace, package, lesson.id)

@router.post('/api/lessons/{lesson_id}/document/save', response_model=CoursePackageView | DocumentSaveDelta)
def save_document(lesson_id: str, request: DocumentSaveRequest, response_mode: Literal['full', 'delta']=Query('full'), user: UserView=Depends(current_user)) -> CoursePackageView | DocumentSaveDelta:
    return _save_document_request(lesson_id, request, user.id, response_mode=response_mode)

@router.post('/api/lessons/{lesson_id}/document/save-beacon', response_model=CoursePackageView | DocumentSaveDelta)
async def save_document_beacon(lesson_id: str, request: Request, response_mode: Literal['full', 'delta']=Query('full'), user: UserView=Depends(current_user)) -> CoursePackageView | DocumentSaveDelta:
    try:
        payload = json.loads((await request.body()).decode('utf-8'))
        save_request = DocumentSaveRequest.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail='Invalid document save payload') from exc
    return _save_document_request(lesson_id, save_request, user.id, response_mode=response_mode)

@router.post('/api/lessons/{lesson_id}/document/ai-edit', response_model=ChatResponse)
def ai_edit_document(lesson_id: str, request: DocumentAIEditRequest, user: UserView=Depends(current_user)) -> ChatResponse:
    return document_ai_edit_request(lesson_id, request.instruction, request.selection_text, request.conversation, user_id=user.id)

def _parse_uploaded_docx(file: UploadFile, *, title: str | None=None) -> BoardDocument:
    safe_name = Path(file.filename or 'document.docx').name
    if Path(safe_name).suffix.lower() != '.docx':
        raise HTTPException(status_code=400, detail='Select a Word document in .docx format.')
    destination = UPLOAD_DIR / f'docx-import-{uuid.uuid4().hex}.docx'
    try:
        with destination.open('wb') as output:
            shutil.copyfileobj(file.file, output)
        try:
            return import_docx(destination, title=title or Path(safe_name).stem)
        except (BadZipFile, PackageNotFoundError, XMLSyntaxError, KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail='Could not read this Word document. Check that the file is intact and uses .docx format.') from exc
    finally:
        destination.unlink(missing_ok=True)

@router.post('/api/documents/import-docx', response_model=BoardDocument)
def parse_document_docx(file: UploadFile=File(...), title: str | None=Form(None), user: UserView=Depends(current_user)) -> BoardDocument:
    """Parse an upload; the caller saves through its active document scope."""
    return _parse_uploaded_docx(file, title=title)

@router.post('/api/lessons/{lesson_id}/document/import-docx', response_model=CoursePackageView)
def import_document_docx(lesson_id: str, file: UploadFile=File(...), user: UserView=Depends(current_user), expected_ridoc_revision: Annotated[str | None, Query(min_length=64, max_length=64)]=None, transaction_id: Annotated[str | None, Query(min_length=1, max_length=200)]=None) -> CoursePackageView:
    workspace = load_workspace_for_user(user.id)
    _, lesson = find_lesson_package(workspace, lesson_id)
    if is_ridoc_lesson_for_user(user.id, lesson_id) and (not (expected_ridoc_revision or '').strip() or not (transaction_id or '').strip()):
        raise HTTPException(status_code=409, detail='RIDOC document saves require revision and transaction identity')
    safe_name = Path(file.filename or 'document.docx').name
    with bind_ai_request_context('/api/lessons/{lesson_id}/document/import-docx', lesson=lesson, trace_prefix='import_docx', filename=safe_name):
        document = _parse_uploaded_docx(file, title=lesson.board_document.title or lesson.title)
        return _save_document_request(lesson_id, DocumentSaveRequest(document=document.model_copy(update={'id': lesson.board_document.id}), label='Import DOCX', message=f'Imported {safe_name} into the rich document editor', metadata={'kind': 'import_docx', 'filename': safe_name}, base_commit_id=current_head_commit(lesson).id, expected_ridoc_revision=expected_ridoc_revision, transaction_id=transaction_id), user.id)

@router.get('/api/lessons/{lesson_id}/document/export-docx')
def export_document_docx(lesson_id: str, user: UserView=Depends(current_user)) -> FileResponse:
    workspace = load_workspace_for_user(user.id)
    _, lesson = find_lesson_package(workspace, lesson_id)
    document = _exportable_document_for_lesson(lesson)
    if is_document_empty(document):
        raise HTTPException(status_code=409, detail='The board document is empty and cannot be exported as DOCX.')
    target_path = _unique_export_path('docx')
    export_docx(document, target_path, owner_user_id=user.id)
    return FileResponse(target_path, media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document', filename=f'{lesson.slug or lesson.id}.docx', headers=_DOCX_NO_STORE_HEADERS, background=BackgroundTask(target_path.unlink, missing_ok=True))

@router.get('/api/lessons/{lesson_id}/document/export-html')
def export_document_html(lesson_id: str, user: UserView=Depends(current_user)) -> FileResponse:
    workspace = load_workspace_for_user(user.id)
    _, lesson = find_lesson_package(workspace, lesson_id)
    document = _exportable_document_for_lesson(lesson)
    if is_document_empty(document):
        raise HTTPException(status_code=409, detail='The board document is empty and cannot be exported as HTML.')
    target_path = _unique_export_path('html')
    try:
        export_html(document, target_path, owner_user_id=user.id)
    except HtmlExportBudgetError as exc:
        target_path.unlink(missing_ok=True)
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    return FileResponse(target_path, media_type='text/html; charset=utf-8', filename=f'{lesson.slug or lesson.id}.html', headers=_DOCX_NO_STORE_HEADERS, background=BackgroundTask(target_path.unlink, missing_ok=True))

@router.get('/api/lessons/{lesson_id}/document/export-ridoc')
def export_lesson_package(lesson_id: str, source_mode: str=Query('evidence', pattern='^(evidence|references)$'), user: UserView=Depends(current_user)) -> FileResponse:
    if is_ridoc_lesson_for_user(user.id, lesson_id):
        store = get_ridoc_workspace_store()
        package_id = store.package_id_for_lesson(user.id, lesson_id)
        if package_id is None:
            raise HTTPException(status_code=404, detail='RIDOC lesson not found')
        package = store.load_package(user.id, package_id=package_id)
        return FileResponse(store.package_path(user.id, package_id), media_type=RIDOC_V3_MEDIA_TYPE, filename=f'{package.id}.ridoc', headers=_DOCX_NO_STORE_HEADERS)
    workspace = load_workspace_for_user(user.id)
    package, lesson = find_lesson_package(workspace, lesson_id)
    target_path = _unique_export_path('ridoc')
    try:
        export_lesson_ridoc(owner_user_id=user.id, package_id=package.id, lesson=lesson, target_path=target_path, source_mode=source_mode)
    except RidocFormatError as exc:
        target_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return FileResponse(target_path, media_type=RIDOC_MEDIA_TYPE, filename=f'{lesson.slug or lesson.id}.ridoc', headers=_DOCX_NO_STORE_HEADERS, background=BackgroundTask(target_path.unlink, missing_ok=True))

def _unique_export_path(extension: str) -> Path:
    safe_extension = extension.lower().lstrip('.')
    if safe_extension not in {'docx', 'html', 'ridoc'}:
        raise ValueError('Unsupported document export extension.')
    return EXPORT_DIR / 'requests' / f'{uuid.uuid4().hex}.{safe_extension}'

def _exportable_document_for_lesson(lesson) -> BoardDocument:
    if not is_document_empty(lesson.board_document):
        return lesson.board_document
    try:
        head_snapshot = current_document_commit(lesson).snapshot
    except Exception:
        return lesson.board_document
    if not is_document_empty(head_snapshot):
        return head_snapshot
    return lesson.board_document

@router.post('/api/lessons/{lesson_id}/branches', response_model=CoursePackageView)
def create_lesson_branch(lesson_id: str, request: CreateBranchRequest, user: UserView=Depends(current_user)) -> CoursePackageView:
    if is_ridoc_lesson_for_user(user.id, lesson_id):
        expected = (request.expected_ridoc_revision or '').strip()
        if not expected:
            raise HTTPException(status_code=409, detail='RIDOC revision is required')
        store = get_ridoc_workspace_store()
        package_id = store.package_id_for_lesson(user.id, lesson_id)
        try:
            store.create_branch(owner_user_id=user.id, package_id=package_id or '', lesson_id=lesson_id, expected_revision=expected, name=request.name, from_node_id=request.from_commit_id)
        except RidocV3Error as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        package = store.load_package(user.id, package_id=package_id)
        return package_view_for_lesson(WorkspaceState(packages=[package], active_package_id=package.id), package, lesson_id)
    workspace, revision = load_workspace_for_user_with_revision(user.id)
    package, lesson = find_lesson_package(workspace, lesson_id)
    package.active_lesson_id = lesson.id
    with bind_ai_request_context('/api/lessons/{lesson_id}/branches', lesson=lesson, trace_prefix='create_branch', branch_name=request.name, from_commit_id=request.from_commit_id):
        create_branch(lesson, request.name, request.from_commit_id)
        save_workspace_for_user_if_revision(user.id, workspace, expected_revision=revision)
    return package_view_for_lesson(workspace, package, lesson.id)

@router.post('/api/lessons/{lesson_id}/branches/checkout', response_model=CoursePackageView)
def checkout_lesson_branch(lesson_id: str, request: SwitchBranchRequest, user: UserView=Depends(current_user)) -> CoursePackageView:
    if is_ridoc_lesson_for_user(user.id, lesson_id):
        expected = (request.expected_ridoc_revision or '').strip()
        if not expected:
            raise HTTPException(status_code=409, detail='RIDOC revision is required')
        store = get_ridoc_workspace_store()
        package_id = store.package_id_for_lesson(user.id, lesson_id)
        try:
            store.switch_branch(owner_user_id=user.id, package_id=package_id or '', lesson_id=lesson_id, expected_revision=expected, name=request.name)
        except RidocV3Error as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        package = store.load_package(user.id, package_id=package_id)
        return package_view_for_lesson(WorkspaceState(packages=[package], active_package_id=package.id), package, lesson_id)
    workspace, revision = load_workspace_for_user_with_revision(user.id)
    package, lesson = find_lesson_package(workspace, lesson_id)
    package.active_lesson_id = lesson.id
    with bind_ai_request_context('/api/lessons/{lesson_id}/branches/checkout', lesson=lesson, trace_prefix='switch_branch', branch_name=request.name):
        switch_branch(lesson, request.name)
        save_workspace_for_user_if_revision(user.id, workspace, expected_revision=revision)
    return package_view_for_lesson(workspace, package, lesson.id)

@router.post('/api/lessons/{lesson_id}/branches/rename', response_model=CoursePackageView)
def rename_lesson_branch(lesson_id: str, request: RenameBranchRequest, user: UserView=Depends(current_user)) -> CoursePackageView:
    if not is_ridoc_lesson_for_user(user.id, lesson_id):
        raise HTTPException(status_code=404, detail='RIDOC lesson not found')
    store = get_ridoc_workspace_store()
    package_id = store.package_id_for_lesson(user.id, lesson_id)
    try:
        store.rename_branch(owner_user_id=user.id, package_id=package_id or '', lesson_id=lesson_id, expected_revision=request.expected_ridoc_revision, old_name=request.old_name, new_name=request.new_name)
    except RidocV3Error as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    package = store.load_package(user.id, package_id=package_id)
    return package_view_for_lesson(WorkspaceState(packages=[package], active_package_id=package.id), package, lesson_id)

@router.post('/api/lessons/{lesson_id}/branches/delete', response_model=CoursePackageView)
def delete_lesson_branch(lesson_id: str, request: DeleteBranchRequest, user: UserView=Depends(current_user)) -> CoursePackageView:
    if not is_ridoc_lesson_for_user(user.id, lesson_id):
        raise HTTPException(status_code=404, detail='RIDOC lesson not found')
    store = get_ridoc_workspace_store()
    package_id = store.package_id_for_lesson(user.id, lesson_id)
    try:
        store.delete_branch(owner_user_id=user.id, package_id=package_id or '', lesson_id=lesson_id, expected_revision=request.expected_ridoc_revision, name=request.name)
    except RidocV3Error as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    package = store.load_package(user.id, package_id=package_id)
    return package_view_for_lesson(WorkspaceState(packages=[package], active_package_id=package.id), package, lesson_id)

@router.post('/api/lessons/{lesson_id}/restore', response_model=CoursePackageView)
def restore_lesson_commit(lesson_id: str, request: RestoreCommitRequest, user: UserView=Depends(current_user)) -> CoursePackageView:
    workspace, revision = load_workspace_for_user_with_revision(user.id)
    package, lesson = find_lesson_package(workspace, lesson_id)
    package.active_lesson_id = lesson.id
    with bind_ai_request_context('/api/lessons/{lesson_id}/restore', lesson=lesson, trace_prefix='restore_commit', commit_id=request.commit_id, restore_label=request.label):
        restore_commit(lesson, request.commit_id, request.label)
        save_workspace_for_user_if_revision(user.id, workspace, expected_revision=revision)
    return package_view_for_lesson(workspace, package, lesson.id)

@router.post('/api/lessons/{lesson_id}/document-history/step', response_model=CoursePackageView)
def step_lesson_document_history(lesson_id: str, request: DocumentHistoryStepRequest, user: UserView=Depends(current_user)) -> CoursePackageView:
    if is_ridoc_lesson_for_user(user.id, lesson_id):
        raise HTTPException(status_code=409, detail='RIDOC document history requires the native revision-aware step endpoint')
    workspace, revision = load_workspace_for_user_with_revision(user.id)
    package, lesson = find_lesson_package(workspace, lesson_id)
    package.active_lesson_id = lesson.id
    try:
        step_document_history(lesson, direction=request.direction, target_commit_id=request.target_commit_id, expected_branch_name=request.expected_branch_name, expected_head_commit_id=request.expected_head_commit_id, expected_document_cursor_commit_id=request.expected_document_cursor_commit_id)
        save_workspace_for_user_if_revision(user.id, workspace, expected_revision=revision)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return package_view_for_lesson(workspace, package, lesson.id)

@router.post('/api/lessons/{lesson_id}/document-history/ensure-branch', response_model=CoursePackageView)
def ensure_lesson_document_history_branch(lesson_id: str, request: EnsureDocumentHistoryBranchRequest, user: UserView=Depends(current_user)) -> CoursePackageView:
    if is_ridoc_lesson_for_user(user.id, lesson_id):
        raise HTTPException(status_code=409, detail='RIDOC document branches are created atomically after a successful outcome')
    workspace, revision = load_workspace_for_user_with_revision(user.id)
    package, lesson = find_lesson_package(workspace, lesson_id)
    package.active_lesson_id = lesson.id
    try:
        ensure_document_divergence_branch(lesson, expected_branch_name=request.expected_branch_name, expected_head_commit_id=request.expected_head_commit_id, expected_document_cursor_commit_id=request.expected_document_cursor_commit_id)
        save_workspace_for_user_if_revision(user.id, workspace, expected_revision=revision)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return package_view_for_lesson(workspace, package, lesson.id)

@router.post('/api/lessons/{lesson_id}/commits/{commit_id}/chat-visibility', response_model=CoursePackageView)
def update_lesson_chat_commit_visibility(lesson_id: str, commit_id: str, request: ChatCommitVisibilityRequest, user: UserView=Depends(current_user)) -> CoursePackageView:
    workspace, revision = load_workspace_for_user_with_revision(user.id)
    package, lesson = find_lesson_package(workspace, lesson_id)
    package.active_lesson_id = lesson.id
    with bind_ai_request_context('/api/lessons/{lesson_id}/commits/{commit_id}/chat-visibility', lesson=lesson, trace_prefix='chat_commit_visibility', commit_id=commit_id, chat_visibility=request.visibility):
        try:
            set_chat_commit_visibility(lesson, commit_id, request.visibility)
        except ChatCommitVisibilityError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        save_workspace_for_user_if_revision(user.id, workspace, expected_revision=revision)
    return package_view_for_lesson(workspace, package, lesson.id)
