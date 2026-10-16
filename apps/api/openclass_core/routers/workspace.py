from __future__ import annotations
import uuid
from pathlib import Path
from typing import Literal
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from openclass_core.models import BatchLessonActionRequest, CoursePackage, CoursePackageView, CreatePackageRequest, GenerateLessonRequest, MoveLessonRequest, LessonView, LessonWorkspaceDelta, ReorderTabsRequest, UpdateLessonRequest, UserView, WorkspaceStateView, now_iso
from openclass_core.studio_dependencies import current_user
from openclass_core.services.history import current_head_commit
from openclass_core.services.lesson_factory import create_empty_lesson, create_untitled_lesson
from openclass_core.services.lesson_package_format import RIDOC_MAX_ARCHIVE_BYTES, RidocFormatError, RidocSizeError, RidocVersionError, read_ridoc
from openclass_core.services.lesson_package_import import import_ridoc_archive, rollback_imported_assets
from openclass_core.services.ridoc_workspace_store import course_store_mode
from openclass_core.services.ridoc_v3 import RidocV3Error, convert_legacy_package_to_ridoc_v3, read_ridoc_v3
from openclass_core.services.workspace_batch_actions import apply_lesson_batch_action
from openclass_core.services.recently_deleted import RecentlyDeletedView, RestoreDeletedCourseRequest, RestoredCourse, list_recently_deleted, restore_recently_deleted
from openclass_core.services.recently_deleted_legacy import list_legacy_deleted, restore_legacy_deleted, with_legacy_recovery
from openclass_core.services.workspace_state import find_lesson_package, get_package, get_course_store, get_ridoc_workspace_store, is_ridoc_package_for_user, is_ridoc_lesson_for_user, load_workspace_for_user, load_workspace_for_user_with_revision, load_workspace_package_for_user_with_revision, lesson_view, normalize_package_state, package_view, package_view_for_lesson, UPLOAD_DIR, save_workspace_for_user_if_revision, workspace_view
router = APIRouter()

@router.get('/api/account/recently-deleted', response_model=RecentlyDeletedView)
def account_recently_deleted(user: UserView=Depends(current_user)) -> RecentlyDeletedView:
    view = list_recently_deleted(get_ridoc_workspace_store(), user.id)
    if course_store_mode() != 'ridoc_required':
        view.items.extend(list_legacy_deleted(get_course_store(), user.id))
        view.items.sort(key=lambda item: (item.deleted_at, item.id), reverse=True)
    return view

@router.post('/api/account/recently-deleted/{entry_id}/restore', response_model=RestoredCourse)
def restore_account_deleted_course(entry_id: str, request: RestoreDeletedCourseRequest, user: UserView=Depends(current_user)) -> RestoredCourse:
    if request.expected_target_revision.startswith('legacy:'):
        if course_store_mode() == 'ridoc_required':
            raise HTTPException(status_code=409, detail='Legacy courses are disabled')
        return restore_legacy_deleted(get_course_store(), user.id, entry_id, request)
    return restore_recently_deleted(get_ridoc_workspace_store(), user.id, entry_id, request)

def _lesson_workspace_delta(operation: Literal['create', 'close', 'delete'], result) -> LessonWorkspaceDelta:
    return LessonWorkspaceDelta(operation=operation, workspace_revision=result.workspace_revision, package_id=result.package_id, active_package_id=result.active_package_id, active_lesson_id=result.active_lesson_id, open_lesson_ids=result.open_lesson_ids, workspace_tab_order=result.workspace_tab_order, created_lesson=LessonView.model_validate(result.created_lesson.model_dump(mode='json')) if result.created_lesson is not None else None, deleted_lesson_id=result.deleted_lesson_id, graph_edge=result.graph_edge)

