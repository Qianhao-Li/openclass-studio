"""Immutable original bytes for explicitly imported repository files, using Sources storage."""
from __future__ import annotations
import hashlib
from openclass_core.services.google_sources.client import GoogleCloudClient
from openclass_core.services.google_sources.config import GoogleSourceConfig, GoogleSourceError
from openclass_core.services.google_sources.storage import GoogleSourceBlobStore, scope_prefix

class RepositoryOriginalStore:

    def __init__(self, blobs=None):
        self._blobs = blobs

    @property
    def blobs(self):
        if self._blobs is None:
            self._blobs = GoogleSourceBlobStore(GoogleCloudClient(GoogleSourceConfig.from_env()))
        return self._blobs

    @staticmethod
    def name(owner: str, package: str, source: str, commit: str, digest: str) -> str:
        return scope_prefix(owner, package, source, commit) + '/repository-originals/' + digest

    def save(self, *, owner: str, package: str, source: str, commit: str, content: bytes) -> dict:
        digest = hashlib.sha256(content).hexdigest()
        return self.blobs.put(bucket=self.blobs.config.source_bucket, name=self.name(owner, package, source, commit, digest), content=content, mime_type='application/octet-stream')

    def reference(self, *, snapshot, file) -> dict:
        ref = file.metadata.get('original_object')
        if not isinstance(ref, dict) or ref.get('bucket') != self.blobs.config.source_bucket or ref.get('sha256') != file.content_hash or (ref.get('name') != self.name(snapshot.owner_user_id, snapshot.package_id, snapshot.source_ingestion_id, snapshot.resolved_commit_sha, file.content_hash)):
            raise GoogleSourceError('Repository original identity does not match this source snapshot.')
        return ref

    def read(self, *, snapshot, file) -> bytes:
        return self.blobs.read(self.reference(snapshot=snapshot, file=file))

    def delete(self, *, snapshot, file) -> None:
        self.blobs.delete(self.reference(snapshot=snapshot, file=file))
repository_original_store = RepositoryOriginalStore()
