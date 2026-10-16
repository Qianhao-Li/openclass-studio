"""Exact image attachments are first-hand carriers with current owner/hash checks."""
from __future__ import annotations
import base64
import hashlib
from openclass_core.models import RetrievalEvidence
from openclass_core.services.chat_attachments import SUPPORTED_CHAT_IMAGE_MIME_TYPES
from openclass_core.services.source_content_contract import SourceReadResult, issue_read_receipt

class _AttachmentAuthorization:

    def __init__(self, owner_user_id, package_id, entries):
        self.owner_user_id, self.package_id, self.entries = (owner_user_id, package_id, entries)

    def validate_current(self):
        from openclass_core.services.source_evidence_store import source_evidence_store
        from openclass_core.services.source_ingestion_service import source_download_path
        for source_id, expected_hash, expected_version in self.entries:
            source = source_evidence_store.get_source(owner_user_id=self.owner_user_id, package_id=self.package_id, source_id=source_id)
            if source is None or source.metadata.get('external_turn_temporary') or source.metadata.get('content_hash') != expected_hash or ((source.metadata.get('source_version') or 'attachment:' + expected_hash) != expected_version):
                raise ValueError('The original attachment authorization or version changed.')
            path = source_download_path(source)
            if path is None:
                raise ValueError('The original attachment is unavailable.')
            with path.open('rb') as raw:
                if hashlib.file_digest(raw, 'sha256').hexdigest() != expected_hash:
                    raise ValueError('The original attachment bytes changed.')

def original_attachment_batch(verified, prepared, *, owner_user_id, package_id):
    from openclass_core.services.original_source_delivery import original_batch
    items, manifests, entries = ([], [], [])
    images = list(prepared.image_inputs)
    for entry in verified:
        source = entry.source
        mime = source.mime_type.split(';', 1)[0].strip().lower()
        if mime not in SUPPORTED_CHAT_IMAGE_MIME_TYPES:
            continue
        index = len(items)
        raw = base64.b64decode(images[index].partition(',')[2], validate=True)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != source.metadata.get('content_hash'):
            raise ValueError('An attachment carrier differs from the selected original.')
        version = source.metadata.get('source_version') or 'attachment:' + digest
        scope = {'kind': 'image_frames', 'start': 1, 'end': 1}
        item = RetrievalEvidence(id='evidence_attachment_' + hashlib.sha256((source.id + digest).encode()).hexdigest()[:24], source_ingestion_id=source.id, source_title=source.title, metadata={'source_content_hash': digest, 'source_version': version, 'source_range': scope, 'source_read_request': scope, 'input_kind': 'image', 'source_transport': 'original_attachment_image', 'content_unit_sha256': digest, 'provenance': {'input_method': 'original_image', 'original_object_sha256': digest}})
        issue_read_receipt(items=[item], owner_user_id=owner_user_id, package_id=package_id, source_id=source.id, source_version=version, content_hash=digest, source_range=scope, authorization='explicit', grant_id='attachment:' + source.id, role='original_attachment_reader', authorized_range=scope)
        items.append(item)
        manifests.append({'image_index': index, 'evidence_id': item.id, 'source_id': source.id, 'sha256': digest, 'size_bytes': len(raw), 'mime_type': mime, 'source_range': scope})
        entries.append((source.id, digest, version))
    if not items:
        return None
    authorization = _AttachmentAuthorization(owner_user_id, package_id, entries)
    return original_batch(SourceReadResult(evidence_items=items, image_inputs=images, image_manifest=manifests, complete=True), owner_user_id=owner_user_id, package_id=package_id, deliveries=(authorization,))
