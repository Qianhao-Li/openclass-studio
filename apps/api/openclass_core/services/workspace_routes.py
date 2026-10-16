"""Stable, owner-scoped browser aliases. Never materialize a repository or edit RIDOC."""
from __future__ import annotations
from dataclasses import dataclass
import re
import sqlite3
import unicodedata
from urllib.parse import quote
from fastapi import HTTPException
from openclass_core.services import workspace_state
from openclass_core.services.ridoc_lock import ridoc_file_lock
from openclass_core.services.ridoc_v3 import RidocV3ConflictError, read_ridoc_v3
from openclass_core.services.ridoc_workspace_store import course_store_mode
from openclass_core.workspace_route_models import WorkspaceRouteView

@dataclass(frozen=True)
class _Target:
    kind: str
    target_id: str
    package_id: str
    lesson_id: str
    course_title: str
    lesson_title: str

def _missing() -> HTTPException:
    return HTTPException(status_code=404, detail='Workspace course not found')

def _slug(title: str) -> str:
    normalized = unicodedata.normalize('NFKC', title).strip().casefold()
    return re.sub('[^\\w-]+', '-', normalized, flags=re.UNICODE).strip('-')[:80].rstrip('-') or 'course'

def _native_target(owner: str, lesson_id: str | None, package_id: str | None=None) -> _Target | None:
    store = workspace_state.get_ridoc_workspace_store()
    resolved = package_id or (store.package_id_for_lesson(owner, lesson_id) if lesson_id else store.active_package_id(owner))
    if resolved is None:
        return None
    index = store._existing_index(owner, resolved)
    if index is None:
        return None
    path = store.package_path(owner, resolved)
    with ridoc_file_lock(path):
        if not path.is_file():
            raise _missing()
        archive = read_ridoc_v3(path)
    if archive.package.id != resolved:
        raise _missing()
    if not archive.revision or archive.revision.value != index.revision:
        raise RidocV3ConflictError('Course index changed; reload before resolving its address')
    selected = lesson_id or index.active_lesson_id
    if not selected or selected not in archive.package.lessons:
        raise _missing()
    lesson = archive.package.lessons[selected]
    standalone = archive.package.kind == 'standalone'
    return _Target('lesson' if standalone else 'package', selected if standalone else resolved, resolved, selected, lesson.title if standalone else archive.package.title, lesson.title)

def _target(owner: str, lesson_id: str | None, package_id: str | None=None) -> _Target:
    mode = course_store_mode()
    if mode != 'legacy':
        native = _native_target(owner, lesson_id, package_id)
        if native is not None:
            return native
        if mode == 'ridoc_required':
            raise _missing()
    store = workspace_state.get_course_store()
    with store._lock, store._connect() as connection:
        workspace = store._read_workspace(connection, owner_user_id=owner)
    if lesson_id:
        found = next(((package, lesson) for package in workspace.packages for lesson in package.lessons if lesson.id == lesson_id and (not package_id or package.id == package_id)), None)
    else:
        package = next((item for item in workspace.packages if item.id == (package_id or workspace.active_package_id)), None)
        lesson = next((item for item in package.lessons if item.id == package.active_lesson_id), None) if package else None
        found = (package, lesson) if package and lesson else None
    if found is None:
        raise _missing()
    package, lesson = found
    standalone = workspace_state.is_standalone_package(workspace, package)
    return _Target('lesson' if standalone else 'package', lesson.id if standalone else package.id, package.id, lesson.id, lesson.title if standalone else package.title, lesson.title)

