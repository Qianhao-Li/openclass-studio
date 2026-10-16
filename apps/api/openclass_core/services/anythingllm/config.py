"""Private source runtime configuration, independent of original storage."""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from openclass_core.services.google_sources.config import GoogleSourceError
UPSTREAM_REVISION = 'feb04ca0a57cda6d0b3a69c62578f0af44d388fc'
CONTRACT = 'openclass_anythingllm_v1'
from openclass_core.services.config import ROOT_DIR
ROOT = ROOT_DIR

def selected(source=None):
    provider = (source.metadata.get('source_processor') if source else None) or os.getenv('OPENCLASS_SOURCE_PROCESSOR', 'native')
    if provider not in {'native', 'anythingllm'}:
        raise GoogleSourceError('OPENCLASS_SOURCE_PROCESSOR must be native or anythingllm.')
    return provider == 'anythingllm'

def eligible(file_name, mime_type):
    from openclass_core.services.source_basic_formats import MEDIA
    return Path(file_name).suffix.lower() not in MEDIA and (not mime_type.startswith(('audio/', 'video/')))

@dataclass(frozen=True)
class AnythingLLMConfig:
    storage: Path
    server_port: int = 3321
    collector_port: int = 8895
    autostart: bool = True

    @classmethod
    def from_env(cls):
        value = os.getenv('OPENCLASS_ANYTHINGLLM_STORAGE_DIR') or str(Path.home() / '.local/share/openclass/anythingllm')
        root = Path(value).expanduser()
        if not root.is_absolute() or root.resolve().is_relative_to(ROOT) or root.is_symlink():
            raise GoogleSourceError('AnythingLLM requires a persistent absolute storage directory outside the checkout.')
        return cls(root, int(os.getenv('OPENCLASS_ANYTHINGLLM_SERVER_PORT', '3321')), int(os.getenv('OPENCLASS_ANYTHINGLLM_COLLECTOR_PORT', '8895')), os.getenv('OPENCLASS_ANYTHINGLLM_AUTOSTART', 'true').lower() == 'true')

    @property
    def endpoint(self):
        return f'http://127.0.0.1:{self.server_port}'

    @property
    def key_file(self):
        return self.storage / 'service-key'

    def key(self):
        if not self.key_file.is_file() or self.key_file.is_symlink() or self.key_file.stat().st_mode & 63:
            raise GoogleSourceError('The AnythingLLM service key is missing or is not private.')
        value = self.key_file.read_text().strip()
        if len(value) < 32:
            raise GoogleSourceError('The AnythingLLM service key is invalid.')
        return value
