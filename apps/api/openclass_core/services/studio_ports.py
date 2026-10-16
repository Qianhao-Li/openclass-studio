"""Host-owned services stay outside the portable course implementation."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Callable, Iterator, Mapping
from typing import Any
_defaults: dict[str, Callable[..., Any]] = {
    'recover_workspace_pull': lambda *_args, **_kwargs: None,
    'model_image_capability': lambda *_args, **_kwargs: None,
}
_scope: ContextVar[Mapping[str, Callable[..., Any]] | None] = ContextVar('studio_services', default=None)

def configure_studio_services(services: Mapping[str, Callable[..., Any]]) -> None:
    _defaults.update(services)

def host_service(name: str, *args, **kwargs):
    services = _scope.get()
    factory = (services if services is not None else _defaults).get(name)
    if factory is None:
        raise RuntimeError(f'The Studio host has not enabled {name}.')
    return factory(*args, **kwargs)

@contextmanager
def bind_studio_services(services: Mapping[str, Callable[..., Any]]) -> Iterator[None]:
    token = _scope.set(services)
    try:
        yield
    finally:
        _scope.reset(token)