@router.post('/api/workspace/import-ridoc', response_model=CoursePackageView)
def import_lesson_package(file: UploadFile=File(...), user: UserView=Depends(current_user)) -> CoursePackageView:
    safe_name = Path(file.filename or 'lesson.ridoc').name
    if Path(safe_name).suffix.lower() != '.ridoc':
        raise HTTPException(status_code=400, detail='Select a .ridoc course file.')
    import_dir = UPLOAD_DIR / 'ridoc-imports'
    import_dir.mkdir(parents=True, exist_ok=True)
    destination = import_dir / f'{uuid.uuid4().hex}.ridoc'
    imported_lesson_id: str | None = None
    try:
        written = 0
        with destination.open('wb') as output:
            while (chunk := file.file.read(1024 * 1024)):
                written += len(chunk)
                if written > RIDOC_MAX_ARCHIVE_BYTES:
                    raise RidocSizeError('RIDOC archive exceeds the 256 MiB limit.')
                output.write(chunk)
        if course_store_mode() != 'legacy':
            try:
                native_archive = read_ridoc_v3(destination)
            except RidocV3Error:
                native_archive = None
            if native_archive is not None:
                record = get_ridoc_workspace_store().import_archive(user.id, native_archive)
                get_ridoc_workspace_store().set_active_package_id(user.id, record.package_id)
                package = get_ridoc_workspace_store().load_package(user.id, package_id=record.package_id)
                workspace = load_workspace_for_user(user.id)
                return package_view_for_lesson(workspace, package, package.active_lesson_id)
        archive = read_ridoc(destination)
        if course_store_mode() != 'legacy':
            from openclass_core.models import WorkspaceState
            standalone = CoursePackage(title='Imported course', summary='Imported from RIDOC 1.x', lessons=[], package_kind='standalone')
            import_workspace = WorkspaceState(packages=[standalone], active_package_id=standalone.id)
            imported = import_ridoc_archive(owner_user_id=user.id, workspace=import_workspace, archive=archive)
            imported_lesson_id = imported.active_lesson_id
            native = convert_legacy_package_to_ridoc_v3(imported, package_kind='standalone')
            record = get_ridoc_workspace_store().import_archive(user.id, native)
            get_ridoc_workspace_store().set_active_package_id(user.id, record.package_id)
            package = get_ridoc_workspace_store().load_package(user.id, package_id=record.package_id)
            workspace = load_workspace_for_user(user.id)
            return package_view_for_lesson(workspace, package, imported_lesson_id)
        workspace, revision = load_workspace_for_user_with_revision(user.id)
        package = import_ridoc_archive(owner_user_id=user.id, workspace=workspace, archive=archive)
        imported_lesson_id = package.active_lesson_id
        if not imported_lesson_id:
            raise RidocFormatError('RIDOC import did not activate the new lesson.')
        try:
            save_workspace_for_user_if_revision(user.id, workspace, expected_revision=revision)
        except Exception:
            rollback_imported_assets(owner_user_id=user.id, lesson_id=imported_lesson_id)
            raise
        return package_view_for_lesson(workspace, package, imported_lesson_id)
    except RidocSizeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except RidocVersionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RidocFormatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        destination.unlink(missing_ok=True)

@router.get('/api/workspace', response_model=WorkspaceStateView)
def get_workspace(user: UserView=Depends(current_user)) -> WorkspaceStateView:
    return workspace_view(load_workspace_for_user(user.id))

@router.get('/api/workspace/tabs')
def get_workspace_tabs(user: UserView=Depends(current_user)):
    from openclass_core.services.workspace_navigation import workspace_tabs
    return workspace_tabs(user.id)

@router.get('/api/workspace/summary')
def get_workspace_summary(user: UserView=Depends(current_user)):
    from openclass_core.services.workspace_summary import workspace_summary
    return workspace_summary(user.id)

@router.get('/api/workspace/activity')
def get_workspace_activity(timezone: str='UTC', user: UserView=Depends(current_user)):
    from openclass_core.services.workspace_activity import workspace_activity
    return workspace_activity(user.id, timezone)

@router.post('/api/packages', response_model=WorkspaceStateView)
def create_package(request: CreatePackageRequest, user: UserView=Depends(current_user)) -> WorkspaceStateView:
    title = request.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail='Package title is required')
    package = CoursePackage(title=title, summary=request.summary.strip(), lessons=[])
    if course_store_mode() != 'legacy':
        package.storage_kind = 'ridoc'
        package.package_kind = 'course'
        store = get_ridoc_workspace_store()
        store.create_from_package(user.id, package)
        store.set_active_package_id(user.id, package.id)
        return workspace_view(load_workspace_for_user(user.id))
    workspace, revision = load_workspace_for_user_with_revision(user.id)
    workspace.packages.append(package)
    workspace.active_package_id = package.id
    save_workspace_for_user_if_revision(user.id, workspace, expected_revision=revision)
    return workspace_view(workspace)

