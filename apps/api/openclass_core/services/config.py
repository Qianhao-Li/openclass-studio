from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv
API_BASE_DIR = Path(__file__).resolve().parents[2]
ROOT_DIR = Path(os.getenv('OPENCLASS_PROJECT_ROOT') or API_BASE_DIR.parents[1]).expanduser().resolve()
DATA_DIR = Path(os.environ['OPENCLASS_DATA_DIR']).expanduser() if os.getenv('OPENCLASS_STUDIO_MODE') == 'local' and os.getenv('OPENCLASS_DATA_DIR') else ROOT_DIR / 'apps/api/data'
EXPLICIT_ENV_FILE_VARIABLE = 'OPENCLASS_ENV_FILE'

def _explicit_env_file() -> Path | None:
    configured = os.getenv(EXPLICIT_ENV_FILE_VARIABLE, '').strip()
    return Path(configured).expanduser() if configured else None

def load_root_dotenv() -> None:
    if os.getenv('OPENCLASS_STUDIO_MODE') == 'local':
        return
    explicit_env = _explicit_env_file()
    root_env = ROOT_DIR / '.env'
    if explicit_env is not None:
        if not explicit_env.is_file():
            raise FileNotFoundError(f'{EXPLICIT_ENV_FILE_VARIABLE} points to a missing file: {explicit_env}')
        load_dotenv(explicit_env, override=False)
        if root_env.is_file() and root_env != explicit_env:
            load_dotenv(root_env, override=False)
        return
    if root_env.exists():
        load_dotenv(root_env, override=False)
        return
    load_dotenv(override=False)
