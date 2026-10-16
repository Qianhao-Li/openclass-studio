from __future__ import annotations

import os
import shutil
import tempfile
import socket
import signal
import json
import subprocess
from pathlib import Path

import pytest


_TEST_RUNTIME_ROOT = Path(tempfile.mkdtemp(prefix="openclass-pytest-"))
os.environ["OPENCLASS_TEST_RUNTIME_ROOT"] = str(_TEST_RUNTIME_ROOT)
os.environ["OPENCLASS_DATABASE_PATH"] = str(_TEST_RUNTIME_ROOT / "openclass.sqlite3")
os.environ["AI_USAGE_LOG_PATH"] = str(_TEST_RUNTIME_ROOT / "logs" / "ai-usage.jsonl")
os.environ["OPENCLASS_UPLOAD_DIR"] = str(_TEST_RUNTIME_ROOT / "uploads")
os.environ["OPENCLASS_EXPORT_DIR"] = str(_TEST_RUNTIME_ROOT / "exports")
os.environ["OPENCLASS_RIDOC_WORKSPACE_DIR"] = str(_TEST_RUNTIME_ROOT / "ridoc-workspace")
os.environ["OPENCLASS_RIDOC_REPOSITORY_DIR"] = str(_TEST_RUNTIME_ROOT / "ridoc-repositories")
os.environ["OPENCLASS_LEARNING_SESSION_DIR"] = str(_TEST_RUNTIME_ROOT / "learning-sessions")
os.environ['OPENCLASS_ANYTHINGLLM_STORAGE_DIR']=str(_TEST_RUNTIME_ROOT/'anythingllm')
for variable in ('OPENCLASS_ANYTHINGLLM_SERVER_PORT','OPENCLASS_ANYTHINGLLM_COLLECTOR_PORT'):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        os.environ[variable]=str(listener.getsockname()[1])
# OCR-specific legacy tests opt in explicitly; production defaults to disabled.


@pytest.fixture(scope="session", autouse=True)
def isolated_test_runtime() -> None:
    yield
    manifest=_TEST_RUNTIME_ROOT/'anythingllm/runtime-pids.json'
    if manifest.exists():
        for child in json.loads(manifest.read_text()):
            if child.get('script') not in {'collector.cjs','server.cjs'}:continue
            command=subprocess.run(['ps','-p',str(child['pid']),'-o','command='],capture_output=True,text=True).stdout
            script=Path(__file__).resolve().parents[3]/'integrations/anythingllm'/child['script']
            if str(script) in command:
                try:os.kill(child['pid'],signal.SIGTERM)
                except ProcessLookupError:pass
    shutil.rmtree(_TEST_RUNTIME_ROOT, ignore_errors=True)
