from __future__ import annotations
from typing import Literal
from pathlib import Path
import json
from collections.abc import AsyncIterator
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool
from openclass_core.models import AIModelSelection, RepositoryMapView, SourceCatalogBatchView, SourceCatalogView, SourceIngestionJob, SourceIngestionRecord, SourceStructureView, UserView
from openclass_core.studio_dependencies import current_user
from openclass_core.services import workspace_state
from openclass_core.services.source_evidence_store import source_evidence_store
from openclass_core.services.source_ingestion_service import SourceIngestionError, source_download_path, source_ingestion_service
from openclass_core.services.source_ingestion_jobs import source_ingestion_task_manager
from openclass_core.services.source_structure_indexer import source_structure_indexer
from openclass_core.services.source_structure_store import source_structure_store
from openclass_core.services.repository_store import repository_store
from openclass_core.services.google_sources.presentation import public_source

async def _bind_repository_source_scope(request: Request, user: UserView=Depends(current_user)) -> AsyncIterator[None]:
    package_id = str(request.path_params.get('package_id') or '')
    if not package_id.startswith('repo__'):
        yield
        return
    parts = package_id.split('__', 2)
    if len(parts) != 3 or not parts[1] or (not parts[2]):
        raise HTTPException(status_code=400, detail='Invalid repository source scope')
    with workspace_state.bind_repository_workspace(parts[1], user.id, package_id_override=package_id):
        yield
router = APIRouter(dependencies=[Depends(_bind_repository_source_scope)])

class SourceUpdateRequest(BaseModel):
    title: str

class SourceContentView(BaseModel):
    source: SourceIngestionRecord
    content: str

class SourceContentUpdateRequest(BaseModel):
    content: str

class SourceCatalogRebuildRequest(BaseModel):
    mode: Literal['continue', 'reverify'] | None = None
    catalog_model: AIModelSelection | None = None
    expected_content_hash: str | None = None
    expected_catalog_version: int | None = Field(default=None, ge=0)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)

class SourceCatalogRefinementRequest(SourceCatalogRebuildRequest):
    target_chapter_id: str | None = Field(default=None, min_length=1, max_length=128, pattern='^[A-Za-z0-9_.:-]+$')

    @model_validator(mode='after')
    def require_continue_for_chapter(self):
        if self.target_chapter_id is not None and self.mode != 'continue':
            raise ValueError('Preparing a chapter reference requires continue mode.')
        return self

class SourceEnhancementRequest(BaseModel):
    expected_content_hash: str | None = None

class BulkDeleteSourcesRequest(BaseModel):
    source_ids: list[str]

class BulkDeleteSourceFailure(BaseModel):
    id: str
    detail: str

class BulkDeleteSourcesResponse(BaseModel):
    removed: list[SourceIngestionRecord]
    failed: list[BulkDeleteSourceFailure]

def _parse_catalog_model(raw: str | None) -> AIModelSelection | None:
    if raw is None or not raw.strip():
        return None
    try:
        payload = json.loads(raw)
        return AIModelSelection.model_validate(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail='Invalid catalog_model selection.') from exc

def _catalog_model_from_source(source: SourceIngestionRecord) -> AIModelSelection | None:
    raw = source.metadata.get('catalog_model_selection')
    if not isinstance(raw, dict):
        return None
    try:
        return AIModelSelection.model_validate(raw)
    except ValueError:
        return None

def _invalidate_publication_for_source_change(*, owner_user_id: str, package_id: str):
    workspace = workspace_state.load_workspace_for_user(owner_user_id)
    return workspace_state.get_package(workspace, package_id)

@router.get('/api/packages/{package_id}/sources', response_model=list[SourceIngestionRecord])
def list_package_sources(package_id: str, user: UserView=Depends(current_user)) -> list[SourceIngestionRecord]:
    if not (workspace_state.is_ridoc_package_for_user(user.id, package_id) or workspace_state.get_course_store().package_belongs_to_user(user.id, package_id)):
        raise HTTPException(status_code=404, detail=f'Unknown course package {package_id}')
    return [public_source(item) for item in source_ingestion_service.list_sources(owner_user_id=user.id, package_id=package_id)]

