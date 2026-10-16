"""Read referenced portable images without rewriting historical asset identities."""
from __future__ import annotations
import io
import re
from PIL import Image, UnidentifiedImageError
from openclass_core.models import BoardDocument
from openclass_core.services.board_asset_store import BoardAssetRecord
from openclass_core.services.learning_session_assets import document_asset_ids

def read_embedded_board_asset(archive, asset_id, owner_user_id):
    if not re.fullmatch('basset_[A-Za-z0-9_-]+_[a-f0-9]{32}', asset_id):
        return None
    referenced = set().union(*(document_asset_ids(BoardDocument.model_validate(value)) for value in archive.documents.values()))
    if asset_id not in referenced:
        return None
    suffix = asset_id.rsplit('_', 1)[-1]
    matches = [(digest, value) for digest, value in archive.assets.items() if digest.startswith(suffix)]
    if len(matches) != 1:
        return None
    digest, content = matches[0]
    try:
        with Image.open(io.BytesIO(content)) as picture:
            mime = Image.MIME.get(picture.format, 'application/octet-stream')
    except (UnidentifiedImageError, OSError):
        return None
    return (BoardAssetRecord(asset_id, owner_user_id, digest, mime, len(content), '', '', next(iter(archive.package.lessons.values())).created_at), content)

class RepositoryBoardAssetStore:
    """Delegate new asset writes, resolve imported reads from the authorized RIDOC."""

    def __init__(self, underlying, repositories, repository_id, owner_user_id):
        self.underlying = underlying
        self.repositories = repositories
        self.repository_id = repository_id
        self.owner_user_id = owner_user_id

    def __getattr__(self, name):
        return getattr(self.underlying, name)

    def read_bytes(self, asset_id, owner_user_id):
        if owner_user_id != self.owner_user_id:
            return None
        result = self.underlying.read_bytes(asset_id, owner_user_id)
        if result is not None:
            return result
        archive = self.repositories.working_archive(self.repository_id, owner_user_id)
        return read_embedded_board_asset(archive, asset_id, owner_user_id)

def read_owned_board_asset(asset_id, owner_user_id, *, asset_store=None):
    """The owned RIDOC remains authoritative when its rebuildable image cache is absent."""
    from openclass_core.services.board_asset_store import get_board_asset_store
    store = get_board_asset_store() if asset_store is None else asset_store
    stored = store.read_bytes(asset_id, owner_user_id)
    if stored is not None:
        return stored
    if not re.fullmatch('basset_[A-Za-z0-9_-]+_[a-f0-9]{32}', asset_id):
        return None
    from openclass_core.services.workspace_state import get_ridoc_workspace_store
    store = get_ridoc_workspace_store()
    for package in store.list_packages(owner_user_id):
        archive = store.load_archive(owner_user_id, package_id=package.id)
        stored = read_embedded_board_asset(archive, asset_id, owner_user_id)
        if stored is not None:
            return stored
    return None
