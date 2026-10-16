from __future__ import annotations
import hashlib
import io
import mimetypes
import os
import re
import sqlite3
import threading
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from openclass_core.models import now_iso
from openclass_core.services.board_asset_identity import board_asset_content_url, stable_board_asset_id
from openclass_core.services.config import DATA_DIR, ROOT_DIR, load_root_dotenv
MAX_BOARD_ASSET_BYTES = 25 * 1024 * 1024
MAX_BOARD_ASSET_PIXELS = 40000000
_MIME_EXTENSIONS = {'image/gif': '.gif', 'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}
_BOARD_ASSET_URL_RE = re.compile('^/api/board-assets/(?P<asset_id>basset_[A-Za-z0-9_-]+)/content$')

class BoardAssetError(ValueError):
    pass

@dataclass(frozen=True)
class BoardAssetRecord:
    id: str
    owner_user_id: str
    content_hash: str
    mime_type: str
    size_bytes: int
    storage_key: str
    file_name: str
    created_at: str

    @property
    def content_url(self) -> str:
        return board_asset_content_url(self.id)

@dataclass(frozen=True)
class BoardAssetReference:
    id: str
    asset_id: str
    owner_user_id: str
    lesson_id: str
    document_id: str
    source_visual_id: str
    created_at: str

def _path_from_env(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    path = Path(raw).expanduser() if raw else default
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path

class BoardAssetStore:
    """Permanent content-addressed images plus lesson/document references."""

    def __init__(self, database_path: Path, asset_dir: Path) -> None:
        self.database_path = database_path
        self.asset_dir = asset_dir.resolve()
        self._lock = threading.RLock()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def put_bytes(self, *, owner_user_id: str, lesson_id: str, content: bytes, mime_type: str, file_name: str='', source_visual_id: str='', document_id: str='') -> BoardAssetRecord:
        owner = owner_user_id.strip()
        lesson = lesson_id.strip()
        if not owner or not lesson:
            raise BoardAssetError('Board assets require an owner and lesson.')
        if not content:
            raise BoardAssetError('Board asset content is empty.')
        if len(content) > MAX_BOARD_ASSET_BYTES:
            raise BoardAssetError('Board asset exceeds the 25 MiB size limit.')
        normalized_mime = mime_type.split(';', 1)[0].strip().lower()
        input_mime = normalized_mime
        if normalized_mime in {'', 'application/octet-stream'}:
            normalized_mime = _mime_type_from_signature(content)
        content, normalized_mime = _normalize_legacy_raster(content, normalized_mime)
        if len(content) > MAX_BOARD_ASSET_BYTES:
            raise BoardAssetError('Normalized board asset exceeds the 25 MiB size limit.')
        extension = _MIME_EXTENSIONS.get(normalized_mime)
        if extension is None:
            raise BoardAssetError(f"Unsupported board asset MIME type: {normalized_mime or 'unknown'}.")
        if not _matches_image_signature(content, normalized_mime):
            raise BoardAssetError('Board asset bytes do not match the declared image MIME type.')
        _validate_board_raster(content, normalized_mime)
        content_hash = hashlib.sha256(content).hexdigest()
        asset_id = stable_board_asset_id(owner_user_id=owner, content_hash=content_hash)
        storage_key = f'{content_hash[:2]}/{content_hash}{extension}'
        safe_file_name = Path(file_name).name if file_name else f'{asset_id}{extension}'
        if normalized_mime != input_mime and input_mime not in {'', 'application/octet-stream'}:
            safe_file_name = f'{Path(safe_file_name).stem}{extension}'
        elif not Path(safe_file_name).suffix:
            safe_file_name = f'{safe_file_name}{extension}'
        created_at = now_iso()
        with self._lock:
            existing = self.get_by_content_hash(owner_user_id=owner, content_hash=content_hash)
            if existing is not None and self.read_bytes(existing.id, owner) is not None:
                self.add_reference(asset_id=existing.id, owner_user_id=owner, lesson_id=lesson, document_id=document_id, source_visual_id=source_visual_id)
                return existing
            target = self._resolve_storage_key(storage_key)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f'.{target.name}.{os.getpid()}.{threading.get_ident()}.tmp')
            try:
                temporary.write_bytes(content)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            with self._connect() as conn:
                with conn:
                    columns = self._board_asset_columns(conn)
                    insert_columns = ['id', 'owner_user_id', 'content_hash', 'mime_type', 'size_bytes', 'storage_key', 'file_name', 'created_at']
                    values: list[object] = [asset_id, owner, content_hash, normalized_mime, len(content), storage_key, safe_file_name, created_at]
                    if 'lesson_id' in columns:
                        insert_columns.append('lesson_id')
                        values.append(lesson)
                    if 'source_visual_id' in columns:
                        insert_columns.append('source_visual_id')
                        values.append(source_visual_id.strip())
                    placeholders = ', '.join(('?' for _ in values))
                    conn.execute(f"\n                        INSERT INTO board_assets({', '.join(insert_columns)})\n                        VALUES ({placeholders})\n                        ON CONFLICT(id) DO UPDATE SET\n                            file_name = CASE\n                                WHEN board_assets.file_name = '' THEN excluded.file_name\n                                ELSE board_assets.file_name\n                            END\n                        ", values)
                    self._insert_reference(conn, asset_id=asset_id, owner_user_id=owner, lesson_id=lesson, document_id=document_id, source_visual_id=source_visual_id)
            record = self.get(asset_id, owner)
            if record is None:
                raise BoardAssetError('Board asset metadata was not persisted.')
            return record

    def add_reference(self, *, asset_id: str, owner_user_id: str, lesson_id: str, document_id: str='', source_visual_id: str='') -> BoardAssetReference:
        if self.get(asset_id, owner_user_id) is None:
            raise BoardAssetError('Cannot reference an unavailable board asset.')
        with self._lock:
            with self._connect() as conn:
                with conn:
                    reference_id = self._insert_reference(conn, asset_id=asset_id, owner_user_id=owner_user_id, lesson_id=lesson_id, document_id=document_id, source_visual_id=source_visual_id)
                row = conn.execute('\n                    SELECT id, asset_id, owner_user_id, lesson_id, document_id,\n                           source_visual_id, created_at\n                    FROM board_asset_refs WHERE id = ?\n                    ', (reference_id,)).fetchone()
        if row is None:
            raise BoardAssetError('Board asset reference was not persisted.')
        return _reference_from_row(row)

    def references_for_lesson(self, *, owner_user_id: str, lesson_id: str) -> list[BoardAssetReference]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute('\n                    SELECT id, asset_id, owner_user_id, lesson_id, document_id,\n                           source_visual_id, created_at\n                    FROM board_asset_refs\n                    WHERE owner_user_id = ? AND lesson_id = ?\n                    ORDER BY created_at, id\n                    ', (owner_user_id, lesson_id)).fetchall()
        return [_reference_from_row(row) for row in rows]

    def remove_lesson_references(self, *, owner_user_id: str, lesson_id: str) -> None:
        with self._lock:
            orphaned_records: list[BoardAssetRecord] = []
            with self._connect() as conn:
                with conn:
                    asset_ids = [str(row['asset_id']) for row in conn.execute('\n                            SELECT DISTINCT asset_id FROM board_asset_refs\n                            WHERE owner_user_id = ? AND lesson_id = ?\n                            ', (owner_user_id, lesson_id)).fetchall()]
                    conn.execute('DELETE FROM board_asset_refs WHERE owner_user_id = ? AND lesson_id = ?', (owner_user_id, lesson_id))
                    for asset_id in asset_ids:
                        still_referenced = conn.execute('SELECT 1 FROM board_asset_refs WHERE asset_id = ? LIMIT 1', (asset_id,)).fetchone()
                        if still_referenced is not None:
                            continue
                        row = conn.execute('\n                            SELECT id, owner_user_id, content_hash, mime_type,\n                                   size_bytes, storage_key, file_name, created_at\n                            FROM board_assets WHERE id = ? AND owner_user_id = ?\n                            ', (asset_id, owner_user_id)).fetchone()
                        if row is None:
                            continue
                        orphaned_records.append(_record_from_row(row))
                        conn.execute('DELETE FROM board_assets WHERE id = ? AND owner_user_id = ?', (asset_id, owner_user_id))
            for record in orphaned_records:
                try:
                    self.resolve_path(record).unlink(missing_ok=True)
                except (BoardAssetError, OSError):
                    continue

    def get(self, asset_id: str, owner_user_id: str) -> BoardAssetRecord | None:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute('\n                    SELECT id, owner_user_id, content_hash, mime_type,\n                           size_bytes, storage_key, file_name, created_at\n                    FROM board_assets\n                    WHERE id = ? AND owner_user_id = ?\n                    ', (asset_id, owner_user_id)).fetchone()
        return _record_from_row(row) if row is not None else None

    def get_by_content_hash(self, *, owner_user_id: str, content_hash: str) -> BoardAssetRecord | None:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute('\n                    SELECT id, owner_user_id, content_hash, mime_type,\n                           size_bytes, storage_key, file_name, created_at\n                    FROM board_assets\n                    WHERE owner_user_id = ? AND content_hash = ?\n                    ', (owner_user_id, content_hash)).fetchone()
        return _record_from_row(row) if row is not None else None

    def read_bytes(self, asset_id: str, owner_user_id: str) -> tuple[BoardAssetRecord, bytes] | None:
        record = self.get(asset_id, owner_user_id)
        if record is None:
            return None
        try:
            path = self.resolve_path(record)
            if not path.is_file():
                return None
            size_on_disk = path.stat().st_size
        except (BoardAssetError, OSError):
            return None
        if size_on_disk != record.size_bytes or size_on_disk > MAX_BOARD_ASSET_BYTES:
            return None
        try:
            content = path.read_bytes()
        except OSError:
            return None
        if len(content) != record.size_bytes or hashlib.sha256(content).hexdigest() != record.content_hash:
            return None
        return (record, content)

    def resolve_path(self, record: BoardAssetRecord) -> Path:
        return self._resolve_storage_key(record.storage_key)

    def _resolve_storage_key(self, storage_key: str) -> Path:
        candidate = (self.asset_dir / storage_key).resolve()
        if candidate != self.asset_dir and self.asset_dir not in candidate.parents:
            raise BoardAssetError('Board asset storage key escapes the asset directory.')
        return candidate

    def _initialize(self) -> None:
        with self._lock:
            with self._connect() as conn:
                with conn:
                    conn.execute("\n                        CREATE TABLE IF NOT EXISTS board_assets (\n                            id TEXT PRIMARY KEY,\n                            owner_user_id TEXT NOT NULL,\n                            content_hash TEXT NOT NULL,\n                            mime_type TEXT NOT NULL,\n                            size_bytes INTEGER NOT NULL,\n                            storage_key TEXT NOT NULL,\n                            file_name TEXT NOT NULL DEFAULT '',\n                            created_at TEXT NOT NULL\n                        )\n                        ")
                    conn.execute('\n                        CREATE UNIQUE INDEX IF NOT EXISTS idx_board_assets_owner_hash\n                        ON board_assets(owner_user_id, content_hash)\n                        ')
                    conn.execute("\n                        CREATE TABLE IF NOT EXISTS board_asset_refs (\n                            id TEXT PRIMARY KEY,\n                            asset_id TEXT NOT NULL,\n                            owner_user_id TEXT NOT NULL,\n                            lesson_id TEXT NOT NULL,\n                            document_id TEXT NOT NULL DEFAULT '',\n                            source_visual_id TEXT NOT NULL DEFAULT '',\n                            created_at TEXT NOT NULL,\n                            FOREIGN KEY(asset_id) REFERENCES board_assets(id) ON DELETE CASCADE\n                        )\n                        ")
                    conn.execute('\n                        CREATE UNIQUE INDEX IF NOT EXISTS idx_board_asset_refs_scope\n                        ON board_asset_refs(\n                            asset_id, owner_user_id, lesson_id, document_id, source_visual_id\n                        )\n                        ')
                    conn.execute('\n                        CREATE INDEX IF NOT EXISTS idx_board_asset_refs_lesson\n                        ON board_asset_refs(owner_user_id, lesson_id)\n                        ')

    def _insert_reference(self, conn: sqlite3.Connection, *, asset_id: str, owner_user_id: str, lesson_id: str, document_id: str, source_visual_id: str) -> str:
        owner = owner_user_id.strip()
        lesson = lesson_id.strip()
        document = document_id.strip()
        visual = source_visual_id.strip()
        if not owner or not lesson:
            raise BoardAssetError('Board asset references require an owner and lesson.')
        identity = '\x00'.join((asset_id, owner, lesson, document, visual))
        reference_id = f"bref_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:40]}"
        conn.execute('\n            INSERT INTO board_asset_refs(\n                id, asset_id, owner_user_id, lesson_id, document_id,\n                source_visual_id, created_at\n            ) VALUES (?, ?, ?, ?, ?, ?, ?)\n            ON CONFLICT(id) DO NOTHING\n            ', (reference_id, asset_id, owner, lesson, document, visual, now_iso()))
        return reference_id

    @staticmethod
    def _board_asset_columns(conn: sqlite3.Connection) -> set[str]:
        return {str(row['name']) for row in conn.execute('PRAGMA table_info(board_assets)')}

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys = ON')
            conn.execute('PRAGMA busy_timeout = 5000')
            conn.execute('PRAGMA journal_mode = WAL')
            conn.execute('PRAGMA synchronous = NORMAL')
            with conn:
                yield conn
        finally:
            conn.close()

def _record_from_row(row: sqlite3.Row) -> BoardAssetRecord:
    return BoardAssetRecord(id=str(row['id']), owner_user_id=str(row['owner_user_id']), content_hash=str(row['content_hash']), mime_type=str(row['mime_type']), size_bytes=int(row['size_bytes']), storage_key=str(row['storage_key']), file_name=str(row['file_name']), created_at=str(row['created_at']))

def _reference_from_row(row: sqlite3.Row) -> BoardAssetReference:
    return BoardAssetReference(id=str(row['id']), asset_id=str(row['asset_id']), owner_user_id=str(row['owner_user_id']), lesson_id=str(row['lesson_id']), document_id=str(row['document_id']), source_visual_id=str(row['source_visual_id']), created_at=str(row['created_at']))
_default_store: BoardAssetStore | None = None
_default_store_lock = threading.Lock()

def get_board_asset_store() -> BoardAssetStore:
    from openclass_core.services.staged_board_assets import current_staged_board_assets
    staged = current_staged_board_assets()
    if staged is not None:
        return staged
    from openclass_core.services.learning_session_context import learning_session_scope
    scope = learning_session_scope.get()
    if scope is not None:
        return scope.board_asset_store
    store = _unscoped_board_asset_store()
    from openclass_core.services.workspace_state import current_repository_workspace_scope, get_repository_store
    repository = current_repository_workspace_scope()
    if repository is not None:
        from openclass_core.services.ridoc_board_assets import RepositoryBoardAssetStore
        return RepositoryBoardAssetStore(store, get_repository_store(), repository.repository_id, repository.owner_user_id)
    return store

def _unscoped_board_asset_store() -> BoardAssetStore:
    global _default_store
    if _default_store is not None:
        return _default_store
    with _default_store_lock:
        if _default_store is None:
            load_root_dotenv()
            database_path = _path_from_env('OPENCLASS_DATABASE_PATH', DATA_DIR / 'openclass.sqlite3')
            asset_dir = _path_from_env('OPENCLASS_BOARD_ASSET_DIR', database_path.parent / 'board-assets')
            _default_store = BoardAssetStore(database_path, asset_dir)
    return _default_store

def guess_image_mime_type(file_name: str) -> str:
    guessed, _ = mimetypes.guess_type(file_name)
    return guessed or 'application/octet-stream'

def board_asset_id_from_url(value: str) -> str:
    match = _BOARD_ASSET_URL_RE.fullmatch((value or '').strip())
    return match.group('asset_id') if match else ''

def _mime_type_from_signature(content: bytes) -> str:
    for mime_type in _MIME_EXTENSIONS:
        if _matches_image_signature(content, mime_type):
            return mime_type
    return ''

def _matches_image_signature(content: bytes, mime_type: str) -> bool:
    if mime_type == 'image/png':
        return content.startswith(b'\x89PNG\r\n\x1a\n')
    if mime_type == 'image/jpeg':
        return content.startswith(b'\xff\xd8\xff')
    if mime_type == 'image/gif':
        return content.startswith((b'GIF87a', b'GIF89a'))
    if mime_type == 'image/webp':
        return len(content) >= 12 and content.startswith(b'RIFF') and (content[8:12] == b'WEBP')
    return False

def _normalize_legacy_raster(content: bytes, mime_type: str) -> tuple[bytes, str]:
    if mime_type not in {'image/tiff', 'image/bmp'}:
        return (content, mime_type)
    try:
        from PIL import Image
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                if image.width <= 0 or image.height <= 0 or image.width * image.height > MAX_BOARD_ASSET_PIXELS:
                    raise BoardAssetError('Legacy raster board asset dimensions are too large.')
                converted = image.convert('RGBA' if 'A' in image.getbands() else 'RGB')
                output = io.BytesIO()
                converted.save(output, format='PNG', optimize=True)
    except BoardAssetError:
        raise
    except Exception as exc:
        raise BoardAssetError('Legacy raster board asset could not be normalized.') from exc
    return (output.getvalue(), 'image/png')

def _validate_board_raster(content: bytes, mime_type: str) -> None:
    expected_format = {'image/png': 'PNG', 'image/jpeg': 'JPEG', 'image/gif': 'GIF', 'image/webp': 'WEBP'}.get(mime_type)
    try:
        from PIL import Image
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                frame_count = max(1, int(getattr(image, 'n_frames', 1)))
                if image.width <= 0 or image.height <= 0 or image.width * image.height * frame_count > MAX_BOARD_ASSET_PIXELS:
                    raise BoardAssetError('Board asset decoded pixel budget is too large.')
                if str(image.format or '').upper() != expected_format:
                    raise BoardAssetError('Board asset media type does not match its image bytes.')
                image.verify()
    except BoardAssetError:
        raise
    except Exception as exc:
        raise BoardAssetError('Board asset bytes are not a valid raster image.') from exc
