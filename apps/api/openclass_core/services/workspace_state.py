from __future__ import annotations
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from fastapi import HTTPException
from openclass_core.models import BoardSegmentKind, CommitRecord, CoursePackage, CoursePackageView, DocumentSegmentSearchResult, Lesson, LessonMergeSession, LessonRuntimeSnapshot, LessonView, ResourceLibraryItem, WorkspaceState, WorkspaceStateView
from openclass_core.services.blank_board_intake import active_requirement_from_history
from openclass_core.services.config import DATA_DIR, ROOT_DIR, load_root_dotenv
from openclass_core.services.course_store import ChatInputEventClaimStatus, LessonDocumentContext, LessonRuntimeState, SqliteCourseStore
from openclass_core.services.studio_ports import host_service
from openclass_core.services.ridoc_workspace_store import RidocWorkspaceStore, course_store_mode
from openclass_core.services.history import commit_operations, current_head_commit
from openclass_core.services.lesson_summary import lesson_content_summary
from openclass_core.services.learning_session_context import learning_session_scope

def _load_root_dotenv() -> None:
    load_root_dotenv()

def _path_from_env(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    path = Path(raw).expanduser() if raw else default
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path
_load_root_dotenv()
DATABASE_PATH = _path_from_env('OPENCLASS_DATABASE_PATH', DATA_DIR / 'openclass.sqlite3')
LEGACY_STORE_PATH = _path_from_env('OPENCLASS_LEGACY_STORE_PATH', DATA_DIR / 'store.json')
STORE = SqliteCourseStore(DATABASE_PATH, legacy_json_path=LEGACY_STORE_PATH)

class _HostRepositoryStore:

    def __init__(self):
        self._value = None

    def __getattr__(self, name):
        if self._value is None:
            self._value = host_service('repository_store', DATABASE_PATH)
        return getattr(self._value, name)
REPOSITORY_STORE = _HostRepositoryStore()
UPLOAD_DIR = _path_from_env('OPENCLASS_UPLOAD_DIR', DATA_DIR / 'uploads')
EXPORT_DIR = _path_from_env('OPENCLASS_EXPORT_DIR', DATA_DIR / 'exports')
RIDOC_WORKSPACE_DIR = _path_from_env('OPENCLASS_RIDOC_WORKSPACE_DIR', DATA_DIR / 'ridoc-workspace')
RIDOC_WORKSPACE_STORE = RidocWorkspaceStore(RIDOC_WORKSPACE_DIR, DATABASE_PATH)

@dataclass
class RepositoryWorkspaceScope:
    repository_id: str
    owner_user_id: str
    version: int
    package_id_override: str | None = None
    defer_saves: bool = False
    workspace_override: WorkspaceState | None = None

@dataclass
class RidocTurnWorkspaceScope:
    owner_user_id: str
    package_id: str
    expected_revision: str
    workspace: WorkspaceState
_REPOSITORY_WORKSPACE_SCOPE: ContextVar[RepositoryWorkspaceScope | None] = ContextVar('openclass_repository_workspace_scope', default=None)
_RIDOC_TURN_WORKSPACE_SCOPE: ContextVar[RidocTurnWorkspaceScope | None] = ContextVar('openclass_ridoc_turn_workspace_scope', default=None)

def current_repository_workspace_scope() -> RepositoryWorkspaceScope | None:
    return _REPOSITORY_WORKSPACE_SCOPE.get()

def current_ridoc_turn_workspace_scope() -> RidocTurnWorkspaceScope | None:
    return _RIDOC_TURN_WORKSPACE_SCOPE.get()

@contextmanager
def bind_ridoc_turn_workspace(owner_user_id: str, lesson_id: str, *, expected_revision: str) -> Iterator[RidocTurnWorkspaceScope]:
    store = get_ridoc_workspace_store()
    package_id = store.package_id_for_lesson(owner_user_id, lesson_id)
    if package_id is None:
        raise HTTPException(status_code=404, detail='RIDOC lesson not found')
    archive = store.load_archive(owner_user_id, package_id=package_id)
    actual = archive.revision.value if archive.revision else ''
    if actual != expected_revision:
        raise HTTPException(status_code=409, detail='RIDOC revision changed; reload before continuing')
    package = store.load_package(owner_user_id, package_id=package_id)
    scope = RidocTurnWorkspaceScope(owner_user_id=owner_user_id, package_id=package_id, expected_revision=expected_revision, workspace=WorkspaceState(packages=[package], active_package_id=package_id))
    token = _RIDOC_TURN_WORKSPACE_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _RIDOC_TURN_WORKSPACE_SCOPE.reset(token)

@contextmanager
def bind_repository_workspace(repository_id: str, owner_user_id: str, *, expected_version: int | None=None, package_id_override: str | None=None) -> Iterator[RepositoryWorkspaceScope]:
    record = REPOSITORY_STORE.get(repository_id)
    if record is None or record.owner_user_id != owner_user_id:
        raise HTTPException(status_code=404, detail='Repository project not found')
    if expected_version is not None and record.version != expected_version:
        raise HTTPException(status_code=409, detail='Repository was updated; reload before continuing')
    scope = RepositoryWorkspaceScope(repository_id=repository_id, owner_user_id=owner_user_id, version=record.version, package_id_override=package_id_override)
    token = _REPOSITORY_WORKSPACE_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _REPOSITORY_WORKSPACE_SCOPE.reset(token)

def _repository_scope_for_user(user_id: str) -> RepositoryWorkspaceScope | None:
    scope = current_repository_workspace_scope()
    if scope is None:
        return None
    if scope.owner_user_id != user_id:
        raise HTTPException(status_code=403, detail='Repository owner access required')
    return scope

def _repository_workspace(scope: RepositoryWorkspaceScope) -> WorkspaceState:
    if scope.workspace_override is not None:
        return scope.workspace_override
    record = REPOSITORY_STORE.get(scope.repository_id)
    if record is None or record.owner_user_id != scope.owner_user_id:
        raise HTTPException(status_code=404, detail='Repository project not found')
    scope.version = record.version
    package = record.working_package.model_copy(deep=True)
    if scope.package_id_override:
        package.id = scope.package_id_override
    lesson_ids = [lesson.id for lesson in package.lessons]
    package.workspace_tab_order = lesson_ids
    package.open_lesson_ids = lesson_ids
    if package.active_lesson_id not in lesson_ids:
        package.active_lesson_id = lesson_ids[0] if lesson_ids else None
    return WorkspaceState(packages=[package], active_package_id=package.id)

def _save_repository_workspace(scope: RepositoryWorkspaceScope, workspace: WorkspaceState, *, expected_version: int) -> int:
    if expected_version != scope.version:
        raise HTTPException(status_code=409, detail='Repository was updated; reload before continuing')
    if len(workspace.packages) != 1:
        raise HTTPException(status_code=400, detail='Repository Studio cannot change the project course collection')
    if scope.defer_saves:
        scope.workspace_override = workspace.model_copy(deep=True)
        return scope.version
    try:
        package_to_save = workspace.packages[0].model_copy(deep=True)
        if scope.package_id_override:
            record = REPOSITORY_STORE.get(scope.repository_id)
            if record is None:
                raise HTTPException(status_code=404, detail='Repository project not found')
            package_to_save.id = record.working_package.id
        updated = REPOSITORY_STORE.save_working(scope.repository_id, scope.owner_user_id, package_to_save, expected_version)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    scope.version = updated.version
    return updated.version

class _RepositoryScopedCourseStore:

    def __getattr__(self, name: str):
        return getattr(_runtime_store(), name)

    def load_lesson_document_context_for_user(self, owner_user_id: str, lesson_id: str) -> LessonDocumentContext | None:
        ridoc_scope = current_ridoc_turn_workspace_scope()
        if ridoc_scope is not None:
            if ridoc_scope.owner_user_id != owner_user_id:
                raise HTTPException(status_code=403, detail='RIDOC owner access required')
            try:
                package, lesson = find_lesson_package(ridoc_scope.workspace, lesson_id)
            except HTTPException:
                return None
            return LessonDocumentContext(package_id=package.id, lesson=lesson.model_copy(deep=True), workspace_revision=0)
        scope = _repository_scope_for_user(owner_user_id)
        if scope is None:
            session = learning_session_scope.get()
            if session is not None:
                if owner_user_id != session.owner_user_id:
                    raise HTTPException(403, 'Learning session owner access required')
                native = session.ridoc_store
                package_id = native.package_id_for_lesson(owner_user_id, lesson_id)
                if package_id is None:
                    return None
                package = native.load_package(owner_user_id, package_id=package_id)
                lesson = next((item for item in package.lessons if item.id == lesson_id))
                return LessonDocumentContext(package_id=package.id, lesson=lesson, workspace_revision=0)
            return _runtime_store().load_lesson_document_context_for_user(owner_user_id, lesson_id)
        workspace = _repository_workspace(scope)
        try:
            package, lesson = find_lesson_package(workspace, lesson_id)
        except HTTPException:
            return None
        return LessonDocumentContext(package_id=package.id, lesson=lesson, workspace_revision=scope.version)

    def package_belongs_to_user(self, owner_user_id: str, package_id: str) -> bool:
        if is_ridoc_package_for_user(owner_user_id, package_id):
            return True
        scope = _repository_scope_for_user(owner_user_id)
        if scope is None:
            return _runtime_store().package_belongs_to_user(owner_user_id, package_id)
        workspace = _repository_workspace(scope)
        return any((package.id == package_id for package in workspace.packages))

    def load_lesson_runtime_state_for_user(self, owner_user_id: str, lesson_id: str) -> LessonRuntimeState | None:
        context = self.load_lesson_document_context_for_user(owner_user_id, lesson_id)
        if context is None:
            return None
        lesson = context.lesson
        head = current_head_commit(lesson)
        runtime = head.runtime_snapshot or LessonRuntimeSnapshot()
        return LessonRuntimeState(lesson_id=lesson.id, current_branch=lesson.history_graph.current_branch, head_commit_id=head.id, runtime_snapshot=runtime, commit_metadata=dict(head.metadata))

    def save_lesson_for_user_if_head(self, owner_user_id: str, lesson: Lesson, *, expected_branch_name: str, expected_head_commit_id: str) -> bool:
        ridoc_scope = current_ridoc_turn_workspace_scope()
        if ridoc_scope is not None:
            package, persisted = find_lesson_package(ridoc_scope.workspace, lesson.id)
            persisted_head = current_head_commit(persisted)
            if persisted.history_graph.current_branch != expected_branch_name or persisted_head.id != expected_head_commit_id:
                return False
            package.lessons = [lesson.model_copy(deep=True) if item.id == lesson.id else item for item in package.lessons]
            return True
        scope = _repository_scope_for_user(owner_user_id)
        if scope is None:
            return _runtime_store().save_lesson_for_user_if_head(owner_user_id, lesson, expected_branch_name=expected_branch_name, expected_head_commit_id=expected_head_commit_id)
        workspace = _repository_workspace(scope)
        package, persisted = find_lesson_package(workspace, lesson.id)
        persisted_head = current_head_commit(persisted)
        if persisted.history_graph.current_branch != expected_branch_name or persisted_head.id != expected_head_commit_id:
            return False
        package.lessons = [lesson.model_copy(deep=True) if item.id == lesson.id else item for item in package.lessons]
        _save_repository_workspace(scope, workspace, expected_version=scope.version)
        return True

    def save_document_for_user_if_head(self, owner_user_id: str, lesson: Lesson, *, expected_branch_name: str, expected_head_commit_id: str) -> int | None:
        saved = self.save_lesson_for_user_if_head(owner_user_id, lesson, expected_branch_name=expected_branch_name, expected_head_commit_id=expected_head_commit_id)
        scope = current_repository_workspace_scope()
        return scope.version if saved and scope is not None else None

    def append_non_document_commit_for_user_if_head(self, owner_user_id: str, lesson_id: str, commit: CommitRecord, *, expected_branch_name: str, expected_head_commit_id: str, lesson_updated_at: str) -> bool:
        ridoc_scope = current_ridoc_turn_workspace_scope()
        if ridoc_scope is None:
            return _runtime_store().append_non_document_commit_for_user_if_head(owner_user_id, lesson_id, commit, expected_branch_name=expected_branch_name, expected_head_commit_id=expected_head_commit_id, lesson_updated_at=lesson_updated_at)
        package, lesson = find_lesson_package(ridoc_scope.workspace, lesson_id)
        head = current_head_commit(lesson)
        if lesson.history_graph.current_branch != expected_branch_name or head.id != expected_head_commit_id:
            return False
        lesson.history_graph.commits.append(commit.model_copy(deep=True))
        lesson.history_graph.branches[expected_branch_name].head_commit_id = commit.id
        lesson.updated_at = lesson_updated_at
        package.lessons = [lesson if item.id == lesson_id else item for item in package.lessons]
        return True
REPOSITORY_SCOPED_STORE = _RepositoryScopedCourseStore()

def ensure_data_dirs() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    RIDOC_WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

def _runtime_store() -> SqliteCourseStore:
    scope = learning_session_scope.get()
    return scope.runtime_store if scope is not None else STORE

def get_store() -> SqliteCourseStore:
    return REPOSITORY_SCOPED_STORE if current_repository_workspace_scope() or current_ridoc_turn_workspace_scope() or learning_session_scope.get() else _runtime_store()

def get_course_store() -> SqliteCourseStore:
    return get_store()

def get_repository_store():
    return REPOSITORY_STORE

def get_ridoc_workspace_store() -> RidocWorkspaceStore:
    temporary_scope = learning_session_scope.get()
    if temporary_scope is not None:
        return temporary_scope.ridoc_store
    scope = current_repository_workspace_scope()
    if scope is not None:
        return host_service('repository_ridoc_adapter', REPOSITORY_STORE, scope.repository_id, scope.owner_user_id)
    return RIDOC_WORKSPACE_STORE

def _ridoc_workspace_for_user(user_id: str) -> WorkspaceState:
    packages = [get_ridoc_workspace_store().load_package(user_id, package_id=view.id) for view in get_ridoc_workspace_store().list_packages(user_id)]
    active = get_ridoc_workspace_store().active_package_id(user_id)
    if active not in {package.id for package in packages}:
        active = packages[0].id if packages else None
    return WorkspaceState(packages=packages, active_package_id=active)

def is_ridoc_package_for_user(user_id: str, package_id: str) -> bool:
    scope = current_repository_workspace_scope()
    if scope is not None:
        record = REPOSITORY_STORE.get(scope.repository_id)
        return bool(record and record.owner_user_id == user_id and (record.working_package.storage_kind == 'ridoc') and (package_id in {record.working_package.id, scope.package_id_override}))
    return any((view.id == package_id for view in get_ridoc_workspace_store().list_packages(user_id)))

def is_ridoc_lesson_for_user(user_id: str, lesson_id: str) -> bool:
    scope = current_repository_workspace_scope()
    if scope is not None:
        record = REPOSITORY_STORE.get(scope.repository_id)
        return bool(record and record.owner_user_id == user_id and (record.working_package.storage_kind == 'ridoc') and any((lesson.id == lesson_id for lesson in record.working_package.lessons)))
    return get_ridoc_workspace_store().package_id_for_lesson(user_id, lesson_id) is not None

def _combined_workspace(user_id: str, legacy: WorkspaceState) -> WorkspaceState:
    ridoc = _ridoc_workspace_for_user(user_id)
    ridoc_ids = {package.id for package in ridoc.packages}
    packages = [package for package in legacy.packages if package.id not in ridoc_ids]
    packages.extend(ridoc.packages)
    active = ridoc.active_package_id or legacy.active_package_id
    if active not in {package.id for package in packages}:
        active = packages[0].id if packages else None
    return WorkspaceState(packages=packages, active_package_id=active)

def load_workspace() -> WorkspaceState:
    return get_store().load()

def save_workspace(workspace: WorkspaceState) -> None:
    get_store().save(workspace)

def load_workspace_for_user(user_id: str) -> WorkspaceState:
    ridoc_scope = current_ridoc_turn_workspace_scope()
    if ridoc_scope is not None:
        if ridoc_scope.owner_user_id != user_id:
            raise HTTPException(status_code=403, detail='RIDOC owner access required')
        return ridoc_scope.workspace.model_copy(deep=True)
    scope = _repository_scope_for_user(user_id)
    if scope is not None:
        return _repository_workspace(scope)
    mode = course_store_mode()
    if mode == 'ridoc_required':
        return _ridoc_workspace_for_user(user_id)
    legacy = get_store().load_for_user(user_id)
    if mode == 'ridoc_new_only':
        return _combined_workspace(user_id, legacy)
    return legacy

def load_workspace_for_user_with_revision(user_id: str) -> tuple[WorkspaceState, int]:
    ridoc_scope = current_ridoc_turn_workspace_scope()
    if ridoc_scope is not None:
        if ridoc_scope.owner_user_id != user_id:
            raise HTTPException(status_code=403, detail='RIDOC owner access required')
        return (ridoc_scope.workspace.model_copy(deep=True), 0)
    scope = _repository_scope_for_user(user_id)
    if scope is not None:
        workspace = _repository_workspace(scope)
        return (workspace, scope.version)
    mode = course_store_mode()
    if mode == 'ridoc_required':
        return (_ridoc_workspace_for_user(user_id), 0)
    legacy, revision = get_store().load_for_user_with_revision(user_id)
    if mode == 'ridoc_new_only':
        return (_combined_workspace(user_id, legacy), revision)
    return (legacy, revision)

def load_lesson_runtime_state_for_user(user_id: str, lesson_id: str) -> LessonRuntimeState | None:
    scope = _repository_scope_for_user(user_id)
    if scope is not None:
        return REPOSITORY_SCOPED_STORE.load_lesson_runtime_state_for_user(user_id, lesson_id)
    if current_ridoc_turn_workspace_scope() is None and is_ridoc_lesson_for_user(user_id, lesson_id):
        package = get_ridoc_workspace_store().load_package(user_id, lesson_id=lesson_id)
        lesson = next((item for item in package.lessons if item.id == lesson_id))
        head = current_head_commit(lesson)
        return LessonRuntimeState(lesson_id=lesson.id, current_branch=lesson.history_graph.current_branch, head_commit_id=head.id, runtime_snapshot=head.runtime_snapshot or LessonRuntimeSnapshot(), commit_metadata=dict(head.metadata))
    return get_store().load_lesson_runtime_state_for_user(user_id, lesson_id)

def claim_chat_input_event(*, user_id: str, session_id: str, input_event_id: str, fingerprint: str, claim_id: str) -> tuple[ChatInputEventClaimStatus, dict[str, object] | None]:
    return get_store().claim_chat_input_event(owner_user_id=user_id, session_id=session_id, input_event_id=input_event_id, fingerprint=fingerprint, claim_id=claim_id)

def complete_chat_input_event(*, user_id: str, session_id: str, input_event_id: str, fingerprint: str, claim_id: str, response: dict[str, object]) -> None:
    get_store().complete_chat_input_event(owner_user_id=user_id, session_id=session_id, input_event_id=input_event_id, fingerprint=fingerprint, claim_id=claim_id, response=response)

def release_chat_input_event(*, user_id: str, session_id: str, input_event_id: str, fingerprint: str, claim_id: str) -> None:
    get_store().release_chat_input_event(owner_user_id=user_id, session_id=session_id, input_event_id=input_event_id, fingerprint=fingerprint, claim_id=claim_id)

def save_workspace_for_user_if_revision(user_id: str, workspace: WorkspaceState, *, expected_revision: int) -> None:
    ridoc_scope = current_ridoc_turn_workspace_scope()
    if ridoc_scope is not None:
        if ridoc_scope.owner_user_id != user_id:
            raise HTTPException(status_code=403, detail='RIDOC owner access required')
        ridoc_scope.workspace = workspace.model_copy(deep=True)
        return
    scope = _repository_scope_for_user(user_id)
    if scope is not None:
        _save_repository_workspace(scope, workspace, expected_version=expected_revision)
        return
    mode = course_store_mode()
    if mode != 'legacy':
        ridoc_packages = [package for package in workspace.packages if package.storage_kind == 'ridoc']
        for package in ridoc_packages:
            persisted = get_ridoc_workspace_store().load_package(user_id, package_id=package.id)
            excluded = {'open_lesson_ids', 'active_lesson_id', 'workspace_tab_order', 'ridoc_revision'}
            if package.model_dump(mode='json', exclude=excluded) != persisted.model_dump(mode='json', exclude=excluded):
                raise HTTPException(status_code=409, detail='RIDOC course changes require a native revision-aware mutation')
            get_ridoc_workspace_store().update_ui_state(user_id, package.id, active_lesson_id=package.active_lesson_id, open_lesson_ids=package.open_lesson_ids, workspace_tab_order=package.workspace_tab_order)
        active_is_ridoc = any((package.id == workspace.active_package_id for package in ridoc_packages))
        get_ridoc_workspace_store().set_active_package_id(user_id, workspace.active_package_id if active_is_ridoc else None)
        legacy_packages = [package for package in workspace.packages if package.storage_kind != 'ridoc']
        if mode == 'ridoc_required':
            if legacy_packages:
                raise HTTPException(status_code=409, detail='Legacy courses are disabled')
            return
        legacy_workspace = WorkspaceState(packages=legacy_packages, active_package_id=workspace.active_package_id if any((package.id == workspace.active_package_id for package in legacy_packages)) else legacy_packages[0].id if legacy_packages else None)
        saved = get_store().save_for_user_if_revision(user_id, legacy_workspace, expected_revision=expected_revision)
    else:
        saved = get_store().save_for_user_if_revision(user_id, workspace, expected_revision=expected_revision)
    if not saved:
        raise HTTPException(status_code=409, detail='The workspace changed during this operation. Refresh and try again.')

def save_lesson_for_user_if_head(user_id: str, lesson: Lesson, *, expected_branch_name: str, expected_head_commit_id: str) -> bool:
    scope = _repository_scope_for_user(user_id)
    if scope is not None:
        return REPOSITORY_SCOPED_STORE.save_lesson_for_user_if_head(user_id, lesson, expected_branch_name=expected_branch_name, expected_head_commit_id=expected_head_commit_id)
    return get_store().save_lesson_for_user_if_head(user_id, lesson, expected_branch_name=expected_branch_name, expected_head_commit_id=expected_head_commit_id)

def append_non_document_commit_for_user_if_head(user_id: str, lesson_id: str, commit: CommitRecord, *, expected_branch_name: str, expected_head_commit_id: str, lesson_updated_at: str) -> bool:
    if is_ridoc_lesson_for_user(user_id, lesson_id):
        from openclass_core.services.ridoc_realtime_context import realtime_completion_guard
        from openclass_core.services.ridoc_v3 import RidocV3Error
        store = get_ridoc_workspace_store()
        package_id = store.package_id_for_lesson(user_id, lesson_id)
        if package_id is None:
            return False
        metadata = commit.metadata if isinstance(commit.metadata, dict) else {}
        transaction_id = str(metadata.get('turn_id') or metadata.get('chat_turn_id') or metadata.get('codex_turn_id') or metadata.get('realtime_turn_id') or commit.id)
        try:
            with realtime_completion_guard(lesson_id, user_id=user_id, session_id=str(metadata.get('realtime_client_session_id') or ''), transaction_id=transaction_id, input_event_id=str(metadata.get('realtime_client_event_id') or '')) as expected_revision:
                archive = store.load_archive(user_id, package_id=package_id)
                graph = archive.package.lessons[lesson_id]
                branch = graph.branches[graph.current_branch]
                if branch.name != expected_branch_name or branch.head_node_id != expected_head_commit_id:
                    return False
                user_message = str(metadata.get('user_message') or '').strip()
                assistant_message = str(metadata.get('assistant_message') or '').strip()
                if not user_message or not assistant_message:
                    store.audit(owner_user_id=user_id, package_id=package_id, lesson_id=lesson_id, kind='incomplete_realtime_turn_suppressed', payload={'source_commit_id': commit.id})
                    return True
                store.commit_turn(owner_user_id=user_id, package_id=package_id, lesson_id=lesson_id, expected_revision=expected_revision or '', transaction_id=transaction_id, user_message=user_message, assistant_message=assistant_message, assistant_source=str(metadata.get('assistant_message_source') or 'realtime'))
        except RidocV3Error:
            return False
        return True
    return get_store().append_non_document_commit_for_user_if_head(user_id, lesson_id, commit, expected_branch_name=expected_branch_name, expected_head_commit_id=expected_head_commit_id, lesson_updated_at=lesson_updated_at)

def load_merge_session_for_user(user_id: str, session_id: str) -> LessonMergeSession | None:
    scope = _repository_scope_for_user(user_id)
    if scope is not None:
        return REPOSITORY_STORE.load_merge_session(scope.repository_id, session_id)
    ridoc_session = get_ridoc_workspace_store().load_merge_session(user_id, session_id)
    if ridoc_session is not None:
        return ridoc_session
    return get_store().load_merge_session_for_user(user_id, session_id)

def load_active_merge_session_for_user(user_id: str, lesson_id: str) -> LessonMergeSession | None:
    scope = _repository_scope_for_user(user_id)
    if scope is not None:
        return REPOSITORY_STORE.load_active_merge_session(scope.repository_id, lesson_id)
    if is_ridoc_lesson_for_user(user_id, lesson_id):
        return get_ridoc_workspace_store().load_active_merge_session(user_id, lesson_id)
    return get_store().load_active_merge_session_for_user(user_id, lesson_id)

def save_merge_session_for_user(session: LessonMergeSession) -> None:
    scope = current_repository_workspace_scope()
    if scope is not None:
        if session.owner_user_id != scope.owner_user_id:
            raise HTTPException(status_code=403, detail='Repository owner access required')
        REPOSITORY_STORE.save_merge_session(scope.repository_id, session)
        return
    if is_ridoc_lesson_for_user(session.owner_user_id, session.lesson_id):
        get_ridoc_workspace_store().save_merge_session(session)
        return
    get_store().save_merge_session_for_user(session)

def save_merge_session_for_user_if_version(session: LessonMergeSession, *, expected_version: int) -> None:
    scope = current_repository_workspace_scope()
    if scope is not None:
        if session.owner_user_id != scope.owner_user_id:
            raise HTTPException(status_code=403, detail='Repository owner access required')
        saved = REPOSITORY_STORE.save_merge_session(scope.repository_id, session, expected_version=expected_version)
        if not saved:
            raise HTTPException(status_code=409, detail='Merge draft was updated; reload before continuing')
        return
    ridoc_session = get_ridoc_workspace_store().load_merge_session(session.owner_user_id, session.id)
    if ridoc_session is not None:
        saved = get_ridoc_workspace_store().save_merge_session(session, expected_version=expected_version)
    else:
        saved = get_store().save_merge_session_for_user_if_version(session, expected_version=expected_version)
    if not saved:
        raise HTTPException(status_code=409, detail='The merge draft changed in another window. Refresh to continue.')

def save_workspace_and_merge_session_for_user_if_revision(user_id: str, workspace: WorkspaceState, session: LessonMergeSession, *, expected_revision: int) -> None:
    scope = _repository_scope_for_user(user_id)
    if scope is not None:
        stored = REPOSITORY_STORE.load_merge_session(scope.repository_id, session.id)
        if stored is None or stored.version != session.version - 1:
            raise HTTPException(status_code=409, detail='Repository or merge draft was updated; reload before continuing')
        _save_repository_workspace(scope, workspace, expected_version=expected_revision)
        saved = REPOSITORY_STORE.save_merge_session(scope.repository_id, session, expected_version=stored.version)
        if not saved:
            raise HTTPException(status_code=409, detail='Merge draft was updated; reload before continuing')
        return
    saved = get_store().save_workspace_and_merge_session_for_user_if_revision(user_id, workspace, session, expected_revision=expected_revision)
    if not saved:
        raise HTTPException(status_code=409, detail='The workspace or merge draft changed. Refresh and try again.')

def search_document_segments_for_user(user_id: str, query: str='', *, kind: BoardSegmentKind | None=None, limit: int=20) -> list[DocumentSegmentSearchResult]:
    return get_store().search_document_segments(query, owner_user_id=user_id, kind=kind, limit=limit)

def get_package(workspace: WorkspaceState, package_id: str) -> CoursePackage:
    for package in workspace.packages:
        if package.id == package_id:
            return package
    raise HTTPException(status_code=404, detail=f'Unknown course package {package_id}')

def get_active_package(workspace: WorkspaceState) -> CoursePackage:
    if not workspace.packages:
        raise HTTPException(status_code=404, detail='No course package available')
    if workspace.active_package_id:
        return get_package(workspace, workspace.active_package_id)
    workspace.active_package_id = workspace.packages[0].id
    return workspace.packages[0]

def get_standalone_package(workspace: WorkspaceState) -> CoursePackage:
    if not workspace.packages:
        raise HTTPException(status_code=404, detail='No standalone course pool available')
    return workspace.packages[0]

def load_workspace_package() -> tuple[WorkspaceState, CoursePackage]:
    workspace = load_workspace()
    package = get_active_package(workspace)
    return (workspace, package)

def load_workspace_package_for_user(user_id: str) -> tuple[WorkspaceState, CoursePackage]:
    workspace = load_workspace_for_user(user_id)
    package = get_active_package(workspace)
    return (workspace, package)

def load_workspace_package_for_user_with_revision(user_id: str) -> tuple[WorkspaceState, CoursePackage, int]:
    workspace, revision = load_workspace_for_user_with_revision(user_id)
    package = get_active_package(workspace)
    return (workspace, package, revision)

def get_lesson(package: CoursePackage, lesson_id: str) -> Lesson:
    for lesson in package.lessons:
        if lesson.id == lesson_id:
            return lesson
    raise HTTPException(status_code=404, detail=f'Unknown lesson {lesson_id}')

def find_lesson_package(workspace: WorkspaceState, lesson_id: str) -> tuple[CoursePackage, Lesson]:
    for package in workspace.packages:
        for lesson in package.lessons:
            if lesson.id == lesson_id:
                return (package, lesson)
    raise HTTPException(status_code=404, detail=f'Unknown lesson {lesson_id}')

def is_standalone_package(workspace: WorkspaceState, package: CoursePackage) -> bool:
    if package.storage_kind == 'ridoc':
        return package.package_kind == 'standalone'
    return bool(workspace.packages and workspace.packages[0].id == package.id)

def resources_visible_to_lesson(package: CoursePackage, *, lesson_id: str | None, isolate_lesson_resources: bool) -> list[ResourceLibraryItem]:
    if not isolate_lesson_resources:
        return list(package.resources)
    if not lesson_id:
        return []
    return [resource for resource in package.resources if resource.scope_lesson_id == lesson_id]

def package_context_for_lesson(workspace: WorkspaceState, package: CoursePackage, lesson_id: str | None) -> CoursePackage:
    resources = resources_visible_to_lesson(package, lesson_id=lesson_id, isolate_lesson_resources=is_standalone_package(workspace, package))
    return package.model_copy(update={'resources': resources})

def normalize_package_state(package: CoursePackage) -> None:
    lesson_ids = [lesson.id for lesson in package.lessons]
    valid_ids = set(lesson_ids)
    package.open_lesson_ids = [lesson_id for lesson_id in package.open_lesson_ids if lesson_id in valid_ids]
    package.workspace_tab_order = [lesson_id for lesson_id in package.workspace_tab_order if lesson_id in valid_ids]
    package.course_graph = [edge for edge in package.course_graph if edge.source_lesson_id in valid_ids and edge.target_lesson_id in valid_ids]
    if not package.lessons:
        package.active_lesson_id = None
        package.open_lesson_ids = []
        package.workspace_tab_order = []
        return
    if not package.workspace_tab_order:
        package.workspace_tab_order = [package.lessons[0].id]
    if not package.open_lesson_ids:
        package.open_lesson_ids = list(package.workspace_tab_order)
    for lesson_id in package.workspace_tab_order:
        if lesson_id not in package.open_lesson_ids:
            package.open_lesson_ids.append(lesson_id)
    if package.active_lesson_id not in valid_ids:
        package.active_lesson_id = package.workspace_tab_order[0]
    elif package.active_lesson_id not in package.workspace_tab_order:
        package.workspace_tab_order.append(package.active_lesson_id)
        if package.active_lesson_id not in package.open_lesson_ids:
            package.open_lesson_ids.append(package.active_lesson_id)

def lesson_view(lesson: Lesson) -> LessonView:
    lesson = _codex_only_lesson(lesson)
    return LessonView.model_validate(lesson.model_dump(mode='json', exclude={'teaching_guide', 'board_teaching_guide'}))

def _codex_only_lesson(lesson: Lesson) -> Lesson:
    board_is_empty = not lesson.board_document.content_text.strip()
    active_requirement = active_requirement_from_history(lesson) if board_is_empty else None
    runtime = current_head_commit(lesson).runtime_snapshot
    active_board_task = runtime.board_task_requirements if not board_is_empty and runtime is not None else None
    return lesson.model_copy(update={'summary': lesson_content_summary(lesson), 'board_teaching_guide': None, 'board_teaching_progress': None, 'learning_requirements': active_requirement, 'board_task_requirements': active_board_task})

def package_view(package: CoursePackage, *, is_standalone: bool=False, resource_lesson_id: str | None=None, isolate_lesson_resources: bool=False) -> CoursePackageView:
    lessons_for_view = [_codex_only_lesson(lesson) for lesson in package.lessons]
    visible_package = package.model_copy(update={'lessons': lessons_for_view, 'resources': resources_visible_to_lesson(package, lesson_id=resource_lesson_id, isolate_lesson_resources=isolate_lesson_resources)})
    package_data = visible_package.model_dump(mode='json', exclude={'lessons': {'__all__': {'teaching_guide', 'board_teaching_guide'}}})
    package_data['is_standalone'] = is_standalone
    return CoursePackageView.model_validate(package_data)

def package_view_for_lesson(workspace: WorkspaceState, package: CoursePackage, lesson_id: str | None) -> CoursePackageView:
    standalone = is_standalone_package(workspace, package)
    return package_view(package, is_standalone=standalone, resource_lesson_id=lesson_id, isolate_lesson_resources=standalone)

def workspace_view(workspace: WorkspaceState) -> WorkspaceStateView:
    return WorkspaceStateView(packages=[package_view_for_lesson(workspace, package, package.active_lesson_id) for package in workspace.packages], active_package_id=workspace.active_package_id)

def commit_document_snapshot(lesson: Lesson, *, label: str, message: str, metadata: dict[str, object] | None=None) -> None:
    commit_operations(lesson, [], label=label, message=message, new_document=lesson.board_document, metadata=metadata)
