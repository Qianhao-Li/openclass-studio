from __future__ import annotations
import json
import logging
import os
import signal
import sqlite3
import subprocess
import threading
import time
from collections import deque
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, TypeVar
from openclass_core.models import SourceIngestionJob, now_iso
from openclass_core.services import workspace_state
from openclass_core.services.account_source_library import source_library_path
WriteResult = TypeVar('WriteResult')
SourceTaskKey = tuple[str, str, str]
logger = logging.getLogger(__name__)
_CURRENT_SOURCE_TASK = threading.local()
_ACTIVE_SOURCE_STATUSES = {'queued', 'fetching', 'parsing', 'indexing'}

class SourceTaskNotActiveError(RuntimeError):
    """The requested source run cannot acquire an active worker lease."""

@dataclass
class SourceTaskHandle:
    cancel_event: threading.Event
    key: SourceTaskKey
    run_id: str = ''
    content_hash: str = ''
    thread: threading.Thread | None = None
    process: subprocess.Popen[str] | None = None
    workspace: Path | None = None
    finished: threading.Event = field(default_factory=threading.Event)
    job_store: SourceIngestionJobStore | None = None

def source_task_cancel_requested() -> bool:
    handle = getattr(_CURRENT_SOURCE_TASK, 'handle', None)
    return bool(handle is not None and handle.cancel_event.is_set())

def register_current_source_task_process(process: subprocess.Popen[str] | None) -> None:
    handle = getattr(_CURRENT_SOURCE_TASK, 'handle', None)
    if handle is None:
        return
    handle.process = process
    _write_task_manifest(handle)

def register_current_source_task_workspace(workspace: Path | None) -> None:
    handle = getattr(_CURRENT_SOURCE_TASK, 'handle', None)
    if handle is None:
        return
    handle.workspace = workspace
    _write_task_manifest(handle)

def update_current_source_task_run_id(run_id: str) -> None:
    handle = getattr(_CURRENT_SOURCE_TASK, 'handle', None)
    if handle is None:
        return
    handle.run_id = run_id
    _write_task_manifest(handle)

def _write_task_manifest(handle: SourceTaskHandle) -> None:
    if handle.workspace is None or not handle.workspace.is_dir():
        return
    owner_user_id, package_id, source_id = handle.key
    manifest_path = handle.workspace / 'source-task-manifest.json'
    payload = {'owner_user_id': owner_user_id, 'package_id': package_id, 'source_id': source_id, 'run_id': handle.run_id, 'content_hash': handle.content_hash, 'pid': handle.process.pid if handle.process is not None else None, 'manager_pid': os.getpid(), 'workspace': str(handle.workspace)}
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    manifest_path.chmod(384)

def _terminate_task_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        logger.warning('Pi process group %s did not exit after SIGKILL', process.pid)

class SourceIngestionCoordinator:
    """Coordinate source work and serialize source writes for one SQLite database."""

    def __init__(self, *, processing_capacity: int | None=None, large_source_bytes: int=64 * 1024 * 1024, lock_retry_delays: tuple[float, ...]=(0.05, 0.15, 0.5, 1.0, 2.0)) -> None:
        if processing_capacity is not None and processing_capacity < 1:
            raise ValueError('processing_capacity must be positive')
        self.processing_capacity = processing_capacity
        self.large_source_bytes = large_source_bytes
        self.lock_retry_delays = lock_retry_delays
        self._processing_available = processing_capacity
        self._processing_waiters: deque[tuple[object, int]] = deque()
        self._processing_condition = threading.Condition()
        self._write_locks: dict[str, threading.RLock] = {}
        self._write_locks_guard = threading.Lock()

    def processing_weight(self, *, size_bytes: int, source_type: str) -> int:
        if self.processing_capacity is None:
            return 1
        if size_bytes >= self.large_source_bytes or source_type in {'audio_file', 'video_file'}:
            return self.processing_capacity
        return 1

    @contextmanager
    def processing_slot(self, *, weight: int=1) -> Iterator[None]:
        if self.processing_capacity is None:
            yield
            return
        normalized_weight = max(1, min(self.processing_capacity, weight))
        ticket = object()
        with self._processing_condition:
            self._processing_waiters.append((ticket, normalized_weight))
            while self._processing_waiters[0][0] is not ticket or self._processing_available < normalized_weight:
                self._processing_condition.wait()
            self._processing_waiters.popleft()
            self._processing_available -= normalized_weight
        try:
            yield
        finally:
            with self._processing_condition:
                self._processing_available += normalized_weight
                self._processing_condition.notify_all()

    def run_write(self, path: Path, operation: Callable[[], WriteResult]) -> WriteResult:
        write_lock = self._write_lock(path)
        with write_lock:
            for attempt in range(len(self.lock_retry_delays) + 1):
                try:
                    return operation()
                except sqlite3.OperationalError as exc:
                    if 'locked' not in str(exc).lower() or attempt >= len(self.lock_retry_delays):
                        raise
                    time.sleep(self.lock_retry_delays[attempt])
        raise RuntimeError('unreachable source write retry state')

    def _write_lock(self, path: Path) -> threading.RLock:
        key = str(path.resolve())
        with self._write_locks_guard:
            return self._write_locks.setdefault(key, threading.RLock())
