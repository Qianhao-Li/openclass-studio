"""The only OpenClass transport into the private AnythingLLM source runtime."""
from __future__ import annotations
import base64
import hashlib
import json
import httpx
from openclass_core.services.google_sources.config import GoogleSourceError
from .config import AnythingLLMConfig, CONTRACT
from .runtime import ensure_started

def source_identity(source):
    return {'owner_user_id': source.owner_user_id, 'id': source.id, 'source_version': source.metadata['source_version'], 'content_hash': source.metadata['content_hash'], 'title': source.title, 'package_id': source.package_id, 'catalog_version': int(source.metadata.get('catalog_version', 1))}

class AnythingLLMClient:

    def __init__(self, config=None, transport=None):
        self.config = config or AnythingLLMConfig.from_env()
        self.transport = transport

    def request(self, endpoint, payload):
        if self.transport is None:
            ensure_started(self.config)
        try:
            with httpx.Client(transport=self.transport, timeout=120, trust_env=False) as client:
                result = client.post(self.config.endpoint + endpoint, json=payload, headers={'Authorization': 'Bearer ' + self.config.key()})
            result.raise_for_status()
            value = result.json()
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except httpx.HTTPStatusError as exc:
            try:
                message = exc.response.json().get('error')
            except ValueError:
                message = None
            raise GoogleSourceError(message if isinstance(message, str) and len(message) < 512 else 'AnythingLLM could not complete this source request.') from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise GoogleSourceError('AnythingLLM could not complete this source request. Verified original reading remains available.') from exc

    def import_source(self, source, path, data):
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != source.metadata['content_hash']:
            raise GoogleSourceError('The original hash changed before AnythingLLM import.')
        result = self.request('/sources/import', {'source': source_identity(source), 'content': data, 'original_base64': base64.b64encode(raw).decode()})
        if result.get('contract') != CONTRACT:
            raise GoogleSourceError('The AnythingLLM source runtime contract is incompatible.')
        return result

    def collect_source(self, source, path):
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != source.metadata['content_hash']:
            raise GoogleSourceError('The original hash changed before AnythingLLM collection.')
        result = self.request('/sources/collect', {'source': source_identity(source), 'file_name': source.file_name, 'mime_type': source.mime_type, 'original_base64': base64.b64encode(raw).decode()})
        if result.get('contract') != CONTRACT or result.get('parser_provider') != 'anythingllm':
            raise GoogleSourceError('The AnythingLLM native reader contract is incompatible.')
        return result

    def publish_catalog(self, source, chapters, metadata):
        return self.request('/catalog/publish', {'source': source_identity(source), 'chapters': [item.model_dump(mode='json') for item in chapters], 'metadata': metadata})

    def activate(self, source):
        return self.request('/sources/activate', {'source': source_identity(source)})

    def statuses(self, sources):
        return self.request('/sources/status', {'sources': [source_identity(source) for source in sources]})['sources']

    def revoke(self, source):
        return self.request('/sources/revoke', {'source': source_identity(source)})

class CatalogAdapter:
    """Execute AnythingLLM catalog tasks through the current user/model adapter."""

    def __init__(self, client, source, selection, adapter, check):
        self.client, self.source, self.selection, self.adapter, self.check = (client, source, selection, adapter, check)

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    def parse_structured(self, *, schema, **kwargs):
        from openclass_core.services.ai_execution_adapter import StructuredExecutionResult
        self.check()
        packet = {'source': source_identity(self.source), 'batch_hash': hashlib.sha256(json.dumps({'model': self.selection.model_dump(mode='json'), 'schema': schema.model_json_schema(), 'system': kwargs['system_prompt'], 'input': kwargs['user_prompt'], 'images': [hashlib.sha256(value.encode()).hexdigest() for value in kwargs.get('image_inputs') or []]}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
        packet['request'] = {'system_prompt': kwargs['system_prompt'], 'user_prompt': kwargs['user_prompt'], 'schema': schema.model_json_schema(), 'model': self.selection.model_dump(mode='json'), 'image_hashes': [hashlib.sha256(value.encode()).hexdigest() for value in kwargs.get('image_inputs') or []]}
        batch = self.client.request('/catalog/begin', packet)
        if batch.get('cached'):
            return StructuredExecutionResult(output_parsed=schema.model_validate(batch['output']))
        role_request = batch['request']
        result = self.adapter.parse_structured(schema=schema, **{**kwargs, 'system_prompt': role_request['system_prompt'], 'user_prompt': role_request['user_prompt']})
        self.check()
        parsed = schema.model_validate(result.output_parsed)
        self.client.request('/catalog/complete', {**packet, 'output': parsed.model_dump(mode='json')})
        return result
