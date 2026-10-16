"""Bounded private checkpoints of verified History drafts, never course facts."""
from __future__ import annotations
import hashlib
import json
import zlib
from collections import OrderedDict
from pathlib import Path
from threading import RLock
from time import monotonic
from types import MappingProxyType
from typing import Any, Callable, Mapping
from pydantic import TypeAdapter
from openclass_core.services.ridoc_edit_operations import PendingEditCheckpoint
from openclass_core.services.ridoc_v3 import RIDOC_V3_MAX_ARCHIVE_BYTES, DocumentState, NodeRef, RidocArchiveV3, RidocCoursePackageV3, RidocRevision, RidocV3Error, RidocV3Node, content_hash
_NODES = TypeAdapter(dict[str, RidocV3Node])
_STATES = TypeAdapter(dict[str, DocumentState])
_REFS = TypeAdapter(dict[str, NodeRef])
_CACHE: OrderedDict[tuple, tuple[float, bytes, Mapping[str, bytes]]] = OrderedDict()
_LOCK = RLock()
_CACHE_VERSION = 1
_MAX_ENTRIES = 8
_MAX_BYTES = 64 * 1024 * 1024
_MAX_RAW_BYTES = 128 * 1024 * 1024
_TTL_SECONDS = 600

def _snapshot_hash(path: Path) -> str:
    if not path.is_file() or path.stat().st_size > RIDOC_V3_MAX_ARCHIVE_BYTES:
        raise RidocV3Error("The History edit draft's base snapshot is unavailable or too large")
    try:
        with path.open('rb') as source:
            return hashlib.file_digest(source, 'sha256').hexdigest()
    except OSError as exc:
        raise RidocV3Error("The History edit draft's base snapshot is unavailable") from exc

def _archive_payload(archive: RidocArchiveV3, assets: dict[str, bytes]) -> dict[str, Any]:
    for identity, data in archive.assets.items():
        if identity not in assets:
            assets[identity] = bytes(data)
    return {'manifest': archive.manifest, 'package': archive.package.model_dump(mode='json'), 'nodes': {key: value.model_dump(mode='json') for key, value in archive.nodes.items()}, 'documents': archive.documents, 'evidence': archive.evidence, 'assets': list(archive.assets), 'revision': archive.revision.model_dump(mode='json') if archive.revision else None, 'document_states': {key: value.model_dump(mode='json') for key, value in archive.document_states.items()}, 'node_refs': {key: value.model_dump(mode='json') for key, value in archive.node_refs.items()}, 'range_proofs': archive.range_proofs, 'range_traces': archive.range_traces, 'range_pending': {key: sorted(value) for key, value in getattr(archive, '_range_pending', {}).items()}, 'range_touched': sorted(getattr(archive, '_range_touched', set()))}

def _archive(value: dict[str, Any], assets: Mapping[str, bytes]) -> RidocArchiveV3:
    archive = RidocArchiveV3(manifest=value['manifest'], package=RidocCoursePackageV3.model_validate(value['package']), nodes=_NODES.validate_python(value['nodes']), documents=value['documents'], evidence=value['evidence'], assets={key: assets[key] for key in value['assets']}, revision=RidocRevision.model_validate(value['revision']) if value['revision'] else None, document_states=_STATES.validate_python(value['document_states']), node_refs=_REFS.validate_python(value['node_refs']), range_proofs=value['range_proofs'], range_traces=value['range_traces'])
    archive._range_pending = {key: set(ids) for key, ids in value['range_pending'].items()}
    archive._range_touched = {tuple(item) for item in value['range_touched']}
    return archive

