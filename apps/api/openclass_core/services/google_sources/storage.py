from __future__ import annotations
import hashlib
import json
from urllib.parse import quote
from .client import GoogleCloudClient
from .config import GoogleSourceError

def scope_prefix(owner: str, package: str, source: str, version: str) -> str:
    return '/'.join((hashlib.sha256(value.encode()).hexdigest()[:32] for value in (owner, package, source, version)))

class GoogleSourceBlobStore:

    def __init__(self, client: GoogleCloudClient):
        self.client = client
        self.config = client.config

    def put(self, *, bucket: str, name: str, content: bytes, mime_type: str) -> dict:
        self.config.validate(write=True)
        if bucket not in {self.config.source_bucket, self.config.artifact_bucket}:
            raise GoogleSourceError('The source object does not belong to the configured storage bucket.')
        digest = hashlib.sha256(content).hexdigest()
        url = f"https://storage.googleapis.com/upload/storage/v1/b/{quote(bucket, safe='')}/o"
        response = self.client.request('POST', url, params={'uploadType': 'media', 'name': name, 'ifGenerationMatch': '0'}, headers={'Content-Type': mime_type}, content=content, allow_status=(412,))
        if response.status_code == 412:
            existing = self.client.json('GET', self._url(bucket, name))
            raw = self.client.request('GET', self._url(bucket, name), params={'alt': 'media', 'generation': existing['generation']}).content
            if hashlib.sha256(raw).hexdigest() != digest:
                raise GoogleSourceError('The immutable source object already exists with a different hash.')
            metadata = existing
        else:
            metadata = response.json()
        if str(metadata.get('size')) != str(len(content)) or not metadata.get('generation'):
            raise GoogleSourceError('The Google original-file size or storage generation could not be verified.')
        return {'bucket': bucket, 'name': name, 'generation': str(metadata['generation']), 'sha256': digest, 'size': len(content), 'mime_type': mime_type, 'uri': f'gs://{bucket}/{name}'}

    def _url(self, bucket: str, name: str) -> str:
        return f"https://storage.googleapis.com/storage/v1/b/{quote(bucket, safe='')}/o/{quote(name, safe='')}"

    def read(self, ref: dict) -> bytes:
        self._validate_ref(ref)
        response = self.client.request('GET', self._url(ref['bucket'], ref['name']), params={'alt': 'media', 'generation': ref['generation']})
        if hashlib.sha256(response.content).hexdigest() != ref['sha256'] or len(response.content) != int(ref['size']):
            raise GoogleSourceError('Source object integrity verification failed.')
        return response.content

    def json(self, ref: dict) -> dict:
        try:
            result = json.loads(self.read(ref))
        except (ValueError, UnicodeError) as exc:
            raise GoogleSourceError('The cloud source artifact contains invalid JSON.') from exc
        if not isinstance(result, dict):
            raise GoogleSourceError('The cloud source artifact has an unexpected format.')
        return result

    def delete(self, ref: dict) -> None:
        self._validate_ref(ref)
        self.client.request('DELETE', self._url(ref['bucket'], ref['name']), params={'ifGenerationMatch': ref['generation']}, allow_status=(404,))

    def delete_namespace(self, *, owner_user_id, package_id, source_id, version):
        prefix = scope_prefix(owner_user_id, package_id, source_id, version) + '/'
        for bucket in {self.config.source_bucket, self.config.artifact_bucket}:
            token = None
            while True:
                params = {'prefix': prefix, 'maxResults': 1000}
                if token:
                    params['pageToken'] = token
                response = self.client.json('GET', f"https://storage.googleapis.com/storage/v1/b/{quote(bucket, safe='')}/o", params=params)
                for item in response.get('items', []):
                    name, generation = (str(item.get('name', '')), str(item.get('generation', '')))
                    if not name.startswith(prefix) or not generation:
                        raise GoogleSourceError('Storage cleanup returned an object outside the current source version.')
                    self.client.request('DELETE', self._url(bucket, name), params={'ifGenerationMatch': generation}, allow_status=(404,))
                token = response.get('nextPageToken')
                if not token:
                    break

    def _validate_ref(self, ref: dict) -> None:
        if ref.get('bucket') not in {self.config.source_bucket, self.config.artifact_bucket} or not all((ref.get(key) for key in ('name', 'generation', 'sha256'))):
            raise GoogleSourceError('The source storage reference is invalid or belongs to another environment.')