@router.post('/api/packages/{package_id}/sources', response_model=SourceIngestionRecord)
async def import_package_source(package_id: str, background_tasks: BackgroundTasks, source_uri: str | None=Form(default=None), connection_id: str | None=Form(default=None), title: str=Form(default=''), text: str | None=Form(default=None), catalog_model: str | None=Form(default=None), file: UploadFile | None=File(default=None), user: UserView=Depends(current_user)) -> SourceIngestionRecord:
    try:
        if file is not None:
            from openclass_core.services.anythingllm.config import selected, eligible
            if source_ingestion_service.source_backend in {'google', 'local'} or (selected() and eligible(file.filename or '', file.content_type or '')):
                upload_limit = source_ingestion_service.google_service.config.max_upload_bytes
                content = await file.read(upload_limit + 1)
                if len(content) > upload_limit:
                    raise HTTPException(status_code=413, detail='The source file exceeds the upload size limit.')
            else:
                content = await file.read()
            if not content:
                raise HTTPException(status_code=400, detail='Uploaded file is empty.')
            package = _invalidate_publication_for_source_change(owner_user_id=user.id, package_id=package_id)
            queued = await run_in_threadpool(source_ingestion_service.queue_file_source, owner_user_id=user.id, package=package, file_name=file.filename or 'source', content=content, mime_type=file.content_type or 'application/octet-stream', title=title, catalog_model=_parse_catalog_model(catalog_model))
            source_ingestion_task_manager.submit(owner_user_id=user.id, package_id=package.id, source_id=queued.id)
            return public_source(queued)
        if text and text.strip():
            package = _invalidate_publication_for_source_change(owner_user_id=user.id, package_id=package_id)
            return public_source(source_ingestion_service.add_text_source(owner_user_id=user.id, package=package, text=text, title=title))
        if source_uri and source_uri.strip():
            package = _invalidate_publication_for_source_change(owner_user_id=user.id, package_id=package_id)
            queued = source_ingestion_service.add_url_source(owner_user_id=user.id, package=package, source_uri=source_uri, title=title, catalog_model=_parse_catalog_model(catalog_model), connection_id=connection_id)
            if queued.source_type == 'code_repository':
                background_tasks.add_task(source_ingestion_service.process_repository_source, owner_user_id=user.id, package_id=package.id, source_id=queued.id)
            return public_source(queued)
    except SourceIngestionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail='Provide a file, source_uri, or pasted text.')

def _enhancement_source(package_id, source_id, user):
    workspace = workspace_state.load_workspace_for_user(user.id)
    workspace_state.get_package(workspace, package_id)
    source = source_evidence_store.get_source(owner_user_id=user.id, package_id=package_id, source_id=source_id)
    if source is None:
        raise HTTPException(status_code=404, detail='Source not found.')
    return source

@router.post('/api/packages/{package_id}/sources/{source_id}/enhancement/prepare', response_model=SourceIngestionRecord)
def prepare_package_source_enhancement(package_id: str, source_id: str, payload: SourceEnhancementRequest | None=None, user: UserView=Depends(current_user)):
    from openclass_core.services.source_enhancement import prepare_enhancement
    from openclass_core.services.google_sources.config import GoogleSourceError
    source = _enhancement_source(package_id, source_id, user)
    try:
        result = prepare_enhancement(source_ingestion_service.google_service, source, expected_hash=payload.expected_content_hash if payload else None)
        return public_source(result)
    except GoogleSourceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

@router.post('/api/packages/{package_id}/sources/{source_id}/enhancement/cancel', response_model=SourceIngestionRecord)
def cancel_package_source_enhancement(package_id: str, source_id: str, user: UserView=Depends(current_user)):
    from openclass_core.services.source_enhancement import cancel_enhancement
    from openclass_core.services.google_sources.config import GoogleSourceError
    source = _enhancement_source(package_id, source_id, user)
    try:
        return public_source(cancel_enhancement(source_ingestion_service.google_service, source))
    except GoogleSourceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

@router.get('/api/packages/{package_id}/sources/jobs', response_model=list[SourceIngestionJob])
def list_package_source_jobs(package_id: str, user: UserView=Depends(current_user)) -> list[SourceIngestionJob]:
    workspace = workspace_state.load_workspace_for_user(user.id)
    workspace_state.get_package(workspace, package_id)
    return source_ingestion_service.list_jobs(owner_user_id=user.id, package_id=package_id)

@router.patch('/api/packages/{package_id}/sources/{source_id}', response_model=SourceIngestionRecord)
def update_package_source(package_id: str, source_id: str, request: SourceUpdateRequest, user: UserView=Depends(current_user)) -> SourceIngestionRecord:
    workspace = workspace_state.load_workspace_for_user(user.id)
    workspace_state.get_package(workspace, package_id)
    updated = source_ingestion_service.rename_source(owner_user_id=user.id, package_id=package_id, source_id=source_id, title=request.title)
    if updated is None:
        raise HTTPException(status_code=404, detail='Source not found or title is empty.')
    return public_source(updated)

