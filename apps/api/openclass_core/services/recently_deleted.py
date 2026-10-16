"""Account-owned recovery archives outside the active RIDOC scan and history.

Deletion journals make recovery entries visible only after the complete batch
commits. Restore installs a validated old or new RIDOC with an explicit revision
check; its durable receipt repairs the index after an interrupted replacement.
"""
from __future__ import annotations
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Literal
import uuid
from fastapi import HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from openclass_core.services.ridoc_lock import ridoc_file_lock
from openclass_core.services.ridoc_v3 import RidocV3ConflictError, RidocV3Error, read_ridoc_v3, write_ridoc_v3
RETENTION_DAYS = 30
_logger = logging.getLogger(__name__)

class DeletedCourse(BaseModel):
    id: str
    kind: Literal['lesson', 'package']
    title: str
    lesson_count: int
    deleted_at: datetime
    expires_at: datetime
    revision: str
    target_revision: str

class RecentlyDeletedView(BaseModel):
    retention_days: int = RETENTION_DAYS
    items: list[DeletedCourse] = Field(default_factory=list)

class RestoreDeletedCourseRequest(BaseModel):
    expected_revision: str = Field(min_length=64, max_length=64)
    expected_target_revision: str = Field(min_length=1, max_length=80)

class RestoredCourse(BaseModel):
    package_id: str
    lesson_id: str | None

class _Entry(BaseModel):
    id: str
    owner: str
    kind: Literal['lesson', 'package']
    package_id: str
    title: str
    lesson_ids: list[str]
    original_order: list[str]
    original_index: dict[str, Any]
    deleted_at: datetime
    expires_at: datetime
    revision: str
    checksum: str
    storage_kind: Literal['ridoc', 'legacy'] = 'ridoc'
    state: Literal['prepared', 'ready', 'restoring', 'restored'] = 'prepared'
    restore_before: str | None = None
    restore_after: str | None = None
    restore_index: dict[str, Any] | None = None

def recycle_root(store) -> Path:
    scope = hashlib.sha256(str(store.root.resolve()).encode()).hexdigest()
    return store.root.parent / 'recently-deleted' / scope

def _owner_root(store, owner: str) -> Path:
    return recycle_root(store) / hashlib.sha256(owner.encode()).hexdigest()

def recycle_lock(store):
    return ridoc_file_lock(recycle_root(store) / 'transactions')

def _directory(store, owner: str, identity: str) -> Path:
    if not re.fullmatch('[a-f0-9]{32}', identity):
        raise HTTPException(status_code=404, detail='Deleted course not found')
    return _owner_root(store, owner) / identity

def _sync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def _save(directory: Path, entry: _Entry) -> None:
    descriptor, name = tempfile.mkstemp(dir=directory, prefix='.manifest-')
    try:
        with os.fdopen(descriptor, 'w') as output:
            output.write(entry.model_dump_json())
            output.flush()
            os.fsync(output.fileno())
        os.replace(Path(name), directory / 'manifest.json')
        _sync(directory)
    finally:
        Path(name).unlink(missing_ok=True)

def _read(directory: Path, owner: str) -> _Entry:
    if directory.is_symlink() or not (directory / 'manifest.json').is_file():
        raise HTTPException(status_code=404, detail='Deleted course not found')
    entry = _Entry.model_validate_json((directory / 'manifest.json').read_bytes())
    if entry.owner != owner or entry.id != directory.name or directory.parent.name != hashlib.sha256(owner.encode()).hexdigest():
        raise RidocV3Error('Deleted course owner or identity mismatch')
    return entry

def _archive(directory: Path, entry: _Entry):
    path = directory / 'course.ridoc'
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != entry.checksum:
        raise RidocV3Error('Deleted course checksum mismatch')
    archive = read_ridoc_v3(path)
    if archive.package.id != entry.package_id or not archive.revision or archive.revision.value != entry.revision or (set(archive.package.lessons) != set(entry.lesson_ids)):
        raise RidocV3Error('Deleted course archive mismatch')
    return archive

