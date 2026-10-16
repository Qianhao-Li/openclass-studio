from __future__ import annotations
from fastapi import HTTPException
from openclass_core.models import BatchLessonActionRequest, CoursePackage, Lesson, WorkspaceState
from openclass_core.services.workspace_state import get_package, normalize_package_state

def apply_lesson_batch_action(workspace: WorkspaceState, request: BatchLessonActionRequest) -> None:
    """Apply one validated lesson batch mutation to the in-memory workspace."""
    lesson_ids = list(dict.fromkeys((lesson_id.strip() for lesson_id in request.lesson_ids if lesson_id.strip())))
    if not lesson_ids:
        raise HTTPException(status_code=400, detail='At least one lesson is required')
    locations = _lesson_locations(workspace, lesson_ids)
    missing_ids = [lesson_id for lesson_id in lesson_ids if lesson_id not in locations]
    if missing_ids:
        raise HTTPException(status_code=404, detail=f'Unknown lesson {missing_ids[0]}')
    if request.action == 'delete':
        _delete_lessons(locations, lesson_ids)
        return
    if not request.target_package_id:
        raise HTTPException(status_code=400, detail='Target package is required')
    target_package = get_package(workspace, request.target_package_id)
    _move_lessons(locations, lesson_ids, target_package)

def _lesson_locations(workspace: WorkspaceState, lesson_ids: list[str]) -> dict[str, tuple[CoursePackage, Lesson]]:
    requested = set(lesson_ids)
    return {lesson.id: (package, lesson) for package in workspace.packages for lesson in package.lessons if lesson.id in requested}

def _delete_lessons(locations: dict[str, tuple[CoursePackage, Lesson]], lesson_ids: list[str]) -> None:
    selected = set(lesson_ids)
    affected_packages = {locations[lesson_id][0].id: locations[lesson_id][0] for lesson_id in lesson_ids}
    for package in affected_packages.values():
        package.lessons = [lesson for lesson in package.lessons if lesson.id not in selected]
        package.open_lesson_ids = [lesson_id for lesson_id in package.open_lesson_ids if lesson_id not in selected]
        package.workspace_tab_order = [lesson_id for lesson_id in package.workspace_tab_order if lesson_id not in selected]
        if package.active_lesson_id in selected:
            package.active_lesson_id = None
        normalize_package_state(package)

def _move_lessons(locations: dict[str, tuple[CoursePackage, Lesson]], lesson_ids: list[str], target_package: CoursePackage) -> None:
    selected = set(lesson_ids)
    affected_sources = {package.id: package for lesson_id in lesson_ids for package, _lesson in [locations[lesson_id]] if package.id != target_package.id}
    lessons_to_move = [locations[lesson_id][1] for lesson_id in lesson_ids if locations[lesson_id][0].id != target_package.id]
    for package in affected_sources.values():
        package.lessons = [lesson for lesson in package.lessons if lesson.id not in selected]
        package.open_lesson_ids = [lesson_id for lesson_id in package.open_lesson_ids if lesson_id not in selected]
        package.workspace_tab_order = [lesson_id for lesson_id in package.workspace_tab_order if lesson_id not in selected]
        if package.active_lesson_id in selected:
            package.active_lesson_id = None
        normalize_package_state(package)
    existing_ids = {lesson.id for lesson in target_package.lessons}
    for lesson in lessons_to_move:
        if lesson.id not in existing_ids:
            target_package.lessons.append(lesson)
            existing_ids.add(lesson.id)
        if lesson.id not in target_package.open_lesson_ids:
            target_package.open_lesson_ids.append(lesson.id)
        if lesson.id not in target_package.workspace_tab_order:
            target_package.workspace_tab_order.append(lesson.id)
    if target_package.active_lesson_id is None and lessons_to_move:
        target_package.active_lesson_id = lessons_to_move[0].id
    normalize_package_state(target_package)
