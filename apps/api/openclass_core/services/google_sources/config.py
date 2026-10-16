from __future__ import annotations
import os
import re
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass

class GoogleSourceError(RuntimeError):
    """Safe, actionable failure. Never include credentials or source bodies."""

class GoogleSourceConflictError(GoogleSourceError):
    status_code = 409

class GoogleSourceUnavailableError(GoogleSourceError):
    """Cloud connectivity/configuration failure eligible for catalog-only fallback."""

@dataclass(frozen=True)
class GoogleSourceConfig:
    project_id: str = ''
    source_bucket: str = ''
    artifact_bucket: str = ''
    data_store_id: str = ''
    location: str = 'global'
    serving_config: str = 'default_search'
    speech_location: str = 'us'
    max_upload_bytes: int = 209715200
    poll_seconds: float = 2.0
    timeout_seconds: float = 900.0
    processing_paused: bool = False
    trial_expires_at: str = ''

    @classmethod
    def from_env(cls) -> 'GoogleSourceConfig':
        prefix = 'OPENCLASS_GOOGLE_'
        return cls(trial_expires_at=os.getenv(prefix + 'TRIAL_EXPIRES_AT', ''), project_id=os.getenv(prefix + 'PROJECT_ID', ''), source_bucket=os.getenv(prefix + 'SOURCE_BUCKET', ''), artifact_bucket=os.getenv(prefix + 'ARTIFACT_BUCKET', ''), data_store_id=os.getenv(prefix + 'SEARCH_DATA_STORE_ID', ''), location=os.getenv(prefix + 'SEARCH_LOCATION', 'global'), serving_config=os.getenv(prefix + 'SEARCH_SERVING_CONFIG', 'default_search'), speech_location=os.getenv(prefix + 'SPEECH_LOCATION', 'us'), max_upload_bytes=int(os.getenv(prefix + 'MAX_UPLOAD_BYTES', '209715200')), poll_seconds=float(os.getenv(prefix + 'SOURCE_POLL_SECONDS', '2')), timeout_seconds=float(os.getenv(prefix + 'SOURCE_TIMEOUT_SECONDS', '900')), processing_paused=os.getenv(prefix + 'SOURCE_PROCESSING_PAUSED', 'false').lower() == 'true')

    @property
    def configured(self) -> bool:
        return bool(self.project_id and self.source_bucket and self.artifact_bucket and self.data_store_id)

    def validate(self, *, write: bool=False, storage_only: bool=False) -> None:
        if write and self.processing_paused:
            raise GoogleSourceError('Google source processing is paused. Check the free credit and billing status.')
        if self.trial_expires_at:
            try:
                expiry = datetime.fromisoformat(self.trial_expires_at.replace('Z', '+00:00'))
                if expiry.tzinfo is None:
                    raise ValueError('timezone required')
            except ValueError as exc:
                raise GoogleSourceError('The Google trial expiration must be an ISO timestamp with a time zone.') from exc
            cutoff = expiry - timedelta(hours=24) if write else expiry
            if datetime.now(timezone.utc) >= cutoff:
                raise GoogleSourceError('The Google trial is expiring or has expired. New billable source operations are paused.')
        if not (bool(self.project_id and self.source_bucket and self.artifact_bucket) if storage_only else self.configured):
            raise GoogleSourceUnavailableError('Google source services are not configured. Set the project, original-file bucket, artifact bucket, and search data store.')
        for value in (self.project_id, self.source_bucket, self.artifact_bucket) if storage_only else (self.project_id, self.source_bucket, self.artifact_bucket, self.data_store_id, self.serving_config):
            if not re.fullmatch('[A-Za-z0-9_.-]+', value):
                raise GoogleSourceError('A Google source service resource ID is invalid.')
        if self.location not in {'global', 'us', 'eu'} or self.speech_location not in {'us', 'eu'}:
            raise GoogleSourceError('The Google source service region configuration is unsupported.')
        if self.max_upload_bytes < 1 or self.poll_seconds < 0 or self.timeout_seconds <= 0:
            raise GoogleSourceError('The Google source processing limits are invalid.')

    @property
    def endpoint(self) -> str:
        prefix = '' if self.location == 'global' else f'{self.location}-'
        return f'https://{prefix}discoveryengine.googleapis.com'

    @property
    def data_store(self) -> str:
        return f'projects/{self.project_id}/locations/{self.location}/collections/default_collection/dataStores/{self.data_store_id}'

    @property
    def documents(self) -> str:
        return self.data_store + '/branches/0/documents'
