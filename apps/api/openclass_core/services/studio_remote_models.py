"""Model-only platform transport; course orchestration and commits remain local."""
from __future__ import annotations
import base64
import hashlib
import threading
from types import SimpleNamespace
from uuid import uuid4
from openclass_core.models import AIModelSelection
from openclass_core.services.ai_execution_adapter import PiAIExecutionAdapter, StructuredExecutionResult, _bind_model_originals, _validate_model_source_input
from openclass_core.services.studio_platform_client import local_platform_client

class PlatformModelClient:

    def __init__(self, owner, selection):
        self.platform = local_platform_client(owner)
        self.selection = selection

    def _request(self, *, system_prompt, user_prompt, schema=None, image_inputs=None, document_path=None, is_cancelled=None, preserve_image_bytes=False, on_activity=None, on_text_delta=None, allow_live_web_search=False):
        operation = uuid4().hex
        connection = self.platform.get('connection')
        payload = {'operation_id': operation, 'selection': self.selection.model_dump(mode='json'), 'system_prompt': system_prompt, 'user_prompt': user_prompt, 'image_inputs': image_inputs or [], 'result_schema': schema.model_json_schema() if schema else None, 'preserve_image_bytes': preserve_image_bytes, 'allow_live_web_search': allow_live_web_search}
        if document_path:
            raw = document_path.read_bytes()
            if len(raw) > 40 * 1024 * 1024:
                raise RuntimeError('The authorized model attachment exceeds the gateway limit.')
            payload['document'] = {'data': base64.b64encode(raw).decode(), 'sha256': hashlib.sha256(raw).hexdigest(), 'media_type': 'application/pdf' if document_path.suffix.lower() == '.pdf' else 'application/octet-stream'}
        if is_cancelled and is_cancelled():
            raise RuntimeError('The model request was cancelled.')
        done = threading.Event()
        result = []

        def invoke():
            try:
                result.append(self.platform.request('POST', 'model-calls', json=payload, connection=connection).json())
            except Exception as error:
                result.append(error)
            finally:
                done.set()
        threading.Thread(target=invoke, daemon=True).start()
        while not done.wait(0.1):
            if is_cancelled and is_cancelled():
                try:
                    self.platform.request('POST', f'model-calls/{operation}/cancel', connection=connection)
                finally:
                    raise RuntimeError('The model request was cancelled.')
        if is_cancelled and is_cancelled():
            raise RuntimeError('The model request was cancelled.')
        if isinstance(result[0], Exception):
            raise result[0]
        receipt = result[0]
        if receipt.get('operation_id') != operation or receipt.get('access_method') != 'platform_credits' or receipt.get('account_id') != (connection or {}).get('account_id'):
            raise RuntimeError('The platform returned an invalid model receipt.')
        if schema:
            return SimpleNamespace(output_parsed=schema.model_validate(receipt['output']), activity=[])
        text = receipt['output']
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError('The platform model returned an empty answer.')
        if on_text_delta:
            on_text_delta(text)
        return SimpleNamespace(output_text=text, activity=[])

    def parse(self, **kwargs):
        return self._request(**kwargs)

    def complete_text(self, **kwargs):
        return self._request(**kwargs)

class RemotePlatformAIExecutionAdapter(PiAIExecutionAdapter):
    runtime_label = 'OpenClass platform'

    def __init__(self, selection, *, owner_user_id, supports_source_visual_tools=False):
        self.owner_user_id = owner_user_id
        self.provider, self.model = (selection.provider, selection.model)
        self.access_method = 'platform_credits'
        self.connection_id = None
        self.reasoning_effort, self.service_tier = (selection.reasoning_effort, selection.service_tier)
        self.supports_source_visual_tools = supports_source_visual_tools
        self._client = PlatformModelClient(owner_user_id, selection)

    @_bind_model_originals
    def parse_document_structured(self, *, system_prompt, user_prompt, document_path, schema, on_activity=None, source_delivery=None):
        _validate_model_source_input(system_prompt=system_prompt, user_prompt=user_prompt, document_path=document_path)
        value = self._client.parse(system_prompt=system_prompt, user_prompt=user_prompt, document_path=document_path, schema=schema, on_activity=on_activity)
        return StructuredExecutionResult(output_parsed=value.output_parsed, activity=value.activity)

def remote_platform_adapter(selection: AIModelSelection, **kwargs):
    return RemotePlatformAIExecutionAdapter(selection, **kwargs)