@router.post('/api/packages/{package_id}/open', response_model=WorkspaceStateView)
def open_package(package_id: str, user: UserView=Depends(current_user)) -> WorkspaceStateView:
    if is_ridoc_package_for_user(user.id, package_id):
        get_ridoc_workspace_store().set_active_package_id(user.id, package_id)
        return workspace_view(load_workspace_for_user(user.id))
    workspace, revision = load_workspace_for_user_with_revision(user.id)
    get_package(workspace, package_id)
    workspace.active_package_id = package_id
    save_workspace_for_user_if_revision(user.id, workspace, expected_revision=revision)
    return workspace_view(workspace)

@router.post('/api/lessons/{lesson_id}/rename', response_model=WorkspaceStateView)
def update_lesson(lesson_id: str, request: UpdateLessonRequest, user: UserView=Depends(current_user)) -> WorkspaceStateView:
    title = request.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail='Lesson title is required')
    if is_ridoc_lesson_for_user(user.id, lesson_id):
        expected = (request.expected_ridoc_revision or '').strip()
        if not expected:
            raise HTTPException(status_code=409, detail='RIDOC revision is required')
        store = get_ridoc_workspace_store()
        package_id = store.package_id_for_lesson(user.id, lesson_id)
        if package_id is None:
            raise HTTPException(status_code=404, detail='RIDOC lesson not found')
        try:
            store.update_lesson_metadata(owner_user_id=user.id, package_id=package_id, lesson_id=lesson_id, expected_revision=expected, title=title)
        except RidocV3Error as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return workspace_view(load_workspace_for_user(user.id))
    workspace, revision = load_workspace_for_user_with_revision(user.id)
    _, lesson = find_lesson_package(workspace, lesson_id)
    lesson.title = title
    lesson.updated_at = now_iso()
    save_workspace_for_user_if_revision(user.id, workspace, expected_revision=revision)
    return workspace_view(workspace)

@router.post('/api/packages/{package_id}/delete', response_model=WorkspaceStateView)
def delete_package(package_id: str, expected_ridoc_revision: str | None=Query(default=None), user: UserView=Depends(current_user)) -> WorkspaceStateView:
    if is_ridoc_package_for_user(user.id, package_id):
        if not expected_ridoc_revision:
            raise HTTPException(status_code=409, detail='RIDOC revision is required')
        try:
            get_ridoc_workspace_store().delete_package(owner_user_id=user.id, package_id=package_id, expected_revision=expected_ridoc_revision)
        except RidocV3Error as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return workspace_view(load_workspace_for_user(user.id))
    workspace, revision = load_workspace_for_user_with_revision(user.id)
    get_package(workspace, package_id)
    if len(workspace.packages) <= 1:
        raise HTTPException(status_code=400, detail='Cannot delete the only course package')

    def remove_package():
        workspace.packages = [package for package in workspace.packages if package.id != package_id]
        if workspace.active_package_id == package_id:
            workspace.active_package_id = workspace.packages[0].id if workspace.packages else None
        save_workspace_for_user_if_revision(user.id, workspace, expected_revision=revision)
    with_legacy_recovery(get_course_store(), user.id, workspace.packages, set(), {package_id}, remove_package)
    return workspace_view(workspace)

