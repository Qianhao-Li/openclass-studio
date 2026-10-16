"""Bounded owner-scoped memoization of verified, snapshot-derived compilation.

Only process-owned JSON bytes are cached. Authoritative RIDOC reads, permissions,
revision checks and final package validation remain at the service boundary.
"""
from __future__ import annotations
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from threading import RLock
from time import monotonic
from typing import Any, Callable, TypedDict
import zlib
from pydantic import TypeAdapter
from openclass_core.models import BoardDocument
from openclass_core.services.ridoc_v3 import content_hash

class VerifiedOccurrence(TypedDict):
    node_id: str
    before: BoardDocument
    after: BoardDocument
    proof: dict[str, Any]
    input_trace: dict[str, Any]
    output_trace: dict[str, Any]

class CompiledItem(TypedDict):
    node_id: str
    before: BoardDocument
    after: BoardDocument
    proof: dict[str, Any]
    occurrence: VerifiedOccurrence

class Compilation(TypedDict):
    items: list[CompiledItem]
    entries: list[dict[str, Any]]

class SourceAction(TypedDict):
    node_id: str
    before: BoardDocument
    after: BoardDocument

class RangeResult(TypedDict):
    document: BoardDocument
    state_trace: dict[str, Any]
    entries: list[dict[str, Any]]
    occurrences: list[VerifiedOccurrence]
    blockers: list[Any]
    excluded_node_ids: list[str]
_ADAPTER = TypeAdapter(Compilation)
_RESULT_ADAPTER = TypeAdapter(RangeResult)
_REPLAY_ADAPTER = TypeAdapter(tuple[BoardDocument, dict[str, Any]])
_SOURCE_ADAPTER = TypeAdapter(tuple[str, BoardDocument, list[SourceAction]])
_SCOPE: ContextVar[tuple | None] = ContextVar('ridoc_compilation_scope', default=None)
_CACHE: OrderedDict[tuple, tuple[float, bytes]] = OrderedDict()
_LOCK = RLock()
_MAX_BYTES = 64 * 1024 * 1024
_MAX_RAW_BYTES = 64 * 1024 * 1024
_MAX_ENTRIES = 64
_TTL_SECONDS = 300
_COMPILER_VERSION = 1

def encode_range_result(value: dict[str, Any]) -> bytes | None:
    if set(value) != set(RangeResult.__annotations__) or not isinstance(value.get('document'), BoardDocument):
        return None
    return _RESULT_ADAPTER.dump_json(value)

def decode_range_result(value: bytes) -> dict[str, Any]:
    return _RESULT_ADAPTER.validate_json(value)

@contextmanager
def compilation_scope(store: Any, owner: str | None, package: str | None):
    scope = None
    if store is not None and owner and package:
        scope = (type(store).__name__, str(store.root), owner, store.deletion_scope_id(package))
    token = _SCOPE.set(scope)
    try:
        yield
    finally:
        _SCOPE.reset(token)

def _cached_value(kind: str, key: tuple, build: Callable, adapter: TypeAdapter):
    scope = _SCOPE.get()
    if scope is None:
        return build()
    identity = (_COMPILER_VERSION, scope, kind, key)
    now = monotonic()
    with _LOCK:
        cached = _CACHE.pop(identity, None)
        if cached and cached[0] > now:
            _CACHE[identity] = cached
        else:
            cached = None
    if cached:
        return adapter.validate_json(zlib.decompress(cached[1]))
    value = build()
    raw = adapter.dump_json(value)
    if len(raw) <= _MAX_RAW_BYTES:
        packed = zlib.compress(raw, level=1)
        with _LOCK:
            _CACHE[identity] = (now + _TTL_SECONDS, packed)
            _CACHE.move_to_end(identity)
            for old_key in [item for item, (expiry, _) in _CACHE.items() if expiry <= now]:
                _CACHE.pop(old_key, None)
            while len(_CACHE) > _MAX_ENTRIES or sum((len(item[1]) for item in _CACHE.values())) > _MAX_BYTES:
                _CACHE.popitem(last=False)
    return value

def compiled_history(key: tuple, build: Callable[[], Compilation]) -> Compilation:
    return _cached_value('compilation', key, build, _ADAPTER)

def verified_source_actions(archive, lesson_id: str, branch_name: str, build: Callable):
    if _SCOPE.get() is None:
        return build()
    key = (lesson_id, branch_name, content_hash(archive.manifest), content_hash(archive.package), tuple(((name, content_hash(value)) for name, value in sorted(archive.nodes.items()))), content_hash(archive.documents), content_hash(archive.evidence), tuple(((name, content_hash(value)) for name, value in sorted(archive.assets.items()))), tuple(((name, content_hash(value)) for name, value in sorted(archive.document_states.items()))), tuple(((name, content_hash(value)) for name, value in sorted(archive.node_refs.items()))), content_hash(archive.range_proofs), content_hash(archive.range_traces))
    return _cached_value('source-actions', key, build, _SOURCE_ADAPTER)

def verified_replay(callback: Callable):
    """Reuse a successful full validation only for identical complete inputs."""

    @wraps(callback)
    def run(proof, source_before, source_after, current, current_trace, *, validate=True):
        if not validate or _SCOPE.get() is None:
            return callback(proof, source_before, source_after, current, current_trace, validate=validate)
        key = tuple((content_hash(value) for value in (proof, source_before, source_after, current, current_trace)))
        return _cached_value('verified-replay', key, lambda: callback(proof, source_before, source_after, current, current_trace, validate=True), _REPLAY_ADAPTER)
    return run
