"""Cleanup of legacy temporary external Sources; new reads require explicit import.

Only temporary cloud originals and runtime rows are removed here. Frozen RIDOC
evidence and explicitly imported Sources are independent and never touched.
"""
from __future__ import annotations
import threading
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_evidence_store import source_evidence_store
_maintenance_lock = threading.RLock()
_maintenance_cursor = ''

def verify_temporary_source_access(source, *, connection_store=None):
    if source.metadata.get('external_turn_temporary'):
        raise GoogleSourceError('Add this external resource to Sources before using it in a question.')

class ExternalTurnEvidenceService:
    """Compatibility name for cleanup only; cannot browse or materialize evidence."""

    def __init__(self, *, connection_store=None, resource_service=None, google_service=None):
        self._google = google_service

    @property
    def google(self):
        if self._google is None:
            from openclass_core.services.google_sources.service import get_google_source_service
            self._google = get_google_source_service()
        return self._google

    def cleanup_connection_cache(self, owner_user_id: str, connection_id: str) -> int:
        removed = 0
        store = self._google.store if self._google is not None else source_evidence_store
        for source in store.temporary_sources_for_connection(owner_user_id=owner_user_id, connection_id=connection_id):
            self._cleanup_source(source)
            removed += 1
        return removed

    def _cleanup_source(self, source):
        if not source.metadata.get('external_turn_temporary'):
            raise GoogleSourceError('Only legacy temporary source objects may be cleaned here.')
        refs = source.metadata.get('google_objects') or [source.metadata.get('google_original')]
        for ref in refs:
            if not isinstance(ref, dict):
                raise GoogleSourceError('The temporary source has no verifiable cloud object for cleanup.')
            self.google._verify_blob_binding(source, ref, original=ref == source.metadata.get('google_original'))
            self.google.storage.delete(ref)
        from openclass_core.services.google_sources.original_cache import delete_cached_original
        delete_cached_original(source)
        self.google.store.delete_source(owner_user_id=source.owner_user_id, package_id=source.package_id, source_id=source.id)

    def cleanup_revoked_external_caches(self, *, limit: int=32) -> dict[str, int]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Choose a maintenance cleanup limit from 1 to 100.')
        global _maintenance_cursor
        store = self._google.store if self._google is not None else source_evidence_store
        sources = sorted(store.temporary_sources(), key=lambda source: source.id)
        if not sources:
            return {'checked': 0, 'removed': 0}
        with _maintenance_lock:
            start = next((index for index, source in enumerate(sources) if source.id > _maintenance_cursor), 0)
            selected = [sources[(start + index) % len(sources)] for index in range(min(limit, len(sources)))]
            _maintenance_cursor = selected[-1].id
        removed = 0
        for source in selected:
            self._cleanup_source(source)
            removed += 1
        return {'checked': len(selected), 'removed': removed}

def cleanup_revoked_external_caches(*, limit: int=32) -> dict[str, int]:
    return ExternalTurnEvidenceService().cleanup_revoked_external_caches(limit=limit)
