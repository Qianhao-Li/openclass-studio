"""Native course deletion with revision checks and recoverable batch rollback.

Backups and the commit marker stay outside RIDOC learning history. A crash
before the audit marker restores the whole batch when the store reopens.
"""
from __future__ import annotations
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any
import uuid
from openclass_core.services.ridoc_lock import ridoc_file_lock
from openclass_core.services.ridoc_v3 import RidocV3ConflictError, RidocV3Error, read_ridoc_v3
from openclass_core.services.ridoc_v34_write_guards import history_write_lock
from openclass_core.services.recently_deleted import finish_ridoc_entries, prepare_ridoc_entry, recycle_lock
_COMPLETED = 'workspace_batch_delete_completed'

def _journal_root(store: Any) -> Path:
    return store.root.parent / 'ridoc-batch-deletions' / hashlib.sha256(str(store.root.resolve()).encode()).hexdigest()

def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def _copy_atomic(source: Path, target: Path) -> None:
    descriptor, name = tempfile.mkstemp(dir=target.parent, prefix='.batch-delete-')
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as output, source.open('rb') as incoming:
            shutil.copyfileobj(incoming, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        _sync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)

def _completed(store: Any, owner: str, batch_id: str) -> bool:
    with store._connect() as connection:
        return connection.execute('SELECT 1 FROM ridoc_runtime_audit WHERE owner_user_id=? AND kind=? AND payload_json=?', (owner, _COMPLETED, json.dumps({'batch_id': batch_id}))).fetchone() is not None

def _restore(store: Any, directory: Path, manifest: dict) -> None:
    from openclass_core.services.ridoc_workspace_store import RidocPackageIndexView
    owner = manifest['owner']
    archives = []
    for item in manifest['packages']:
        backup = directory / item['backup']
        if backup.resolve().parent != directory.resolve():
            raise RidocV3Error('Invalid batch deletion recovery path')
        archive = read_ridoc_v3(backup)
        if not archive.revision or archive.revision.value != item['index']['revision'] or archive.package.id != item['index']['id']:
            raise RidocV3Error('Batch deletion recovery archive mismatch')
        archives.append((item, backup, archive))
    for item, backup, archive in archives:
        path = store.package_path(owner, archive.package.id)
        _copy_atomic(backup, path)
        store._upsert_index(owner, path, archive, previous=RidocPackageIndexView.model_validate(item['index']))
    if 'active_package_id' in manifest:
        store.set_active_package_id(owner, manifest['active_package_id'])

def recover_ridoc_batch_deletions(store: Any) -> None:
    root = _journal_root(store)
    if not root.exists():
        return
    with store._lock, recycle_lock(store):
        for directory in sorted(root.glob('*/*')):
            manifest_path = directory / 'manifest.json'
            if not manifest_path.exists():
                shutil.rmtree(directory)
                continue
            manifest = json.loads(manifest_path.read_text())
            if hashlib.sha256(manifest['owner'].encode()).hexdigest() != directory.parent.name:
                raise RidocV3Error('Batch deletion recovery owner mismatch')
            with ExitStack() as locks:
                for item in sorted(manifest['packages'], key=lambda value: value['index']['id']):
                    locks.enter_context(ridoc_file_lock(store.package_path(manifest['owner'], item['index']['id'])))
                committed = _completed(store, manifest['owner'], directory.name)
                if not committed:
                    _restore(store, directory, manifest)
                finish_ridoc_entries(store, manifest['owner'], manifest.get('trash_ids', []), committed=committed)
            shutil.rmtree(directory)