source_ingestion_coordinator = SourceIngestionCoordinator()

class SourceIngestionJobStore:

    def __init__(self, path: Path | None=None, *, coordinator: SourceIngestionCoordinator=source_ingestion_coordinator) -> None:
        self._path = path
        self.coordinator = coordinator
        self._lock = threading.RLock()
        self._initialized_paths: set[str] = set()

    @property
    def path(self) -> Path:
        return self._path or source_library_path()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys = ON')
            conn.execute('PRAGMA busy_timeout = 5000')
            conn.execute('PRAGMA journal_mode = WAL')
            self._initialize_connection(conn, path)
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize_connection(self, conn: sqlite3.Connection, path: Path) -> None:
        with self._lock:
            key = str(path)
            if key in self._initialized_paths:
                return
            conn.executescript("\n                CREATE TABLE IF NOT EXISTS source_ingestion_jobs (\n                    id TEXT PRIMARY KEY,\n                    owner_user_id TEXT NOT NULL,\n                    package_id TEXT NOT NULL,\n                    source_ingestion_id TEXT NOT NULL,\n                    source_type TEXT NOT NULL,\n                    source_uri TEXT,\n                    adapter TEXT NOT NULL,\n                    status TEXT NOT NULL,\n                    progress INTEGER NOT NULL,\n                    error TEXT NOT NULL,\n                    phase_history_json TEXT NOT NULL DEFAULT '[]',\n                    agent_activity_json TEXT NOT NULL DEFAULT '[]',\n                    run_id TEXT NOT NULL DEFAULT '',\n                    cancel_requested INTEGER NOT NULL DEFAULT 0,\n                    heartbeat_at TEXT,\n                    created_at TEXT NOT NULL,\n                    updated_at TEXT NOT NULL\n                );\n                CREATE INDEX IF NOT EXISTS idx_source_ingestion_jobs_scope\n                    ON source_ingestion_jobs(owner_user_id, package_id, updated_at);\n                CREATE INDEX IF NOT EXISTS idx_source_ingestion_jobs_source\n                    ON source_ingestion_jobs(owner_user_id, package_id, source_ingestion_id, updated_at);\n                ")
            columns = {str(row['name']) for row in conn.execute('PRAGMA table_info(source_ingestion_jobs)').fetchall()}
            if 'agent_activity_json' not in columns:
                conn.execute("ALTER TABLE source_ingestion_jobs ADD COLUMN agent_activity_json TEXT NOT NULL DEFAULT '[]'")
            if 'run_id' not in columns:
                conn.execute("ALTER TABLE source_ingestion_jobs ADD COLUMN run_id TEXT NOT NULL DEFAULT ''")
            if 'cancel_requested' not in columns:
                conn.execute('ALTER TABLE source_ingestion_jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0')
            if 'heartbeat_at' not in columns:
                conn.execute('ALTER TABLE source_ingestion_jobs ADD COLUMN heartbeat_at TEXT')
            self._initialized_paths.add(key)

    def save(self, job: SourceIngestionJob, *, owner_user_id: str, package_id: str) -> SourceIngestionJob:
        job = job.model_copy(update={'updated_at': now_iso()})

        def save_job() -> SourceIngestionJob:
            with self._lock, self._connect() as conn, conn:
                conn.execute('\n                    INSERT INTO source_ingestion_jobs(\n                        id, owner_user_id, package_id, source_ingestion_id, source_type, source_uri,\n                        adapter, status, progress, error, phase_history_json, agent_activity_json,\n                        run_id, cancel_requested, heartbeat_at, created_at, updated_at\n                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                    ON CONFLICT(id) DO UPDATE SET\n                        status = excluded.status,\n                        progress = excluded.progress,\n                        error = excluded.error,\n                        phase_history_json = excluded.phase_history_json,\n                        agent_activity_json = excluded.agent_activity_json,\n                        run_id = excluded.run_id,\n                        cancel_requested = excluded.cancel_requested,\n                        heartbeat_at = CASE\n                            WHEN excluded.heartbeat_at IS NOT NULL\n                              AND (source_ingestion_jobs.heartbeat_at IS NULL\n                                   OR excluded.heartbeat_at > source_ingestion_jobs.heartbeat_at)\n                            THEN excluded.heartbeat_at\n                            ELSE source_ingestion_jobs.heartbeat_at\n                        END,\n                        updated_at = excluded.updated_at\n                    ', (job.id, owner_user_id, package_id, job.resource_id or '', job.source_type, job.source_uri, job.adapter, job.status, job.progress, job.error, json.dumps(job.phase_history, ensure_ascii=False), json.dumps([event.model_dump(mode='json') for event in job.agent_activity], ensure_ascii=False), job.run_id or job.id, int(job.cancel_requested), job.heartbeat_at, job.created_at, job.updated_at))
            return job
        return self.coordinator.run_write(self.path, save_job)

    def list(self, *, owner_user_id: str, package_id: str) -> list[SourceIngestionJob]:
        with self._lock, self._connect() as conn:
            rows = conn.execute('\n                SELECT * FROM source_ingestion_jobs\n                WHERE owner_user_id = ? AND package_id = ?\n                ORDER BY updated_at DESC\n                ', (owner_user_id, package_id)).fetchall()
        return [self._from_row(row) for row in rows]

    def list_active_scopes(self) -> list[SourceTaskKey]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("\n                SELECT owner_user_id, package_id, source_ingestion_id\n                FROM source_ingestion_jobs AS candidate\n                WHERE candidate.id = (\n                    SELECT latest.id\n                    FROM source_ingestion_jobs AS latest\n                    WHERE latest.owner_user_id = candidate.owner_user_id\n                      AND latest.package_id = candidate.package_id\n                      AND latest.source_ingestion_id = candidate.source_ingestion_id\n                    ORDER BY latest.updated_at DESC, latest.id DESC\n                    LIMIT 1\n                )\n                  AND candidate.status IN ('queued', 'fetching', 'parsing', 'indexing')\n                ORDER BY candidate.updated_at ASC\n                ").fetchall()
            staged = []
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'google_source_pending_versions' in tables:
                for pending in conn.execute('SELECT owner_user_id,package_id,source_id,source_json FROM google_source_pending_versions'):
                    payload = json.loads(pending['source_json'])
                    if payload.get('status') in {'queued', 'fetching', 'parsing', 'indexing'}:
                        staged.append((pending['owner_user_id'], pending['package_id'], pending['source_id']))
            if 'source_ingestions' in tables:
                from openclass_core.services.source_basic_recovery import ordinary_metadata_active
                for source in conn.execute("SELECT owner_user_id,package_id,id,status,metadata_json FROM source_ingestions WHERE status IN ('queued','fetching','parsing','indexing','ready')"):
                    metadata = json.loads(source['metadata_json'])
                    if metadata.get('source_backend') in {'google', 'local'} and (source['status'] in _ACTIVE_SOURCE_STATUSES or metadata.get('update_status') in _ACTIVE_SOURCE_STATUSES or ordinary_metadata_active(metadata)):
                        staged.append((source['owner_user_id'], source['package_id'], source['id']))
        return list(dict.fromkeys([(str(row['owner_user_id']), str(row['package_id']), str(row['source_ingestion_id'])) for row in rows if str(row['source_ingestion_id'] or '').strip()] + staged))

    def latest_for_source(self, *, owner_user_id: str, package_id: str, source_id: str) -> SourceIngestionJob | None:
        with self._lock, self._connect() as conn:
            row = conn.execute('\n                SELECT * FROM source_ingestion_jobs\n                WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                ORDER BY updated_at DESC LIMIT 1\n                ', (owner_user_id, package_id, source_id)).fetchone()
        return self._from_row(row) if row else None

    def delete_for_source(self, *, owner_user_id: str, package_id: str, source_id: str) -> None:

        def delete_jobs() -> None:
            with self._lock, self._connect() as conn, conn:
                conn.execute('DELETE FROM source_ingestion_jobs WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?', (owner_user_id, package_id, source_id))
        self.coordinator.run_write(self.path, delete_jobs)

    def mark_cancel_requested(self, *, owner_user_id: str, package_id: str, source_id: str) -> None:

        def mark_cancelled() -> None:
            with self._lock, self._connect() as conn, conn:
                conn.execute('\n                    UPDATE source_ingestion_jobs\n                    SET cancel_requested = 1, updated_at = ?\n                    WHERE id = (\n                        SELECT id FROM source_ingestion_jobs\n                        WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                        ORDER BY updated_at DESC, id DESC LIMIT 1\n                    )\n                    ', (now_iso(), owner_user_id, package_id, source_id))
        self.coordinator.run_write(self.path, mark_cancelled)

    def touch_worker_heartbeat(self, *, key: SourceTaskKey, run_id: str) -> bool:
        """Renew only the current active worker's lease, without changing progress."""

        def touch() -> bool:
            with self._lock, self._connect() as conn, conn:
                cursor = conn.execute("\n                    UPDATE source_ingestion_jobs SET heartbeat_at = ?\n                    WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                      AND (run_id = ? OR id = ?)\n                      AND status IN ('queued', 'fetching', 'parsing', 'indexing')\n                      AND cancel_requested = 0\n                      AND id = (\n                          SELECT latest.id FROM source_ingestion_jobs AS latest\n                          WHERE latest.owner_user_id = ? AND latest.package_id = ?\n                            AND latest.source_ingestion_id = ?\n                          ORDER BY latest.updated_at DESC, latest.id DESC LIMIT 1\n                      )\n                    ", (now_iso(), *key, run_id, run_id, *key))
                return cursor.rowcount == 1
        return self.coordinator.run_write(self.path, touch)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> SourceIngestionJob:
        try:
            phases = json.loads(row['phase_history_json'] or '[]')
        except json.JSONDecodeError:
            phases = []
        try:
            activity = json.loads(row['agent_activity_json'] or '[]')
        except (json.JSONDecodeError, IndexError):
            activity = []
        return SourceIngestionJob(id=row['id'], run_id=row['run_id'] or row['id'], resource_id=row['source_ingestion_id'], source_type=row['source_type'], source_uri=row['source_uri'], adapter=row['adapter'], status=row['status'], progress=row['progress'], error=row['error'], phase_history=phases, agent_activity=activity, processing_metrics=next((event.get('metadata', {}).get('processing_metrics') for event in reversed(activity) if event.get('metadata', {}).get('processing_metrics_snapshot')), None), cancel_requested=bool(row['cancel_requested']), heartbeat_at=row['heartbeat_at'], created_at=row['created_at'], updated_at=row['updated_at'])
