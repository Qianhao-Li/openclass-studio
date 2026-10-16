"""Verified current-turn attachments at the existing board source boundary."""
import base64
import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from openclass_core.models import SourceQueryRef, SourceQueryScope
from openclass_core.services.chat_attachments import SUPPORTED_CHAT_IMAGE_MIME_TYPES, prepare_chat_attachments, verify_chat_attachments
from openclass_core.services.codex_app_server import CodexAppServerError
from openclass_core.services.google_sources.board_context import request_source_scope
from openclass_core.services.source_content_contract import SourceRoleContext
_attachments = ContextVar('teaching_current_attachments', default=None)

def prepare_teaching_attachments(request, *, user_id, package_id, board_empty=False, board_write_opt_out=False):
    if not request.attachments:
        return (request, None)
    verified = verify_chat_attachments(owner_user_id=user_id, package_id=package_id, attachments=request.attachments)
    expected_hashes = {item.source.id: str(item.source.metadata.get('content_hash') or '').lower() for item in verified}
    for item in verified:
        _verify_attachment_hash(item.path, expected_hashes[item.source.id])
    prepared = prepare_chat_attachments(attachments=verified)
    image_index = 0
    for item in verified:
        expected_hash = expected_hashes[item.source.id]
        _verify_attachment_hash(item.path, expected_hash)
        mime = item.source.mime_type.split(';', 1)[0].strip().lower()
        if mime in SUPPORTED_CHAT_IMAGE_MIME_TYPES:
            raw = base64.b64decode(prepared.image_inputs[image_index].partition(',')[2], validate=True)
            if hashlib.sha256(raw).hexdigest() != expected_hash:
                raise CodexAppServerError('The prepared attachment does not match its original hash. Add it again.')
            image_index += 1
    scope = request_source_scope(request)
    refs = [SourceQueryRef(source_ingestion_id=item.source.id, source_content_hash=expected_hashes[item.source.id]) for item in verified if board_empty or not item.source.mime_type.startswith('image/')]
    if refs and scope is None:
        request = request.model_copy(update={'source_query_scope': SourceQueryScope(mode='source' if len(refs) == 1 else 'sources', refs=refs)})
    elif refs and scope is not None and (not board_write_opt_out):
        known = {ref.source_ingestion_id for ref in scope.refs}
        extra = [ref for ref in refs if ref.source_ingestion_id not in known]
        if extra:
            if scope.mode in {'source', 'sources'}:
                request = request.model_copy(update={'source_query_scope': SourceQueryScope(mode='sources', refs=[*scope.refs, *extra])})
            else:
                raise ValueError('Choose one chapter/page scope or whole-file attachments for board teaching; these mixed ranges cannot be frozen together yet.')
    from openclass_core.services.original_attachment_delivery import original_attachment_batch
    batch = original_attachment_batch(verified, prepared, owner_user_id=user_id, package_id=package_id)
    return (request, SourceRoleContext(prepared.prompt_context, image_inputs=prepared.image_inputs, image_manifest=batch.image_manifest if batch is not None else [], original_delivery=batch))

@contextmanager
def bind_teaching_attachments(context):
    token = _attachments.set(context)
    try:
        yield
    finally:
        _attachments.reset(token)

def with_turn_attachments(context):
    attachment = _attachments.get()
    if attachment is None:
        return context
    from openclass_core.services.original_source_delivery import OriginalSourceBatch, combine_original_batches
    first, second = (getattr(context, 'original_delivery', None), getattr(attachment, 'original_delivery', None))
    batches = [item for item in (first, second) if item is not None]
    if any((not isinstance(item, OriginalSourceBatch) for item in batches)):
        from openclass_core.services.original_delivery_group import OriginalDeliveryGroup
        combined = OriginalDeliveryGroup(batches)
        return SourceRoleContext('Authorized original source and attachment identities await bounded delivery.', original_delivery=combined)
    combined = combine_original_batches(batches) if batches else None
    images = combined.model_image_inputs if combined is not None else []
    if combined is None and (getattr(context, 'image_inputs', []) or attachment.image_inputs):
        raise ValueError('An attachment image cannot be delivered without its original input contract.')
    return SourceRoleContext('\n\n'.join((str(part) for part in (context, attachment) if part)), image_inputs=images, image_manifest=combined.image_manifest if combined else [], original_delivery=combined)

def _verify_attachment_hash(path, expected_hash):
    if len(expected_hash) != 64 or any((char not in '0123456789abcdef' for char in expected_hash)):
        raise CodexAppServerError('The attachment has no verifiable original hash. Add it again.')
    with path.open('rb') as original:
        actual_hash = hashlib.file_digest(original, 'sha256').hexdigest()
    if actual_hash != expected_hash:
        raise CodexAppServerError('The attachment original changed and no longer matches its hash. Add it again.')
