from __future__ import annotations
import hashlib
import fcntl
import ipaddress
import os
import re
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlparse
from openclass_core.models import AgentActivityEvent, AIModelSelection, CoursePackage, SourceIngestionJob, SourceIngestionRecord, SourceStructureQuality, new_id, now_iso
from openclass_core.services.ai_logging import ai_usage_logger
from openclass_core.services.ai_model_catalog import default_text_selection
from openclass_core.services.open_notebook_adapter import OpenNotebookAdapter, OpenNotebookAdapterError, open_notebook_adapter
from openclass_core.services.open_notebook_source_backend import OpenNotebookSourceBackend, status_from_open_notebook
from openclass_core.services.source_evidence_store import SourceEvidenceStore, source_evidence_store
from openclass_core.services.source_directory_extractor import supports_directory_catalog
from openclass_core.services.source_codex_progress import SourceCodexProgressTracker
from openclass_core.services.source_directory_processor import AI_CATALOG_SCHEMA_VERSIONS, CATALOG_SCHEMA_VERSION, LEGACY_CATALOG_SCHEMA_VERSIONS, PREVIOUS_CATALOG_SCHEMA_VERSION, SourceDirectoryProcessor
from openclass_core.services.source_ingestion_jobs import SourceIngestionJobStore, SourceTaskNotActiveError, source_task_cancel_requested, source_ingestion_task_manager, source_ingestion_job_store, update_current_source_task_run_id
from openclass_core.services.source_structure_indexer import SourceStructureIndexer
from openclass_core.services.source_structure_store import SourceStructureStore, source_structure_store
from openclass_core.services.repository_source import is_github_repository_url, parse_github_url, repository_source_processor
from openclass_core.services.repository_store import RepositoryStore, repository_store
from openclass_core.services.github_app import GitHubAppError, github_app_service
from openclass_core.services.source_url_snapshot import SourceUrlSnapshotError, fetch_url_source_snapshot
from openclass_core.services.youtube_transcript_adapter import YouTubeTranscriptAdapter, YouTubeTranscriptAdapterError, is_youtube_url, youtube_transcript_adapter
from openclass_core.services import workspace_state
SUPPORTED_FILE_MIME_PREFIXES = ('application/pdf', 'application/epub+zip', 'application/vnd.openxmlformats-officedocument', 'text/', 'audio/', 'video/', 'image/')
SOURCE_INTERRUPTED_HEARTBEAT_SECONDS = max(15, int(os.getenv('OPENCLASS_SOURCE_INTERRUPTED_HEARTBEAT_SECONDS', '45')))

def _source_exception_is_interruption(exc: Exception) -> bool:
    message = str(exc).lower()
    return any((marker in message for marker in ('connection termination', 'connection reset', 'disconnect/reset', 'server disconnected', 'broken pipe', 'request failed: terminated', 'request was terminated', 'model connection interrupted')))
_DIRECTORY_CATALOG_LOCKS_GUARD = threading.Lock()
_DIRECTORY_CATALOG_LOCKS: dict[tuple[str, str, str, str], threading.RLock] = {}
_DIRECTORY_CATALOG_LOCK_DEPTH = threading.local()

@contextmanager
def _directory_catalog_processing_slot(*, database_path: Path, record: SourceIngestionRecord) -> Iterator[None]:
    content_identity = str(record.metadata.get('content_hash') or record.id)
    with _source_content_processing_slot(database_path=database_path, owner_user_id=record.owner_user_id, package_id=record.package_id, content_identity=content_identity):
        yield

@contextmanager
def _source_content_processing_slot(*, database_path: Path, owner_user_id: str, package_id: str, content_identity: str) -> Iterator[None]:
    key = (str(database_path.resolve()), owner_user_id, package_id, content_identity)
    with _DIRECTORY_CATALOG_LOCKS_GUARD:
        lock = _DIRECTORY_CATALOG_LOCKS.setdefault(key, threading.RLock())
    with lock:
        depths = getattr(_DIRECTORY_CATALOG_LOCK_DEPTH, 'values', None)
        if depths is None:
            depths = {}
            _DIRECTORY_CATALOG_LOCK_DEPTH.values = depths
        if depths.get(key, 0):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        lock_directory = database_path.parent / 'source-ingestion-locks'
        lock_directory.mkdir(parents=True, exist_ok=True)
        lock_name = hashlib.sha256('\x00'.join(key).encode('utf-8')).hexdigest()
        with (lock_directory / f'{lock_name}.lock').open('a+') as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            depths[key] = 1
            try:
                yield
            finally:
                depths.pop(key, None)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

class SourceIngestionError(RuntimeError):

    def __init__(self, message: str, status_code: int=400) -> None:
        super().__init__(message)
        self.status_code = status_code

@dataclass(frozen=True)
class SourceImportAutomationOutcome:
    artifact_ids: list[str]
    errors: list[str]
SourceImportAutomationRunner = Callable[..., SourceImportAutomationOutcome]

