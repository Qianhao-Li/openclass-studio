"""Exact local originals and immutable derived objects, scoped to a local profile."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from uuid import uuid4
from openclass_core.models import SourceIngestionRecord
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.google_sources.storage import scope_prefix
from openclass_core.services.google_sources.service import GoogleSourceService

class LocalSourceObjects:

    def __init__(self, root):
        self.root = Path(root)
        if self.root.is_symlink():
            raise GoogleSourceError('The local source directory cannot be a symlink.')
        self.root.mkdir(parents=True, exist_ok=True, mode=448)

    def path(self, bucket, name):
        parts = PurePosixPath(name)
        if bucket not in {'originals', 'artifacts'} or parts.is_absolute() or '..' in parts.parts or (not parts.parts) or ('\\' in name) or ('\x00' in name):
            raise GoogleSourceError('Invalid local source object reference.')
        path = self.root / bucket / name
        if not path.resolve().is_relative_to(self.root.resolve()) or path.is_symlink():
            raise GoogleSourceError('The source object escaped its storage scope.')
        return path

    def put(self, *, bucket, name, content, mime_type):
        path = self.path(bucket, name)
        path.parent.mkdir(parents=True, exist_ok=True, mode=448)
        digest = hashlib.sha256(content).hexdigest()
        if path.is_file():
            if path.read_bytes() != content:
                raise GoogleSourceError('The immutable local source object already has different bytes.')
        else:
            temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
            try:
                with temporary.open('xb') as target:
                    os.chmod(temporary, 384)
                    target.write(content)
                    target.flush()
                    os.fsync(target.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    if path.read_bytes() != content:
                        raise GoogleSourceError('The immutable source object changed during storage.')
            finally:
                temporary.unlink(missing_ok=True)
        return {'storage': 'local', 'bucket': bucket, 'name': name, 'generation': digest, 'sha256': digest, 'size': len(content), 'mime_type': mime_type, 'uri': 'local-source://' + bucket + '/' + name}

    def read(self, ref):
        if ref.get('storage') != 'local' or ref.get('generation') != ref.get('sha256'):
            raise GoogleSourceError('The source reference belongs to another storage adapter.')
        path = self.path(ref.get('bucket'), ref.get('name', ''))
        raw = path.read_bytes()
        if len(raw) != ref.get('size') or hashlib.sha256(raw).hexdigest() != ref.get('sha256'):
            raise GoogleSourceError('Local source object integrity verification failed.')
        return raw

    def json(self, ref):
        return json.loads(self.read(ref))

    def delete(self, ref):
        self.read(ref)
        self.path(ref['bucket'], ref['name']).unlink(missing_ok=True)

    def delete_namespace(self, *, owner_user_id, package_id, source_id, version):
        import shutil
        prefix = scope_prefix(owner_user_id, package_id, source_id, version)
        for bucket in ('originals', 'artifacts'):
            path = self.path(bucket, prefix)
            if path.exists():
                shutil.rmtree(path)

class LocalSourceService(GoogleSourceService):
    """Reuse source authorization/catalog contracts with a local storage adapter."""

    def __init__(self, *, store=None, job_store=None, structure_store=None):
        from openclass_core.services.source_evidence_store import source_evidence_store
        from openclass_core.services.source_ingestion_jobs import SourceIngestionJobStore
        from openclass_core.services.source_structure_store import SourceStructureStore
        from openclass_core.services.google_sources.state import GoogleSourceState
        from openclass_core.services.config import DATA_DIR
        self.store = store or source_evidence_store
        self.job_store = job_store or SourceIngestionJobStore(path=self.store.path, coordinator=self.store.coordinator)
        self.structure_store = structure_store or SourceStructureStore(path=self.store.path, coordinator=self.store.coordinator)
        self.state = GoogleSourceState(self.store)
        self.storage = LocalSourceObjects(DATA_DIR / 'sources/objects')
        self.config = SimpleNamespace(source_bucket='originals', artifact_bucket='artifacts', max_upload_bytes=256 * 1024 * 1024, validate=lambda **_kwargs: None)
        self.catalog_manual = None
        self.catalog_fallback = None

    def configured(self):
        return True

    def cloud_enabled(self, source):
        return False

    def _check_cloud(self, source):
        raise GoogleSourceError('Cloud enhancement is unavailable in this local Studio. Verified ordinary reading remains available.')

    def queue_file(self, *, owner_user_id, package_id, file_name, content, mime_type, title='', source_id=None, origin_metadata=None, source_uri=None, expected_source_version=None, expected_content_hash=None):
        if not owner_user_id or not package_id or (not content) or (len(content) > self.config.max_upload_bytes):
            raise GoogleSourceError('Invalid source identity or upload size.')
        old = self.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id) if source_id else None
        if source_id and (not old):
            raise GoogleSourceError('Source not found.')
        if old and (expected_source_version and old.metadata.get('source_version') != expected_source_version or (expected_content_hash and old.metadata.get('content_hash') != expected_content_hash)):
            raise GoogleSourceError('The source version changed before replacement.')
        digest = hashlib.sha256(content).hexdigest()
        version = uuid4().hex
        record = SourceIngestionRecord(**{'id': source_id} if source_id else {}, owner_user_id=owner_user_id, package_id=package_id, title=title.strip() or Path(file_name).name, file_name=Path(file_name).name, mime_type=mime_type or 'application/octet-stream', size_bytes=len(content), source_type='local_file', source_uri=source_uri, status='queued', metadata={'source_backend': 'local', 'source_version': version, 'content_hash': digest, 'origin': origin_metadata or {}, 'source_processing_owner': 'local', 'storage_status': 'ready', 'processing_mode': 'ordinary', 'search_ready': False, 'catalog_ready': False, 'catalog_status': 'queued', 'enhancement_status': 'not_prepared', 'catalog_version': int(old.metadata.get('catalog_version', 0)) + 1 if old else 1})
        prefix = scope_prefix(owner_user_id, package_id, record.id, version)
        original = self.storage.put(bucket='originals', name=prefix + '/original/' + digest, content=content, mime_type=record.mime_type)
        from openclass_core.services.google_sources.original_cache import cache_original
        record.metadata.update(local_original=original, local_objects=[original], **cache_original(record, content))
        record = self.state.stage(record, old, reject_pending=expected_source_version is not None)
        self._job(record, 'queued', 0)
        return record

    def process(self, source):
        if source.metadata.get('enhancement_requested'):
            return self._check_cloud(source)
        from openclass_core.services.source_basic_service import process_basic_source
        return process_basic_source(self, source)

    def get(self, *, owner_user_id, package_id, source_id, content_hash=None, ready=True):
        source = self.store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
        if not source or source.metadata.get('source_backend') != 'local' or source.metadata.get('external_turn_temporary'):
            raise GoogleSourceError('The source does not belong to this profile and course.')
        if content_hash and source.metadata.get('content_hash') != content_hash:
            raise GoogleSourceError('The source version changed.')
        if ready and (not source.metadata.get('original_read_ready')):
            raise GoogleSourceError('The source is not ready for ordinary reading.')
        self._verify_blob_binding(source, source.metadata['local_original'], original=True)
        return source

    def download(self, *, owner_user_id, package_id, source_id):
        source = self.get(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id, ready=False)
        return (source, self.storage.read(source.metadata['local_original']))
