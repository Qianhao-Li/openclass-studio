"""Public provider/model metadata from the exact Pi installation used at runtime."""
from __future__ import annotations
import json
import logging
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
logger = logging.getLogger(__name__)
_CATALOG_SCRIPT = "\nimport { pathToFileURL } from 'node:url';\nconst { builtinProviders } = await import(pathToFileURL(process.argv[1] + '/providers/all.js'));\nconst { getSupportedThinkingLevels } = await import(pathToFileURL(process.argv[1] + '/index.js'));\nconst providers = builtinProviders().filter(p => p.auth?.apiKey?.login).map(p => ({\n  provider: p.id,\n  label: p.id === 'google' ? 'Google Gemini' : p.name,\n  models: p.getModels().filter(m => m.input?.includes('text')).map(m => ({\n    model: m.id, label: m.name, supports_image_inputs: m.input.includes('image'),\n    reasoning_efforts: m.reasoning ? getSupportedThinkingLevels(m) : [],\n  })),\n}));\nprocess.stdout.write(JSON.stringify(providers));\n"
_CONFIGURATION_FIELDS = {'amazon-bedrock': [{'name': 'AWS_REGION', 'label': 'AWS region', 'placeholder': 'us-east-1'}], 'azure-openai-responses': [{'name': 'AZURE_OPENAI_BASE_URL', 'label': 'Azure endpoint', 'placeholder': 'https://your-resource.openai.azure.com'}], 'cloudflare-workers-ai': [{'name': 'CLOUDFLARE_ACCOUNT_ID', 'label': 'Cloudflare account ID'}], 'cloudflare-ai-gateway': [{'name': 'CLOUDFLARE_ACCOUNT_ID', 'label': 'Cloudflare account ID'}, {'name': 'CLOUDFLARE_GATEWAY_ID', 'label': 'Cloudflare gateway ID'}]}

@lru_cache(maxsize=4)
def _read_catalog(module_dir: str, modified_at: int) -> tuple[dict, ...]:
    del modified_at
    node = shutil.which('node')
    if not node:
        return ()
    try:
        result = subprocess.run([node, '--input-type=module', '-e', _CATALOG_SCRIPT, module_dir], capture_output=True, text=True, check=True, timeout=10)
        providers = json.loads(result.stdout)
        if not isinstance(providers, list):
            return ()
        return tuple((item for item in providers if isinstance(item, dict) and re.fullmatch('[a-z][a-z0-9-]{0,63}', item.get('provider', '')) and isinstance(item.get('label'), str) and isinstance(item.get('models'), list)))
    except (OSError, subprocess.SubprocessError, ValueError):
        logger.warning('Unable to read the installed Pi provider catalog')
        return ()

def pi_api_provider_catalog() -> tuple[dict, ...]:
    from openclass_core.services.pi_agent_runtime import pi_binary_path
    binary = pi_binary_path()
    if not binary:
        return ()
    try:
        path = Path(binary).resolve()
        for parent in path.parents:
            module = parent / 'node_modules' / '@earendil-works' / 'pi-ai' / 'dist'
            metadata = module.parent / 'package.json'
            if metadata.is_file() and (module / 'providers' / 'all.js').is_file():
                return _read_catalog(str(module), metadata.stat().st_mtime_ns)
        return ()
    except OSError:
        return ()

def pi_api_provider(provider: str) -> dict | None:
    normalized = provider.strip().lower().replace('_', '-')
    return next((item for item in pi_api_provider_catalog() if item['provider'] == normalized), None)

def pi_provider_configuration_fields(provider: str) -> list[dict[str, str]]:
    return [dict(field) for field in _CONFIGURATION_FIELDS.get(provider, [])]

def validate_pi_provider_configuration(provider: str, values: dict[str, str]) -> dict[str, str]:
    fields = pi_provider_configuration_fields(provider)
    allowed = {field['name'] for field in fields}
    if set(values) - allowed:
        raise ValueError('Unsupported provider configuration field.')
    result = {}
    for field in fields:
        value = values.get(field['name'], '').strip()
        if not value:
            raise ValueError(f"{field['label']} is required.")
        if len(value) > 512 or any((character.isspace() for character in value)):
            raise ValueError(f"{field['label']} is invalid.")
        if field['name'] == 'AZURE_OPENAI_BASE_URL':
            parsed = urlsplit(value)
            if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query or parsed.fragment or (parsed.port not in (None, 443)) or (not (parsed.hostname or '').endswith(('.openai.azure.com', '.cognitiveservices.azure.com', '.services.ai.azure.com'))):
                raise ValueError('Use an HTTPS Azure resource endpoint.')
        elif not re.fullmatch('[A-Za-z0-9_-]+', value):
            raise ValueError(f"{field['label']} is invalid.")
        result[field['name']] = value
    return result
