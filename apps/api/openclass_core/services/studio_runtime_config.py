"""Explicit local paths. Starting Studio never reads the platform .env."""
from __future__ import annotations
from dataclasses import dataclass
import os
import hashlib
from pathlib import Path
import sys

@dataclass(frozen=True)
class StudioRuntimeConfig:
    data_root: Path
    launch_token: str
    web_origin: str = 'http://127.0.0.1:3510'
    api_origin: str = 'http://127.0.0.1:8510'

    @classmethod
    def from_env(cls):
        default = Path.home() / 'Library/Application Support/OpenClass Studio' if sys.platform == 'darwin' else Path.home() / '.local/share/openclass-studio'
        root = Path(os.getenv('OPENCLASS_STUDIO_DATA_DIR') or default).expanduser()
        checkout = Path(os.getenv('OPENCLASS_PROJECT_ROOT') or Path(__file__).resolve().parents[4]).resolve()
        if not root.is_absolute() or root.resolve().is_relative_to(checkout) or root.is_symlink():
            raise ValueError('Studio requires an absolute persistent data directory outside the checkout.')
        return cls(root, os.getenv('OPENCLASS_STUDIO_LAUNCH_TOKEN', ''), os.getenv('OPENCLASS_STUDIO_WEB_ORIGIN', 'http://127.0.0.1:3510'), os.getenv('OPENCLASS_STUDIO_API_ORIGIN', 'http://127.0.0.1:8510'))

    def configure_environment(self):
        root = self.data_root
        source_port = 20000 + int(hashlib.sha256(str(root).encode()).hexdigest()[:8], 16) % 14000 * 2
        paths = {'OPENCLASS_DATA_DIR': root, 'OPENCLASS_DATABASE_PATH': root / 'runtime.sqlite3', 'OPENCLASS_LEGACY_STORE_PATH': root / 'legacy-unused.json', 'OPENCLASS_UPLOAD_DIR': root / 'sources/originals', 'OPENCLASS_EXPORT_DIR': root / 'exports', 'OPENCLASS_RIDOC_WORKSPACE_DIR': root / 'courses', 'OPENCLASS_RIDOC_REPOSITORY_DIR': root / 'remote-cache', 'OPENCLASS_LEARNING_SESSION_DIR': root / 'previews', 'OPENCLASS_PI_RUNTIME_ROOT': root / 'pi', 'OPENCLASS_CODEX_HOME': root / 'model-accounts', 'OPENCLASS_BOARD_ASSET_DIR': root / 'assets', 'AI_USAGE_LOG_PATH': root / 'logs/ai-usage.jsonl', 'OPENCLASS_GOOGLE_ORIGINAL_CACHE_DIR': root / 'sources/original-cache', 'OPENCLASS_ANYTHINGLLM_STORAGE_DIR': root / 'sources/anythingllm'}
        os.environ.update({name: str(path) for name, path in paths.items()})
        os.environ.update({'OPENCLASS_STUDIO_MODE': 'local', 'OPENCLASS_COURSE_STORE_MODE': 'ridoc_required', 'OPENCLASS_SOURCE_BACKEND': 'local', 'OPENCLASS_SOURCE_PROCESSOR': os.getenv('OPENCLASS_STUDIO_SOURCE_PROCESSOR', 'native'), 'OPENCLASS_LOCAL_RUNTIME': 'true', 'OPENCLASS_CREDIT_BILLING_ENABLED': 'false'})
        os.environ.update(OPENCLASS_ANYTHINGLLM_SERVER_PORT=os.getenv('OPENCLASS_STUDIO_SOURCE_SERVER_PORT', str(source_port)), OPENCLASS_ANYTHINGLLM_COLLECTOR_PORT=os.getenv('OPENCLASS_STUDIO_SOURCE_COLLECTOR_PORT', str(source_port + 1)))