def _queue_google_source_retry(source: SourceIngestionRecord, catalog_model: AIModelSelection | None=None) -> SourceIngestionRecord:
    from openclass_core.services.google_sources.config import GoogleSourceError
    service = source_ingestion_service.google_service

    def queue() -> None:
        key = (source.owner_user_id, source.package_id, source.id)
        if source_ingestion_task_manager.is_active(key):
            raise HTTPException(status_code=409, detail='The source task is still running. Wait for it to finish or cancel it first.')
        candidate = service.work_source(source)
        update = {'update_status': 'queued', 'update_error': ''}
        if candidate.metadata.get('processing_mode') == 'ordinary':
            update.update(catalog_rebuild_requested=True, enhancement_requested=False, catalog_status='pending')
        if catalog_model is not None:
            update['catalog_model_selection'] = catalog_model.model_dump(mode='json')
        candidate = service.state.update(candidate, active=False, metadata=update, **{'status': 'queued', 'error': ''} if candidate.status != 'ready' else {})
        service._job(candidate.model_copy(update={'status': 'queued', 'error': ''}), 'catalog_queued', 5)
        source_ingestion_task_manager.submit(owner_user_id=source.owner_user_id, package_id=source.package_id, source_id=source.id, retry=True)
    try:
        service.store.coordinator.run_write(service.store.path, queue)
    except GoogleSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SourceIngestionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    current = source_evidence_store.get_source(owner_user_id=source.owner_user_id, package_id=source.package_id, source_id=source.id)
    if current is None:
        raise HTTPException(status_code=404, detail='Source not found.')
    return source_ingestion_service._attach_job(source_ingestion_service.structure_store.attach_summary(current))

@router.post('/api/packages/{package_id}/sources/{source_id}/retry', response_model=SourceIngestionRecord)
def retry_package_source(package_id: str, source_id: str, user: UserView=Depends(current_user), request: SourceCatalogRebuildRequest | None=None) -> SourceIngestionRecord:
    from openclass_core.services.source_directory_processor import CATALOG_SCHEMA_VERSION
    workspace = workspace_state.load_workspace_for_user(user.id)
    workspace_state.get_package(workspace, package_id)
    existing = source_evidence_store.get_source(owner_user_id=user.id, package_id=package_id, source_id=source_id)
    if existing is None:
        raise HTTPException(status_code=404, detail='Source not found.')
    if existing.metadata.get('source_backend') in {'google', 'local'}:
        return public_source(_queue_google_source_retry(existing, request.catalog_model if request else None))
    metadata = dict(existing.metadata)
    if request is not None and request.catalog_model is not None:
        metadata['catalog_model_selection'] = request.catalog_model.model_dump(mode='json')
        metadata['adapter'] = CATALOG_SCHEMA_VERSION
        metadata['catalog_pipeline'] = CATALOG_SCHEMA_VERSION
    queued = source_evidence_store.save_source(existing.model_copy(update={'status': 'queued', 'error': '', 'metadata': metadata}))
    _invalidate_publication_for_source_change(owner_user_id=user.id, package_id=package_id)
    try:
        source_ingestion_task_manager.submit(owner_user_id=user.id, package_id=package_id, source_id=source_id, retry=True)
    except SourceIngestionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    updated = next((source for source in source_ingestion_service.list_sources(owner_user_id=user.id, package_id=package_id) if source.id == source_id), None)
    if updated is None:
        raise HTTPException(status_code=404, detail='Source not found.')
    return public_source(updated)

@router.get('/api/packages/{package_id}/sources/{source_id}/content', response_model=SourceContentView)
def get_package_source_content(package_id: str, source_id: str, user: UserView=Depends(current_user)) -> SourceContentView:
    workspace = workspace_state.load_workspace_for_user(user.id)
    workspace_state.get_package(workspace, package_id)
    result = source_ingestion_service.source_content(owner_user_id=user.id, package_id=package_id, source_id=source_id)
    if result is None:
        raise HTTPException(status_code=404, detail='Source not found.')
    source, content = result
    return SourceContentView(source=public_source(source), content=content)

