from __future__ import annotations
import json
from openclass_core.models import SourceIngestionRecord, now_iso
from openclass_core.services.source_evidence_store import SourceEvidenceStore
from .config import GoogleSourceError, GoogleSourceConflictError
from .original_cache import normalize_cached_source

def _initialize(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS google_source_pending_versions (source_id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL, package_id TEXT NOT NULL, base_version TEXT NOT NULL, source_json TEXT NOT NULL)')
    conn.execute("CREATE TABLE IF NOT EXISTS google_source_cleanup (id TEXT PRIMARY KEY, source_json TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)")

def _enqueue(conn, source):
    source = normalize_cached_source(source)
    identity = source.id + ':' + str(source.metadata.get('source_version', ''))
    conn.execute('INSERT INTO google_source_cleanup(id,source_json,updated_at) VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET source_json=excluded.source_json,updated_at=excluded.updated_at', (identity, source.model_dump_json(), now_iso()))

class GoogleSourceState:
    """Published records stay readable while a replacement is prepared separately."""

    def __init__(self, store: SourceEvidenceStore):
        self.store = store

        def initialize():
            with store._lock, store._connect() as conn:
                _initialize(conn)
        store.coordinator.run_write(store.path, initialize)

    def _published(self, source, conn):
        row = conn.execute('SELECT * FROM source_ingestions WHERE id=? AND owner_user_id=? AND package_id=?', (source.id, source.owner_user_id, source.package_id)).fetchone()
        return self.store._source_from_row(row) if row else None

    @staticmethod
    def _pending(source, conn):
        row = conn.execute('SELECT * FROM google_source_pending_versions WHERE source_id=? AND owner_user_id=? AND package_id=?', (source.id, source.owner_user_id, source.package_id)).fetchone()
        return SourceIngestionRecord.model_validate_json(row['source_json']) if row else None

    def work_source(self, source):
        with self.store._lock, self.store._connect() as conn:
            if self._published(source, conn) is None:
                raise GoogleSourceError('The source task has been deleted.')
            return self._pending(source, conn) or self.current(source, connection=conn, active=False)

    def current(self, source: SourceIngestionRecord, *, active: bool=True, connection=None) -> SourceIngestionRecord:
        if connection is None:
            with self.store._lock, self.store._connect() as conn:
                return self.current(source, active=active, connection=conn)
        published = self._published(source, connection)
        if published is None:
            raise GoogleSourceError('The source task has been deleted or replaced by a newer version.')
        version = source.metadata.get('source_version')
        if published.metadata.get('source_version') == version:
            current = published
        else:
            current = self._pending(source, connection)
            pending_row = connection.execute('SELECT base_version FROM google_source_pending_versions WHERE source_id=?', (source.id,)).fetchone()
            if pending_row is None or pending_row['base_version'] != str(published.metadata.get('source_version', '')) or current is None or (current.metadata.get('source_version') != version):
                raise GoogleSourceError('The source task has been deleted or replaced by a newer version.')
        if active and current.status == 'cancelled':
            raise GoogleSourceError('The source task has been cancelled.')
        return current

    def stage(self, source, previous=None, *, reject_pending=False):
        source = normalize_cached_source(source)

        def write():
            with self.store._lock, self.store._connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                published = self._published(source, conn)
                tombstone = conn.execute('SELECT 1 FROM source_ingestion_tombstones WHERE id=?', (source.id,)).fetchone()
                if tombstone or (previous is not None and (published is None or published.metadata.get('source_version') != previous.metadata.get('source_version'))):
                    _enqueue(conn, source)
                    return False
                if previous is None:
                    if published is not None:
                        raise GoogleSourceError('This source version already exists and cannot be overwritten.')
                    self.store.save_source(source, connection=conn)
                else:
                    pending = self._pending(source, conn)
                    if pending is not None and reject_pending:
                        _enqueue(conn, source)
                        return False
                    if pending is not None:
                        _enqueue(conn, pending)
                    conn.execute('INSERT INTO google_source_pending_versions VALUES(?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET base_version=excluded.base_version, source_json=excluded.source_json', (source.id, source.owner_user_id, source.package_id, str(previous.metadata.get('source_version', '')), source.model_dump_json()))
                    metadata = {**published.metadata, 'pending_source_version': source.metadata['source_version'], 'update_status': source.status, 'update_error': ''}
                    self.store.save_source(published.model_copy(update={'metadata': metadata}), connection=conn)
                return True
        if not self.store.coordinator.run_write(self.store.path, write):
            raise GoogleSourceConflictError('The source was deleted or replaced by a newer version during upload.')
        return source

    def update(self, source: SourceIngestionRecord, *, metadata=None, active: bool=True, **fields) -> SourceIngestionRecord:

        def write():
            with self.store._lock, self.store._connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                current = self.current(source, connection=conn, active=active)
                patch = metadata(dict(current.metadata)) if callable(metadata) else metadata
                updated = current.model_copy(update={**fields, 'updated_at': now_iso(), 'metadata': {**current.metadata, **(patch or {})}})
                updated = normalize_cached_source(updated)
                published = self._published(source, conn)
                if published.metadata.get('source_version') == source.metadata.get('source_version'):
                    return self.store.save_source(updated, connection=conn)
                conn.execute('UPDATE google_source_pending_versions SET source_json=? WHERE source_id=?', (updated.model_dump_json(), source.id))
                self.store.save_source(published.model_copy(update={'metadata': {**published.metadata, 'update_status': updated.status, 'update_error': updated.error}}), connection=conn)
                return updated
        return self.store.coordinator.run_write(self.store.path, write)

    def publish(self, source, *, structure_store, structure, chapters, metadata, expected_catalog_version=None):
        direct = metadata.get('catalog_display_mode') == 'direct' or structure.catalog_schema_version in {'pi_catalog_refinement_v1', 'anythingllm_source_catalog_v2', 'anythingllm_direct_catalog_v1'}
        if not direct:
            from .catalog_contract import require_complete_catalog
            require_complete_catalog(source, structure, chapters, metadata)
        with structure_store._lock, structure_store._connect():
            pass

        def write():
            with self.store._lock, self.store._connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                current = self.current(source, connection=conn)
                previous = self._published(source, conn)
                prior_row = conn.execute('SELECT * FROM source_structures WHERE owner_user_id=? AND package_id=? AND source_ingestion_id=?', (source.owner_user_id, source.package_id, source.id)).fetchone()
                prior = structure_store._structure_from_row(prior_row) if prior_row is not None else None
                if expected_catalog_version is not None and (prior.catalog_version if prior else 0) != expected_catalog_version:
                    raise GoogleSourceError('The catalog changed while Pi was refining it. The newer directory is preserved.')
                published_structure, published_chapters, published_metadata = (structure, chapters, metadata)
                if prior is not None and previous.metadata.get('source_version') == source.metadata.get('source_version') and (previous.metadata.get('content_hash') == source.metadata.get('content_hash')) and (metadata.get('catalog_display_mode') != 'direct'):
                    from .catalog_merge import merge_catalog
                    old_rows = conn.execute('SELECT * FROM source_chapters WHERE owner_user_id=? AND package_id=? AND source_ingestion_id=? ORDER BY order_index', (source.owner_user_id, source.package_id, source.id)).fetchall()
                    prior_chapters = [structure_store._chapter_from_row(row) for row in old_rows]
                    from .catalog_contract import catalog_publication_issue
                    if catalog_publication_issue(previous, prior, prior_chapters) is None and (not structure.metadata.get('native_catalog_complete')):
                        published_structure, published_chapters, published_metadata = merge_catalog(source, prior, prior_chapters, structure, chapters, metadata)
                if metadata.get('catalog_display_mode') == 'direct' and prior is not None:
                    version = max(structure.catalog_version, prior.catalog_version + 1)
                    published_structure = structure.model_copy(update={'catalog_version': version})
                    published_chapters = [chapter.model_copy(update={'catalog_version': version}) for chapter in chapters]
                    published_metadata = {**metadata, 'catalog_version': version}
                if not direct:
                    require_complete_catalog(current, published_structure, published_chapters, published_metadata)
                ready = current.model_copy(update={'status': 'ready', 'error': '', 'metadata': {key: value for key, value in {**current.metadata, **published_metadata}.items() if key not in {'pending_source_version', 'update_status', 'update_error'}}})
                ready = normalize_cached_source(ready)
                structure_store.save_structure_bundle(structure=published_structure, chapters=published_chapters, chunks=[], connection=conn)
                self.store.save_source(ready, connection=conn)
                conn.execute('DELETE FROM google_source_pending_versions WHERE source_id=?', (source.id,))
                if previous.metadata.get('source_backend') in {'google', 'local'} and previous.metadata.get('source_version') != ready.metadata.get('source_version'):
                    _enqueue(conn, previous)
                return ready
        return self.store.coordinator.run_write(self.store.path, write)

    def queue_cleanup(self, source: SourceIngestionRecord) -> None:

        def write():
            with self.store._lock, self.store._connect() as conn:
                _enqueue(conn, source)
                pending = self._pending(source, conn)
                if pending is not None:
                    _enqueue(conn, pending)
                    conn.execute('DELETE FROM google_source_pending_versions WHERE source_id=?', (source.id,))
        self.store.coordinator.run_write(self.store.path, write)

    def discard_pending(self, source: SourceIngestionRecord) -> None:
        """Undo a revoked external update without deleting the published copy."""

        def write():
            with self.store._lock, self.store._connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                pending = self._pending(source, conn)
                if pending is None or pending.metadata.get('source_version') != source.metadata.get('source_version'):
                    return
                _enqueue(conn, pending)
                conn.execute('DELETE FROM google_source_pending_versions WHERE source_id=?', (source.id,))
                published = self._published(source, conn)
                if published is not None:
                    metadata = {key: value for key, value in published.metadata.items() if key not in {'pending_source_version', 'update_status', 'update_error'}}
                    self.store.save_source(published.model_copy(update={'metadata': metadata}), connection=conn)
        self.store.coordinator.run_write(self.store.path, write)

    def cleanup_is_referenced(self, source):
        with self.store._lock, self.store._connect() as conn:
            candidates = [self._published(source, conn), self._pending(source, conn)]
            return any((item is not None and item.metadata.get('source_version') == source.metadata.get('source_version') for item in candidates))

    def cleanups(self) -> list[tuple[str, SourceIngestionRecord]]:
        with self.store._lock, self.store._connect() as conn:
            return [(row[0], SourceIngestionRecord.model_validate_json(row[1])) for row in conn.execute('SELECT id,source_json FROM google_source_cleanup ORDER BY updated_at LIMIT 100')]

    def finish_cleanup(self, identity: str, error: str='') -> None:

        def write():
            with self.store._lock, self.store._connect() as conn:
                if error:
                    conn.execute('UPDATE google_source_cleanup SET error=?,updated_at=? WHERE id=?', (error, now_iso(), identity))
                else:
                    conn.execute('DELETE FROM google_source_cleanup WHERE id=?', (identity,))
        self.store.coordinator.run_write(self.store.path, write)

def enqueue_account_cleanup(conn, user_id: str) -> int:
    """Call inside the existing account-delete transaction before source rows vanish."""
    exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='source_ingestions'").fetchone()
    if not exists:
        return 0
    _initialize(conn)
    count = 0
    cursor = conn.execute('SELECT * FROM source_ingestions WHERE owner_user_id=?', (user_id,))
    columns = [item[0] for item in cursor.description]
    for raw in cursor.fetchall():
        row = dict(zip(columns, raw))
        metadata = json.loads(row.pop('metadata_json') or '{}')
        if metadata.get('source_backend') not in {'google', 'local'}:
            continue
        _enqueue(conn, SourceIngestionRecord.model_validate({**row, 'metadata': metadata}))
        count += 1
    for row in conn.execute('SELECT source_json FROM google_source_pending_versions WHERE owner_user_id=?', (user_id,)).fetchall():
        _enqueue(conn, SourceIngestionRecord.model_validate_json(row[0]))
        count += 1
    conn.execute('DELETE FROM google_source_pending_versions WHERE owner_user_id=?', (user_id,))
    return count
