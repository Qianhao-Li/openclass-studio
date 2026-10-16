from __future__ import annotations
import hashlib
import json
import uuid
from pathlib import Path
from urllib.parse import quote
from openclass_core.models import SourceIngestionRecord, SourceIngestionJob, SourceQueryRef, SourceRange, now_iso
from openclass_core.services.source_evidence_store import source_evidence_store
from openclass_core.services.source_ingestion_jobs import SourceIngestionJobStore, source_task_cancel_requested, update_current_source_task_run_id
from openclass_core.services.source_structure_store import SourceStructureStore
from .adapters import adapt_source
from .catalog import build_catalog
from .coverage import chunk_page_coverage, validate_inputs, validate_output
from .client import GoogleCloudClient
from .config import GoogleSourceConfig, GoogleSourceError, GoogleSourceUnavailableError, GoogleSourceConflictError
from .processor import GoogleLayoutProcessor, normalized_blocks
from .read import evidence_from_chunk, in_range, query_table, read_artifact
from .state import GoogleSourceState
from .storage import GoogleSourceBlobStore, scope_prefix

class _OriginalModeRequested(GoogleSourceError):
    pass

class GoogleSourceService:

    def __init__(self, *, store=None, job_store=None, structure_store=None, config=None, client=None, catalog_fallback=None):
        self.store = store or source_evidence_store
        self.job_store = job_store or SourceIngestionJobStore(path=self.store.path, coordinator=self.store.coordinator)
        self.structure_store = structure_store or SourceStructureStore(path=self.store.path, coordinator=self.store.coordinator)
        self.config = config or (client.config if client else GoogleSourceConfig.from_env())
        self.client = client or GoogleCloudClient(self.config)
        self.storage = GoogleSourceBlobStore(self.client)
        self.processor = GoogleLayoutProcessor(self.client)
        self.state = GoogleSourceState(self.store)
        self.catalog_fallback = catalog_fallback
        self.catalog_manual = None

    def cloud_enabled(self, source):
        if source.metadata.get('enhancement_requested'):
            return True
        if source.metadata.get('processing_mode') == 'ordinary':
            return source.metadata.get('enhancement_status') == 'ready'
        from openclass_core.services.source_retrieval_preferences import auto_source_retrieval_enabled
        return auto_source_retrieval_enabled(source.owner_user_id, self.store.path)

    def _check_cloud(self, source):
        self._check(source)
        if not self.cloud_enabled(self.state.current(source)):
            raise _OriginalModeRequested('Automatic search is off. Only explicitly referenced original sources can be read in this turn.')

    def process_original_catalog(self, source):
        self._check(source)
        if self.catalog_manual is None:
            from openclass_core.services.source_ingestion_service import SourceIngestionService
            from .catalog_fallback import build_manual_catalog
            ingestion = SourceIngestionService(source_backend='google', store=self.store, job_store=self.job_store, structure_store=self.structure_store, google_service=self)
            return build_manual_catalog(ingestion, source)
        return self.catalog_manual(source)

    @property
    def configured(self):
        return self.config.configured

    def queue_file(self, *, owner_user_id: str, package_id: str, file_name: str, content: bytes, mime_type: str, title: str='', source_id: str | None=None, origin_metadata: dict | None=None, source_uri: str | None=None, expected_source_version: str | None=None, expected_content_hash: str | None=None):
        self.config.validate(write=True, storage_only=True)
        if not owner_user_id or not package_id or (not content) or (len(content) > self.config.max_upload_bytes):
            raise GoogleSourceError('The source identity, file contents, or size does not meet upload requirements.')
        old = self.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id) if source_id else None
        if source_id and old is None:
            raise GoogleSourceError('The source to update was not found or does not belong to the current user.')
        if expected_source_version is not None and (old is None or old.metadata.get('source_version') != expected_source_version or old.metadata.get('content_hash') != expected_content_hash or old.metadata.get('pending_source_version')):
            raise GoogleSourceConflictError('The source changed after the update was confirmed. Refresh before updating it.')
        digest = hashlib.sha256(content).hexdigest()
        if old and old.metadata.get('source_backend') == 'google' and old.metadata.get('google_original') and (old.status == 'ready') and (old.metadata.get('original_read_ready') or old.metadata.get('search_ready')) and (old.metadata.get('content_hash') == digest) and (origin_metadata is None or old.metadata.get('origin') == origin_metadata):
            return old
        version = uuid.uuid4().hex
        record = SourceIngestionRecord(**{'id': source_id} if source_id else {}, owner_user_id=owner_user_id, package_id=package_id, title=title.strip() or file_name, file_name=Path(file_name).name, mime_type=mime_type or 'application/octet-stream', size_bytes=len(content), source_uri=source_uri, source_type='audio_file' if mime_type.startswith('audio/') else 'local_file', status='queued', metadata={'origin': origin_metadata or {}, 'source_backend': 'google', 'adapter': 'google_layout_v1', 'layout_batch_pages': 200, 'source_processing_owner': 'google', 'source_version': version, 'content_hash': digest, 'catalog_version': int(old.metadata.get('catalog_version', 0)) + 1 if old else 1, 'storage_status': 'uploading', 'parsing_status': 'pending', 'search_status': 'pending', 'catalog_status': 'pending', 'search_ready': False, 'catalog_ready': False})
        prefix = scope_prefix(owner_user_id, package_id, record.id, version)
        original = self.storage.put(bucket=self.config.source_bucket, name=f'{prefix}/original/{digest}', content=content, mime_type=record.mime_type)
        record.metadata.update({'google_original': original, 'storage_status': 'ready', 'google_documents': [], 'google_objects': [original]})
        record = self.state.stage(record, old, reject_pending=expected_source_version is not None)
        self._job(record, 'uploaded', 15)
        return record

    def work_source(self, source):
        return self.state.work_source(source)

    def retry(self, source):
        pending = self.work_source(source)
        if pending.metadata.get('processing_mode') == 'ordinary' and (not pending.metadata.get('enhancement_requested')):
            from openclass_core.services.source_basic_service import process_basic_source
            return process_basic_source(self, pending)
        if not self.cloud_enabled(pending):
            return self.process_original_catalog(pending)
        if pending.status == 'ready' and pending.metadata.get('search_ready'):
            return pending
        retrying = self.state.update(pending, active=False, status='queued', error='')
        return self.process(retrying)

    def cancel(self, source):
        pending = self.work_source(source)
        if pending.metadata.get('catalog_refinement', {}).get('requested'):
            request_id = pending.metadata['catalog_refinement'].get('request_id')

            def cancel_refinement(metadata):
                task = metadata.get('catalog_refinement', {})
                if task.get('request_id') != request_id or not task.get('requested') or task.get('status') not in {'queued', 'running'}:
                    return {}
                return {'catalog_refinement': {**task, 'status': 'cancelled', 'requested': False}, 'catalog_refinement_status': 'cancelled', 'update_status': 'cancelled', 'catalog_status': 'ready' if metadata.get('catalog_ready') else 'cancelled', 'catalog_error': 'Catalog refinement cancelled; saved directory and reading checkpoints remain available.'}
            return self.state.update(pending, active=False, metadata=cancel_refinement)
        if pending.status == 'ready' and pending.metadata.get('native_text_ready') and (pending.metadata.get('parsing_status') == 'running'):
            cancelled = self.state.update(pending, metadata={'parsing_status': 'cancelled', 'update_status': 'cancelled', **({} if pending.metadata.get('catalog_ready') else {'catalog_status': 'failed', 'catalog_warning': 'Catalog processing cancelled. Native text questions remain available.'})})
            self._job(cancelled.model_copy(update={'status': 'cancelled'}), 'cancelled', 100, 'Source cloud processing cancelled.')
            return cancelled
        if pending.metadata.get('processing_mode') == 'ordinary' and pending.metadata.get('original_read_ready'):
            enhanced = pending.metadata.get('enhancement_requested') is True
            metadata = {'enhancement_status': 'cancelled', 'enhancement_requested': False} if enhanced else {'catalog_status': 'ready' if pending.metadata.get('catalog_ready') else 'cancelled', 'catalog_error': 'Catalog processing was cancelled.'}
            return self.state.update(pending, active=False, status='ready', metadata=metadata)
        if pending.status not in {'queued', 'fetching', 'parsing', 'indexing'}:
            return pending
        cancelled = self.state.update(pending, status='cancelled', error='Source processing cancelled.')
        self._job(cancelled, 'cancelled', 100, cancelled.error)
        return cancelled

    def _job(self, source, phase, progress, error=''):
        if source.metadata.get('enhancement_requested') and source.status == 'ready':
            source = source.model_copy(update={'status': 'parsing'})
        if phase == 'google_layout_processing' and source.metadata.get('native_text_ready') and (source.metadata.get('parsing_status') == 'running'):
            source = source.model_copy(update={'status': 'parsing'})
        current = self.job_store.latest_for_source(owner_user_id=source.owner_user_id, package_id=source.package_id, source_id=source.id)
        if current is None or phase in {'uploaded', 'catalog_queued'} or current.run_id != source.metadata['source_version']:
            current = SourceIngestionJob(resource_id=source.id, run_id=source.metadata['source_version'], source_type=source.source_type, adapter='google_layout_v1', status=source.status, progress=progress, phase_history=[phase], error=error, heartbeat_at=now_iso())
        else:
            current = current.model_copy(update={'status': source.status, 'progress': progress, 'phase_history': [*current.phase_history, phase] if current.phase_history[-1:] != [phase] else current.phase_history, 'error': error, 'heartbeat_at': now_iso()})
        saved = self.job_store.save(current, owner_user_id=source.owner_user_id, package_id=source.package_id)
        update_current_source_task_run_id(saved.run_id or saved.id)
        return saved

    def _check(self, source):
        if source_task_cancel_requested():
            raise GoogleSourceError('Source processing cancelled.')
        self.state.current(source)

    def process(self, source):
        self._check(source)
        source = self.state.current(source)
        if source.metadata.get('catalog_refinement', {}).get('requested'):
            from openclass_core.services.source_catalog_refinement import process_refinement
            return process_refinement(self, source)
        if source.metadata.get('processing_mode') == 'ordinary' and (not source.metadata.get('enhancement_requested')):
            from openclass_core.services.source_basic_service import process_basic_source
            return process_basic_source(self, source)
        if not self.cloud_enabled(source):
            return self.process_original_catalog(source)
        if source.status == 'ready' and source.metadata.get('search_ready'):
            return source
        self._check(source)
        ordinary = source.metadata.get('processing_mode') == 'ordinary' or source.metadata.get('preserve_catalog_on_enhancement') is True
        source = self.state.update(source, status='ready' if source.metadata.get('native_text_ready') or (ordinary and source.metadata.get('original_read_ready')) else 'parsing', error='', metadata={'parsing_status': 'running', 'catalog_mode': 'cloud', **({'update_status': 'parsing'} if source.metadata.get('native_text_ready') else {})})
        self._job(source, 'google_layout_processing', 25)
        try:
            self.config.validate(write=True)
            self._check_cloud(source)
            self.processor.verify_configuration()
            self._verify_blob_binding(source, source.metadata['google_original'], original=True)
            raw = self.storage.read(source.metadata['google_original'])
            adapted = adapt_source(raw, source.file_name, source.mime_type, source.metadata.get('layout_batch_pages', 400))
            transcript = None
            if adapted.format == 'audio':
                from .audio import transcribe
                adapted, transcript = transcribe(source, raw, client=self.client, processor=self.processor, checkpoint=source.metadata.get('google_speech_checkpoint', {}), save=lambda value: self.state.update(source, metadata={'google_speech_checkpoint': value}), check=lambda: self._check_cloud(source))
            page_count = validate_inputs(raw, adapted)
            prefix = scope_prefix(source.owner_user_id, source.package_id, source.id, source.metadata['source_version'])
            from .layout_batches import process_layout_batches
            outputs = process_layout_batches(self, source, adapted.parts)
            if len(outputs) != len(adapted.parts) or not any((str(chunk.get('content') or '').strip() for output in outputs for chunk in output['chunks'])):
                raise GoogleSourceError('Google returned no searchable content. The original has been preserved; empty results cannot be marked as search-ready.')
            readable_pages = sorted({page for output in outputs for page in output.get('retrievable_pages', [])}) if page_count is not None else []
            page_coverage = {'page_count': page_count, 'retrievable_pages': readable_pages, 'unmapped_pages': sorted(set(range(1, page_count + 1)) - set(readable_pages)) if page_count is not None else []}
            artifact = {'source_id': source.id, 'source_version': source.metadata['source_version'], 'content_hash': source.metadata['content_hash'], 'parts': outputs, 'tables': adapted.tables, 'transcript': transcript, **page_coverage}
            ref = self.storage.put(bucket=self.config.artifact_bucket, name=f'{prefix}/artifact.json', content=json.dumps(artifact, ensure_ascii=False).encode(), mime_type='application/json')
            self._check_cloud(source)
            source = self.state.update(source, status='ready', metadata={**page_coverage, 'google_artifact': ref, 'parsing_status': 'ready', 'search_status': 'ready', 'search_ready': True, 'original_read_ready': True, 'parser_run_id': source.metadata['source_version'], 'catalog_status': 'running'})
            if ordinary:
                source = self.state.update(source, metadata={'enhancement_status': 'ready', 'enhancement_progress': 100, 'enhancement_error': '', 'enhancement_requested': False, 'catalog_status': 'ready' if source.metadata.get('catalog_ready') else 'failed'})
                self._job(source, 'enhancement_ready', 100)
                return self.structure_store.attach_summary(source)
            if source.metadata.get('native_catalog_complete'):
                view = self.structure_store.get_catalog_view(source=source)
                if view.chapters and view.chapter_count == view.verified_chapter_count and (view.directory_status == 'complete'):
                    ready = self.state.update(source, metadata={'catalog_status': 'ready', 'catalog_ready': True, 'update_status': 'complete', 'google_objects': [*self.state.current(source).metadata.get('google_objects', []), ref], 'capabilities': ['search', 'read', 'download', 'read_original']})
                    self._job(ready, 'ready', 100)
                    return self.structure_store.attach_summary(ready)
            from .catalog_publication import build_source_catalog
            structure, chapters, catalog_metadata = build_source_catalog(self, source, adapted, outputs)
            self._check_cloud(source)
            live = self.state.current(source)
            ready = self.state.publish(source, structure_store=self.structure_store, structure=structure, chapters=chapters, metadata={**page_coverage, **catalog_metadata, 'google_artifact': ref, 'google_objects': [*live.metadata.get('google_objects', []), ref], 'parsing_status': 'ready', 'search_status': 'ready', 'search_ready': True, 'catalog_mode': 'cloud', 'original_read_ready': True, 'catalog_fallback': False, 'catalog_warning': structure.metadata.get('stop_reason', ''), 'cloud_unavailable': '', 'catalog_status': structure.metadata['catalog_status'], 'catalog_ready': structure.metadata['catalog_status'] == 'ready', 'parser_run_id': source.metadata['source_version'], 'capabilities': ['search', 'read', 'download', 'read_original', *(['query_table'] if adapted.tables else [])]})
            self._job(ready, 'ready', 100)
            return self.structure_store.attach_summary(ready)
        except Exception as exc:
            if ordinary:
                current = self.state.current(source, active=False)
                cancelled = source_task_cancel_requested() or current.metadata.get('enhancement_status') == 'cancelled'
                message = 'Enhanced preparation was cancelled.' if cancelled else str(exc) if isinstance(exc, GoogleSourceError) else 'Enhanced source preparation failed; ordinary reading and the catalog are preserved.'
                failed = self.state.update(current, active=False, status='ready', metadata={'enhancement_status': 'cancelled' if cancelled else 'failed', 'enhancement_error': message, 'enhancement_progress': 100, 'enhancement_requested': False, 'update_status': 'cancelled' if cancelled else 'failed', 'update_error': message, 'catalog_status': 'ready' if current.metadata.get('catalog_ready') else 'failed'})
                self._job(failed.model_copy(update={'status': 'failed'}), 'enhancement_failed', 100, message)
                return failed
            if isinstance(exc, _OriginalModeRequested):
                return self.process_original_catalog(self.state.current(source))
            if isinstance(exc, GoogleSourceUnavailableError) and self.catalog_fallback is not None:
                self._check(source)
                if not self.cloud_enabled(source):
                    return self.process_original_catalog(self.state.current(source))
                return self.catalog_fallback(self.state.current(source), exc)
            try:
                committed = self.store.get_source(owner_user_id=source.owner_user_id, package_id=source.package_id, source_id=source.id)
                if committed is not None and committed.metadata.get('source_version') == source.metadata.get('source_version') and (committed.status == 'ready') and (committed.metadata.get('search_ready') or committed.metadata.get('native_text_ready')):
                    if not committed.metadata.get('search_ready'):
                        message = str(exc) if isinstance(exc, GoogleSourceError) else 'Cloud augmentation failed. Native text questions and the original remain available.'
                        committed = self.state.update(committed, metadata={'parsing_status': 'failed', 'search_status': 'failed', 'update_status': 'failed', 'update_error': message, **({} if committed.metadata.get('catalog_ready') else {'catalog_status': 'failed', 'catalog_ready': False, 'catalog_warning': message})})
                        self._job(committed.model_copy(update={'status': 'failed'}), 'cloud_failed', 100, message)
                        return committed
                    if committed.metadata.get('catalog_status') == 'running':
                        message = str(exc) if isinstance(exc, GoogleSourceError) else 'Original-content catalog generation failed. Search indexing and the original are ready; retry catalog generation to continue.'
                        published_view = self.structure_store.get_catalog_view(source=committed)
                        catalog_ready = bool(published_view.chapters and published_view.chapter_count == published_view.verified_chapter_count and (published_view.directory_status == 'complete'))
                        committed = self.state.update(committed, metadata={'catalog_status': 'ready' if catalog_ready else 'failed', 'catalog_ready': catalog_ready, 'catalog_warning': message, 'stop_reason': message, 'update_status': 'failed', 'update_error': message})
                        self._job(committed.model_copy(update={'status': 'failed'}), 'catalog_failed', 100, message)
                    return committed
            except GoogleSourceError:
                pass
            message = str(exc) if isinstance(exc, GoogleSourceError) else 'Google source processing failed. The original file and completed cloud operations have been preserved.'
            try:
                current = self.state.current(source)
                failed = self.state.update(current, status='failed', error=message, metadata={'parsing_status': 'failed', 'search_ready': False, 'search_status': 'failed'})
                self._job(failed, 'failed', 100, message)
                return failed
            except GoogleSourceError:
                raise GoogleSourceError(message) from exc

    def get(self, *, owner_user_id, package_id, source_id, content_hash=None, ready=True):
        source = self.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
        if source is None or source.metadata.get('source_backend') != 'google':
            raise GoogleSourceError('The source does not exist or does not belong to the current user and course.')
        if source.metadata.get('external_turn_temporary'):
            raise GoogleSourceError('Add this external resource to Sources before reading or searching it.')
        from openclass_core.services.google_drive_sources import ensure_drive_source_access, DriveSourceError
        try:
            ensure_drive_source_access(source, self.store.path)
        except DriveSourceError as exc:
            raise GoogleSourceError(str(exc)) from exc
        material_ready = source.metadata.get('source_processor') == 'anythingllm' and source.metadata.get('original_read_ready')
        if ready and material_ready and (source.status != 'ready' or not source.metadata.get('catalog_ready')):
            raise GoogleSourceError('The complete material catalog has not finished verification.')
        if ready and (not material_ready):
            self._check_cloud(source)
        if ready and (not material_ready) and (source.status != 'ready' or not source.metadata.get('search_ready')):
            raise GoogleSourceError('Google search indexing for this source is incomplete.')
        if content_hash and content_hash != source.metadata.get('content_hash'):
            raise GoogleSourceError('The source version has changed. Please select it again.')
        return source

    def _verify_blob_binding(self, source, ref, *, original=False):
        expected = scope_prefix(source.owner_user_id, source.package_id, source.id, source.metadata['source_version']) + '/'
        if not str(ref.get('name', '')).startswith(expected):
            raise GoogleSourceError('The source object does not belong to the current user, course, and file version.')
        if original and (ref.get('sha256') != source.metadata.get('content_hash') or ref.get('size') != source.size_bytes):
            raise GoogleSourceError('The original-file hash or size does not match the source version.')

    def artifact(self, source):
        self._check_cloud(source)
        self._verify_blob_binding(source, source.metadata['google_artifact'])
        artifact = self.storage.json(source.metadata['google_artifact'])
        if any((artifact.get(key) != expected for key, expected in {'source_id': source.id, 'source_version': source.metadata['source_version'], 'content_hash': source.metadata['content_hash']}.items())):
            raise GoogleSourceError('The source parsing output does not match the current version.')
        if any((part.get('layout_required') and (not part.get('layout_verified')) for part in artifact.get('parts', []))):
            raise GoogleSourceError('The image source has not passed Layout Parser verification.')
        self._check_cloud(source)
        return artifact

    def read(self, *, owner_user_id, package_id, source_id, content_hash, source_range=None, cursor=None):
        source = self.get(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id, content_hash=content_hash)
        artifact = self.artifact(source)
        self._check(source)
        result = read_artifact(source, artifact, source_range, cursor)
        self.get(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id, content_hash=content_hash)
        self._check(source)
        return result

    def query_table(self, *, owner_user_id, package_id, source_id, content_hash, query_plan):
        source = self.get(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id, content_hash=content_hash)
        artifact = self.artifact(source)
        self._check(source)
        result = {'source_id': source.id, 'source_version': source.metadata['source_version'], **query_table(artifact.get('tables', {}), query_plan)}
        self.get(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id, content_hash=content_hash)
        self._check(source)
        return result

    def download(self, *, owner_user_id, package_id, source_id):
        source = self.get(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id, ready=False)
        self._verify_blob_binding(source, source.metadata['google_original'], original=True)
        raw = self.storage.read(source.metadata['google_original'])
        self.get(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id, content_hash=source.metadata['content_hash'], ready=False)
        self.state.current(source, active=False)
        return (source, raw)

    def catalog_search(self, source, query, *, outputs=None):
        from .catalog_search import catalog_locator_hints
        return catalog_locator_hints(self, source, query, outputs=outputs)

    def search(self, *, owner_user_id, package_id, query, refs=None, limit=8):
        from .search import search_sources
        return search_sources(self, owner_user_id=owner_user_id, package_id=package_id, query=query, refs=refs, limit=limit)

    def delete(self, source):
        self.state.queue_cleanup(source)
        self.drain_cleanup()

    def drain_cleanup(self):
        for identity, source in self.state.cleanups():
            try:
                from openclass_core.services.source_ingestion_jobs import source_ingestion_task_manager
                if source_ingestion_task_manager.is_active((source.owner_user_id, source.package_id, source.id)):
                    raise GoogleSourceError('Cloud cleanup is waiting for the source task to exit.')
                if self.state.cleanup_is_referenced(source):
                    raise GoogleSourceError('This source version is still referenced and cannot be cleaned up.')
                if source.metadata.get('source_processor') == 'anythingllm' and source.metadata.get('anythingllm_document'):
                    from openclass_core.services.anythingllm.client import AnythingLLMClient
                    AnythingLLMClient().revoke(source)
                from .catalog_fallback import delete_cached_original
                delete_cached_original(source)
                for document_id in source.metadata.get('google_documents', []):
                    self.client.request('DELETE', f"{self.config.endpoint}/v1/{self.config.documents}/{quote(document_id, safe='')}", allow_status=(404,))
                self.storage.delete_namespace(owner_user_id=source.owner_user_id, package_id=source.package_id, source_id=source.id, version=source.metadata.get('source_version', ''))
                self.state.finish_cleanup(identity)
            except GoogleSourceError as exc:
                self.state.finish_cleanup(identity, str(exc))

def get_google_source_service(*, store=None, job_store=None, structure_store=None):
    import os
    if os.getenv('OPENCLASS_STUDIO_MODE') == 'local' and os.getenv('OPENCLASS_SOURCE_BACKEND') == 'local':
        from openclass_core.services.studio_source_storage import LocalSourceService
        return LocalSourceService(store=store, job_store=job_store, structure_store=structure_store)
    return GoogleSourceService(store=store, job_store=job_store, structure_store=structure_store)
