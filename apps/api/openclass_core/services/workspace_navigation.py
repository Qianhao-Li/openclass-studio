"""Disposable navigation metadata; editable content still comes from validated RIDOC."""
from __future__ import annotations
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel
from openclass_core.models import CoursePackage
from openclass_core.services import workspace_state
from openclass_core.services.ridoc_workspace_store import course_store_mode

class LessonNavigationItem(BaseModel):
    id: str
    package_id: str
    package_kind: Literal['course', 'standalone']
    title: str
    current_branch: str | None = None
    updated_at: str

class WorkspaceTabsView(BaseModel):
    lessons: list[LessonNavigationItem]
    active_package_id: str | None

def workspace_tabs(user_id: str) -> WorkspaceTabsView:
    if course_store_mode() == 'ridoc_required' and (not workspace_state.current_repository_workspace_scope()):
        store = workspace_state.get_ridoc_workspace_store()
        lessons = [LessonNavigationItem(id=lesson.id, package_id=package.id, package_kind=package.package_kind, title=lesson.title, current_branch=lesson.current_branch, updated_at=lesson.updated_at) for package in store.list_packages(user_id) for lesson in package.lessons if lesson.id in package.workspace_tab_order]
        active = store.active_package_id(user_id)
    else:
        workspace = workspace_state.load_workspace_for_user(user_id)
        lessons = [LessonNavigationItem(id=lesson.id, package_id=package.id, package_kind='standalone' if workspace_state.is_standalone_package(workspace, package) else 'course', title=lesson.title, current_branch=lesson.history_graph.current_branch, updated_at=lesson.updated_at) for package in workspace.packages for lesson in package.lessons if lesson.id in package.workspace_tab_order]
        active = workspace.active_package_id
    lessons.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
    return WorkspaceTabsView(lessons=lessons, active_package_id=active)

def readable_course_package(user_id: str, lesson_id: str | None=None) -> tuple[CoursePackage, bool]:
    scoped = workspace_state.current_repository_workspace_scope() or workspace_state.current_ridoc_turn_workspace_scope()
    if course_store_mode() == 'ridoc_required' and (not scoped):
        store = workspace_state.get_ridoc_workspace_store()
        package_id = store.package_id_for_lesson(user_id, lesson_id) if lesson_id else store.active_package_id(user_id)
        if not package_id and (not lesson_id):
            packages = store.list_packages(user_id)
            package_id = packages[0].id if packages else None
        if not package_id:
            raise HTTPException(404, 'Course is not available')
        package = store.load_package(user_id, package_id=package_id)
        standalone = package.package_kind == 'standalone'
    else:
        workspace = workspace_state.load_workspace_for_user(user_id)
        if lesson_id:
            package, _ = workspace_state.find_lesson_package(workspace, lesson_id)
        else:
            package = workspace_state.get_active_package(workspace)
        standalone = workspace_state.is_standalone_package(workspace, package)
    if lesson_id:
        if not any((lesson.id == lesson_id for lesson in package.lessons)):
            raise HTTPException(404, 'Lesson is not available')
        package = package.model_copy(update={'active_lesson_id': lesson_id})
    return (package, standalone)