@router.put('/api/packages/{package_id}/sources/{source_id}/content', response_model=SourceContentView)
def update_package_source_content(package_id: str, source_id: str, request: SourceContentUpdateRequest, user: UserView=Depends(current_user)) -> SourceContentView:
    workspace = workspace_state.load_workspace_for_user(user.id)
    workspace_state.get_package(workspace, package_id)
    existing = source_evidence_store.get_source(owner_user_id=user.id, package_id=package_id, source_id=source_id)
    if existing is None:
        raise HTTPException(status_code=404, detail='Source not found.')
    _invalidate_publication_for_source_change(owner_user_id=user.id, package_id=package_id)
    try:
        updated = source_ingestion_service.update_source_content(owner_user_id=user.id, package_id=package_id, source_id=source_id, content=request.content)
    except SourceIngestionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail='Source not found.')
    content_result = source_ingestion_service.source_content(owner_user_id=user.id, package_id=package_id, source_id=source_id)
    return SourceContentView(source=public_source(updated), content=content_result[1] if content_result else '')

@router.get('/api/packages/{package_id}/sources/{source_id}/download')
def download_package_source(package_id: str, source_id: str, user: UserView=Depends(current_user)) -> FileResponse:
    workspace = workspace_state.load_workspace_for_user(user.id)
    workspace_state.get_package(workspace, package_id)
    source = source_evidence_store.get_source(owner_user_id=user.id, package_id=package_id, source_id=source_id)
    if source is None:
        raise HTTPException(status_code=404, detail='Source not found.')
    if source.metadata.get('source_backend') in {'google', 'local'}:
        from fastapi.responses import Response
        from urllib.parse import quote
        from openclass_core.services.google_sources.config import GoogleSourceError
        try:
            service = source_ingestion_service.google_service
            current = service.get(owner_user_id=user.id, package_id=package_id, source_id=source_id, ready=False)
            if not current.metadata.get('google_original', current.metadata.get('local_original')):
                from openclass_core.services.google_sources.catalog_fallback import original_path
                content = original_path(current).read_bytes()
                service.state.current(current, active=False)
            else:
                current, content = service.download(owner_user_id=user.id, package_id=package_id, source_id=source_id)
        except GoogleSourceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(content=content, media_type=current.mime_type, headers={'Content-Disposition': "attachment; filename*=UTF-8''" + quote(current.file_name), 'Cache-Control': 'private, no-store'})
    path = source_download_path(source)
    if path is None:
        raise HTTPException(status_code=404, detail='Source file is unavailable.')
    return FileResponse(Path(path), media_type=str(source.metadata.get('original_mime_type') or source.mime_type or 'application/octet-stream'), filename=str(source.metadata.get('original_file_name') or source.file_name or path.name))

@router.post('/api/packages/{package_id}/sources/bulk-delete', response_model=BulkDeleteSourcesResponse)
def bulk_delete_package_sources(package_id: str, payload: BulkDeleteSourcesRequest, background_tasks: BackgroundTasks, user: UserView=Depends(current_user)) -> BulkDeleteSourcesResponse:
    workspace = workspace_state.load_workspace_for_user(user.id)
    workspace_state.get_package(workspace, package_id)
    source_ids = [source_id for source_id in payload.source_ids if source_id]
    if not source_ids:
        return BulkDeleteSourcesResponse(removed=[], failed=[])
    _invalidate_publication_for_source_change(owner_user_id=user.id, package_id=package_id)
    removed, failed = source_ingestion_service.remove_sources(owner_user_id=user.id, package_id=package_id, source_ids=source_ids, defer_cleanup=True)
    if removed:
        background_tasks.add_task(source_ingestion_service.cleanup_removed_sources, removed)
    return BulkDeleteSourcesResponse(removed=removed, failed=[BulkDeleteSourceFailure(id=source_id, detail=detail) for source_id, detail in failed])

@router.delete('/api/packages/{package_id}/sources/{source_id}', response_model=SourceIngestionRecord)
def delete_package_source(package_id: str, source_id: str, background_tasks: BackgroundTasks, user: UserView=Depends(current_user)) -> SourceIngestionRecord:
    workspace = workspace_state.load_workspace_for_user(user.id)
    workspace_state.get_package(workspace, package_id)
    existing = source_evidence_store.get_source(owner_user_id=user.id, package_id=package_id, source_id=source_id)
    if existing is None:
        raise HTTPException(status_code=404, detail='Source not found.')
    _invalidate_publication_for_source_change(owner_user_id=user.id, package_id=package_id)
    removed = source_ingestion_service.remove_source(owner_user_id=user.id, package_id=package_id, source_id=source_id, defer_cleanup=True)
    if removed is None:
        raise HTTPException(status_code=404, detail='Source not found.')
    background_tasks.add_task(source_ingestion_service.cleanup_removed_source, removed)
    return public_source(removed)