@router.post('/api/lessons/{lesson_id}/move', response_model=WorkspaceStateView)
def move_lesson(lesson_id: str, request: MoveLessonRequest, user: UserView=Depends(current_user)) -> WorkspaceStateView:
    workspace, revision = load_workspace_for_user_with_revision(user.id)
    source_package, lesson = find_lesson_package(workspace, lesson_id)
    target_package = get_package(workspace, request.target_package_id)
    if source_package.id == target_package.id:
        raise HTTPException(status_code=400, detail='Lesson is already in the selected package')
    source_package.lessons = [current for current in source_package.lessons if current.id != lesson_id]
    source_package.open_lesson_ids = [current for current in source_package.open_lesson_ids if current != lesson_id]
    source_package.workspace_tab_order = [current for current in source_package.workspace_tab_order if current != lesson_id]
    if source_package.active_lesson_id == lesson_id:
        source_package.active_lesson_id = None
    target_package.lessons.append(lesson)
    if lesson.id not in target_package.open_lesson_ids:
        target_package.open_lesson_ids.append(lesson.id)
    if lesson.id not in target_package.workspace_tab_order:
        target_package.workspace_tab_order.append(lesson.id)
    if target_package.active_lesson_id is None:
        target_package.active_lesson_id = lesson.id
    normalize_package_state(source_package)
    normalize_package_state(target_package)
    save_workspace_for_user_if_revision(user.id, workspace, expected_revision=revision)
    return workspace_view(workspace)

