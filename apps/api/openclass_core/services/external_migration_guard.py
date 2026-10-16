"""Require a verified offline backup before changing legacy account state."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import stat

class ExternalMigrationBlocked(RuntimeError):
    status_code = 503

def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()

def require_verified_backup() -> None:
    """The operator must stop writes, audit, and back up the matching release.

    This check reads only the backup marker and its two verified artifacts.
    Absence or corruption never triggers a compatibility migration.
    """
    raw_path = os.getenv('OPENCLASS_EXTERNAL_MIGRATION_BACKUP_MANIFEST', '').strip()
    if not raw_path:
        raise ExternalMigrationBlocked('Run and verify the external-account migration backup before upgrading legacy connections.')
    marker = Path(raw_path).expanduser()
    if not marker.is_absolute() or marker.name != 'backup-manifest.json' or marker.is_symlink() or (not marker.is_file()):
        raise ExternalMigrationBlocked('The external-account backup manifest is unavailable or invalid.')
    backup_dir = marker.parent
    if backup_dir.is_symlink() or not backup_dir.is_dir() or stat.S_IMODE(backup_dir.stat().st_mode) & 63:
        raise ExternalMigrationBlocked('The external-account backup directory is not private.')
    if stat.S_IMODE(marker.stat().st_mode) & 63:
        raise ExternalMigrationBlocked('The external-account backup manifest is not private.')
    try:
        manifest = json.loads(marker.read_text(encoding='utf-8'))
        if not isinstance(manifest, dict) or any((manifest.get(key) is not True for key in ('backup_verified', 'migration_allowed', 'services_stopped_attested', 'no_open_database_handles'))):
            raise ValueError('backup incomplete')
        for name, field in (('runtime.sqlite3', 'sqlite_sha256'), ('persistent-files.tar.gz', 'archive_sha256')):
            artifact = backup_dir / name
            digest = manifest.get(field)
            if artifact.is_symlink() or not artifact.is_file() or (not isinstance(digest, str)) or (len(digest) != 64) or (_digest(artifact) != digest):
                raise ValueError('backup artifact changed')
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ExternalMigrationBlocked('The external-account backup cannot be verified; legacy state was left unchanged.') from exc