def delete_ridoc_lessons_batch(store: Any, owner: str, lesson_ids: list[str], expected_revisions: dict[str, str], *, package_ids: list[str] | None=None) -> None:
    selected = list(dict.fromkeys((value.strip() for value in lesson_ids if value.strip())))
    whole_packages = set(package_ids or [])
    if not selected and (not whole_packages):
        raise RidocV3Error('At least one lesson is required')
    with store._lock, recycle_lock(store), ExitStack() as locks:
        groups: dict[str, set[str]] = {identity: set() for identity in whole_packages}
        for lesson_id in selected:
            package_id = store.package_id_for_lesson(owner, lesson_id)
            if package_id is None:
                raise RidocV3Error('Selected lesson is unavailable')
            groups.setdefault(package_id, set()).add(lesson_id)
        indexes = {}
        archives = {}
        for package_id in sorted(groups):
            locks.enter_context(history_write_lock(store, owner, package_id))
            archive = store.load_archive(owner, package_id=package_id)
            actual = archive.revision.value if archive.revision else ''
            if not expected_revisions.get(package_id) or expected_revisions[package_id] != actual:
                raise RidocV3ConflictError('A selected course changed. Refresh the course list and select again before deleting')
            if not groups[package_id].issubset(archive.package.lessons):
                raise RidocV3Error('Selected lesson is unavailable')
            indexes[package_id] = store._existing_index(owner, package_id)
            archives[package_id] = archive
            if package_id in whole_packages:
                groups[package_id] = set(archive.package.lessons)
        root = _journal_root(store)
        owner_root = root / hashlib.sha256(owner.encode()).hexdigest()
        owner_root.mkdir(parents=True, exist_ok=True)
        _sync_directory(store.root.parent)
        _sync_directory(root.parent)
        _sync_directory(root)
        directory = owner_root / uuid.uuid4().hex
        directory.mkdir()
        _sync_directory(owner_root)
        manifest: dict = {'owner': owner, 'packages': [], 'trash_ids': [], 'active_package_id': store.active_package_id(owner)}
        try:
            for position, package_id in enumerate(sorted(groups)):
                name = f'{position}.backup'
                _copy_atomic(store.package_path(owner, package_id), directory / name)
                manifest['packages'].append({'backup': name, 'index': indexes[package_id].model_dump(mode='json')})
                selections = [list(groups[package_id])] if package_id in whole_packages else [[identity] for identity in archives[package_id].package.lesson_order if identity in groups[package_id]]
                for selection in selections:
                    manifest['trash_ids'].append(prepare_ridoc_entry(store, owner, archives[package_id], indexes[package_id], selection, kind='package' if package_id in whole_packages else 'lesson'))
            with (directory / 'manifest.tmp').open('w') as output:
                json.dump(manifest, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(directory / 'manifest.tmp', directory / 'manifest.json')
            _sync_directory(directory)
        except Exception:
            finish_ridoc_entries(store, owner, manifest['trash_ids'], committed=False)
            shutil.rmtree(directory)
            raise
        finished = False
        try:
            for package_id in sorted(groups):
                if package_id in whole_packages:
                    store._delete_package_to_journal(owner_user_id=owner, package_id=package_id, expected_revision=expected_revisions[package_id], tombstone=directory / f'{uuid.uuid4().hex}.deleted')
                    continue
                removed = groups[package_id]

                def delete(archive, removed=removed):
                    archive.package.lesson_order = [value for value in archive.package.lesson_order if value not in removed]
                    archive.package.lessons = {key: value for key, value in archive.package.lessons.items() if key not in removed}
                    archive.nodes = {key: value for key, value in archive.nodes.items() if value.lesson_id not in removed}
                    store._prune_documents(archive)
                store.mutate(owner, package_id, expected_revisions[package_id], delete)
            store.audit(owner_user_id=owner, kind=_COMPLETED, payload={'batch_id': directory.name})
            finish_ridoc_entries(store, owner, manifest['trash_ids'], committed=True)
            finished = True
        except Exception:
            if _completed(store, owner, directory.name):
                finish_ridoc_entries(store, owner, manifest['trash_ids'], committed=True)
                finished = True
                return
            _restore(store, directory, manifest)
            finish_ridoc_entries(store, owner, manifest['trash_ids'], committed=False)
            finished = True
            raise
        finally:
            if finished:
                shutil.rmtree(directory, ignore_errors=True)
