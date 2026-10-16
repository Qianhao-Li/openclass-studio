"""Identity-bound original cache. Persistent manifests never contain local paths."""
from __future__ import annotations
import hashlib
import os
import re
import uuid
from pathlib import Path
from openclass_core.services import workspace_state
from .config import GoogleSourceError
from .storage import scope_prefix

def original_cache_root():
    configured = os.getenv('OPENCLASS_GOOGLE_ORIGINAL_CACHE_DIR', '').strip()
    root = Path(configured).expanduser() if configured else workspace_state.UPLOAD_DIR / 'sources' / 'google-originals'
    if configured and (not root.is_absolute()):
        raise GoogleSourceError('The Google original-file cache directory must be an absolute path.')
    return root.resolve()

def _cache_target(source):
    digest = str(source.metadata.get('content_hash') or '')
    version = str(source.metadata.get('source_version') or '')
    if source.metadata.get('source_backend') not in {'google', 'local'} or not all((source.owner_user_id, source.package_id, source.id, version)) or (not re.fullmatch('[a-f0-9]{64}', digest)):
        raise GoogleSourceError('The Google original-file cache identity or hash is invalid.')
    folder = original_cache_root() / scope_prefix(source.owner_user_id, source.package_id, source.id, version)
    if folder.resolve() != folder:
        raise GoogleSourceError('The Google original-file cache directory does not belong to this source.')
    return folder / (digest + Path(source.file_name).suffix.lower())

def cache_original(source, content):
    digest = hashlib.sha256(content).hexdigest()
    if digest != source.metadata.get('content_hash'):
        raise GoogleSourceError('The original-file hash does not match for catalog fallback.')
    path = _cache_target(source)
    path.parent.mkdir(parents=True, exist_ok=True, mode=448)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            os.chmod(temporary, 384)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {'original_cache': cache_manifest(source)}

def original_path(source):
    path = _bound_cache_path(source)
    if not path.is_file():
        raise GoogleSourceError('Google is unavailable and no verifiable local original is available to the catalog AI.')
    if hashlib.sha256(path.read_bytes()).hexdigest() != source.metadata.get('content_hash'):
        raise GoogleSourceError('The original-file hash does not match for catalog fallback.')
    return path

def cache_manifest(source):
    _cache_target(source)
    identity = scope_prefix(source.owner_user_id, source.package_id, source.id, source.metadata['source_version'])
    return {'format_version': 1, 'present': True, 'identity_hash': hashlib.sha256(identity.encode()).hexdigest(), 'source_version': source.metadata['source_version'], 'content_hash': source.metadata['content_hash'], 'file_extension': Path(source.file_name).suffix.lower()}

def _bound_cache_path(source):
    expected = _cache_target(source)
    manifest = source.metadata.get('original_cache')
    legacy = source.metadata.get('local_source_path')
    if manifest is not None and manifest != cache_manifest(source):
        raise GoogleSourceError('The Google original-file cache manifest does not match the current source.')
    if legacy is not None:
        if not isinstance(legacy, str) or not Path(legacy).is_absolute() or Path(legacy).expanduser().resolve() != expected:
            raise GoogleSourceError('Google is unavailable and no local original belonging to this source is available to the catalog AI.')
    if manifest is None and legacy is None:
        raise GoogleSourceError('Google is unavailable and no local original belonging to this source is available to the catalog AI.')
    if expected.resolve() != expected:
        raise GoogleSourceError('Symbolic links are not allowed in the Google original-file cache.')
    return expected

def normalize_cached_source(source):
    """Upgrade legacy cache metadata on writes; native storage is left unchanged."""
    if source.metadata.get('source_backend') not in {'google', 'local'}:
        return source
    metadata = dict(source.metadata)
    if metadata.get('local_source_path') is not None or metadata.get('original_cache') is not None:
        _bound_cache_path(source)
        metadata['original_cache'] = cache_manifest(source)
    metadata.pop('local_source_path', None)
    metadata.pop('original_source_path', None)
    return source.model_copy(update={'metadata': metadata})

def delete_cached_original(source):
    """Delete only the identity-derived immutable file; never scan a cache root."""
    if source.metadata.get('original_cache') is None and source.metadata.get('local_source_path') is None:
        return
    path = _bound_cache_path(source)
    if not path.exists():
        return
    original_path(source).unlink(missing_ok=True)