def _encode(result: tuple, checkpoint: PendingEditCheckpoint) -> tuple[bytes, Mapping[str, bytes]] | None:
    assets: dict[str, bytes] = {}
    base, (display, effective, removed, affected, changed) = result
    value = {'base': _archive_payload(base, assets), 'display': _archive_payload(display, assets), 'effective': _archive_payload(effective, assets), 'removed': removed, 'affected': affected, 'changed': changed, 'checkpoint': {'display': _archive_payload(checkpoint.display, assets), 'baseline_cursors': [[*key, value] for key, value in checkpoint.baseline_cursors.items()], 'identities': sorted(checkpoint.identities), 'operations': checkpoint.operations, 'session_id': checkpoint.session_id, 'created_at': checkpoint.created_at, 'default_lesson_id': checkpoint.default_lesson_id}}
    asset_bytes = sum((len(data) for data in assets.values()))
    if asset_bytes > min(_MAX_BYTES, _MAX_RAW_BYTES):
        return None
    raw = bytearray()
    for chunk in json.JSONEncoder(ensure_ascii=False, separators=(',', ':')).iterencode(value):
        encoded = chunk.encode('utf-8')
        if asset_bytes + len(raw) + len(encoded) > _MAX_RAW_BYTES:
            return None
        raw.extend(encoded)
    return (zlib.compress(raw, level=1), MappingProxyType(assets))

def _decode(packed: bytes, assets: Mapping[str, bytes], *, checkpoint_only: bool=False):
    value = json.loads(zlib.decompress(packed))
    if checkpoint_only:
        item = value['checkpoint']
        return PendingEditCheckpoint(_archive(item['display'], assets), {(lesson, branch): cursor for lesson, branch, cursor in item['baseline_cursors']}, set(item['identities']), item['operations'], item['session_id'], item['created_at'], item['default_lesson_id'])
    return (_archive(value['base'], assets), (_archive(value['display'], assets), _archive(value['effective'], assets), value['removed'], value['affected'], value['changed']))

def verified_draft(store: Any, record: dict[str, Any], path: Path, build: Callable[[PendingEditCheckpoint | None, Callable], tuple]) -> tuple:
    """Reuse only a complete verified result or its exact operation prefix.

    Callers still read the authoritative course, authorize, lock and check CAS.
    Every call hashes the base file's actual bytes, even on a warm cache hit.
    Cache eviction/restart only loses acceleration; it cannot change the draft.
    """
    digest = _snapshot_hash(path)
    operations = json.loads(record['operations_json'])
    prefix = tuple((content_hash(operation) for operation in operations))
    context = (_CACHE_VERSION, type(store).__module__, type(store).__name__, str(store.root), str(store.database_path), str(path), digest, *(record.get(key) for key in ('owner_id', 'scope_id', 'package_id', 'lesson_id', 'session_id', 'base_revision', 'base_repository_version', 'draft_schema_version', 'created_at')))
    identity = (context, record['version'], prefix)
    now = monotonic()
    with _LOCK:
        for old in [key for key, (expiry, _packed, _assets) in _CACHE.items() if expiry <= now]:
            _CACHE.pop(old, None)
        exact = _CACHE.get(identity)
        prior = None
        if exact:
            _CACHE.move_to_end(identity)
        else:
            candidates = [key for key in _CACHE if key[0] == context and key[1] < record['version'] and (len(key[2]) < len(prefix)) and (prefix[:len(key[2])] == key[2])]
            if candidates:
                selected = max(candidates, key=lambda key: (len(key[2]), key[1]))
                prior = _CACHE[selected]
    checkpoint = None
    try:
        if exact:
            return _decode(exact[1], exact[2])
        if prior:
            checkpoint = _decode(prior[1], prior[2], checkpoint_only=True)
    except (ValueError, TypeError, KeyError, zlib.error):
        with _LOCK:
            _CACHE.pop(identity, None)
        checkpoint = None
    checkpoints: list[PendingEditCheckpoint] = []
    result = build(checkpoint, checkpoints.append)
    if _snapshot_hash(path) != digest:
        raise RidocV3Error("The History edit draft's base snapshot changed while it was being read")
    encoded = _encode(result, checkpoints[-1]) if checkpoints else None
    if encoded is not None and len(encoded[0]) + sum((len(data) for data in encoded[1].values())) <= _MAX_BYTES:
        with _LOCK:
            _CACHE[identity] = (monotonic() + _TTL_SECONDS, *encoded)
            _CACHE.move_to_end(identity)
            while len(_CACHE) > _MAX_ENTRIES or sum((len(packed) + sum((len(data) for data in assets.values())) for _expiry, packed, assets in _CACHE.values())) > _MAX_BYTES:
                _CACHE.popitem(last=False)
    return result
