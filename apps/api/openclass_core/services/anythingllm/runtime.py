"""One private source server and collector; model credentials stay in OpenClass."""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import socket
import time
import httpx
from .config import AnythingLLMConfig, CONTRACT, ROOT, UPSTREAM_REVISION
from openclass_core.services.google_sources.config import GoogleSourceError

def healthy(config, *, collector=False):
    endpoint = f'http://127.0.0.1:{config.collector_port}' if collector else config.endpoint
    try:
        result = httpx.get(endpoint + '/health', timeout=1, trust_env=False).json()
        return result.get('instance_id') == hashlib.sha256(config.key().encode()).hexdigest() and result.get('contract') == CONTRACT and (result.get('upstream_revision') == UPSTREAM_REVISION) and (result.get('local_ocr') is False) and (result.get('catalog_engine') == 'anythingllm_source_catalog_v2')
    except (httpx.HTTPError, ValueError, GoogleSourceError):
        return False

def ensure_started(config=None):
    config = config or AnythingLLMConfig.from_env()
    if healthy(config) and healthy(config, collector=True):
        config.key()
        return
    if not config.autostart:
        raise GoogleSourceError('AnythingLLM is unavailable. Start its configured source server and collector.')
    directory = ROOT / 'integrations/anythingllm'
    node = shutil.which('node')
    if not node or not (directory / 'node_modules/@lancedb/lancedb').is_dir():
        raise GoogleSourceError('AnythingLLM dependencies are missing. Run npm ci --prefix integrations/anythingllm first.')
    config.storage.mkdir(parents=True, exist_ok=True, mode=448)
    with (config.storage / 'runtime.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if healthy(config) and healthy(config, collector=True):
            return
        for port, collector in ((config.server_port, False), (config.collector_port, True)):
            if healthy(config, collector=collector):
                continue
            with socket.socket() as listener:
                try:
                    listener.bind(('127.0.0.1', port))
                except OSError as error:
                    raise GoogleSourceError('An unrelated source runtime occupies the configured port. Choose separate source ports.') from error
        if not config.key_file.exists():
            descriptor = os.open(config.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 384)
            with os.fdopen(descriptor, 'w') as target:
                target.write(secrets.token_hex(32))
                target.flush()
                os.fsync(target.fileno())
        config.key()
        environment = {name: value for name, value in os.environ.items() if name in {'PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'SSL_CERT_FILE', 'NODE_EXTRA_CA_CERTS'}}
        environment.update(NODE_ENV='production', NODE_PATH=str(directory / 'node_modules'), STORAGE_DIR=str(config.storage), OPENCLASS_SOURCE_ONLY='true', OPENCLASS_ANYTHINGLLM_SERVICE_KEY_FILE=str(config.key_file), OPENCLASS_ANYTHINGLLM_SERVER_PORT=str(config.server_port), OPENCLASS_ANYTHINGLLM_COLLECTOR_PORT=str(config.collector_port), EMBEDDING_MODEL_PREF='MintplexLabs/multilingual-e5-small')
        manifest = config.storage / 'runtime-pids.json'
        processes = json.loads(manifest.read_text()) if manifest.is_file() else []
        for script, collector in (('collector.cjs', True), ('server.cjs', False)):
            if healthy(config, collector=collector):
                continue
            with (config.storage / script.replace('.cjs', '.log')).open('ab') as log:
                process = subprocess.Popen([node, str(directory / script)], cwd=directory, env=environment, stdout=log, stderr=log, start_new_session=True)
            processes.append({'pid': process.pid, 'script': script})
        temporary = manifest.with_suffix('.new')
        with temporary.open('w') as target:
            os.chmod(temporary, 384)
            target.write(json.dumps(processes))
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, manifest)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if healthy(config) and healthy(config, collector=True):
                return
            time.sleep(0.1)
    raise GoogleSourceError('AnythingLLM could not start. Check its runtime logs and configured ports.')