@router.post('/api/lessons/{lesson_id}/delete', response_model=WorkspaceStateView | LessonWorkspaceDelta)
def delete_lesson(lesson_id: str, response_mode: Literal['full', 'delta']=Query('full'), expected_ridoc_revision: str | None=Query(default=None), user: UserView=Depends(current_user)) -> WorkspaceStateView | LessonWorkspaceDelta:
    if is_ridoc_lesson_for_user(user.id, lesson_id):
        if not expected_ridoc_revision:
            raise HTTPException(status_code=409, detail='RIDOC revision is required')
        store = get_ridoc_workspace_store()
        package_id = store.package_id_for_lesson(user.id, lesson_id)
        if package_id is None:
            raise HTTPException(status_code=404, detail='RIDOC lesson not found')
        try:
            store.delete_lesson(owner_user_id=user.id, package_id=package_id, lesson_id=lesson_id, expected_revision=expected_ridoc_revision)
        except RidocV3Error as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return workspace_view(load_workspace_for_user(user.id))
    try:
        legacy_store = get_course_store()
        result = with_legacy_recovery(legacy_store, user.id, None, {lesson_id}, set(), lambda: legacy_store.delete_lesson_for_user(user.id, lesson_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    if response_mode == 'delta':
        return _lesson_workspace_delta('delete', result)
    return workspace_view(load_workspace_for_user(user.id))

@router.post('/api/lessons/batch', response_model=WorkspaceStateView)
def batch_lessons(request: BatchLessonActionRequest, user: UserView=Depends(current_user)) -> WorkspaceStateView:
    workspace, revision = load_workspace_for_user_with_revision(user.id)
    if request.action == 'delete':
        selected = [find_lesson_package(workspace, lesson_id.strip()) for lesson_id in request.lesson_ids if lesson_id.strip()]
        if any((package.storage_kind == 'ridoc' for package, _ in selected)):
            if any((package.storage_kind != 'ridoc' for package, _ in selected)):
                raise HTTPException(status_code=409, detail='Delete legacy and RIDOC courses separately')
            from openclass_core.services.ridoc_batch_delete import delete_ridoc_lessons_batch
            try:
                delete_ridoc_lessons_batch(get_ridoc_workspace_store(), user.id, request.lesson_ids, request.expected_ridoc_revisions)
            except RidocV3Error as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return workspace_view(load_workspace_for_user(user.id))

    def apply_batch():
        apply_lesson_batch_action(workspace, request)
        save_workspace_for_user_if_revision(user.id, workspace, expected_revision=revision)
    if request.action == 'delete':
        with_legacy_recovery(get_course_store(), user.id, workspace.packages, set(request.lesson_ids), set(), apply_batch)
    else:
        apply_batch()
    return workspace_view(workspace)

@router.get('/api/course-package', response_model=CoursePackageView)
def get_course_package(user: UserView=Depends(current_user), lesson_id: str | None=None) -> CoursePackageView:
    from openclass_core.services.workspace_navigation import readable_course_package
    package, standalone = readable_course_package(user.id, lesson_id)
    return package_view(package, is_standalone=standalone, resource_lesson_id=package.active_lesson_id, isolate_lesson_resources=standalone)

@router.post('/api/lessons/generate', response_model=CoursePackageView | LessonWorkspaceDelta)
def generate_lesson(request: GenerateLessonRequest, response_mode: Literal['full', 'delta']=Query('full'), user: UserView=Depends(current_user)) -> CoursePackageView | LessonWorkspaceDelta:
    requested_title = (request.topic or '').strip()
    lesson = create_empty_lesson(requested_title) if requested_title else create_untitled_lesson(timezone_name=request.timezone)
    mode = course_store_mode()
    ridoc_store = get_ridoc_workspace_store()
    target_is_ridoc = bool(request.target_package_id and is_ridoc_package_for_user(user.id, request.target_package_id))
    if mode != 'legacy' and (request.target_package_id is None or target_is_ridoc):
        if request.branch_from_lesson_id:
            raise HTTPException(status_code=400, detail='RIDOC lesson branching is not available until the native branch phase')
        if request.target_package_id:
            expected_revision = (request.expected_ridoc_revision or '').strip()
            if not expected_revision:
                raise HTTPException(status_code=409, detail='RIDOC revision is required before adding a lesson')
            try:
                record = ridoc_store.add_lesson(owner_user_id=user.id, package_id=request.target_package_id, expected_revision=expected_revision, lesson=lesson)
            except RidocV3Error as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            package_id = record.package_id
        else:
            package = CoursePackage(title=lesson.title, summary=lesson.summary, lessons=[lesson], active_lesson_id=lesson.id, open_lesson_ids=[lesson.id], workspace_tab_order=[lesson.id], storage_kind='ridoc', package_kind='standalone')
            record = ridoc_store.create_from_package(user.id, package)
            package_id = package.id
        ridoc_store.set_active_package_id(user.id, package_id)
        open_lesson_ids = list(dict.fromkeys([*record.index.open_lesson_ids, lesson.id]))
        workspace_tab_order = list(dict.fromkeys([*record.index.workspace_tab_order, lesson.id]))
        ridoc_store.update_ui_state(user.id, package_id, active_lesson_id=lesson.id, open_lesson_ids=open_lesson_ids, workspace_tab_order=workspace_tab_order)
        workspace = load_workspace_for_user(user.id)
        package = get_package(workspace, package_id)
        if response_mode == 'delta':
            return LessonWorkspaceDelta(operation='create', workspace_revision=0, package_id=package_id, active_package_id=package_id, active_lesson_id=lesson.id, open_lesson_ids=list(package.open_lesson_ids), workspace_tab_order=list(package.workspace_tab_order), created_lesson=lesson_view(package.lessons[-1]), storage_kind='ridoc', ridoc_revision=record.revision)
        return package_view_for_lesson(workspace, package, lesson.id)
    try:
        result = get_course_store().create_lesson_for_user(user.id, lesson, target_package_id=request.target_package_id, branch_from_lesson_id=request.branch_from_lesson_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if response_mode == 'delta':
        return _lesson_workspace_delta('create', result)
    workspace = load_workspace_for_user(user.id)
    package = get_package(workspace, result.package_id)
    return package_view_for_lesson(workspace, package, result.active_lesson_id)

@router.post('/api/workspace/reorder', response_model=CoursePackageView)
def reorder_workspace_tabs(request: ReorderTabsRequest, user: UserView=Depends(current_user)) -> CoursePackageView:
    if request.ordered_lesson_ids:
        package_id = get_ridoc_workspace_store().package_id_for_lesson(user.id, request.ordered_lesson_ids[0])
        if package_id is not None:
            store = get_ridoc_workspace_store()
            index = next((item for item in store.list_packages(user.id) if item.id == package_id))
            store.update_ui_state(user.id, package_id, active_lesson_id=request.active_lesson_id, open_lesson_ids=request.ordered_lesson_ids, workspace_tab_order=request.ordered_lesson_ids)
            workspace = load_workspace_for_user(user.id)
            package = get_package(workspace, package_id)
            return package_view_for_lesson(workspace, package, request.active_lesson_id)
    workspace, package, revision = load_workspace_package_for_user_with_revision(user.id)
    package.workspace_tab_order = request.ordered_lesson_ids
    package.open_lesson_ids = request.ordered_lesson_ids
    package.active_lesson_id = request.active_lesson_id or (request.ordered_lesson_ids[0] if request.ordered_lesson_ids else None)
    save_workspace_for_user_if_revision(user.id, workspace, expected_revision=revision)
    return package_view_for_lesson(workspace, package, package.active_lesson_id)

@router.post('/api/lessons/{lesson_id}/open', response_model=CoursePackageView)
def open_lesson_tab(lesson_id: str, user: UserView=Depends(current_user)) -> CoursePackageView:
    if is_ridoc_lesson_for_user(user.id, lesson_id):
        store = get_ridoc_workspace_store()
        package_id = store.package_id_for_lesson(user.id, lesson_id)
        if package_id is None:
            raise HTTPException(status_code=404, detail=f'Unknown lesson {lesson_id}')
        index = next((item for item in store.list_packages(user.id) if item.id == package_id))
        opened = list(index.open_lesson_ids)
        tabs = list(index.workspace_tab_order)
        if lesson_id not in opened:
            opened.append(lesson_id)
        if lesson_id not in tabs:
            tabs.append(lesson_id)
        store.update_ui_state(user.id, package_id, active_lesson_id=lesson_id, open_lesson_ids=opened, workspace_tab_order=tabs)
        store.set_active_package_id(user.id, package_id)
        package = store.load_package(user.id, package_id=package_id)
        standalone = package.package_kind == 'standalone'
        return package_view(package, is_standalone=standalone, resource_lesson_id=lesson_id, isolate_lesson_resources=standalone)
    workspace, revision = load_workspace_for_user_with_revision(user.id)
    package, _ = find_lesson_package(workspace, lesson_id)
    if lesson_id not in package.open_lesson_ids:
        package.open_lesson_ids.append(lesson_id)
    if lesson_id not in package.workspace_tab_order:
        package.workspace_tab_order.append(lesson_id)
    package.active_lesson_id = lesson_id
    workspace.active_package_id = package.id
    save_workspace_for_user_if_revision(user.id, workspace, expected_revision=revision)
    return package_view_for_lesson(workspace, package, lesson_id)

@router.post('/api/lessons/{lesson_id}/close', response_model=CoursePackageView | LessonWorkspaceDelta)
def close_lesson_tab(lesson_id: str, response_mode: Literal['full', 'delta']=Query('full'), user: UserView=Depends(current_user)) -> CoursePackageView | LessonWorkspaceDelta:
    if is_ridoc_lesson_for_user(user.id, lesson_id):
        store = get_ridoc_workspace_store()
        package_id = store.package_id_for_lesson(user.id, lesson_id)
        if package_id is None:
            raise HTTPException(status_code=404, detail='RIDOC lesson not found')
        index = next((item for item in store.list_packages(user.id) if item.id == package_id))
        opened = [item for item in index.open_lesson_ids if item != lesson_id]
        tabs = [item for item in index.workspace_tab_order if item != lesson_id]
        active = index.active_lesson_id
        if active == lesson_id:
            active = tabs[-1] if tabs else None
        store.update_ui_state(user.id, package_id, active_lesson_id=active, open_lesson_ids=opened, workspace_tab_order=tabs)
        workspace = load_workspace_for_user(user.id)
        package = get_package(workspace, package_id)
        if response_mode == 'delta':
            return LessonWorkspaceDelta(operation='close', workspace_revision=0, package_id=package_id, active_package_id=package_id, active_lesson_id=active, open_lesson_ids=list(package.open_lesson_ids), workspace_tab_order=list(package.workspace_tab_order), storage_kind='ridoc', ridoc_revision=index.revision)
        return package_view_for_lesson(workspace, package, active)
    try:
        result = get_course_store().close_lesson_for_user(user.id, lesson_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    if response_mode == 'delta':
        return _lesson_workspace_delta('close', result)
    workspace = load_workspace_for_user(user.id)
    package = get_package(workspace, result.package_id)
    return package_view_for_lesson(workspace, package, result.active_lesson_id)

@router.get('/api/lessons/{lesson_id}/head')
def get_lesson_head(lesson_id: str, user: UserView=Depends(current_user)) -> dict[str, str]:
    workspace = load_workspace_for_user(user.id)
    _, lesson = find_lesson_package(workspace, lesson_id)
    head = current_head_commit(lesson)
    return {'lesson_id': lesson_id, 'head_commit_id': head.id}