@router.get('/api/packages/{package_id}/sources/{source_id}/structure', response_model=SourceStructureView)
def get_package_source_structure(package_id: str, source_id: str, user: UserView=Depends(current_user)) -> SourceStructureView:
    workspace = workspace_state.load_workspace_for_user(user.id)
    workspace_state.get_package(workspace, package_id)
    source = source_evidence_store.get_source(owner_user_id=user.id, package_id=package_id, source_id=source_id)
    if source is None:
        raise HTTPException(status_code=404, detail='Source not found.')
    return source_structure_store.get_structure_view(source=source).model_copy(update={'source': public_source(source)})

@router.get('/api/packages/{package_id}/sources/{source_id}/repository-map', response_model=RepositoryMapView)
def get_package_repository_map(package_id: str, source_id: str, user: UserView=Depends(current_user)) -> RepositoryMapView:
    workspace = workspace_state.load_workspace_for_user(user.id)
    workspace_state.get_package(workspace, package_id)
    source = source_evidence_store.get_source(owner_user_id=user.id, package_id=package_id, source_id=source_id)
    if source is None or source.source_type != 'code_repository':
        raise HTTPException(status_code=404, detail='Repository source not found.')
    result = repository_store.get_map(source=source)
    if result is None:
        raise HTTPException(status_code=409, detail='Repository map is not ready.')
    return result

@router.post('/api/packages/{package_id}/sources/{source_id}/repository-refresh', response_model=SourceIngestionRecord)
def refresh_package_repository_source(package_id: str, source_id: str, background_tasks: BackgroundTasks, request: SourceCatalogRebuildRequest | None=None, connection_id: str | None=None, user: UserView=Depends(current_user)) -> SourceIngestionRecord:
    workspace = workspace_state.load_workspace_for_user(user.id)
    package = workspace_state.get_package(workspace, package_id)
    source = source_evidence_store.get_source(owner_user_id=user.id, package_id=package_id, source_id=source_id)
    if source is None or source.source_type != 'code_repository' or (not source.source_uri):
        raise HTTPException(status_code=404, detail='Repository source not found.')
    try:
        queued = source_ingestion_service.add_url_source(owner_user_id=user.id, package=package, source_uri=source.source_uri, title=source.title, supersedes_source_id=source.id, connection_id=connection_id or str(source.metadata.get('github_connection_id') or '') or None, public_only=connection_id is None and source.metadata.get('github_public_only') is True, catalog_model=request.catalog_model if request is not None and request.catalog_model is not None else _catalog_model_from_source(source))
    except SourceIngestionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    background_tasks.add_task(source_ingestion_service.process_repository_source, owner_user_id=user.id, package_id=package_id, source_id=queued.id)
    return public_source(queued)

@router.post('/api/packages/{package_id}/sources/{source_id}/cancel', response_model=SourceIngestionRecord)
def cancel_package_source(package_id: str, source_id: str, user: UserView=Depends(current_user)) -> SourceIngestionRecord:
    workspace = workspace_state.load_workspace_for_user(user.id)
    workspace_state.get_package(workspace, package_id)
    cancelled = source_ingestion_service.cancel_source(owner_user_id=user.id, package_id=package_id, source_id=source_id)
    if cancelled is None:
        raise HTTPException(status_code=404, detail='Source not found.')
    return public_source(cancelled)

@router.get('/api/packages/{package_id}/sources/catalogs', response_model=SourceCatalogBatchView)
def get_package_source_catalogs(package_id: str, user: UserView=Depends(current_user)) -> SourceCatalogBatchView:
    workspace = workspace_state.load_workspace_for_user(user.id)
    workspace_state.get_package(workspace, package_id)
    sources = source_evidence_store.list_sources(owner_user_id=user.id, package_id=package_id)
    return source_structure_store.get_catalog_views(package_id=package_id, sources=sources)

@router.get('/api/packages/{package_id}/sources/{source_id}/catalog', response_model=SourceCatalogView)
def get_package_source_catalog(package_id: str, source_id: str, user: UserView=Depends(current_user)) -> SourceCatalogView:
    workspace = workspace_state.load_workspace_for_user(user.id)
    workspace_state.get_package(workspace, package_id)
    source = source_evidence_store.get_source(owner_user_id=user.id, package_id=package_id, source_id=source_id)
    if source is None:
        raise HTTPException(status_code=404, detail='Source not found.')
    return source_structure_store.get_catalog_view(source=source)

