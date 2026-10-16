"""Turn-local board images enter the course only with a successful RIDOC outcome."""
from __future__ import annotations
import hashlib
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from openclass_core.models import now_iso
from openclass_core.services.board_asset_identity import stable_board_asset_id
from openclass_core.services.board_asset_store import BoardAssetError, BoardAssetRecord, MAX_BOARD_ASSET_BYTES, _MIME_EXTENSIONS, _matches_image_signature, _mime_type_from_signature, _normalize_legacy_raster, _validate_board_raster
_staged: ContextVar[StagedBoardAssetStore | None] = ContextVar('staged_board_assets', default=None)

class StagedBoardAssetStore:

    def __init__(self, underlying, owner_user_id, archive):
        self.underlying = underlying
        self.owner_user_id = owner_user_id
        self.archive = archive
        self._temporary = tempfile.TemporaryDirectory(prefix='.board-turn-', dir=underlying.asset_dir.parent)
        self.items: dict[str, tuple[BoardAssetRecord, Path]] = {}

    def __getattr__(self, name):
        return getattr(self.underlying, name)

    def put_bytes(self, *, owner_user_id, lesson_id, content, mime_type, file_name='', source_visual_id='', document_id=''):
        if owner_user_id != self.owner_user_id or not lesson_id.strip():
            raise BoardAssetError("The staged image is outside this turn's owner or lesson.")
        if not isinstance(content, bytes) or not content or len(content) > MAX_BOARD_ASSET_BYTES:
            raise BoardAssetError('The staged image has invalid or oversized bytes.')
        mime = mime_type.split(';', 1)[0].strip().lower()
        if mime in {'', 'application/octet-stream'}:
            mime = _mime_type_from_signature(content)
        content, mime = _normalize_legacy_raster(content, mime)
        if len(content) > MAX_BOARD_ASSET_BYTES or mime not in _MIME_EXTENSIONS or (not _matches_image_signature(content, mime)):
            raise BoardAssetError('The staged image does not match a supported image format.')
        _validate_board_raster(content, mime)
        digest = hashlib.sha256(content).hexdigest()
        asset_id = stable_board_asset_id(owner_user_id=owner_user_id, content_hash=digest)
        record = BoardAssetRecord(asset_id, owner_user_id, digest, mime, len(content), '', Path(file_name).name or asset_id + _MIME_EXTENSIONS[mime], now_iso())
        path = Path(self._temporary.name) / (digest + _MIME_EXTENSIONS[mime])
        path.write_bytes(content)
        self.items[asset_id] = (record, path)
        return record

    def read_bytes(self, asset_id, owner_user_id):
        if owner_user_id != self.owner_user_id:
            return None
        if asset_id in self.items:
            record, path = self.items[asset_id]
            return (record, path.read_bytes())
        stored = self.underlying.read_bytes(asset_id, owner_user_id)
        if stored is not None:
            return stored
        from openclass_core.services.ridoc_board_assets import read_embedded_board_asset
        return read_embedded_board_asset(self.archive, asset_id, owner_user_id)

    def get(self, asset_id, owner_user_id):
        stored = self.read_bytes(asset_id, owner_user_id)
        return stored[0] if stored else None

    def resolve_path(self, record):
        staged = self.items.get(record.id)
        return staged[1] if staged is not None else self.underlying.resolve_path(record)

    def get_by_content_hash(self, *, owner_user_id, content_hash):
        return self.get(stable_board_asset_id(owner_user_id=owner_user_id, content_hash=content_hash), owner_user_id)

    def add_reference(self, *, asset_id, owner_user_id, lesson_id, document_id='', source_visual_id=''):
        from openclass_core.services.board_asset_identity import stable_board_asset_reference_id
        from openclass_core.services.board_asset_store import BoardAssetReference
        if not lesson_id or self.read_bytes(asset_id, owner_user_id) is None:
            raise BoardAssetError('The staged image reference is not authorized.')
        identity = stable_board_asset_reference_id(asset_id=asset_id, owner_user_id=owner_user_id, lesson_id=lesson_id, document_id=document_id, source_visual_id=source_visual_id)
        return BoardAssetReference(identity, asset_id, owner_user_id, lesson_id, document_id, source_visual_id, now_iso())

    def document_payloads(self, document):
        from openclass_core.services.learning_session_assets import document_asset_ids
        from openclass_core.services.ridoc_v3 import RidocV3Error
        payloads = {}
        for asset_id in document_asset_ids(document):
            stored = self.read_bytes(asset_id, self.owner_user_id)
            if stored is None:
                raise RidocV3Error('A board image is missing; the complete turn cannot be saved.')
            record, raw = stored
            digest = hashlib.sha256(raw).hexdigest()
            if record.owner_user_id != self.owner_user_id or record.id != asset_id or digest != record.content_hash:
                raise RidocV3Error('The board image identity no longer matches its actual bytes.')
            payloads[digest] = raw
        return payloads

def current_staged_board_assets():
    return _staged.get()

@contextmanager
def stage_board_assets(*, owner_user_id, archive):
    from openclass_core.services.board_asset_store import get_board_asset_store
    parent = _staged.get()
    if parent is not None:
        if parent.owner_user_id != owner_user_id:
            raise BoardAssetError('A board-image transaction cannot change owners.')
        yield parent
        return
    store = StagedBoardAssetStore(get_board_asset_store(), owner_user_id, archive)
    token = _staged.set(store)
    try:
        yield store
    finally:
        _staged.reset(token)
        store.items.clear()
        store._temporary.cleanup()
