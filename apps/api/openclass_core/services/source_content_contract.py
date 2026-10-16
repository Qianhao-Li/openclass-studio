"""Versioned contracts for source locations, original reads and bounded delivery.

Locations are never evidence. A receipt binds evidence to the exact content
prepared by the reader, not to a search snippet or a model's assertion.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import os
import secrets
import fcntl
from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field
from openclass_core.models import RetrievalEvidence
from openclass_core.services.google_sources.config import GoogleSourceError
READ_CONTRACT = 'source_read_v1'

def _receipt_key() -> bytes:
    """Shared by API and scoped tool workers; never exposed to the model."""
    from openclass_core.services.google_sources.original_cache import original_cache_root
    root = original_cache_root()
    root.mkdir(parents=True, exist_ok=True, mode=448)
    path = root / '.read-receipt-key'
    with (root / '.read-receipt-key.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.is_symlink():
            raise GoogleSourceError('The source receipt signing key is not a regular private file.')
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 384)
            with os.fdopen(fd, 'wb') as target:
                target.write(secrets.token_bytes(32))
                target.flush()
                os.fsync(target.fileno())
        key = path.read_bytes()
        if len(key) != 32 or path.stat().st_mode & 63:
            raise GoogleSourceError('The source receipt signing key has invalid size or permissions.')
        return key

def content_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

class SourceLocator(BaseModel):
    model_config = ConfigDict(extra='forbid')
    locator_id: str
    source_id: str
    source_title: str = ''
    source_version: str
    content_hash: str = Field(pattern='^[a-f0-9]{64}$')
    coordinate_type: str
    source_range: dict[str, Any]
    authorized_range: dict[str, Any] | None = None
    document_id: str = ''
    chunk_id: str = ''
    rank: int = Field(default=1, ge=1)
    citable: Literal[False] = False

def seal_locator(**values) -> SourceLocator:
    locator = SourceLocator(locator_id='pending', **values)
    payload = locator.model_dump(exclude={'locator_id'})
    locator.locator_id = 'location_' + hmac.new(_receipt_key(), content_digest(payload).encode(), hashlib.sha256).hexdigest()
    return locator

def verified_locator(locator: SourceLocator) -> bool:
    payload = locator.model_dump(exclude={'locator_id'})
    expected = 'location_' + hmac.new(_receipt_key(), content_digest(payload).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(locator.locator_id, expected)

@dataclass
class SourceReadBudget:
    max_searches: int = 3
    max_reads: int | None = 6
    max_characters: int | None = 36000
    max_visual_units: int | None = 24
    searches: int = 0
    reads: int = 0
    characters: int = 0
    visual_units: int = 0

    @classmethod
    def continuation(cls) -> 'SourceReadBudget':
        """Count a finite authorized range without imposing a total page ceiling.

        Individual reads remain limited by their transport and format budgets.
        Search expansion and explicitly supplied caller/cost budgets stay bounded.
        """
        return cls(max_reads=None, max_characters=None, max_visual_units=None)

    @property
    def read_visual_limit(self) -> int:
        return 6 if self.max_visual_units is None else min(6, max(0, self.max_visual_units - self.visual_units))

    @property
    def read_character_limit(self) -> int:
        return 18000 if self.max_characters is None else min(18000, max(0, self.max_characters - self.characters))

    def search(self) -> None:
        if self.searches >= self.max_searches:
            raise GoogleSourceError('The source search budget is exhausted. Continue from an existing location or narrow the request.')
        self.searches += 1

    def begin_read(self) -> None:
        if self.max_reads is not None and self.reads >= self.max_reads:
            raise GoogleSourceError('The source reading budget is exhausted; the requested source has not been read completely.')
        self.reads += 1

    def consume(self, *, characters: int, visual_units: int) -> None:
        if self.max_characters is not None and self.characters + characters > self.max_characters or (self.max_visual_units is not None and self.visual_units + visual_units > self.max_visual_units):
            raise GoogleSourceError('The source content budget is exhausted; partial results cannot be treated as complete reading.')
        self.characters += characters
        self.visual_units += visual_units

    def model_dump(self, **_kwargs) -> dict:
        return asdict(self)

def _fingerprint(item: RetrievalEvidence) -> str:
    immutable = {'source_content_hash', 'source_version', 'source_range', 'source_read_request', 'content_unit_sha256', 'page_image_sha256', 'page_start', 'page_end', 'provenance', 'source_transport', 'crop_bbox', 'render_dpi', 'input_kind'}
    metadata = {key: value for key, value in item.metadata.items() if key in immutable}
    return content_digest({'id': item.id, 'source_id': item.source_ingestion_id, 'excerpt': item.excerpt, 'text': item.expanded_text, 'metadata': metadata})

def issue_read_receipt(*, items: list[RetrievalEvidence], owner_user_id: str, package_id: str, source_id: str, source_version: str, content_hash: str, source_range: dict, authorization: str, grant_id: str, role: str, authorized_range: dict | None=None) -> dict:
    receipt = {'version': READ_CONTRACT, 'read_id': 'read_' + secrets.token_hex(16), 'owner_user_id': owner_user_id, 'package_id': package_id, 'source_id': source_id, 'source_version': source_version, 'content_hash': content_hash, 'range': source_range, 'authorization': authorization, 'grant_id': grant_id, 'role': role, 'authorized_range': authorized_range, 'evidence_hashes': {item.id: _fingerprint(item) for item in items}}
    receipt['content_digest'] = content_digest(receipt['evidence_hashes'])
    receipt['signature'] = hmac.new(_receipt_key(), content_digest(receipt).encode(), hashlib.sha256).hexdigest()
    for item in items:
        item.metadata['source_read_receipt'] = dict(receipt)
    return receipt

def verified_read_evidence(item) -> bool:
    try:
        item = item if isinstance(item, RetrievalEvidence) else RetrievalEvidence.model_validate(item)
        receipt = dict(item.metadata.get('source_read_receipt') or {})
        signature = receipt.pop('signature', '')
        expected = hmac.new(_receipt_key(), content_digest(receipt).encode(), hashlib.sha256).hexdigest()
        return bool(signature and hmac.compare_digest(signature, expected) and (receipt.get('version') == READ_CONTRACT) and (receipt.get('source_id') == item.source_ingestion_id) and (receipt.get('content_hash') == item.metadata.get('source_content_hash')) and (receipt.get('source_version') == item.metadata.get('source_version')) and (receipt.get('evidence_hashes', {}).get(item.id) == _fingerprint(item)))
    except (TypeError, ValueError, AttributeError):
        return False

def verified_read_receipt(receipt) -> bool:
    try:
        data = dict(receipt)
        signature = data.pop('signature', '')
        expected = hmac.new(_receipt_key(), content_digest(data).encode(), hashlib.sha256).hexdigest()
        return bool(signature and data.get('version') == READ_CONTRACT and hmac.compare_digest(signature, expected) and (data.get('content_digest') == content_digest(data.get('evidence_hashes'))))
    except (TypeError, ValueError, AttributeError):
        return False

@dataclass
class SourceReadResult:
    evidence_items: list[RetrievalEvidence] = field(default_factory=list)
    image_inputs: list[str] = field(default_factory=list)
    image_manifest: list[dict] = field(default_factory=list)
    receipt: dict = field(default_factory=dict)
    source_identity: dict = field(default_factory=dict)
    complete: bool = False
    next_cursor: str | None = None
    missing: list = field(default_factory=list)
    coverage: list[dict] = field(default_factory=list)
    prompt_context: str = ''

    def tool_payload(self) -> dict:
        if len(self.image_inputs) != len(self.image_manifest):
            raise GoogleSourceError('The source images do not match the content manifest.')
        images = []
        for data_url, manifest in zip(self.image_inputs, self.image_manifest, strict=True):
            header, separator, data = data_url.partition(',')
            if not separator or not header.startswith('data:image/') or (not header.endswith(';base64')):
                raise GoogleSourceError('The source reader returned an invalid original image.')
            images.append({'data': data, 'mimeType': header[5:-7], **manifest})
        return {**self.source_identity, 'items': [item.model_dump(mode='json') for item in self.evidence_items], 'images': images, 'image_manifest': self.image_manifest, 'receipt': self.receipt, 'complete': self.complete, 'next_cursor': self.next_cursor, 'missing': self.missing, 'usage': 'Only these original-source read results may be cited. Complete refers to this requested range, never the entire source unless that was the requested range.'}

class SourceRoleContext(str):
    """Backward-compatible text context carrying the actual source image inputs."""

    def __new__(cls, text, *, image_inputs=None, image_manifest=None, original_delivery=None):
        value = super().__new__(cls, text)
        value.image_inputs = list(image_inputs or [])
        value.image_manifest = list(image_manifest or [])
        value.original_delivery = original_delivery
        return value

def source_context_image_kwargs(context):
    images = getattr(context, 'image_inputs', [])
    delivery = getattr(context, 'original_delivery', None)
    return {**({'image_inputs': images, 'preserve_image_bytes': True} if images else {}), **({'source_delivery': delivery} if delivery is not None else {})}
