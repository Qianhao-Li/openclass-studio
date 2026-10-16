"""Bounded, request-local reuse of pure snapshot-derived range replay results."""
from __future__ import annotations
from collections import OrderedDict
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
from typing import Any, Callable
from openclass_core.services.ridoc_range_compilation_cache import compilation_scope, decode_range_result, encode_range_result
_RESULTS: ContextVar[OrderedDict[tuple, dict[str, Any] | bytes] | None] = ContextVar('ridoc_range_request_results', default=None)
_MAX_RESULTS = 4

def range_history_request(callback: Callable):

    @wraps(callback)
    def run(*args, **kwargs):
        if _RESULTS.get() is not None:
            return callback(*args, **kwargs)
        token = _RESULTS.set(OrderedDict())
        try:
            with compilation_scope(args[0] if args else kwargs.get('store'), kwargs.get('owner_user_id'), kwargs.get('package_id')):
                return callback(*args, **kwargs)
        finally:
            _RESULTS.reset(token)
    return run

def replay_result(key: tuple, build: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    cache = _RESULTS.get()
    if cache is None:
        return build()
    if key in cache:
        cache.move_to_end(key)
        value = cache[key]
        return decode_range_result(value) if isinstance(value, bytes) else deepcopy(value)
    result = build()
    cache[key] = encode_range_result(result) or deepcopy(result)
    while len(cache) > _MAX_RESULTS:
        cache.popitem(last=False)
    return result