def prepare_ridoc_entry(store, owner, archive, index, lesson_ids, *, kind='lesson') -> str:
    selected = set(lesson_ids)
    saved = deepcopy(archive)
    saved.package.lesson_order = [identity for identity in archive.package.lesson_order if identity in selected]
    saved.package.lessons = {identity: graph for identity, graph in saved.package.lessons.items() if identity in selected}
    saved.nodes = {identity: node for identity, node in saved.nodes.items() if node.lesson_id in selected}
    store._prune_documents(saved)
    identity = uuid.uuid4().hex
    directory = _directory(store, owner, identity)
    directory.mkdir(parents=True)
    for parent in (directory.parent, directory.parent.parent, directory.parent.parent.parent):
        _sync(parent)
    try:
        saved.revision = write_ridoc_v3(saved, directory / 'course.ridoc')
        timestamp = datetime.now(timezone.utc)
        entry = _Entry(id=identity, owner=owner, kind=kind, package_id=archive.package.id, title=archive.package.title if kind == 'package' else saved.package.lessons[saved.package.lesson_order[0]].title, lesson_ids=list(saved.package.lesson_order), original_order=list(archive.package.lesson_order), original_index=index.model_dump(mode='json'), deleted_at=timestamp, expires_at=timestamp + timedelta(days=RETENTION_DAYS), revision=saved.revision.value, checksum=hashlib.sha256((directory / 'course.ridoc').read_bytes()).hexdigest())
        _save(directory, entry)
        return identity
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise

def finish_ridoc_entries(store, owner: str, identities: list[str], *, committed: bool) -> None:
    for identity in identities:
        directory = _directory(store, owner, identity)
        if not directory.exists():
            continue
        if not committed:
            shutil.rmtree(directory)
        else:
            entry = _read(directory, owner)
            if entry.state == 'prepared':
                _archive(directory, entry)
                entry.state = 'ready'
                _save(directory, entry)
        _sync(directory.parent)

def _recover_restore(store, directory: Path, entry: _Entry) -> bool:
    """A replace may have succeeded while the index or receipt write failed."""
    from openclass_core.services.ridoc_workspace_store import RidocPackageIndexView
    target = store.package_path(entry.owner, entry.package_id)
    with ridoc_file_lock(target):
        current = read_ridoc_v3(target) if target.exists() else None
        actual = current.revision.value if current and current.revision else 'missing'
        if actual == entry.restore_after:
            store._upsert_index(entry.owner, target, current, previous=RidocPackageIndexView.model_validate(entry.restore_index))
            entry.state = 'restored'
            _save(directory, entry)
            return True
        if actual != entry.restore_before:
            raise RidocV3ConflictError('Course changed during recovery; refresh before restoring')
        entry.state = 'ready'
        _save(directory, entry)
        return False

def _recover_entries(store) -> None:
    root = recycle_root(store)
    for directory in sorted(root.glob('*/*')):
        if not directory.is_dir() or directory.is_symlink():
            continue
        manifest = directory / 'manifest.json'
        if not manifest.exists():
            shutil.rmtree(directory)
            continue
        entry = _Entry.model_validate_json(manifest.read_bytes())
        _read(directory, entry.owner)
        if entry.state == 'prepared':
            target = store.package_path(entry.owner, entry.package_id)
            current = read_ridoc_v3(target) if target.exists() else None
            deleted = current is None or not any((identity in current.package.lessons for identity in entry.lesson_ids))
            finish_ridoc_entries(store, entry.owner, [entry.id], committed=deleted)
        elif entry.state == 'restored' or (entry.state == 'restoring' and _recover_restore(store, directory, entry)):
            shutil.rmtree(directory)
            _sync(directory.parent)

def list_recently_deleted(store, owner: str) -> RecentlyDeletedView:
    from openclass_core.services.ridoc_batch_delete import recover_ridoc_batch_deletions
    with store._lock, recycle_lock(store):
        recover_ridoc_batch_deletions(store)
        _recover_entries(store)
        cleanup_recently_deleted(store, owner=owner)
        items = []
        for directory in _owner_root(store, owner).glob('*'):
            if not directory.is_dir():
                continue
            entry = _read(directory, owner)
            if entry.state != 'ready':
                continue
            target = store.package_path(owner, entry.package_id)
            current = read_ridoc_v3(target) if target.exists() else None
            items.append(DeletedCourse(id=entry.id, kind=entry.kind, title=entry.title, lesson_count=len(entry.lesson_ids), deleted_at=entry.deleted_at, expires_at=entry.expires_at, revision=entry.revision, target_revision=current.revision.value if current and current.revision else 'missing'))
        return RecentlyDeletedView(items=sorted(items, key=lambda item: (item.deleted_at, item.id), reverse=True))

