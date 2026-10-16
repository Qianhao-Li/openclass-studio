"""One reentrant process/thread/file lock for a complete RIDOC transaction."""
from __future__ import annotations
import fcntl
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
_registry_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_held = threading.local()

@contextmanager
def ridoc_file_lock(path: Path) -> Iterator[None]:
    key = str(path.resolve())
    with _registry_guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        held = getattr(_held, 'paths', None)
        if held is None:
            held = _held.paths = set()
        if key in held:
            yield
            return
        lock_path = path.with_name(f'{path.name}.lock')
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open('a+b') as descriptor:
            fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX)
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)