def _schema(connection: sqlite3.Connection) -> None:
    connection.execute('CREATE TABLE IF NOT EXISTS workspace_course_routes (\n        owner_user_id TEXT NOT NULL, target_kind TEXT NOT NULL, target_id TEXT NOT NULL,\n        slug TEXT NOT NULL, default_lesson_id TEXT NOT NULL,\n        PRIMARY KEY(owner_user_id, target_kind, target_id), UNIQUE(owner_user_id, slug)\n    )')
    connection.execute('CREATE TABLE IF NOT EXISTS workspace_lesson_routes (\n        owner_user_id TEXT NOT NULL, course_id TEXT NOT NULL, lesson_id TEXT NOT NULL,\n        slug TEXT NOT NULL, PRIMARY KEY(owner_user_id, course_id, lesson_id),\n        UNIQUE(owner_user_id, course_id, slug)\n    )')

def _unique_slug(connection: sqlite3.Connection, title: str, query: str, scope: tuple[str, ...]) -> str:
    base = _slug(title)
    candidate, suffix = (base, 2)
    while connection.execute(query, (*scope, candidate)).fetchone():
        candidate = f'{base[:74]}-{suffix}'
        suffix += 1
    return candidate

def _ensure_route(connection: sqlite3.Connection, owner: str, target: _Target) -> WorkspaceRouteView:
    course = connection.execute('SELECT slug FROM workspace_course_routes WHERE owner_user_id=? AND target_kind=? AND target_id=?', (owner, target.kind, target.target_id)).fetchone()
    if course is None:
        course_slug = _unique_slug(connection, target.course_title, 'SELECT 1 FROM workspace_course_routes WHERE owner_user_id=? AND slug=?', (owner,))
        connection.execute('INSERT INTO workspace_course_routes VALUES (?,?,?,?,?)', (owner, target.kind, target.target_id, course_slug, target.lesson_id))
    else:
        course_slug = str(course['slug'])
    path = f"/studio/{quote(course_slug, safe='-._~')}"
    if target.kind == 'package':
        lesson = connection.execute('SELECT slug FROM workspace_lesson_routes WHERE owner_user_id=? AND course_id=? AND lesson_id=?', (owner, target.target_id, target.lesson_id)).fetchone()
        if lesson is None:
            lesson_slug = _unique_slug(connection, target.lesson_title, 'SELECT 1 FROM workspace_lesson_routes WHERE owner_user_id=? AND course_id=? AND slug=?', (owner, target.target_id))
            connection.execute('INSERT INTO workspace_lesson_routes VALUES (?,?,?,?)', (owner, target.target_id, target.lesson_id, lesson_slug))
        else:
            lesson_slug = str(lesson['slug'])
        path += f"/{quote(lesson_slug, safe='-._~')}"
    return WorkspaceRouteView(canonical_path=path, package_id=target.package_id, lesson_id=target.lesson_id)

def route_for_lesson(owner: str, lesson_id: str | None=None) -> WorkspaceRouteView:
    target = _target(owner, lesson_id)
    store = workspace_state.get_course_store()
    with store._lock, store._connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        _schema(connection)
        return _ensure_route(connection, owner, target)

def resolve_route(owner: str, course_slug: str, lesson_slug: str | None=None) -> WorkspaceRouteView:
    store = workspace_state.get_course_store()
    with store._lock, store._connect() as connection:
        _schema(connection)
        course = connection.execute('SELECT * FROM workspace_course_routes WHERE owner_user_id=? AND slug=?', (owner, course_slug.casefold())).fetchone()
        if course is None:
            raise _missing()
        lesson_id = course['default_lesson_id']
        if lesson_slug is not None:
            if course['target_kind'] != 'package':
                raise _missing()
            lesson = connection.execute('SELECT lesson_id FROM workspace_lesson_routes WHERE owner_user_id=? AND course_id=? AND slug=?', (owner, course['target_id'], lesson_slug.casefold())).fetchone()
            if lesson is None:
                raise _missing()
            lesson_id = lesson['lesson_id']
    target = _target(owner, lesson_id, course['target_id'] if course['target_kind'] == 'package' else None)
    if (target.kind, target.target_id) != (course['target_kind'], course['target_id']):
        raise _missing()
    with store._lock, store._connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        return _ensure_route(connection, owner, target)
