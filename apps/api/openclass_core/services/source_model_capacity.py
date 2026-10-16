"""Conservative source input budgets from the selected model's SDK registry."""
from __future__ import annotations
import json
import os
import subprocess
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

def declared_platform_context(selection):
    """Verified provider declaration; unknown/personal endpoints stay conservative.

    DeepSeek's official Pi configuration declares 1,000,000 tokens for these IDs:
    https://api-docs.deepseek.com/quick_start/agent_integrations/pi_mono/
    """
    if selection.provider != 'deepseek' or getattr(selection, 'access_method', None) not in (None, 'platform_credits'):
        return None
    from openclass_core.services.deepseek_api import DEEPSEEK_CURATED_MODELS, canonical_deepseek_model, deepseek_config
    if canonical_deepseek_model(selection.model) not in {model for model, _ in DEEPSEEK_CURATED_MODELS}:
        return None
    endpoint = urlparse(deepseek_config().base_url)
    if endpoint.scheme == 'https' and endpoint.hostname == 'api.deepseek.com' and (not endpoint.username) and (not endpoint.password) and (endpoint.path.rstrip('/') in ('', '/v1')):
        return 1000000
    return None

@lru_cache(maxsize=64)
def model_context_tokens(provider, model):
    if provider == 'openai_codex':
        homes = [os.getenv(key) for key in ('OPENCLASS_PLATFORM_CODEX_HOME', 'OPENCLASS_CODEX_HOME', 'CODEX_HOME')]
        for home in [*(value for value in homes if value), str(Path.home() / '.codex')]:
            try:
                data = json.loads((Path(home) / 'models_cache.json').read_text())
                entry = next((item for item in data.get('models', []) if item.get('slug') == model), None)
                if entry and type(entry.get('context_window')) is int and (entry['context_window'] > 0):
                    percent = entry.get('effective_context_window_percent', 100)
                    return int(entry['context_window'] * min(100, max(1, percent)) / 100)
            except (OSError, ValueError, TypeError):
                pass
    from openclass_core.services.pi_agent_runtime import pi_binary_path
    binary = pi_binary_path()
    if binary:
        script = 'import {createRequire} from "node:module"; import {existsSync,readFileSync} from "node:fs"; import {resolve} from "node:path"; import {pathToFileURL} from "node:url"; const r=createRequire(process.argv[1]); const root=r.resolve.paths("@earendil-works/pi-ai").map(p=>resolve(p,"@earendil-works/pi-ai")).find(p=>existsSync(resolve(p,"package.json"))); const modern=resolve(root,"dist/providers/all.js"); const ai=await import(pathToFileURL(existsSync(modern)?modern:resolve(root,"dist/compat.js"))); const m=(ai.getBuiltinModel??ai.getModel)(process.argv[2],process.argv[3]); console.log(JSON.stringify(m?.contextWindow??null));'
        try:
            result = subprocess.run(['node', '--input-type=module', '-e', script, str(Path(binary).resolve()), 'openai-codex' if provider == 'openai_codex' else provider, model], capture_output=True, text=True, timeout=8)
            value = json.loads(result.stdout) if result.returncode == 0 else None
            if type(value) is int and value > 0:
                return value
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return 8192

class SourceInputCapacity:
    """Count the selected encoding, reserving output and transport framing."""

    def __init__(self, selection, adapter=None):
        context = getattr(adapter, 'context_window', None)
        if type(context) is not int or context < 1:
            context = declared_platform_context(selection) or model_context_tokens(selection.provider, selection.model)
        self.limit = max(512, context - max(1024, min(16384, context // 8)))
        self.encoding = None
        self.max_input_bytes = getattr(adapter, 'max_input_bytes', None) or 8 * 1024 * 1024
        from openclass_core.services.model_image_limits import image_transfer_limits
        client = getattr(adapter, '_client', None)
        self.image_limits = image_transfer_limits(getattr(client, 'provider', selection.provider), selection.model)
        self.max_image_count = self.image_limits.count
        self.measurement = 'utf8_upper_bound'
        if selection.provider == 'openai_codex':
            import tiktoken
            try:
                self.encoding = tiktoken.encoding_for_model(selection.model)
            except KeyError:
                self.encoding = tiktoken.get_encoding('o200k_base')
            self.measurement = 'selected_model_encoding'

    def fits_request(self, instructions, body, schema, image_inputs=None):
        schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        total = self.measure(instructions) + self.measure(body) + self.measure(schema_text) + 512
        images = list(image_inputs or [])
        text_bytes = len(instructions.encode()) + len(body.encode()) + len(schema_text.encode())
        sizes = []
        for image in images:
            payload = image.partition(',')[2]
            sizes.append(len(payload) // 4 * 3 - (len(payload) - len(payload.rstrip('='))))
        return total + len(images) * 1024 <= self.limit and text_bytes <= self.max_input_bytes and (len(images) <= self.max_image_count) and all((0 < size <= self.image_limits.per_image for size in sizes)) and (sum(sizes) <= self.image_limits.total_images) and (text_bytes + sum((len(image.encode()) for image in images)) + 64 * 1024 <= self.image_limits.request_bytes)

    def measure(self, value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if self.encoding is not None:
            return len(self.encoding.encode(text, disallowed_special=()))
        return len(text.encode())

def positioned_text_batches(units, capacity, *, reserved=0):
    batch, used = ([], 0)
    maximum = capacity.limit - reserved
    for unit in units:
        size = capacity.measure([unit['id'], unit['text']])
        if size > maximum:
            raise ValueError("A positioned source unit exceeds the selected model's input capacity.")
        if batch and used + size > maximum:
            yield batch
            batch, used = ([], 0)
        batch.append(unit)
        used += size
    if batch:
        yield batch