source_ingestion_job_store = SourceIngestionJobStore()

class SourceIngestionTaskManager:
    """Run persisted source work outside an individual HTTP request lifecycle."""

    def __init__(self, job_store: SourceIngestionJobStore=source_ingestion_job_store, *, heartbeat_interval_seconds: float=10.0) -> None:
        if heartbeat_interval_seconds <= 0:
            raise ValueError('heartbeat_interval_seconds must be positive')
        self.job_store = job_store
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self._active: dict[SourceTaskKey, SourceTaskHandle] = {}
        self._lock = threading.Lock()

    @contextmanager
    def track_running(self, *, key: SourceTaskKey, run_id: str, job_store: SourceIngestionJobStore | None=None) -> Iterator[None]:
        """Track directory work also when it runs in an HTTP worker thread.

        A worker-owned periodic lease survives long model calls without relying
        on progress events or browser polling. Reuse a submitted task's handle
        so its existing cancellation signal remains authoritative.
        """
        previous = getattr(_CURRENT_SOURCE_TASK, 'handle', None)
        store = job_store or self.job_store
        owns_handle = previous is None or previous.key != key
        with self._lock:
            existing = self._active.get(key)
            if existing is not None and existing is not previous and (not existing.finished.is_set()):
                if existing.thread is None or existing.thread.is_alive():
                    raise SourceTaskNotActiveError('A source directory worker is already active')
            handle = SourceTaskHandle(cancel_event=threading.Event(), key=key, thread=threading.current_thread()) if owns_handle else previous
            handle.run_id = run_id
            handle.job_store = store
            self._active[key] = handle
        _CURRENT_SOURCE_TASK.handle = handle
        stop_heartbeat = threading.Event()

        def heartbeat() -> None:
            while not stop_heartbeat.wait(self.heartbeat_interval_seconds):
                if handle.cancel_event.is_set() or handle.finished.is_set():
                    return
                if handle.thread is not None and (not handle.thread.is_alive()):
                    return
                try:
                    if not store.touch_worker_heartbeat(key=key, run_id=run_id):
                        return
                except Exception:
                    logger.exception('Could not renew source worker heartbeat')
        thread: threading.Thread | None = None
        try:
            if not store.touch_worker_heartbeat(key=key, run_id=run_id):
                handle.cancel_event.set()
                raise SourceTaskNotActiveError('Source directory job is no longer active; it was cancelled, completed, or superseded.')
            thread = threading.Thread(target=heartbeat, daemon=True, name=f'source-heartbeat-{key[2]}')
            thread.start()
            yield
        finally:
            stop_heartbeat.set()
            if thread is not None:
                thread.join(timeout=1.0)
            _CURRENT_SOURCE_TASK.handle = previous
            if owns_handle:
                handle.finished.set()
                with self._lock:
                    if self._active.get(key) is handle:
                        self._active.pop(key, None)

    def submit(self, *, owner_user_id: str, package_id: str, source_id: str, retry: bool=False) -> bool:
        key = (owner_user_id, package_id, source_id)
        latest_job = self.job_store.latest_for_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
        latest_run_id = latest_job.run_id or latest_job.id if latest_job is not None else ''
        superseded: SourceTaskHandle | None = None
        with self._lock:
            existing = self._active.get(key)
            if existing is not None:
                if existing.finished.is_set() or (existing.thread is not None and (not existing.thread.is_alive())):
                    self._active.pop(key, None)
                elif existing.run_id == latest_run_id:
                    return False
                else:
                    existing.cancel_event.set()
                    superseded = existing
            handle = SourceTaskHandle(cancel_event=threading.Event(), key=key, run_id=latest_run_id)
            self._active[key] = handle
        if superseded is not None:
            _terminate_task_process(superseded.process)
        thread = threading.Thread(target=copy_context().run, args=(self._run,), kwargs={'key': key, 'retry': retry, 'handle': handle}, daemon=True, name=f'source-ingestion-{source_id}')
        handle.thread = thread
        thread.start()
        return True

    def recover_active(self) -> int:
        from openclass_core.services.source_ingestion_service import source_ingestion_service
        candidates: list[tuple[SourceIngestionJob, SourceTaskKey, object]] = []
        for key in self.job_store.list_active_scopes():
            owner_user_id, package_id, source_id = key
            job = self.job_store.latest_for_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
            record = source_ingestion_service.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
            if record is not None and record.metadata.get('source_backend') in {'google', 'local'}:
                from openclass_core.services.google_sources.config import GoogleSourceError
                try:
                    google = source_ingestion_service.google_service
                    record = google.state.current(google.work_source(record), active=False)
                    same_version = job is not None and job.run_id == record.metadata['source_version']
                    from openclass_core.services.source_basic_recovery import ordinary_task_active
                    updating_catalog = record.status == 'ready' and (record.metadata.get('update_status') in _ACTIVE_SOURCE_STATUSES or ordinary_task_active(record))
                    if same_version and job.cancel_requested:
                        if record.metadata.get('processing_mode') == 'ordinary':
                            google.cancel(record)
                        elif updating_catalog:
                            message = 'Catalog retry cancelled. The original file and existing catalog have been preserved.'
                            record = google.state.update(record, active=False, metadata={'update_status': 'cancelled', 'update_error': message})
                            google._job(record.model_copy(update={'status': 'cancelled'}), 'cancelled', 100, message)
                        else:
                            google.cancel(record)
                        continue
                    if updating_catalog:
                        if record.metadata.get('pending_source_version') or (job is not None and (not same_version)):
                            continue
                        if same_version and job.status in {'cancelled', 'failed', 'interrupted'}:
                            continue
                        if job is None or job.status == 'ready':
                            job = google._job(record.model_copy(update={'status': 'queued'}), 'catalog_queued', 5)
                    elif record.status not in _ACTIVE_SOURCE_STATUSES:
                        if record.status == 'ready' and (not record.metadata.get('update_status')) and same_version and (job.status in _ACTIVE_SOURCE_STATUSES):
                            google._job(record, 'ready', 100)
                        continue
                    elif same_version and job.status in {'cancelled', 'failed', 'interrupted'}:
                        continue
                    elif not same_version:
                        job = google._job(record, 'recovered', 15)
                except GoogleSourceError:
                    continue
            if job is not None and record is not None:
                candidates.append((job, key, record))
        candidates.sort(key=lambda item: (item[0].updated_at, item[0].id), reverse=True)
        recovered = 0
        seen_content: set[tuple[str, str, str]] = set()
        for job, key, record in candidates:
            owner_user_id, package_id, source_id = key
            metadata = getattr(record, 'metadata', {})
            content_hash = str(metadata.get('content_hash') or source_id)
            identity = (owner_user_id, package_id, f"{source_id}:{metadata.get('source_version', '')}" if metadata.get('source_backend') in {'google', 'local'} else content_hash)
            if identity in seen_content:
                self.job_store.save(job.model_copy(update={'status': 'failed', 'progress': 100, 'error': 'Superseded by recovery of the same content hash.', 'cancel_requested': True, 'phase_history': [*job.phase_history, 'superseded_on_recovery']}), owner_user_id=owner_user_id, package_id=package_id)
                source_ingestion_service.store.save_source(record.model_copy(update={'status': 'failed', 'error': 'A newer task for the same file content was recovered.'}))
                continue
            seen_content.add(identity)
            recovered += int(self.submit(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id, **{'retry': True} if metadata.get('source_backend') in {'google', 'local'} and record.status == 'ready' else {}))
        return recovered

    def is_active(self, key: SourceTaskKey) -> bool:
        with self._lock:
            handle = self._active.get(key)
            if handle is None:
                return False
            if handle.finished.is_set() or (handle.thread is not None and (not handle.thread.is_alive())):
                self._active.pop(key, None)
                return False
            return True

    def cancel(self, key: SourceTaskKey, *, timeout_seconds: float=5.0) -> bool:
        started_at = time.monotonic()
        with self._lock:
            handle = self._active.get(key)
        if handle is None:
            return True
        handle.cancel_event.set()
        owner_user_id, package_id, source_id = key
        (handle.job_store or self.job_store).mark_cancel_requested(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
        _terminate_task_process(handle.process)
        thread = handle.thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            remaining = max(0.0, timeout_seconds - (time.monotonic() - started_at))
            handle.finished.wait(timeout=remaining)
        return handle.finished.is_set() or thread is None or (not thread.is_alive())

    def _run(self, *, key: SourceTaskKey, retry: bool, handle: SourceTaskHandle) -> None:
        owner_user_id, package_id, source_id = key
        _CURRENT_SOURCE_TASK.handle = handle
        try:
            from openclass_core.services.source_ingestion_service import source_ingestion_service
            record = source_ingestion_service.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
            if record is not None and record.metadata.get('source_backend') in {'google', 'local'}:
                record = source_ingestion_service.google_service.work_source(record)
            if record is not None:
                handle.content_hash = str(record.metadata.get('content_hash') or '')
                _write_task_manifest(handle)
            weight = self.job_store.coordinator.processing_capacity or 1
            if record is not None:
                weight = self.job_store.coordinator.processing_weight(size_bytes=record.size_bytes, source_type=record.source_type)
            operation = source_ingestion_service.retry_source if retry else source_ingestion_service.process_file_source
            with self.job_store.coordinator.processing_slot(weight=weight):
                operation(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
        except Exception:
            if not handle.cancel_event.is_set():
                logger.exception('Source ingestion task failed for %s', source_id)
        finally:
            _CURRENT_SOURCE_TASK.handle = None
            handle.finished.set()
            with self._lock:
                if self._active.get(key) is handle:
                    self._active.pop(key, None)
source_ingestion_task_manager = SourceIngestionTaskManager()