@router.post('/api/packages/{package_id}/sources/{source_id}/catalog/rebuild', response_model=SourceCatalogView)
def rebuild_package_source_catalog(package_id: str, source_id: str, request: SourceCatalogRefinementRequest | None=None, user: UserView=Depends(current_user)) -> SourceCatalogView:
    workspace = workspace_state.load_workspace_for_user(user.id)
    workspace_state.get_package(workspace, package_id)
    source = source_evidence_store.get_source(owner_user_id=user.id, package_id=package_id, source_id=source_id)
    if source is None:
        raise HTTPException(status_code=404, detail='Source not found.')
    if source.metadata.get('source_backend') in {'google', 'local'}:
        from openclass_core.services.source_catalog_refinement import queue_refinement
        from openclass_core.services.google_sources.config import GoogleSourceError
        try:
            queued = queue_refinement(source_ingestion_service.google_service, source, task_manager=source_ingestion_task_manager, **request.model_dump(exclude={'catalog_model'}) if request else {}, catalog_model=request.catalog_model if request else None)
        except GoogleSourceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return source_structure_store.get_catalog_view(source=queued)
    if request and request.target_chapter_id is not None:
        raise HTTPException(status_code=409, detail='Chapter reference preparation is unavailable for this source.')
    if source.metadata.get('source_processing_owner') == 'open_notebook' and source.metadata.get('catalog_pipeline') not in {'native_directory_v1', 'hybrid_directory_v1', 'ai_directory_v1', 'codex_directory_v1', 'agent_catalog_v2', 'agent_catalog_v3'}:
        raise HTTPException(status_code=409, detail='OpenNotebook-managed sources do not use the local catalog rebuild pipeline.')
    _invalidate_publication_for_source_change(owner_user_id=user.id, package_id=package_id)
    try:
        rebuilt = source_ingestion_service.rebuild_catalog(owner_user_id=user.id, package_id=package_id, source_id=source_id, catalog_model=request.catalog_model if request is not None else None)
    except SourceIngestionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if rebuilt is None:
        raise HTTPException(status_code=404, detail='Source not found.')
    return source_structure_store.get_catalog_view(source=rebuilt)

@router.post('/api/packages/{package_id}/sources/{source_id}/structure/rebuild', response_model=SourceStructureView)
def rebuild_package_source_structure(package_id: str, source_id: str, user: UserView=Depends(current_user), request: SourceCatalogRebuildRequest | None=None) -> SourceStructureView:
    workspace = workspace_state.load_workspace_for_user(user.id)
    workspace_state.get_package(workspace, package_id)
    source = source_evidence_store.get_source(owner_user_id=user.id, package_id=package_id, source_id=source_id)
    if source is None:
        raise HTTPException(status_code=404, detail='Source not found.')
    if source.metadata.get('source_backend') in {'google', 'local'}:
        queued = _queue_google_source_retry(source, request.catalog_model if request else None)
        _invalidate_publication_for_source_change(owner_user_id=user.id, package_id=package_id)
        return source_structure_store.get_structure_view(source=queued).model_copy(update={'source': public_source(queued)})
    current = source_structure_store.get_structure(owner_user_id=user.id, package_id=package_id, source_id=source_id)
    if current is not None and current.strategy in {'native_directory_v1', 'hybrid_directory_v1', 'ai_directory_v1', 'codex_directory_v1'}:
        _invalidate_publication_for_source_change(owner_user_id=user.id, package_id=package_id)
        rebuilt = source_ingestion_service.rebuild_catalog(owner_user_id=user.id, package_id=package_id, source_id=source_id)
        if rebuilt is None:
            raise HTTPException(status_code=404, detail='Source not found.')
        return source_structure_store.get_structure_view(source=rebuilt).model_copy(update={'source': public_source(rebuilt)})
    if source.metadata.get('source_processing_owner') == 'open_notebook':
        raise HTTPException(status_code=409, detail='OpenNotebook-managed sources do not use the local structure rebuild pipeline.')
    _invalidate_publication_for_source_change(owner_user_id=user.id, package_id=package_id)
    source_structure_indexer.rebuild_structure(source)
    return source_structure_store.get_structure_view(source=source).model_copy(update={'source': public_source(source)})
