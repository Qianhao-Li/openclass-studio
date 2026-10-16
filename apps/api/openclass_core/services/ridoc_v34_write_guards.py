"""Keep course writes and pending History decisions on the same file lock."""
from __future__ import annotations
from contextlib import contextmanager
from typing import Any, Iterator
from openclass_core.services.ridoc_lock import ridoc_file_lock

@contextmanager
def history_write_lock(store: Any, owner: str, package_id: str) -> Iterator[None]:
    from openclass_core.services.ridoc_edit_session_store import assert_no_pending_history
    with store._lock, ridoc_file_lock(store.package_path(owner, package_id)):
        archive = store.load_archive(owner, **{} if hasattr(store, 'repository_id') else {'package_id': package_id})
        assert_no_pending_history(store, owner, archive.package.id)
        yield