def restore_recently_deleted(store, owner: str, identity: str, request: RestoreDeletedCourseRequest) -> RestoredCourse:
    from openclass_core.services.ridoc_workspace_store import RidocPackageIndexView
    from openclass_core.services.ridoc_edit_session_store import assert_no_pending_history
    from openclass_core.services.ridoc_batch_delete import recover_ridoc_batch_deletions
    with store._lock, recycle_lock(store):
        recover_ridoc_batch_deletions(store)
        _recover_entries(store)
        directory = _directory(store, owner, identity)
        entry = _read(directory, owner)
        if entry.state != 'ready' or datetime.now(timezone.utc) >= entry.expires_at:
            raise HTTPException(status_code=410, detail='Deleted course has expired or was already restored')
        if request.expected_revision != entry.revision:
            raise RidocV3ConflictError('Deleted course changed; refresh before restoring')
        saved = _archive(directory, entry)
        target = store.package_path(owner, entry.package_id)
        with ridoc_file_lock(target):
            current = read_ridoc_v3(target) if target.exists() else None
            actual = current.revision.value if current and current.revision else 'missing'
            if request.expected_target_revision != actual:
                raise RidocV3ConflictError('Course changed; refresh Recently deleted before restoring')
            assert_no_pending_history(store, owner, entry.package_id)
            if current and set(entry.lesson_ids).intersection(current.package.lessons):
                raise RidocV3ConflictError('A restored lesson already exists; nothing was overwritten')
            for lesson_id in entry.lesson_ids:
                if store.package_id_for_lesson(owner, lesson_id) is not None:
                    raise RidocV3ConflictError('A restored lesson already exists; nothing was overwritten')
            previous = store._existing_index(owner, entry.package_id)
            if current:
                candidate = deepcopy(current)
                candidate.package.lessons.update(saved.package.lessons)
                for identity in saved.package.lesson_order:
                    position = entry.original_order.index(identity)
                    following = next((value for value in entry.original_order[position + 1:] if value in candidate.package.lesson_order), None)
                    offset = candidate.package.lesson_order.index(following) if following else len(candidate.package.lesson_order)
                    candidate.package.lesson_order.insert(offset, identity)
                for name in ('nodes', 'documents', 'assets', 'evidence', 'document_states', 'node_refs', 'range_proofs', 'range_traces'):
                    destination = getattr(candidate, name)
                    for key, value in getattr(saved, name).items():
                        if key in destination and destination[key] != value:
                            raise RidocV3Error('Restored course object conflicts with an existing object')
                        destination[key] = value
            else:
                candidate = saved
            index = previous or RidocPackageIndexView.model_validate(entry.original_index)
            index = index.model_copy(update={'open_lesson_ids': list(dict.fromkeys([*index.open_lesson_ids, *entry.lesson_ids])), 'workspace_tab_order': list(dict.fromkeys([*index.workspace_tab_order, *entry.lesson_ids]))})
            candidate.revision = write_ridoc_v3(candidate, directory / 'restored.ridoc')
            entry.restore_before, entry.restore_after = (actual, candidate.revision.value)
            entry.restore_index, entry.state = (index.model_dump(mode='json'), 'restoring')
            _save(directory, entry)
            try:
                candidate.revision = write_ridoc_v3(candidate, target, expected_revision=actual if current else None)
                store._upsert_index(owner, target, candidate, previous=index)
                entry.state = 'restored'
                _save(directory, entry)
            except Exception:
                _recover_restore(store, directory, entry)
                raise
            shutil.rmtree(directory)
            _sync(directory.parent)
            return RestoredCourse(package_id=entry.package_id, lesson_id=entry.lesson_ids[0] if entry.lesson_ids else None)

def cleanup_recently_deleted(store, *, owner: str | None=None, now: datetime | None=None) -> int:
    timestamp = now or datetime.now(timezone.utc)
    removed = 0
    with store._lock, recycle_lock(store):
        roots = [_owner_root(store, owner)] if owner else list(recycle_root(store).glob('*'))
        for root in roots:
            if not root.is_dir() or root.is_symlink():
                continue
            for directory in root.glob('*'):
                if not directory.is_dir() or directory.is_symlink() or (not (directory / 'manifest.json').exists()):
                    continue
                entry = _Entry.model_validate_json((directory / 'manifest.json').read_bytes())
                _read(directory, owner or entry.owner)
                if entry.state == 'ready' and timestamp >= entry.expires_at:
                    shutil.rmtree(directory)
                    _sync(root)
                    removed += 1
    return removed

async def maintain_recently_deleted() -> None:
    from openclass_core.services.workspace_state import get_ridoc_workspace_store, get_course_store
    from openclass_core.services.recently_deleted_legacy import legacy_scope
    while True:
        try:
            await run_in_threadpool(cleanup_recently_deleted, get_ridoc_workspace_store())
            await run_in_threadpool(cleanup_recently_deleted, legacy_scope(get_course_store()))
        except Exception:
            _logger.exception('Recently deleted cleanup failed; archives retained for retry')
        await asyncio.sleep(3600)
