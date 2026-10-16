"""Explicit compatibility adapter for recoverable deletion in legacy storage.

These account-private recovery snapshots are never imported into native RIDOC
history. Legacy writes retain their existing SQLite transaction and revision.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import hashlib
import shutil
from types import SimpleNamespace
import uuid
from fastapi import HTTPException
from openclass_core.models import CoursePackage
from openclass_core.services.recently_deleted import RETENTION_DAYS, DeletedCourse, RestoredCourse, _Entry, _directory, _owner_root, _read, _save, _sync, cleanup_recently_deleted, recycle_lock
from openclass_core.services.ridoc_v3 import RidocV3ConflictError, RidocV3Error

def legacy_scope(store):
    digest = hashlib.sha256(str(store.path.resolve()).encode()).hexdigest()
    return SimpleNamespace(root=store.path.parent / f'legacy-recovery-{digest}', _lock=store._lock)

def _package(directory, entry):
    path = directory / 'legacy-course.json'
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != entry.checksum:
        raise RidocV3Error('Deleted legacy course checksum mismatch')
    package = CoursePackage.model_validate_json(path.read_bytes())
    if package.storage_kind != 'legacy' or package.id != entry.package_id or set((lesson.id for lesson in package.lessons)) != set(entry.lesson_ids):
        raise RidocV3Error('Deleted legacy course snapshot mismatch')
    return package

def _prepare(store, owner, package, selected, kind):
    import os
    import tempfile
    scope = legacy_scope(store)
    saved = package.model_copy(deep=True)
    saved.lessons = [lesson for lesson in saved.lessons if lesson.id in selected]
    saved.course_graph = [edge for edge in saved.course_graph if edge.source_lesson_id in selected or edge.target_lesson_id in selected]
    identity = uuid.uuid4().hex
    directory = _directory(scope, owner, identity)
    directory.mkdir(parents=True)
    for parent in (directory.parent, directory.parent.parent, directory.parent.parent.parent):
        _sync(parent)
    payload = saved.model_dump_json().encode()
    descriptor, name = tempfile.mkstemp(dir=directory, prefix='.legacy-')
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, directory / 'legacy-course.json')
        timestamp = datetime.now(timezone.utc)
        digest = hashlib.sha256(payload).hexdigest()
        entry = _Entry(id=identity, owner=owner, kind=kind, package_id=package.id, title=package.title if kind == 'package' else saved.lessons[0].title, lesson_ids=[lesson.id for lesson in saved.lessons], original_order=[lesson.id for lesson in package.lessons], original_index={}, deleted_at=timestamp, expires_at=timestamp + timedelta(days=RETENTION_DAYS), revision=digest, checksum=digest, storage_kind='legacy')
        _save(directory, entry)
        return (directory, entry)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    finally:
        from pathlib import Path
        Path(name).unlink(missing_ok=True)

def _recover(store, owner):
    workspace = store.load_for_user(owner)
    packages = {package.id: package for package in workspace.packages}
    for directory in _owner_root(legacy_scope(store), owner).glob('*'):
        if not directory.is_dir() or directory.is_symlink():
            continue
        if not (directory / 'manifest.json').exists():
            shutil.rmtree(directory)
            continue
        entry = _read(directory, owner)
        package = packages.get(entry.package_id)
        present = {lesson.id for lesson in package.lessons} if package else set()
        if entry.state == 'prepared':
            deleted = not package if entry.kind == 'package' else not present.intersection(entry.lesson_ids)
            if deleted:
                entry.state = 'ready'
                _save(directory, entry)
            else:
                shutil.rmtree(directory)
        elif entry.state == 'restoring':
            if set(entry.lesson_ids).issubset(present):
                saved = _package(directory, entry)
                actual = {lesson.id: lesson for lesson in package.lessons}
                if any((actual[lesson.id].board_document != lesson.board_document or actual[lesson.id].history_graph != lesson.history_graph for lesson in saved.lessons)):
                    raise RidocV3ConflictError('Course changed during recovery; nothing was overwritten')
                entry.state = 'restored'
                _save(directory, entry)
            elif not present.intersection(entry.lesson_ids):
                entry.state = 'ready'
                _save(directory, entry)
            else:
                raise RidocV3Error('Incomplete legacy course restore')
        if directory.exists() and entry.state == 'restored':
            shutil.rmtree(directory)

def with_legacy_recovery(store, owner, packages, lesson_ids, package_ids, operation):
    scope = legacy_scope(store)
    with store._lock, recycle_lock(scope):
        _recover(store, owner)
        if packages is None:
            packages = store.load_for_user(owner).packages
        entries = []
        committed = False
        try:
            for package in packages:
                selections = [set((lesson.id for lesson in package.lessons))] if package.id in package_ids else [{lesson.id} for lesson in package.lessons if lesson.id in lesson_ids]
                for selected in selections:
                    entries.append(_prepare(store, owner, package, selected, 'package' if package.id in package_ids else 'lesson'))
            result = operation()
            committed = True
            for directory, entry in entries:
                entry.state = 'ready'
                _save(directory, entry)
            return result
        except Exception:
            if not committed:
                for directory, _ in entries:
                    shutil.rmtree(directory, ignore_errors=True)
            raise

def list_legacy_deleted(store, owner):
    scope = legacy_scope(store)
    with store._lock, recycle_lock(scope):
        _recover(store, owner)
        cleanup_recently_deleted(scope, owner=owner)
        _, revision = store.load_for_user_with_revision(owner)
        items = []
        for directory in _owner_root(scope, owner).glob('*'):
            if not directory.is_dir():
                continue
            entry = _read(directory, owner)
            if entry.state == 'ready':
                items.append(DeletedCourse(id=entry.id, kind=entry.kind, title=entry.title, lesson_count=len(entry.lesson_ids), deleted_at=entry.deleted_at, expires_at=entry.expires_at, revision=entry.revision, target_revision=f'legacy:{revision}'))
        return items

def restore_legacy_deleted(store, owner, identity, request):
    from openclass_core.services.workspace_state import normalize_package_state
    scope = legacy_scope(store)
    with store._lock, recycle_lock(scope):
        _recover(store, owner)
        directory = _directory(scope, owner, identity)
        entry = _read(directory, owner)
        if entry.state != 'ready' or datetime.now(timezone.utc) >= entry.expires_at:
            raise HTTPException(status_code=410, detail='Deleted course has expired')
        workspace, revision = store.load_for_user_with_revision(owner)
        if request.expected_revision != entry.revision or request.expected_target_revision != f'legacy:{revision}':
            raise RidocV3ConflictError('Course changed; refresh Recently deleted before restoring')
        if any((lesson.id in entry.lesson_ids for package in workspace.packages for lesson in package.lessons)):
            raise RidocV3ConflictError('A restored lesson already exists; nothing was overwritten')
        saved = _package(directory, entry)
        target = next((package for package in workspace.packages if package.id == saved.id), None)
        if target is None:
            target = saved
            workspace.packages.append(target)
        else:
            target.lessons.extend(saved.lessons)
            known = {lesson.id: lesson for lesson in target.lessons}
            target.lessons = [known.pop(identity) for identity in entry.original_order if identity in known] + list(known.values())
            edges = {edge.id: edge for edge in target.course_graph}
            present = {lesson.id for lesson in target.lessons}
            edges.update({edge.id: edge for edge in saved.course_graph if edge.source_lesson_id in present and edge.target_lesson_id in present})
            target.course_graph = list(edges.values())
        target.open_lesson_ids = list(dict.fromkeys([*target.open_lesson_ids, *entry.lesson_ids]))
        target.workspace_tab_order = list(dict.fromkeys([*target.workspace_tab_order, *entry.lesson_ids]))
        normalize_package_state(target)
        if not workspace.active_package_id:
            workspace.active_package_id = target.id
        entry.state = 'restoring'
        _save(directory, entry)
        if not store.save_for_user_if_revision(owner, workspace, expected_revision=revision):
            entry.state = 'ready'
            _save(directory, entry)
            raise RidocV3ConflictError('Workspace changed; refresh before restoring')
        entry.state = 'restored'
        _save(directory, entry)
        shutil.rmtree(directory)
        _sync(directory.parent)
        return RestoredCourse(package_id=target.id, lesson_id=entry.lesson_ids[0] if entry.lesson_ids else None)