class SourceIngestionService:

    def __init__(self, *, adapter: OpenNotebookAdapter=open_notebook_adapter, source_backend: str | None=None, youtube_adapter: YouTubeTranscriptAdapter=youtube_transcript_adapter, store: SourceEvidenceStore=source_evidence_store, job_store: SourceIngestionJobStore=source_ingestion_job_store, structure_indexer: SourceStructureIndexer | None=None, structure_store: SourceStructureStore | None=None, directory_processor: SourceDirectoryProcessor | None=None, import_automation_runner: SourceImportAutomationRunner | None=None, media_transcription_provider: object | None=None, repository_processor: object=repository_source_processor, repository_data_store: RepositoryStore=repository_store, google_service: object | None=None) -> None:
        self.adapter = adapter
        self.open_notebook_backend = OpenNotebookSourceBackend(adapter=adapter, store=store, local_path=source_local_path)
        self.source_backend = (source_backend or os.getenv('OPENCLASS_SOURCE_BACKEND', 'native')).strip().lower()
        if self.source_backend not in {'open_notebook', 'native', 'google', 'local'}:
            raise ValueError('OPENCLASS_SOURCE_BACKEND must be open_notebook, native or google')
        self.youtube_adapter = youtube_adapter
        self.store = store
        self.job_store = job_store
        self.structure_store = structure_store or _structure_store_for_source_store(store)
        self.structure_indexer = structure_indexer or SourceStructureIndexer(store=self.structure_store)
        self.directory_processor = directory_processor or SourceDirectoryProcessor(store=self.structure_store)
        self.directory_catalog_enabled = directory_processor is not None or structure_indexer is None
        self.import_automation_runner = import_automation_runner
        self.media_transcription_provider = media_transcription_provider
        self.repository_processor = repository_processor
        self.repository_store = repository_data_store
        self._google_service = google_service

    @property
    def google_service(self):
        if self._google_service is None:
            from openclass_core.services.google_sources.service import get_google_source_service
            self._google_service = get_google_source_service(store=self.store, job_store=self.job_store, structure_store=self.structure_store)
        from openclass_core.services.google_sources.catalog_fallback import build_fallback_catalog, build_manual_catalog
        self._google_service.catalog_manual = lambda source: build_manual_catalog(self, source)
        self._google_service.catalog_fallback = lambda source, error: build_fallback_catalog(self, source, error)
        return self._google_service

    def list_sources(self, *, owner_user_id: str, package_id: str) -> list[SourceIngestionRecord]:
        records = self.store.list_sources(owner_user_id=owner_user_id, package_id=package_id)
        if any((record.metadata.get('source_processor') == 'anythingllm' for record in records)):
            from openclass_core.services.anythingllm.provider import refresh_statuses
            records = refresh_statuses(self.google_service, records)
        if self.source_backend == 'open_notebook':
            records = [record if _uses_directory_catalog(record) else self._refresh_open_notebook_source(record) for record in records]
        attached: list[SourceIngestionRecord] = []
        for record in records:
            record = self._mark_interrupted_if_orphaned(record)
            summarized = self.structure_store.attach_summary(record)
            if summarized.status == 'ready' and summarized.source_type == 'code_repository' and (summarized.structure_status in {'pending', 'building'}) and self.repository_store.has_snapshot(source=summarized):
                summarized = summarized.model_copy(update={'structure_status': 'ready', 'structure_strategy': 'code_repository_v1', 'structure_error': ''})
            attached.append(self._attach_job(summarized))
        return attached

    def _mark_interrupted_if_orphaned(self, record: SourceIngestionRecord) -> SourceIngestionRecord:
        """Close a persisted active job whose worker disappeared.

        A short grace period prevents the upload-to-worker handoff and a live
        model call from being mistaken for an interruption. Existing files and
        catalog rows are intentionally left untouched so retry can reuse them.
        """
        active_statuses = {'queued', 'fetching', 'parsing', 'indexing'}
        job = self.job_store.latest_for_source(owner_user_id=record.owner_user_id, package_id=record.package_id, source_id=record.id)
        if record.status == 'failed' and job is not None and (job.status == 'failed') and _source_exception_is_interruption(RuntimeError(job.error or record.error)):
            return self._persist_interrupted(record, job, detail=job.error or record.error)
        from openclass_core.services.source_basic_recovery import ordinary_task_active
        if record.status not in active_statuses and (not ordinary_task_active(record)):
            return record
        key = (record.owner_user_id, record.package_id, record.id)
        if source_ingestion_task_manager.is_active(key):
            return record
        if job is None or job.status not in active_statuses:
            return record
        activity_times = [job.heartbeat_at or job.updated_at or job.created_at]
        try:
            parsed_activity_times = [datetime.fromisoformat(value.replace('Z', '+00:00')) for value in activity_times]
            normalized_activity_times = [value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc) for value in parsed_activity_times]
            age_seconds = (datetime.now(timezone.utc) - max(normalized_activity_times)).total_seconds()
        except (TypeError, ValueError):
            return record
        if age_seconds < SOURCE_INTERRUPTED_HEARTBEAT_SECONDS:
            return record
        return self._persist_interrupted(record, job)

    def _persist_interrupted(self, record: SourceIngestionRecord, job: SourceIngestionJob, *, detail: str='') -> SourceIngestionRecord:
        message = 'The source agent task was interrupted. The original file and existing catalog have been preserved. Refresh to run it again.'
        event = AgentActivityEvent(turn_id=f'source-interrupted:{job.id}', stage='final', label='Source agent task interrupted', status='failed', role='OpenClass', metadata={'kind': 'sourceCatalogStage', 'detail': detail or message, 'interrupted': True})
        phases = job.phase_history
        if not phases or phases[-1] != 'interrupted':
            phases = [*phases, 'interrupted']
        self.job_store.save(job.model_copy(update={'status': 'interrupted', 'error': message, 'phase_history': phases, 'agent_activity': [*job.agent_activity, event]}), owner_user_id=record.owner_user_id, package_id=record.package_id)
        if record.metadata.get('processing_mode') == 'ordinary':
            from openclass_core.services.source_basic_recovery import interrupted_ordinary_source
            return self.store.save_source(interrupted_ordinary_source(record, message))
        return self.store.save_source(record.model_copy(update={'status': 'interrupted', 'error': message}))

    def add_url_source(self, *, owner_user_id: str, package: CoursePackage, source_uri: str, title: str='', supersedes_source_id: str | None=None, catalog_model: AIModelSelection | None=None, connection_id: str | None=None, public_only: bool=False) -> SourceIngestionRecord:
        normalized_uri = _validate_public_url(source_uri)
        if is_github_repository_url(normalized_uri):
            return self.queue_repository_source(owner_user_id=owner_user_id, package=package, source_uri=normalized_uri, title=title, supersedes_source_id=supersedes_source_id, catalog_model=catalog_model, connection_id=connection_id, public_only=public_only)
        if connection_id or public_only:
            raise SourceIngestionError('A GitHub connection can only be selected for a GitHub repository URL.')
        if is_youtube_url(normalized_uri):
            if self.source_backend in {'google', 'local'}:
                from openclass_core.services.google_sources.youtube import queue_youtube_source
                from openclass_core.services.google_sources.config import GoogleSourceError
                try:
                    queued = queue_youtube_source(service=self.google_service, adapter=self.youtube_adapter, owner_user_id=owner_user_id, package_id=package.id, source_uri=normalized_uri, title=title)
                    if queued.status == 'queued':
                        source_ingestion_task_manager.submit(owner_user_id=owner_user_id, package_id=package.id, source_id=queued.id)
                    return queued
                except (YouTubeTranscriptAdapterError, GoogleSourceError) as exc:
                    raise SourceIngestionError(str(exc)) from exc
            return self._save_youtube_transcript_source(owner_user_id=owner_user_id, package=package, source_uri=normalized_uri, title=title)
        if self.source_backend in {'google', 'local'}:
            from openclass_core.services.google_sources.web_snapshot import make_web_snapshot
            from openclass_core.services.google_sources.config import GoogleSourceError
            try:
                content, snapshot = make_web_snapshot(normalized_uri)
                queued = self.google_service.queue_file(owner_user_id=owner_user_id, package_id=package.id, file_name='snapshot.webbundle.zip', content=content, mime_type='application/zip', title=title or snapshot.get('title') or normalized_uri, source_uri=normalized_uri, origin_metadata={'origin': 'web_snapshot', 'url': snapshot['url']})
                source_ingestion_task_manager.submit(owner_user_id=owner_user_id, package_id=package.id, source_id=queued.id)
                return queued
            except GoogleSourceError as exc:
                raise SourceIngestionError(str(exc)) from exc
        if self.source_backend == 'open_notebook':
            return self._add_url_source_open_notebook(owner_user_id=owner_user_id, package=package, source_uri=normalized_uri, title=title)
        display_title = title.strip() or normalized_uri
        record = SourceIngestionRecord(owner_user_id=owner_user_id, package_id=package.id, title=display_title, source_type='web_url', source_uri=normalized_uri, file_name='', mime_type='text/html', size_bytes=0, status='fetching', metadata={'adapter': 'openclass_native_url'})
        record = self.store.save_source(record)
        job = self._start_job(record, adapter='openclass_native_url', phase='fetching', progress=10)
        try:
            snapshot_metadata = fetch_url_source_snapshot(record, normalized_uri)
        except SourceUrlSnapshotError as exc:
            failed = self.store.save_source(self._failed_record(record, str(exc), phase='fetch_url'))
            self._finish_job(job, record=failed, status='failed', progress=100, error=str(exc), phase='failed')
            return failed
        local_path = Path(str(snapshot_metadata.get('local_source_path') or ''))
        size_bytes = local_path.stat().st_size if local_path.is_file() else 0
        ready = self.store.save_source(record.model_copy(update={'status': 'queued', 'file_name': local_path.name, 'size_bytes': size_bytes, 'metadata': {**record.metadata, **snapshot_metadata, 'content_hash': _file_content_hash(local_path), 'native_index_version': 1, 'catalog_model_selection': (catalog_model or default_text_selection()).model_dump(mode='json')}}))
        return self._save_and_index(ready, job)

    def queue_repository_source(self, *, owner_user_id: str, package: CoursePackage, source_uri: str, title: str='', supersedes_source_id: str | None=None, catalog_model: AIModelSelection | None=None, connection_id: str | None=None, public_only: bool=False) -> SourceIngestionRecord:
        if not github_app_service.enabled:
            raise SourceIngestionError('GitHub repository sources are disabled.')
        if public_only and connection_id:
            raise SourceIngestionError('A public-only repository import cannot select a GitHub account.')
        parsed = parse_github_url(source_uri)
        if public_only:
            connected = []
        else:
            try:
                connected = [row for row in github_app_service.connections.connections(owner_user_id) if row['status'] == 'connected' and row.get('capabilities', {}).get('repositories', {}).get('granted') is True and (row.get('capabilities', {}).get('repositories', {}).get('enabled') is True)]
            except Exception as exc:
                raise SourceIngestionError('GitHub account status could not be verified.', 503) from exc
        if not connection_id and len(connected) > 1:
            raise SourceIngestionError('Choose one connected GitHub account before importing a repository URL.', 409)
        chosen_id = connection_id or (connected[0]['id'] if connected else '')
        if connection_id:
            if not any((row['id'] == connection_id for row in connected)):
                raise SourceIngestionError('The selected GitHub account is not connected for this user.', 403)
            try:
                token = github_app_service.token_for_repository(owner_user_id=owner_user_id, owner=parsed.owner, name=parsed.name, connection_id=connection_id)
            except GitHubAppError as exc:
                raise SourceIngestionError(str(exc), 403) from exc
            if not token:
                raise SourceIngestionError('The selected GitHub account cannot read this repository.', 403)
        record = SourceIngestionRecord(owner_user_id=owner_user_id, package_id=package.id, title=title.strip() or source_uri, source_type='code_repository', source_uri=source_uri, file_name='repository.tree.json', mime_type='application/vnd.openclass.code-repository+json', status='queued', structure_status='pending', metadata={'adapter': 'github_repository_v1', 'package_title': package.title, 'supersedes_source_id': supersedes_source_id, 'github_connection_id': chosen_id, 'github_public_fallback': connection_id is None, 'github_public_only': not chosen_id, 'catalog_model_selection': (catalog_model or default_text_selection()).model_dump(mode='json')})
        queued = self.store.save_source(record)
        self._start_job(queued, adapter='github_repository_v1', phase='queued', progress=5)
        return self._attach_job(queued)

    def process_repository_source(self, *, owner_user_id: str, package_id: str, source_id: str) -> SourceIngestionRecord:
        record = self.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
        if record is None or record.source_type != 'code_repository':
            raise SourceIngestionError('Repository source not found.')
        job = self.job_store.latest_for_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id) or self._start_job(record, adapter='github_repository_v1', phase='queued', progress=5)
        current = self.store.save_source(record.model_copy(update={'status': 'fetching', 'error': ''}))
        activities: list[AgentActivityEvent] = []

        def report_progress(phase: str, progress: int) -> None:
            nonlocal current, job
            status = 'fetching' if progress < 48 else 'parsing' if progress < 72 else 'indexing'
            current = self.store.save_source(current.model_copy(update={'status': status}))
            job = self._update_job(job, record=current, status=status, progress=progress, phase=phase, agent_activity=activities)

        def report_activity(event: AgentActivityEvent) -> None:
            nonlocal job
            activities.append(event)
            job = self._update_job(job, record=current, status='indexing', progress=max(job.progress, 72), phase='analyzing_repository', agent_activity=activities)
        raw_model = record.metadata.get('catalog_model_selection') or record.metadata.get('catalog_model')
        try:
            catalog_model = AIModelSelection.model_validate(raw_model)
        except (TypeError, ValueError):
            catalog_model = default_text_selection()
        started_at = time.monotonic()
        try:
            result = self.repository_processor.process(record=record, source_uri=str(record.source_uri or ''), learning_goal=str(record.metadata.get('learning_goal') or ''), catalog_model=catalog_model, supersedes_source_id=str(record.metadata.get('supersedes_source_id')) if record.metadata.get('supersedes_source_id') else None, progress_callback=report_progress, activity_callback=report_activity)
            self.repository_store.save_repository(snapshot=result.snapshot, files=result.files, nodes=result.nodes)
        except Exception as exc:
            ai_usage_logger.log_event('repository_source_import_failed', owner_user_id=owner_user_id, package_id=package_id, source_ingestion_id=source_id, elapsed_seconds=round(time.monotonic() - started_at, 3), error_type=type(exc).__name__)
            failed = self.store.save_source(self._failed_record(current, str(exc), phase='repository_import'))
            self._finish_job(job, record=failed, status='failed', progress=100, phase='failed', error=str(exc))
            return self._attach_job(failed)
        project_nodes = [node for node in result.nodes if node.tree_kind == 'project']
        learning_nodes = [node for node in result.nodes if node.tree_kind == 'learning']
        referenceable_nodes = [node for node in learning_nodes if node.selectable]
        readable_files = [item for item in result.files if item.text_status == 'ready']
        analyzed_files = [item for item in readable_files if item.metadata.get('analyzed')]
        coverage_ratio = len(analyzed_files) / len(readable_files) if readable_files else 0.0
        ready = self.store.save_source(current.model_copy(update={'title': current.title if current.title != current.source_uri else f'{result.snapshot.owner}/{result.snapshot.name}', 'file_name': f'{result.snapshot.name}-{result.snapshot.resolved_commit_sha[:12]}.tree.json', 'size_bytes': result.archive_size, 'status': 'ready', 'error': '', 'structure_status': 'ready', 'structure_strategy': 'code_repository_v1', 'structure_quality': SourceStructureQuality(level='fully_verified' if learning_nodes and len(referenceable_nodes) == len(learning_nodes) else 'unverified', text_readiness='ready' if readable_files else 'empty', confidence=1.0 if learning_nodes else 0.0, total_chapter_count=len(learning_nodes), verified_chapter_count=len(referenceable_nodes), unverified_chapter_count=len(learning_nodes) - len(referenceable_nodes), verified_leaf_count=len(referenceable_nodes), expected_leaf_count=len(learning_nodes), verified_ratio=len(referenceable_nodes) / len(learning_nodes) if learning_nodes else 0.0, boundary_valid_ratio=1.0, body_coverage_ratio=coverage_ratio, diagnostics=['One native Pi task navigated the immutable commit tree, read bounded files, and generated the citable chapter structure.', *result.warnings]), 'metadata': {**current.metadata, 'repository_owner': result.snapshot.owner, 'repository_name': result.snapshot.name, 'repository_visibility': result.snapshot.visibility, 'repository_commit_sha': result.snapshot.resolved_commit_sha, 'repository_requested_ref': result.snapshot.requested_ref, 'repository_scope_path': result.snapshot.scope_path, 'repository_snapshot_hash': result.snapshot.archive_hash, 'repository_manifest_hash': result.snapshot.manifest_hash, 'repository_file_count': len(result.files), 'repository_readable_file_count': len(readable_files), 'repository_analyzed_file_count': len(analyzed_files), 'repository_processing_mode': 'single_native_pi_repository_tools', 'repository_no_model_calls': False, 'repository_warnings': result.warnings}}))
        ai_usage_logger.log_event('repository_source_import_completed', owner_user_id=owner_user_id, package_id=package_id, source_ingestion_id=source_id, elapsed_seconds=round(time.monotonic() - started_at, 3), visibility=result.snapshot.visibility, archive_bytes=result.archive_size, total_file_count=len(result.files), readable_file_count=len(readable_files), project_node_count=len(project_nodes), referenceable_node_count=len(referenceable_nodes), coverage_ratio=coverage_ratio)
        self._finish_job(job, record=ready, status='ready', progress=100, phase='ready')
        return self._attach_job(ready)

    def add_file_source(self, *, owner_user_id: str, package: CoursePackage, file_name: str, content: bytes, mime_type: str, title: str='', catalog_model: AIModelSelection | None=None) -> SourceIngestionRecord:
        queued = self.queue_file_source(owner_user_id=owner_user_id, package=package, file_name=file_name, content=content, mime_type=mime_type, title=title, catalog_model=catalog_model)
        return self.process_file_source(owner_user_id=owner_user_id, package_id=package.id, source_id=queued.id)

    def queue_file_source(self, *, owner_user_id: str, package: CoursePackage, file_name: str, content: bytes, mime_type: str, title: str='', catalog_model: AIModelSelection | None=None) -> SourceIngestionRecord:
        from openclass_core.services.anythingllm.config import selected, eligible
        anythingllm = selected() and eligible(file_name, mime_type)
        if self.source_backend in {'google', 'local'} or anythingllm:
            from openclass_core.services.google_sources.config import GoogleSourceError
            from openclass_core.services.google_sources.catalog_fallback import queue_with_catalog_recovery
            try:
                queued = queue_with_catalog_recovery(self.google_service, owner_user_id=owner_user_id, package_id=package.id, file_name=file_name, content=content, mime_type=mime_type, title=title, catalog_model=catalog_model)
                return self.google_service.state.update(queued, metadata={'source_processor': 'anythingllm'}) if anythingllm else queued
            except GoogleSourceError as exc:
                raise SourceIngestionError(str(exc)) from exc
        if not file_name.strip():
            raise SourceIngestionError('File name is required.')
        if not _supported_mime(mime_type, file_name):
            raise SourceIngestionError('This file type is not supported by the native source importer.')
        display_title = title.strip() or file_name
        source_type = _source_type_for_upload(mime_type=mime_type, file_name=file_name)
        use_directory_catalog = source_type == 'local_file' and _supported_directory_file_name(file_name) and self.directory_catalog_enabled
        content_hash = hashlib.sha256(content).hexdigest()
        directory_schema = 'ai_directory_v1' if getattr(self.directory_processor, 'ai_generator', None) is not None else CATALOG_SCHEMA_VERSION

        def create_source() -> SourceIngestionRecord:
            record = SourceIngestionRecord(owner_user_id=owner_user_id, package_id=package.id, title=display_title, source_type=source_type, source_uri=None, file_name=file_name, mime_type=mime_type or 'application/octet-stream', size_bytes=len(content), status='parsing' if use_directory_catalog else 'queued' if self.source_backend == 'open_notebook' else 'parsing', metadata={'adapter': directory_schema if use_directory_catalog else 'open_notebook' if self.source_backend == 'open_notebook' else 'openclass_native', 'package_title': package.title, **({'catalog_pipeline': directory_schema, **({'catalog_model_selection': catalog_model.model_dump(mode='json')} if catalog_model is not None else {})} if use_directory_catalog else {})})
            file_metadata = _save_local_source_file(record, content)
            queued = self.store.save_source(record.model_copy(update={'metadata': {**record.metadata, **file_metadata, 'content_hash': content_hash, 'native_index_version': 1}}))
            self._start_job(queued, adapter=str(queued.metadata.get('adapter') or 'openclass_native'), phase='uploaded', progress=15)
            current = self.store.get_source(owner_user_id=owner_user_id, package_id=package.id, source_id=queued.id) or queued
            return self._attach_job(self.structure_store.attach_summary(current))
        if not use_directory_catalog:
            return create_source()
        with _source_content_processing_slot(database_path=self.store.path, owner_user_id=owner_user_id, package_id=package.id, content_identity=content_hash):
            existing = next((candidate for candidate in self.store.list_sources(owner_user_id=owner_user_id, package_id=package.id) if candidate.source_type == source_type and str(candidate.metadata.get('content_hash') or '') == content_hash), None)
            if existing is None:
                return create_source()
            ai_usage_logger.log_event('source_ingestion_dedupe_hit', owner_user_id=owner_user_id, package_id=package.id, source_ingestion_id=existing.id, existing_status=existing.status, source_type=source_type)
            if existing.status in {'failed', 'ready', 'linear_only'}:
                if existing.status in {'ready', 'linear_only'}:
                    self.structure_store.delete_for_source(owner_user_id=owner_user_id, package_id=package.id, source_id=existing.id)
                file_metadata = _save_local_source_file(existing, content)
                refreshed_metadata = {key: value for key, value in existing.metadata.items() if key not in {'donor_source_ingestion_id', 'catalog_authority', 'processing_mode', 'force_ai_directory'}}
                existing = self.store.save_source(existing.model_copy(update={'status': 'parsing', 'error': '', 'metadata': {**refreshed_metadata, **file_metadata, 'adapter': directory_schema, 'catalog_pipeline': directory_schema, 'content_hash': content_hash, **({'catalog_model_selection': catalog_model.model_dump(mode='json')} if catalog_model is not None else {})}}))
                self._start_job(existing, adapter=directory_schema, phase='uploaded', progress=15)
                existing = self.store.get_source(owner_user_id=owner_user_id, package_id=package.id, source_id=existing.id) or existing
            return self._attach_job(self.structure_store.attach_summary(existing))

    def process_file_source(self, *, owner_user_id: str, package_id: str, source_id: str) -> SourceIngestionRecord:
        record = self.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
        if record is None:
            raise SourceIngestionError('Source not found.')
        with _directory_catalog_processing_slot(database_path=self.store.path, record=record):
            current = self.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
            if current is None:
                raise SourceIngestionError('Source not found.')
            if current.metadata.get('source_backend') in {'google', 'local'}:
                current = self.google_service.work_source(current)
                if current.status in {'ready', 'failed', 'cancelled'}:
                    return self._attach_job(self.structure_store.attach_summary(current))
                return self._process_file_source_unlocked(current)
            current_job = self.job_store.latest_for_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
            if current.status == 'ready' and current_job is not None and (current_job.status in {'parsing', 'indexing'}) and current_job.phase_history and (current_job.phase_history[-1] == 'source_agent_working'):
                return self._save_and_index(current, current_job, rebuild=True)
            if current.status in {'ready', 'failed'}:
                return self._attach_job(self.structure_store.attach_summary(current))
            return self._process_file_source_unlocked(current)

    def _process_file_source_unlocked(self, record: SourceIngestionRecord) -> SourceIngestionRecord:
        if record.metadata.get('source_backend') in {'google', 'local'}:
            from openclass_core.services.google_sources.catalog_fallback import process_with_catalog_recovery
            return process_with_catalog_recovery(self, record)
        owner_user_id = record.owner_user_id
        package_id = record.package_id
        source_id = record.id
        use_directory_catalog = _uses_directory_catalog(record)
        if use_directory_catalog:
            record = _as_directory_catalog_record(record)
        elif self.source_backend == 'native':
            record = _detach_open_notebook_state(record)
        job = self.job_store.latest_for_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
        if job is None:
            job = self._start_job(record, adapter=str(record.metadata.get('adapter') or 'openclass_native'), phase='parsing', progress=20)
        else:
            job = self._update_job(job, record=record, status='parsing', progress=20, phase='parsing')
        file_metadata = dict(record.metadata)
        if record.source_type in {'audio_file', 'video_file'}:
            original_path = Path(str(file_metadata.get('local_source_path') or ''))
            if self.media_transcription_provider is None:
                error = 'Media transcription requires an explicitly configured adapter.'
                failed = self.store.save_source(self._failed_record(record.model_copy(update={'metadata': {**record.metadata, **file_metadata}}), error, phase='transcription'))
                self._finish_job(job, record=failed, status='failed', progress=100, phase='failed', error=error)
                return self._attach_job(self.structure_store.attach_summary(failed))
            try:
                transcript = self.media_transcription_provider.transcribe(original_path, mime_type=record.mime_type)
            except Exception as exc:
                failed = self.store.save_source(self._failed_record(record.model_copy(update={'metadata': {**record.metadata, **file_metadata}}), str(exc), phase='transcription'))
                self._finish_job(job, record=failed, status='failed', progress=100, phase='failed', error=str(exc))
                return self._attach_job(self.structure_store.attach_summary(failed))
            transcript_record = record.model_copy(update={'file_name': f'{Path(record.file_name).stem}-transcript.txt', 'mime_type': 'text/plain'})
            transcript_metadata = _save_local_source_text(transcript_record, transcript.text)
            file_metadata = {**transcript_metadata, 'original_source_path': str(original_path), 'original_mime_type': record.mime_type, 'transcription_provider': transcript.provider, 'transcription_model': transcript.model, 'transcript_language': transcript.language}
            record = transcript_record
        ready = record.model_copy(update={'status': 'parsing', 'metadata': {**record.metadata, **file_metadata, 'native_index_version': 1}})
        if self.source_backend == 'open_notebook' and (not use_directory_catalog):
            synced = self.open_notebook_backend.sync_file(ready)
            if synced.status == 'failed':
                self._finish_job(job, record=synced, status='failed', progress=100, phase='failed', error=synced.error)
                return self._attach_job(self.structure_store.attach_summary(synced))
            self._update_open_notebook_job(job, synced)
            return self._attach_job(self.structure_store.attach_summary(synced))
        return self._save_and_index(ready, job)

    def add_text_source(self, *, owner_user_id: str, package: CoursePackage, text: str, title: str='') -> SourceIngestionRecord:
        content = text.strip()
        if self.source_backend in {'google', 'local'}:
            queued = self.queue_file_source(owner_user_id=owner_user_id, package=package, file_name='pasted-text.md', content=content.encode('utf-8'), mime_type='text/markdown', title=title or _text_title(content))
            source_ingestion_task_manager.submit(owner_user_id=owner_user_id, package_id=package.id, source_id=queued.id)
            return queued
        if not content:
            raise SourceIngestionError('Pasted text is empty.')
        record = SourceIngestionRecord(owner_user_id=owner_user_id, package_id=package.id, title=title.strip() or _text_title(content), source_type='pasted_text', file_name='pasted-text.md', mime_type='text/markdown', size_bytes=len(content.encode('utf-8')), status='queued' if self.source_backend == 'open_notebook' else 'ready', metadata={'adapter': 'open_notebook' if self.source_backend == 'open_notebook' else 'openclass_native_text', 'native_index_version': 1, 'package_title': package.title})
        metadata = _save_local_source_text(record, content)
        record = record.model_copy(update={'metadata': {**record.metadata, **metadata, 'content_hash': hashlib.sha256(content.encode('utf-8')).hexdigest()}})
        if self.source_backend == 'open_notebook':
            record = self.open_notebook_backend.sync_file(record)
            if record.status == 'failed':
                job = self._start_job(record, adapter='open_notebook', phase='failed', progress=100)
                return self._attach_job(self.structure_store.attach_summary(record))
            job = self._start_job(record, adapter='open_notebook', phase='open_notebook_processing', progress=35)
            self._update_open_notebook_job(job, record)
            return self._attach_job(self.structure_store.attach_summary(record))
        job = self._start_job(record, adapter=str(record.metadata.get('adapter') or 'openclass_native_text'), phase='parsing', progress=25)
        return self._save_and_index(record, job)

    def remove_source(self, *, owner_user_id: str, package_id: str, source_id: str, defer_cleanup: bool=False) -> SourceIngestionRecord | None:
        record = self.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
        if record is None:
            return None
        package_id = record.package_id
        if record.metadata.get('source_backend') in {'google', 'local'}:
            self.google_service.state.queue_cleanup(record)
        removed = self.store.delete_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
        if removed is None:
            return None
        cancel_started_at = time.monotonic()
        cancelled_synchronously = source_ingestion_task_manager.cancel((owner_user_id, package_id, source_id), timeout_seconds=0.0 if defer_cleanup else 5.0)
        ai_usage_logger.log_event('source_ingestion_cancelled', owner_user_id=owner_user_id, package_id=package_id, source_ingestion_id=source_id, cancelled_synchronously=cancelled_synchronously, cancel_duration_ms=max(0, round((time.monotonic() - cancel_started_at) * 1000)))
        if defer_cleanup:
            return removed
        return self._remove_source_unlocked(record, removed=removed)

    def remove_sources(self, *, owner_user_id: str, package_id: str, source_ids: list[str], defer_cleanup: bool=True) -> tuple[list[SourceIngestionRecord], list[tuple[str, str]]]:
        removed: list[SourceIngestionRecord] = []
        failed: list[tuple[str, str]] = []
        seen: set[str] = set()
        for source_id in source_ids:
            if not source_id or source_id in seen:
                continue
            seen.add(source_id)
            try:
                record = self.remove_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id, defer_cleanup=defer_cleanup)
            except Exception as exc:
                failed.append((source_id, str(exc) or 'Source removal failed.'))
                continue
            if record is None:
                failed.append((source_id, 'Source not found.'))
                continue
            removed.append(record)
        return (removed, failed)

    def cleanup_removed_source(self, record: SourceIngestionRecord) -> None:
        self._remove_source_unlocked(record, removed=record)

    def cleanup_removed_sources(self, records: list[SourceIngestionRecord]) -> None:
        for record in records:
            self.cleanup_removed_source(record)

    def _remove_source_unlocked(self, record: SourceIngestionRecord, *, removed: SourceIngestionRecord | None=None) -> SourceIngestionRecord | None:
        if record.metadata.get('source_backend') in {'google', 'local'}:
            self.google_service.delete(record)
        if self.source_backend == 'open_notebook' and record.open_notebook_source_id:
            try:
                self.adapter.delete_source(record.open_notebook_source_id)
            except OpenNotebookAdapterError:
                pass
        self.structure_store.delete_for_source(owner_user_id=record.owner_user_id, package_id=record.package_id, source_id=record.id)
        if record.source_type == 'code_repository':
            self.repository_store.delete_source(record.id)
        if removed is None:
            removed = self.store.delete_source(owner_user_id=record.owner_user_id, package_id=record.package_id, source_id=record.id)
        self.job_store.delete_for_source(owner_user_id=record.owner_user_id, package_id=record.package_id, source_id=record.id)
        _delete_local_source_file(record)
        return removed

    def rename_source(self, *, owner_user_id: str, package_id: str, source_id: str, title: str) -> SourceIngestionRecord | None:
        record = self.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
        normalized = title.strip()
        if record is None or not normalized:
            return None
        if supports_directory_catalog(record):
            with _directory_catalog_processing_slot(database_path=self.store.path, record=record):
                current = self.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
                if current is None:
                    return None
                return self._rename_source_unlocked(current, normalized)
        return self._rename_source_unlocked(record, normalized)

    def _rename_source_unlocked(self, record: SourceIngestionRecord, title: str) -> SourceIngestionRecord:
        return self.structure_store.attach_summary(self.store.save_source(record.model_copy(update={'title': title})))

    def update_source_content(self, *, owner_user_id: str, package_id: str, source_id: str, content: str) -> SourceIngestionRecord | None:
        record = self.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
        if record is None:
            return None
        if record.source_type == 'code_repository':
            raise SourceIngestionError('Repository snapshots are immutable; create a new snapshot instead.')
        normalized = content.strip()
        if not normalized:
            raise SourceIngestionError('Source content cannot be empty.')
        if supports_directory_catalog(record):
            with _directory_catalog_processing_slot(database_path=self.store.path, record=record):
                current = self.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
                if current is None:
                    return None
                return self._update_source_content_unlocked(current, normalized)
        return self._update_source_content_unlocked(record, normalized)

    def _update_source_content_unlocked(self, record: SourceIngestionRecord, normalized: str) -> SourceIngestionRecord:
        if record.metadata.get('source_backend') in {'google', 'local'}:
            from openclass_core.services.google_sources.content_edit import update_text_source
            return update_text_source(self, record, normalized)
        current_path = source_local_path(record)
        metadata = dict(record.metadata)
        is_native_text = record.source_type == 'pasted_text'
        if not is_native_text and 'original_source_path' not in metadata and (current_path is not None):
            metadata['original_source_path'] = str(current_path)
            metadata['original_mime_type'] = record.mime_type
            metadata['original_file_name'] = record.file_name
        editable_record = record.model_copy(update={'file_name': record.file_name if is_native_text else f'{Path(record.file_name or record.id).stem}-edited.md', 'mime_type': 'text/markdown', 'size_bytes': len(normalized.encode('utf-8')), 'status': 'parsing', 'error': '', 'metadata': metadata})
        local_metadata = _save_local_source_text(editable_record, normalized)
        editable_record = editable_record.model_copy(update={'metadata': {**metadata, **local_metadata, 'adapter': 'openclass_native_text', 'content_hash': hashlib.sha256(normalized.encode('utf-8')).hexdigest(), 'content_edited': True, 'native_index_version': 1}})
        use_directory_catalog = _uses_directory_catalog(editable_record)
        if use_directory_catalog:
            editable_record = _as_directory_catalog_record(editable_record)
        job = self._start_job(editable_record, adapter=CATALOG_SCHEMA_VERSION if use_directory_catalog else 'open_notebook' if self.source_backend == 'open_notebook' else 'openclass_native_text', phase='parsing', progress=25)
        if self.source_backend == 'open_notebook' and (not use_directory_catalog):
            editable_record = self.open_notebook_backend.replace_file(editable_record)
            if editable_record.status == 'failed':
                self._finish_job(job, record=editable_record, status='failed', progress=100, phase='failed', error=editable_record.error)
                return self._attach_job(self.structure_store.attach_summary(editable_record))
            self.structure_store.delete_for_source(owner_user_id=record.owner_user_id, package_id=record.package_id, source_id=record.id)
            self._update_open_notebook_job(job, editable_record)
            return self._attach_job(self.structure_store.attach_summary(editable_record))
        return self._save_and_index(editable_record, job, rebuild=True)

    def retry_source(self, *, owner_user_id: str, package_id: str, source_id: str) -> SourceIngestionRecord | None:
        record = self.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
        if record is None:
            return None
        with _directory_catalog_processing_slot(database_path=self.store.path, record=record):
            current = self.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
            if current is None:
                return None
            return self._retry_source_unlocked(current)

    def cancel_source(self, *, owner_user_id: str, package_id: str, source_id: str) -> SourceIngestionRecord | None:
        record = self.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
        if record is None:
            return None
        package_id = record.package_id
        active_statuses = {'queued', 'fetching', 'parsing', 'indexing'}
        if record.metadata.get('source_backend') in {'google', 'local'}:
            source_ingestion_task_manager.cancel((owner_user_id, package_id, source_id), timeout_seconds=5.0)
            return self.google_service.cancel(record)
        if record.status not in active_statuses:
            return self._attach_job(self.structure_store.attach_summary(record))
        source_ingestion_task_manager.cancel((owner_user_id, package_id, source_id), timeout_seconds=5.0)
        current = self.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
        if current is None:
            return None
        if current.status == 'ready':
            return self._attach_job(self.structure_store.attach_summary(current))
        event = AgentActivityEvent(turn_id=f'catalog-cancel:{source_id}', stage='execute_role', label='Source processing cancelled', status='cancelled', role='OpenClass', metadata={'kind': 'sourceCatalogStage', 'detail': 'Source processing was stopped by the user. The original file and existing catalog records have been preserved.'})
        job = self.job_store.latest_for_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
        cancelled = current.model_copy(update={'status': 'cancelled', 'error': ''})
        cancelled = self.store.save_source(cancelled)
        if job is not None:
            activities = [item for item in job.agent_activity if item.id != event.id]
            self._update_job(job.model_copy(update={'cancel_requested': False}), record=cancelled, status='cancelled', progress=100, phase='cancelled', agent_activity=[*activities, event])
        return self._attach_job(self.structure_store.attach_summary(cancelled))

    def _retry_google_source(self, record: SourceIngestionRecord, catalog_model=None):
        from openclass_core.services.google_sources.retry import retry_source
        return retry_source(self, record, catalog_model)

    def _run_google_source_retry(self, record: SourceIngestionRecord, catalog_model=None):
        service = self.google_service
        candidate = service.work_source(record)
        if candidate.metadata.get('catalog_refinement', {}).get('requested'):
            return service.process(candidate)
        metadata = {'catalog_model_selection': catalog_model.model_dump(mode='json')} if catalog_model is not None else {}
        if candidate.metadata.get('processing_mode') == 'ordinary':
            if metadata:
                candidate = service.state.update(candidate, active=False, metadata=metadata)
            return service.process(candidate)
        if not service.cloud_enabled(candidate):
            if metadata:
                candidate = service.state.update(candidate, active=False, metadata=metadata)
            return service.process_original_catalog(candidate)
        if candidate.status == 'ready' and candidate.metadata.get('search_ready'):
            from openclass_core.services.google_sources.catalog_inputs import rebuild_inputs
            from openclass_core.services.google_sources.catalog_publication import build_source_catalog
            from openclass_core.services.google_sources.config import GoogleSourceError, GoogleSourceUnavailableError
            try:
                service.config.validate(write=True)
                artifact = service.artifact(candidate)
                from openclass_core.services.google_sources.catalog_fallback import ensure_original_cache
                candidate, original = ensure_original_cache(service, candidate)
                raw = original.read_bytes()
                if hashlib.sha256(raw).hexdigest() != candidate.metadata['content_hash']:
                    raise GoogleSourceError('The original-file cache hash has changed. Read the source again.')
                candidate = service.state.update(candidate, metadata={'update_status': 'parsing', 'update_error': ''})
                previous_job = service.job_store.latest_for_source(owner_user_id=candidate.owner_user_id, package_id=candidate.package_id, source_id=candidate.id)
                progress = previous_job.progress if previous_job is not None and previous_job.status in {'queued', 'fetching', 'parsing', 'indexing'} else 0
                service._job(candidate.model_copy(update={'status': 'parsing'}), 'catalog_generation', progress)
                metadata['catalog_version'] = int(candidate.metadata.get('catalog_version', 0)) + 1
                candidate = candidate.model_copy(update={'metadata': {**candidate.metadata, **metadata}})
                structure, chapters, catalog_metadata = build_source_catalog(service, candidate, rebuild_inputs(service, candidate, artifact), artifact['parts'])
                metadata.update(catalog_metadata)
            except GoogleSourceError as exc:
                if not service.cloud_enabled(candidate):
                    return service.process_original_catalog(service.state.current(candidate))
                if isinstance(exc, GoogleSourceUnavailableError):
                    from openclass_core.services.google_sources.catalog_fallback import build_fallback_catalog
                    return build_fallback_catalog(self, service.state.current(candidate), exc)
                raise
            service._check(candidate)
            if not service.cloud_enabled(candidate):
                return service.process_original_catalog(service.state.current(candidate))
            ready = service.state.publish(candidate, structure_store=self.structure_store, structure=structure, chapters=chapters, metadata={**metadata, 'catalog_mode': 'cloud', 'original_read_ready': True, 'catalog_status': structure.metadata['catalog_status'], 'catalog_ready': structure.metadata['catalog_status'] == 'ready'})
            service._job(ready, 'ready', 100)
            return ready
        candidate = service.state.update(candidate, active=False, status='queued', error='', metadata=metadata)
        return self._process_file_source_unlocked(candidate)

    def _retry_source_unlocked(self, record: SourceIngestionRecord) -> SourceIngestionRecord | None:
        if record.metadata.get('source_backend') in {'google', 'local'}:
            return self._retry_google_source(record)
        if record.source_type == 'code_repository':
            raise SourceIngestionError('Repository snapshots are immutable; create a new snapshot instead.')
        record = _repair_local_source_storage(record)
        local_path = source_local_path(record)
        if local_path is None:
            raise SourceIngestionError('Source content is unavailable; import the source again.')
        retrying = record.model_copy(update={'status': 'queued', 'error': ''})
        use_directory_catalog = _uses_directory_catalog(retrying)
        if use_directory_catalog:
            retrying = _as_directory_catalog_record(retrying)
        elif self.source_backend == 'native':
            retrying = _detach_open_notebook_state(retrying)
        self.store.save_source(retrying)
        job = self._start_job(retrying, adapter=CATALOG_SCHEMA_VERSION if use_directory_catalog else 'open_notebook' if self.source_backend == 'open_notebook' else str(record.metadata.get('adapter') or 'openclass_native'), phase='parsing' if use_directory_catalog else 'queued' if self.source_backend == 'open_notebook' else 'parsing', progress=20)
        if self.source_backend == 'open_notebook' and (not use_directory_catalog):
            retrying = self.open_notebook_backend.replace_file(retrying)
            if retrying.status == 'failed':
                self._finish_job(job, record=retrying, status='failed', progress=100, phase='failed', error=retrying.error)
                return self._attach_job(self.structure_store.attach_summary(retrying))
            self.structure_store.delete_for_source(owner_user_id=record.owner_user_id, package_id=record.package_id, source_id=record.id)
            self._update_open_notebook_job(job, retrying)
            return self._attach_job(self.structure_store.attach_summary(retrying))
        return self._save_and_index(retrying, job, rebuild=True)

    def rebuild_catalog(self, *, owner_user_id: str, package_id: str, source_id: str, catalog_model: AIModelSelection | None=None) -> SourceIngestionRecord | None:
        record = self.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
        if record is None:
            return None
        with _directory_catalog_processing_slot(database_path=self.store.path, record=record):
            current = self.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
            if current is None:
                return None
            return self._rebuild_catalog_unlocked(current, catalog_model=catalog_model)

    def _rebuild_catalog_unlocked(self, record: SourceIngestionRecord, *, catalog_model: AIModelSelection | None=None) -> SourceIngestionRecord:
        if not supports_directory_catalog(record):
            raise SourceIngestionError('This source format does not support a directory catalog.')
        if record.metadata.get('source_backend') in {'google', 'local'}:
            return self._retry_google_source(record, catalog_model)
        record = _repair_local_source_storage(record)
        local_path = source_local_path(record)
        if local_path is None:
            raise SourceIngestionError('Source content is unavailable; import the source again.')
        selected_model = catalog_model or _catalog_model_for_record(record)
        rebuilding = _as_directory_catalog_record(record).model_copy(update={'status': 'parsing', 'error': '', 'metadata': {**{key: value for key, value in record.metadata.items() if key not in {'donor_source_ingestion_id', 'catalog_authority', 'processing_mode', 'force_ai_directory'}}, 'catalog_pipeline': CATALOG_SCHEMA_VERSION, 'catalog_model_selection': selected_model.model_dump(mode='json') if selected_model else None}})
        self.store.save_source(rebuilding)
        job = self._start_job(rebuilding, adapter=CATALOG_SCHEMA_VERSION, phase='reading_native_directory_index', progress=20)
        return self._save_and_index(rebuilding, job, rebuild=True)

    def list_jobs(self, *, owner_user_id: str, package_id: str) -> list[SourceIngestionJob]:
        sources = self.store.list_sources(owner_user_id=owner_user_id, package_id=package_id)
        allowed_ids = {source.id for source in sources}
        jobs = [job for storage_package in {source.package_id for source in sources} for job in self.job_store.list(owner_user_id=owner_user_id, package_id=storage_package) if job.resource_id in allowed_ids]
        return sorted(jobs, key=lambda job: job.updated_at, reverse=True)

    def source_content(self, *, owner_user_id: str, package_id: str, source_id: str) -> tuple[SourceIngestionRecord, str] | None:
        record = self.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
        if record is None:
            return None
        if record.metadata.get('source_backend') in {'google', 'local'}:
            if record.mime_type.startswith('text/') and record.mime_type != 'text/html':
                _, content = self.google_service.download(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
                return (record, content.decode('utf-8', errors='strict'))
            return (record, '')
        path = source_local_path(record)
        if path is None:
            return (record, '')
        if record.mime_type.startswith('text/') and record.mime_type != 'text/html':
            return (record, path.read_text(encoding='utf-8', errors='replace'))
        view = self.structure_store.get_structure_view(source=record, chunk_limit=5000)
        return (record, '\n\n'.join((chunk.text for chunk in view.chunks)))

    def search_open_notebook(self, *, owner_user_id: str, package_id: str, query: str, limit: int=8, source_ids: list[str] | None=None) -> list[dict[str, object]]:
        """Search only the Open Notebook index for an explicitly scoped request."""
        if self.source_backend != 'open_notebook' or not query.strip():
            return []
        records = self.store.ready_sources(owner_user_id=owner_user_id, package_id=package_id)
        records = [record for record in records if not _uses_directory_catalog(record)]
        try:
            return self.open_notebook_backend.search(owner_user_id=owner_user_id, package_id=package_id, query=query, records=records, limit=limit, source_ids=source_ids)
        except RuntimeError as exc:
            raise SourceIngestionError(str(exc)) from exc

    def _add_url_source_open_notebook(self, *, owner_user_id: str, package: CoursePackage, source_uri: str, title: str) -> SourceIngestionRecord:
        display_title = title.strip() or source_uri
        record = SourceIngestionRecord(owner_user_id=owner_user_id, package_id=package.id, title=display_title, source_type='web_url', source_uri=source_uri, file_name='', mime_type='text/html', size_bytes=0, status='queued', metadata={'adapter': 'open_notebook', 'package_title': package.title})
        job = self._start_job(record, adapter='open_notebook', phase='queued', progress=10)
        notebook_id, error = self.open_notebook_backend.resolve_notebook(owner_user_id=owner_user_id, package_id=package.id, package_title=package.title)
        if error:
            failed = self.store.save_source(self.open_notebook_backend.failed_record(record, error, phase='create_notebook'))
            self._finish_job(job, record=failed, status='failed', progress=100, phase='failed', error=error)
            return self._attach_job(self.structure_store.attach_summary(failed))
        try:
            result = self.adapter.add_url_source(notebook_id=notebook_id, source_uri=source_uri, title=display_title)
        except OpenNotebookAdapterError as exc:
            error = self.open_notebook_backend.format_error(exc)
            failed = self.store.save_source(self.open_notebook_backend.failed_record(record, error, phase='add_source'))
            self._finish_job(job, record=failed, status='failed', progress=100, phase='failed', error=error)
            return self._attach_job(self.structure_store.attach_summary(failed))
        remote_status = status_from_open_notebook(result.status)
        metadata: dict[str, object] = {**record.metadata, 'source_processing_owner': 'open_notebook', 'open_notebook_sync_status': remote_status, 'open_notebook_response': result.raw or {}}
        saved = self.store.save_source(record.model_copy(update={'status': remote_status, 'open_notebook_notebook_id': notebook_id, 'open_notebook_source_id': result.source_id, 'open_notebook_command_id': result.command_id, 'metadata': metadata}))
        self._update_open_notebook_job(job, saved)
        return self._attach_job(saved)

    def _refresh_open_notebook_source(self, record: SourceIngestionRecord) -> SourceIngestionRecord:
        if record.open_notebook_source_id and record.metadata.get('source_processing_owner') != 'open_notebook':
            record = self.store.save_source(record.model_copy(update={'metadata': {**record.metadata, 'source_processing_owner': 'open_notebook'}}))
        refreshed = self.open_notebook_backend.refresh(record, mirror_status=True)
        job = self.job_store.latest_for_source(owner_user_id=refreshed.owner_user_id, package_id=refreshed.package_id, source_id=refreshed.id)
        if job is not None:
            self._update_open_notebook_job(job, refreshed)
        return refreshed

    def _update_open_notebook_job(self, job: SourceIngestionJob, record: SourceIngestionRecord) -> None:
        if record.status == 'ready':
            phase, progress = ('ready', 100)
        elif record.status == 'failed':
            phase, progress = ('failed', 100)
        else:
            phase, progress = ('open_notebook_processing', 35)
        self._update_job(job, record=record, status=record.status, progress=progress, phase=phase, error=record.error)

    def _start_job(self, record: SourceIngestionRecord, *, adapter: str, phase: str, progress: int) -> SourceIngestionJob:
        job_id = new_id('ingest')
        job = self.job_store.save(SourceIngestionJob(id=job_id, run_id=job_id, resource_id=record.id, source_type=record.source_type, source_uri=record.source_uri, adapter=adapter, status=record.status, progress=progress, phase_history=[phase]), owner_user_id=record.owner_user_id, package_id=record.package_id)
        update_current_source_task_run_id(job.id)
        if adapter in {CATALOG_SCHEMA_VERSION, PREVIOUS_CATALOG_SCHEMA_VERSION, *LEGACY_CATALOG_SCHEMA_VERSIONS}:
            self.store.save_source(record.model_copy(update={'metadata': {**record.metadata, 'active_catalog_run_id': job.id}}))
        return job

    def _finish_job(self, job: SourceIngestionJob, *, record: SourceIngestionRecord, status: str, progress: int, phase: str, error: str='') -> SourceIngestionJob:
        return self._update_job(job, record=record, status=status, progress=progress, phase=phase, error=error)

    def _update_job(self, job: SourceIngestionJob, *, record: SourceIngestionRecord, status: str, progress: int, phase: str, error: str='', agent_activity: list[AgentActivityEvent] | None=None) -> SourceIngestionJob:
        current = self.store.get_source(owner_user_id=record.owner_user_id, package_id=record.package_id, source_id=record.id)
        if current is None:
            return job
        if job.adapter in {CATALOG_SCHEMA_VERSION, PREVIOUS_CATALOG_SCHEMA_VERSION, *LEGACY_CATALOG_SCHEMA_VERSIONS}:
            active_run_id = str(current.metadata.get('active_catalog_run_id') or '')
            if active_run_id and active_run_id != (job.run_id or job.id):
                return job
        phases = job.phase_history
        if not phases or phases[-1] != phase:
            phases = [*phases, phase]
        activities = job.agent_activity if agent_activity is None else agent_activity
        heartbeat_at = job.heartbeat_at
        for event in reversed(activities):
            source_progress = event.metadata.get('source_progress')
            if not isinstance(source_progress, dict):
                continue
            candidate = source_progress.get('heartbeat_at')
            if isinstance(candidate, str) and candidate.strip():
                heartbeat_at = candidate
                break
        return self.job_store.save(job.model_copy(update={'status': status, 'progress': max(job.progress, min(100, progress)), 'error': error, 'phase_history': phases, 'agent_activity': activities, 'heartbeat_at': heartbeat_at}), owner_user_id=record.owner_user_id, package_id=record.package_id)

    def _attach_job(self, record: SourceIngestionRecord) -> SourceIngestionRecord:
        job = self.job_store.latest_for_source(owner_user_id=record.owner_user_id, package_id=record.package_id, source_id=record.id)
        return record.model_copy(update={'ingestion_job': job})

    def _save_and_index(self, record: SourceIngestionRecord, job: SourceIngestionJob, *, rebuild: bool=False) -> SourceIngestionRecord:
        if not _uses_directory_catalog(record):
            return self._save_and_index_unlocked(record, job, rebuild=rebuild)
        with _directory_catalog_processing_slot(database_path=self.store.path, record=record):
            current = self.store.get_source(owner_user_id=record.owner_user_id, package_id=record.package_id, source_id=record.id)
            if current is None:
                raise SourceIngestionError('Source was removed before directory processing could start.')
            try:
                with source_ingestion_task_manager.track_running(key=(current.owner_user_id, current.package_id, current.id), run_id=job.run_id or job.id, job_store=self.job_store):
                    return self._save_and_index_unlocked(current, job, rebuild=rebuild)
            except SourceTaskNotActiveError as exc:
                raise SourceIngestionError(str(exc)) from exc

    def _save_and_index_unlocked(self, record: SourceIngestionRecord, job: SourceIngestionJob, *, rebuild: bool=False) -> SourceIngestionRecord:
        use_directory_catalog = _uses_directory_catalog(record)
        previous_structure = self.structure_store.get_structure(owner_user_id=record.owner_user_id, package_id=record.package_id, source_id=record.id) if use_directory_catalog else None
        saved = self.store.save_source(record.model_copy(update={'status': 'parsing', 'error': ''}))
        indexing_job = self._update_job(job, record=saved, status='parsing', progress=25, phase='parsing')
        activity_by_id: dict[str, AgentActivityEvent] = {}
        activity_order: list[str] = []
        codex_progress_tracker: SourceCodexProgressTracker | None = None
        catalog_start_activity: AgentActivityEvent | None = None

        def raise_if_cancelled() -> None:
            if source_task_cancel_requested():
                raise SourceIngestionError('Source ingestion was cancelled by the user.')

        def report_progress(phase: str, progress: int) -> None:
            nonlocal saved, indexing_job
            raise_if_cancelled()
            next_status = 'indexing' if progress >= 60 else 'parsing'
            if saved.status != next_status:
                saved = self.store.save_source(saved.model_copy(update={'status': next_status}))
            indexing_job = self._update_job(indexing_job, record=saved, status=next_status, progress=progress, phase=phase, agent_activity=[activity_by_id[event_id] for event_id in activity_order])

        def report_codex_activity(event: AgentActivityEvent) -> None:
            nonlocal saved, indexing_job
            if event.status != 'cancelled':
                raise_if_cancelled()
            progress = indexing_job.progress
            phase = indexing_job.phase_history[-1] if indexing_job.phase_history else 'parsing'
            if codex_progress_tracker is not None:
                observation = codex_progress_tracker.observe(event)
                event = observation.event
                progress = observation.progress
                phase = observation.phase
            next_status = 'indexing' if progress >= 60 else 'parsing'
            if saved.status != next_status:
                saved = self.store.save_source(saved.model_copy(update={'status': next_status}))
            if event.id not in activity_by_id:
                activity_order.append(event.id)
            activity_by_id[event.id] = event
            current_activity = [activity_by_id[event_id] for event_id in activity_order]
            indexing_job = self._update_job(indexing_job, record=saved, status=next_status, progress=progress, phase=phase, agent_activity=current_activity)
        try:
            if use_directory_catalog:
                local_path = source_local_path(saved)
                if local_path is None:
                    raise SourceIngestionError('Source file is unavailable for directory cataloging.')
                catalog_model = _catalog_model_for_record(saved) or default_text_selection()
                catalog_start_activity = AgentActivityEvent(turn_id=f'catalog-start:{saved.id}', stage='execute_role', label='Source agent is reading the file structure', status='running', role='pi', metadata={'kind': 'sourceCatalogStage', 'source_progress': {'phase': 'directory_discovery', 'label': 'Source agent is reading the file structure', 'detail': 'Checking page count, native table of contents, and available navigation structure', 'determinate': False, 'heartbeat_at': now_iso()}})
                report_codex_activity(catalog_start_activity)
                codex_progress_tracker = SourceCodexProgressTracker(local_path)
                report_codex_activity(catalog_start_activity)
                structure = self.directory_processor.process(record=saved, path=local_path, catalog_model=catalog_model, progress_callback=report_progress, activity_callback=report_codex_activity)
                raise_if_cancelled()
                report_codex_activity(catalog_start_activity.model_copy(update={'label': 'Source agent finished reading the file structure', 'status': 'completed', 'metadata': {'kind': 'sourceCatalogStage', 'detail': 'The catalog has been verified and saved. You can expand and cite chapters.'}}))
            else:
                structure = self.structure_indexer.rebuild_structure(saved, progress_callback=report_progress) if rebuild else self.structure_indexer.ensure_structure(saved, progress_callback=report_progress)
                raise_if_cancelled()
            if self.store.get_source(owner_user_id=saved.owner_user_id, package_id=saved.package_id, source_id=saved.id) is None:
                raise SourceIngestionError('Source was removed during processing.')
        except Exception as exc:
            cancelled_by_user = source_task_cancel_requested()
            interrupted = not cancelled_by_user and _source_exception_is_interruption(exc)
            if catalog_start_activity is not None:
                report_codex_activity(catalog_start_activity.model_copy(update={'label': 'Source agent file structure reading cancelled' if cancelled_by_user else 'Source agent task interrupted' if interrupted else 'Source agent file structure reading failed', 'status': 'cancelled' if cancelled_by_user else 'failed', 'metadata': {'kind': 'sourceCatalogStage', 'interrupted': interrupted, 'detail': 'Source processing was stopped by the user.' if cancelled_by_user else str(exc)}}))
            if self.store.get_source(owner_user_id=saved.owner_user_id, package_id=saved.package_id, source_id=saved.id) is None:
                self.structure_store.delete_for_source(owner_user_id=saved.owner_user_id, package_id=saved.package_id, source_id=saved.id)
                self.job_store.delete_for_source(owner_user_id=saved.owner_user_id, package_id=saved.package_id, source_id=saved.id)
                raise SourceIngestionError('Source was removed during processing.') from exc
            if cancelled_by_user:
                cancelled = saved.model_copy(update={'status': 'cancelled', 'error': ''})
                self._update_job(indexing_job.model_copy(update={'cancel_requested': False}), record=cancelled, status='cancelled', progress=100, phase='cancelled', agent_activity=[activity_by_id[event_id] for event_id in activity_order])
                cancelled = self.store.save_source(cancelled)
                return self._attach_job(self.structure_store.attach_summary(cancelled))
            if interrupted:
                message = 'The source agent task was interrupted. The original file and existing catalog have been preserved. Refresh to run it again.'
                interrupted_record = saved.model_copy(update={'status': 'interrupted', 'error': message})
                self._update_job(indexing_job, record=interrupted_record, status='interrupted', progress=indexing_job.progress, error=message, phase='interrupted', agent_activity=[activity_by_id[event_id] for event_id in activity_order])
                interrupted_record = self.store.save_source(interrupted_record)
                return self._attach_job(self.structure_store.attach_summary(interrupted_record))
            preserve_previous_catalog = previous_structure is not None and previous_structure.status in {'ready', 'linear_only'} and (Path(saved.file_name or '').suffix.lower() != '.pdf' or (previous_structure.metadata.get('index_status') == 'complete' and int(previous_structure.metadata.get('unresolved_node_count') or 0) == 0) or (not previous_structure.metadata.get('index_status') and previous_structure.chapter_count > 0 and (previous_structure.quality.unverified_chapter_count == 0)))
            if preserve_previous_catalog:
                self.structure_store.record_rebuild_failure(structure=previous_structure, error=str(exc))
            failed = saved.model_copy(update={'status': 'ready' if preserve_previous_catalog else 'failed', 'error': '' if preserve_previous_catalog else str(exc)})
            self._update_job(indexing_job, record=failed, status='failed', progress=100, error=str(exc), phase='failed')
            failed = self.store.save_source(failed)
            return self._attach_job(self.structure_store.attach_summary(failed))
        final_status = 'ready' if structure.status in {'ready', 'linear_only'} else 'failed'
        error = structure.error if final_status == 'failed' else ''
        automation_outcome = SourceImportAutomationOutcome(artifact_ids=[], errors=[])
        if final_status == 'ready' and self.import_automation_runner is not None and (not use_directory_catalog):
            transforming_job = self._update_job(indexing_job, record=saved, status='indexing', progress=97, phase='transforming')
            indexing_job = transforming_job
            try:
                automation_outcome = self.import_automation_runner(owner_user_id=record.owner_user_id, package_id=record.package_id, source_ingestion_id=record.id)
            except Exception as exc:
                automation_outcome = SourceImportAutomationOutcome(artifact_ids=[], errors=[str(exc)])
            if automation_outcome.errors:
                error = 'Automatic import transformation failed: ' + '; '.join(automation_outcome.errors)
        final_metadata = {key: value for key, value in saved.metadata.items() if key != 'force_ai_directory'}
        final_record = saved.model_copy(update={'status': final_status, 'error': error, 'metadata': {**final_metadata, 'import_transformation_status': 'not_applicable' if use_directory_catalog else 'failed' if automation_outcome.errors else 'ready' if automation_outcome.artifact_ids else 'not_configured', 'import_transformation_artifact_ids': automation_outcome.artifact_ids}})
        self._update_job(indexing_job, record=final_record, status=final_status, progress=100, error=error, phase=final_status)
        final_record = self.store.save_source(final_record)
        return self._attach_job(self.structure_store.attach_summary(final_record))

    def _failed_record(self, record: SourceIngestionRecord, error: str, *, phase: str) -> SourceIngestionRecord:
        return record.model_copy(update={'status': 'failed', 'error': error, 'metadata': {**record.metadata, 'error_phase': phase}})

    def _save_youtube_transcript_source(self, *, owner_user_id: str, package: CoursePackage, source_uri: str, title: str) -> SourceIngestionRecord:
        display_title = title.strip() or source_uri
        record = SourceIngestionRecord(owner_user_id=owner_user_id, package_id=package.id, title=display_title, source_type='video_url', source_uri=source_uri, file_name='youtube-transcript.txt', mime_type='text/plain', size_bytes=0, status='fetching', metadata={'adapter': 'youtube_transcript', 'media_provider': 'youtube', 'media_kind': 'video'})
        try:
            transcript = self.youtube_adapter.extract(source_uri, title=title)
        except YouTubeTranscriptAdapterError as exc:
            failed = self.store.save_source(self._failed_record(record, str(exc), phase='youtube_transcript'))
            job = self._start_job(failed, adapter='youtube_transcript', phase='transcription', progress=100)
            self._finish_job(job, record=failed, status='failed', progress=100, phase='failed', error=str(exc))
            return failed
        transcript_file_name = _safe_file_name(f'{transcript.video_id or record.id}-transcript.txt')
        record_for_file = record.model_copy(update={'title': transcript.title, 'file_name': transcript_file_name})
        transcript_bytes = transcript.text.encode('utf-8')
        ready = record_for_file.model_copy(update={'status': 'ready', 'error': '', 'size_bytes': len(transcript_bytes), 'metadata': {**record_for_file.metadata, **transcript.metadata, **_save_local_source_text(record_for_file, transcript.text), 'content_hash': hashlib.sha256(transcript_bytes).hexdigest(), 'native_index_version': 1}})
        if self.source_backend == 'open_notebook':
            ready = ready.model_copy(update={'metadata': {**ready.metadata, 'adapter': 'open_notebook', 'package_title': package.title}})
            ready = self.open_notebook_backend.sync_file(ready)
            if ready.status == 'failed':
                job = self._start_job(ready, adapter='open_notebook', phase='failed', progress=100)
                return self._attach_job(self.structure_store.attach_summary(ready))
            job = self._start_job(ready, adapter='open_notebook', phase='open_notebook_processing', progress=35)
            self._update_open_notebook_job(job, ready)
            return self._attach_job(self.structure_store.attach_summary(ready))
        job = self._start_job(ready, adapter=str(ready.metadata.get('adapter') or 'youtube_transcript'), phase='parsing', progress=25)
        return self._save_and_index(ready, job)

def _supported_mime(mime_type: str, file_name: str) -> bool:
    lowered = file_name.lower()
    if lowered.endswith(('.pdf', '.epub', '.docx', '.pptx', '.xlsx', '.csv', '.txt', '.md', '.markdown', '.html', '.htm', '.json', '.xml', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.mp3', '.m4a', '.wav', '.ogg', '.mp4', '.mov', '.webm', '.mpeg')):
        return True
    return any(((mime_type or '').startswith(prefix) for prefix in SUPPORTED_FILE_MIME_PREFIXES))

def _supported_directory_file_name(file_name: str) -> bool:
    return Path(file_name).suffix.lower() in {'.pdf', '.epub', '.docx', '.pptx', '.xlsx', '.csv', '.txt', '.md', '.markdown', '.html', '.htm', '.json', '.xml'}

def _save_local_source_file(record: SourceIngestionRecord, content: bytes) -> dict[str, Any]:
    if record.metadata.get('source_backend') in {'google', 'local'}:
        from openclass_core.services.google_sources.original_cache import cache_original
        return cache_original(record, content)
    source_dir = workspace_state.UPLOAD_DIR / 'sources'
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / _stored_source_file_name(record)
    path.write_bytes(content)
    return {'local_source_path': str(path)}

def _save_local_source_text(record: SourceIngestionRecord, text: str) -> dict[str, Any]:
    return _save_local_source_file(record, text.encode('utf-8'))

def source_local_path(record: SourceIngestionRecord) -> Path | None:
    if record.metadata.get('source_backend') in {'google', 'local'}:
        from openclass_core.services.google_sources.original_cache import original_path
        from openclass_core.services.google_sources.config import GoogleSourceError
        try:
            return original_path(record)
        except GoogleSourceError:
            return None
    raw_path = record.metadata.get('local_source_path')
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    path = Path(raw_path).expanduser().resolve()
    allowed_root = (workspace_state.UPLOAD_DIR / 'sources').resolve()
    if allowed_root not in path.parents or not path.is_file():
        return None
    return path

def source_download_path(record: SourceIngestionRecord) -> Path | None:
    if record.metadata.get('source_backend') in {'google', 'local'}:
        return source_local_path(record)
    raw_path = record.metadata.get('original_source_path') or record.metadata.get('local_source_path')
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    path = Path(raw_path).expanduser().resolve()
    allowed_root = (workspace_state.UPLOAD_DIR / 'sources').resolve()
    return path if allowed_root in path.parents and path.is_file() else None

def _stored_source_file_name(record: SourceIngestionRecord) -> str:
    suffix = Path(record.file_name).suffix.lower()
    return f'{record.id}{suffix}'

def _repair_local_source_storage(record: SourceIngestionRecord) -> SourceIngestionRecord:
    if record.metadata.get('source_backend') in {'google', 'local'}:
        from openclass_core.services.google_sources.original_cache import normalize_cached_source
        return normalize_cached_source(record)
    raw_path = record.metadata.get('local_source_path')
    if not isinstance(raw_path, str) or not raw_path.strip():
        return record
    current_path = Path(raw_path).expanduser().resolve()
    allowed_root = (workspace_state.UPLOAD_DIR / 'sources').resolve()
    if allowed_root not in current_path.parents or not current_path.is_file():
        return record
    expected_path = allowed_root / _stored_source_file_name(record)
    if current_path == expected_path:
        return record
    if expected_path.exists():
        raise SourceIngestionError('The stable storage target for this source already exists.')
    try:
        current_path.replace(expected_path)
    except OSError as exc:
        raise SourceIngestionError('The stored source could not be moved to its stable identity path.') from exc
    return record.model_copy(update={'metadata': {**record.metadata, 'local_source_path': str(expected_path)}})

def _delete_local_source_file(record: SourceIngestionRecord) -> None:
    if record.metadata.get('source_backend') in {'google', 'local'}:
        from openclass_core.services.google_sources.original_cache import delete_cached_original
        delete_cached_original(record)
        return
    paths = [source_local_path(record)]
    raw_original = record.metadata.get('original_source_path')
    if isinstance(raw_original, str) and raw_original.strip():
        candidate = Path(raw_original).expanduser().resolve()
        allowed_root = (workspace_state.UPLOAD_DIR / 'sources').resolve()
        if allowed_root in candidate.parents and candidate.is_file():
            paths.append(candidate)
    for path in dict.fromkeys((item for item in paths if item is not None)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

def _file_content_hash(path: Path) -> str:
    if not path.is_file():
        return ''
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()

def _source_type_for_upload(*, mime_type: str, file_name: str) -> str:
    normalized_mime = (mime_type or '').lower()
    suffix = Path(file_name).suffix.lower()
    if normalized_mime.startswith('audio/') or suffix in {'.mp3', '.m4a', '.wav', '.ogg'}:
        return 'audio_file'
    if normalized_mime.startswith('video/') or suffix in {'.mp4', '.mov', '.webm', '.mpeg'}:
        return 'video_file'
    return 'local_file'

def _text_title(text: str, limit: int=80) -> str:
    first_line = next((line.strip('# *\t') for line in text.splitlines() if line.strip()), '')
    return first_line[:limit] or 'Pasted text'

def _detach_open_notebook_state(record: SourceIngestionRecord) -> SourceIngestionRecord:
    metadata = {key: value for key, value in record.metadata.items() if key != 'source_processing_owner' and (not key.startswith('open_notebook_')) and (not key.startswith('last_open_notebook_'))}
    metadata['adapter'] = 'openclass_native_text' if record.source_type == 'pasted_text' or record.mime_type == 'text/markdown' else 'openclass_native'
    return record.model_copy(update={'open_notebook_notebook_id': '', 'open_notebook_source_id': '', 'open_notebook_command_id': '', 'metadata': metadata})

def _uses_directory_catalog(record: SourceIngestionRecord) -> bool:
    return record.metadata.get('catalog_pipeline') in {CATALOG_SCHEMA_VERSION, *AI_CATALOG_SCHEMA_VERSIONS, PREVIOUS_CATALOG_SCHEMA_VERSION, *LEGACY_CATALOG_SCHEMA_VERSIONS} and supports_directory_catalog(record)

def _as_directory_catalog_record(record: SourceIngestionRecord) -> SourceIngestionRecord:
    detached = _detach_open_notebook_state(record)
    return detached.model_copy(update={'metadata': {**detached.metadata, 'adapter': CATALOG_SCHEMA_VERSION, 'catalog_pipeline': CATALOG_SCHEMA_VERSION}})

def _catalog_model_for_record(record: SourceIngestionRecord) -> AIModelSelection | None:
    raw = record.metadata.get('catalog_model_selection')
    if not isinstance(raw, dict):
        return None
    try:
        return AIModelSelection.model_validate(raw)
    except ValueError:
        return None

def _safe_file_name(file_name: str) -> str:
    name = Path(file_name).name.strip() or 'source'
    return re.sub('[^A-Za-z0-9._ -]+', '_', name)

def _structure_store_for_source_store(store: SourceEvidenceStore) -> SourceStructureStore:
    if getattr(store, '_path', None) is None:
        return source_structure_store
    return SourceStructureStore(store.path)

def _validate_public_url(raw_uri: str) -> str:
    uri = raw_uri.strip()
    parsed = urlparse(uri)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        raise SourceIngestionError('Only http and https URLs are supported.')
    hostname = parsed.hostname or ''
    if hostname in {'localhost', '127.0.0.1', '::1'}:
        raise SourceIngestionError('Localhost URLs are not allowed for source ingestion.')
    try:
        for info in socket.getaddrinfo(hostname, None):
            address = ipaddress.ip_address(info[4][0])
            if address.is_private or address.is_loopback or address.is_link_local:
                raise SourceIngestionError('Private network URLs are not allowed for source ingestion.')
    except socket.gaierror as exc:
        raise SourceIngestionError('URL hostname could not be resolved.') from exc
    return uri
source_ingestion_service = SourceIngestionService()
