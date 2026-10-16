"""Account Sources keep their storage identity independently of course runtimes."""
from __future__ import annotations
from functools import lru_cache
from pathlib import Path
from openclass_core.models import SourceIngestionRecord
from openclass_core.services import workspace_state

def source_library_path() -> Path:
    return workspace_state.STORE.path

def source_accessible(source: SourceIngestionRecord, owner_user_id: str, package_id: str) -> bool:
    return source.owner_user_id == owner_user_id and (not source.metadata.get('external_turn_temporary') or source.package_id == package_id)

@lru_cache(maxsize=128)
def _scope_store(path: Path):
    from openclass_core.services.source_evidence_store import SourceEvidenceStore
    return SourceEvidenceStore(path)

def source_storage_package(path: Path, owner_user_id: str, package_id: str, source_id: str) -> str:
    source = _scope_store(path).get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
    return source.package_id if source is not None else package_id
