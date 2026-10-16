"""Explicit course exchange. A failed transfer never overwrites local content."""
from __future__ import annotations
from dataclasses import fields
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from openclass_core.services.ridoc_v3 import RidocV3ConflictError, read_ridoc_v3
from openclass_core.services.ridoc_lock import ridoc_file_lock
from openclass_core.services.studio_platform_client import local_platform_client
from openclass_core.services.workspace_state import get_ridoc_workspace_store

class PushCourse(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation_id: str = Field(pattern='^[A-Za-z0-9_-]{16,128}$')
    expected_ridoc_revision: str = Field(pattern='^[0-9a-f]{64}$')

class PullCourse(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation_id: str = Field(pattern='^[A-Za-z0-9_-]{16,128}$')
    revision: str | None = Field(default=None, pattern='^[0-9a-f]{64}$')
    package_id: str | None = Field(default=None, max_length=128)
    expected_ridoc_revision: str | None = Field(default=None, pattern='^[0-9a-f]{64}$')

def push(owner, package_id, request: PushCourse):
    platform = local_platform_client(owner)
    store = get_ridoc_workspace_store()
    path = store.package_path(owner, package_id)
    with ridoc_file_lock(path):
        archive = store.load_archive(owner, package_id=package_id)
        if archive.revision.value != request.expected_ridoc_revision:
            raise RidocV3ConflictError('Course changed; save and review before Push.')
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
    baseline = platform.get('sync:' + package_id) or {}
    if baseline and baseline.get('origin') != platform.origin:
        raise HTTPException(409, 'This course is linked to another platform origin.')
    fork = bool(baseline and baseline.get('can_write') is False)
    metadata = {'operation_id': request.operation_id, 'file_sha256': digest, 'ridoc_revision': request.expected_ridoc_revision, 'repository_id': None if fork else baseline.get('repository_id'), 'expected_remote_revision': None if fork else baseline.get('ridoc_revision'), 'source_repository_id': baseline.get('repository_id') if fork else None, 'source_revision': baseline.get('ridoc_revision') if fork else None}
    identity = {'origin': platform.origin, 'account_id': platform.status().get('account_id')}
    attempt = platform.get('push:' + request.operation_id)
    if attempt:
        if any((attempt.get(key) != value for key, value in identity.items())) or attempt['metadata']['file_sha256'] != digest or attempt['metadata']['ridoc_revision'] != request.expected_ridoc_revision:
            raise HTTPException(409, 'This Push identity belongs to different course contents or a different platform account.')
        metadata = attempt['metadata']
    else:
        active = platform.get('push-active:' + package_id)
        if active and active['operation_id'] != request.operation_id:
            raise HTTPException(409, 'Retry the earlier pending Push before uploading another revision. Your local contents are saved.')
        platform.put('push:' + request.operation_id, {**identity, 'metadata': metadata})
        platform.put('push-active:' + package_id, {'operation_id': request.operation_id})
    receipt = platform.request('POST', 'courses/push', files={'file': ('course.ridoc', raw, 'application/octet-stream')}, data={'metadata': json.dumps(metadata)}).json()
    if receipt.get('operation_id') != request.operation_id or receipt.get('uploaded_sha256') != digest or receipt.get('ridoc_revision') != archive.revision.value:
        raise HTTPException(502, 'The platform returned an invalid Push receipt.')
    platform.put('sync:' + package_id, {**receipt, 'origin': platform.origin, 'local_revision': archive.revision.value, 'can_write': True})
    platform.put('push-active:' + package_id, None)
    return receipt

def pull(owner, identity, request: PullCourse):
    platform = local_platform_client(owner)
    store = get_ridoc_workspace_store()
    key = 'pull:' + request.operation_id
    signature = {'origin': platform.origin, 'account_id': platform.status().get('account_id'), 'repository_id': identity, 'request': request.model_dump()}
    fingerprint = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    pending = platform.get(key)
    if pending and pending['fingerprint'] != fingerprint:
        raise HTTPException(409, 'This Pull identity belongs to a different request or account.')
    if pending and pending.get('phase') == 'committed':
        return store.load_package(owner, package_id=pending['package_id'])
    if pending:
        path = store.package_path(owner, pending['package_id'])
        if path.exists() and read_ridoc_v3(path).revision.value == pending['incoming_revision']:
            platform.claim(pending['slot'], {'fingerprint': request.operation_id})
            store.rebuild_owner_index(owner)
            return _finish_pull(platform, store, owner, key, pending)
    revision = pending['incoming_revision'] if pending else request.revision
    response = platform.request('GET', 'courses/' + identity + '/download', params={'revision': revision} if revision else {})
    raw = response.content
    if not raw or len(raw) > 256 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != response.headers.get('X-File-SHA256'):
        raise HTTPException(422, 'The downloaded RIDOC file hash is invalid.')
    with tempfile.TemporaryDirectory(prefix='studio-pull-') as directory:
        path = Path(directory) / 'course.ridoc'
        path.write_bytes(raw)
        incoming = read_ridoc_v3(path)
    if incoming.revision.value != response.headers.get('X-RIDOC-Revision'):
        raise HTTPException(422, 'The downloaded RIDOC revision is invalid.')
    package_id = incoming.package.id
    if pending and pending['file_sha256'] != hashlib.sha256(raw).hexdigest():
        raise HTTPException(422, 'The frozen Pull snapshot changed between retries.')
    path = store.package_path(owner, package_id)
    if not request.package_id and path.exists():
        raise HTTPException(409, 'This course already exists locally. Compare it before importing another copy.')
    if request.package_id:
        if request.package_id != package_id or not request.expected_ridoc_revision:
            raise HTTPException(422, 'Pull requires the same package identity and an expected local revision.')
        baseline = platform.get('sync:' + package_id)
        if not baseline or baseline['repository_id'] != identity or baseline['origin'] != platform.origin or (baseline['local_revision'] != request.expected_ridoc_revision):
            raise HTTPException(409, 'Local changes require comparison before Pull. Export them or keep a branch first.')
    slot = 'pull-base:' + package_id + ':' + (request.expected_ridoc_revision or 'new')
    pending = platform.claim(key, {'fingerprint': fingerprint, 'phase': 'applying', 'slot': slot, 'package_id': package_id, 'expected_local_revision': request.expected_ridoc_revision, 'repository_id': identity, 'origin': platform.origin, 'incoming_revision': incoming.revision.value, 'file_sha256': hashlib.sha256(raw).hexdigest(), 'can_write': response.headers.get('X-Repository-Can-Write') == 'true'})
    platform.claim(slot, {'fingerprint': request.operation_id})
    if request.package_id:

        def replace(archive):
            for item in fields(incoming):
                if item.name != 'revision':
                    setattr(archive, item.name, deepcopy(getattr(incoming, item.name)))
        record = store.mutate(owner, package_id, request.expected_ridoc_revision, replace)
    else:
        record = store.import_archive(owner, incoming)
    return _finish_pull(platform, store, owner, key, pending)

def _finish_pull(platform, store, owner, key, pending):
    package_id = pending['package_id']
    platform.put('sync:' + package_id, {'repository_id': pending['repository_id'], 'ridoc_revision': pending['incoming_revision'], 'local_revision': pending['incoming_revision'], 'origin': pending['origin'], 'file_sha256': pending['file_sha256'], 'can_write': pending['can_write']})
    platform.put(key, {**pending, 'phase': 'committed'})
    if pending.get('expected_local_revision') == pending['incoming_revision']:
        platform.put(pending['slot'], None)
    store.set_active_package_id(owner, package_id)
    return store.load_package(owner, package_id=package_id)
