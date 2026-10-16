"""First-hand source capabilities, validated against the actual model request.

The source reading receipt proves a read. This module additionally proves that
the exact original carrier is present in the role's outgoing input.
"""
from __future__ import annotations
import base64
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
from openclass_core.models import RetrievalEvidence
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_content_contract import SourceReadResult, verified_read_evidence
ORIGINAL_DELIVERY_VERSION = 'original_source_delivery_v1'
_ACTIVE: ContextVar['OriginalSourceBatch | None'] = ContextVar('original_source_batch', default=None)
_FORBIDDEN_METHODS = {'caption_snapshot', 'audio_transcript', 'caption_transcript', 'original_native_calculation'}
_FORBIDDEN_KEYS = {'reading_notes', 'source_summary', 'source_summaries', 'ocr_text', 'parsed_source_text'}
_OPAQUE_FIELDS = {'user_message', 'current_user_message', 'conversation', 'recent_conversation', 'response_contract', 'schema', 'expanded_text', 'excerpt', 'text', 'original_text'}
_SOURCE_CONTEXT_FIELDS = {'verified_source_context', 'original_source_context', 'source_context'}

def _image_bytes(value: str) -> bytes:
    if not isinstance(value, str) or not value.startswith('data:image/') or ';base64,' not in value:
        raise GoogleSourceError('Original image delivery requires embedded original bytes.')
    try:
        return base64.b64decode(value.partition(',')[2], validate=True)
    except (ValueError, TypeError):
        raise GoogleSourceError('The original image bytes are invalid.') from None

def require_original_evidence(item, *, owner_user_id, package_id):
    if not isinstance(item, RetrievalEvidence):
        item = RetrievalEvidence.model_validate(item)
    receipt = item.metadata.get('source_read_receipt') or {}
    if not verified_read_evidence(item) or receipt.get('owner_user_id') != owner_user_id or receipt.get('package_id') != package_id:
        raise GoogleSourceError('The source carrier has no authentic original reading for this owner and course.')
    meta = item.metadata
    provenance = meta.get('provenance') or {}
    method = provenance.get('input_method')
    mode = str(meta.get('retrieval_mode') or '')
    if method in _FORBIDDEN_METHODS or any((word in mode for word in ('summary', 'ocr', 'transcript', 'search'))) or meta.get('ocr_engine') or meta.get('ocr_text') or meta.get('reading_notes'):
        raise GoogleSourceError('OCR, transcripts, parsed search content and reading notes cannot replace the original source.')
    kind = meta.get('input_kind')
    if kind == 'image':
        if item.expanded_text or item.excerpt:
            raise GoogleSourceError('An original image carrier cannot contain a text substitution.')
    elif kind == 'text':
        if hashlib.sha256(item.expanded_text.encode()).hexdigest() != meta.get('content_unit_sha256'):
            raise GoogleSourceError('The exact native original text no longer matches its read bytes.')
        if method == 'basic_source_text':
            artifact_hash = provenance.get('parsed_artifact_sha256')
            if meta.get('source_transport') != 'basic_source_text' or provenance.get('representation') not in {'faithful_parsed_text', 'converted_native_text'} or provenance.get('parser_schema') != 'source_basic_content_v1' or (provenance.get('original_sha256') != meta.get('source_content_hash')) or (not isinstance(artifact_hash, str)) or (len(artifact_hash) != 64) or any((value not in '0123456789abcdef' for value in artifact_hash)):
                raise GoogleSourceError('The native source cache lacks its exact original and parser binding.')
        elif method not in {None, 'original_native_object'}:
            raise GoogleSourceError('Only exact native original text may be delivered as source text.')
    else:
        raise GoogleSourceError('This source has no supported first-hand carrier.')
    return item

@dataclass
class OriginalSourceBatch(SourceReadResult):
    owner_user_id: str = ''
    package_id: str = ''
    deliveries: tuple = field(default_factory=tuple, repr=False)
    visual_inputs: tuple = field(default_factory=tuple, repr=False)

    @property
    def model_image_inputs(self):
        result = list(self.image_inputs)
        hashes = {hashlib.sha256(_image_bytes(image)).hexdigest() for image in result}
        for _visual, image in self.visual_inputs:
            digest = hashlib.sha256(_image_bytes(image)).hexdigest()
            if digest not in hashes:
                result.append(image)
                hashes.add(digest)
        return result

    def with_verified_visuals(self, visual_evidence, image_inputs):
        return replace(self, visual_inputs=tuple(zip(visual_evidence, image_inputs, strict=True))).validate_current()

    def crop_review(self, parent_evidence_id, visual_evidence, crop_data_url):
        pairs = [(image, entry) for image, entry in zip(self.image_inputs, self.image_manifest, strict=True) if entry['evidence_id'] == parent_evidence_id]
        if len(pairs) != 1:
            raise GoogleSourceError('A crop review requires its actual authorized parent image.')
        return replace(self, evidence_items=[item for item in self.evidence_items if item.id == parent_evidence_id], image_inputs=[pairs[0][0]], image_manifest=[{**pairs[0][1], 'image_index': 0}], visual_inputs=((visual_evidence, crop_data_url),)).validate_current()

    def validate_current(self):
        for delivery in self.deliveries:
            delivery.validate_current()
        for item in self.evidence_items:
            require_original_evidence(item, owner_user_id=self.owner_user_id, package_id=self.package_id)
        if len(self.image_inputs) != len(self.image_manifest):
            raise GoogleSourceError('Original images and their delivery manifest do not match.')
        by_id = {item.id: item for item in self.evidence_items}
        delivered = set()
        for index, (image, entry) in enumerate(zip(self.image_inputs, self.image_manifest, strict=True)):
            item = by_id.get(entry.get('evidence_id'))
            digest = hashlib.sha256(_image_bytes(image)).hexdigest()
            if item is None or item.metadata.get('input_kind') != 'image' or entry.get('image_index') != index or (digest != entry.get('sha256')) or (digest != item.metadata.get('content_unit_sha256', item.metadata.get('page_image_sha256'))):
                raise GoogleSourceError('The actual original image does not match its authenticated page.')
            delivered.add(item.id)
        if any((item.metadata.get('input_kind') == 'image' and item.id not in delivered for item in self.evidence_items)):
            raise GoogleSourceError('An authenticated original page is missing from the actual image input.')
        for visual, image in self.visual_inputs:
            _validate_visual_pixels(self, visual, image)
        return self

def _validate_visual_pixels(batch, visual, image):
    import io
    from PIL import Image
    meta = visual.metadata
    parent = next((item for item in batch.evidence_items if item.id == meta.get('source_unit_id')), None)
    pair = next(((raw, entry) for raw, entry in zip(batch.image_inputs, batch.image_manifest, strict=True) if entry['evidence_id'] == meta.get('source_unit_id')), None)
    if parent is None or pair is None or visual.source_ingestion_id != parent.source_ingestion_id or (visual.package_id != batch.package_id):
        raise GoogleSourceError('The visual crop belongs to another original source scope.')
    raw_parent, raw_crop = (_image_bytes(pair[0]), _image_bytes(image))
    if hashlib.sha256(raw_parent).hexdigest() != meta.get('parent_image_sha256') or hashlib.sha256(raw_crop).hexdigest() != visual.content_hash:
        raise GoogleSourceError('The visual crop bytes do not match their declared original.')
    box = meta.get('crop_pixel_bbox')
    if not isinstance(box, (list, tuple)) or len(box) != 4 or any((type(value) is not int for value in box)):
        raise GoogleSourceError('A visual crop requires verified original pixel coordinates.')
    with Image.open(io.BytesIO(raw_parent)) as parent_image, Image.open(io.BytesIO(raw_crop)) as crop_image:
        if not (0 <= box[0] < box[2] <= parent_image.width and 0 <= box[1] < box[3] <= parent_image.height):
            raise GoogleSourceError('The visual crop exceeds the original authorized region.')
        expected = parent_image.crop(tuple(box))
        if expected.size != crop_image.size or expected.mode != crop_image.mode or expected.tobytes() != crop_image.tobytes() or (expected.mode == 'P' and expected.getpalette() != crop_image.getpalette()):
            raise GoogleSourceError('The visual is not an unchanged crop of the original pixels.')

class OriginalSourceDelivery:

    def __init__(self, *, owner_user_id, package_id, evidence_items, is_cancelled=None, source_store=None, structure_store=None, google_service=None):
        self.owner_user_id, self.package_id = (owner_user_id, package_id)
        self.evidence_items = [item.model_copy(deep=True) for item in evidence_items]
        self.is_cancelled = is_cancelled
        self.source_store, self.structure_store, self.google_service = (source_store, structure_store, google_service)
        if not self.evidence_items:
            raise GoogleSourceError('Original source delivery requires an authorized original range.')
        for item in self.evidence_items:
            require_original_evidence(item, owner_user_id=owner_user_id, package_id=package_id)

    def validate_current(self):
        from openclass_core.services.source_evidence_store import source_evidence_store
        from openclass_core.services.source_structure_store import source_structure_store
        from openclass_core.services.source_ingestion_service import source_download_path
        from openclass_core.services.source_retrieval_preferences import auto_source_retrieval_enabled
        from openclass_core.services.source_range_reader import _catalog_version
        store, structures = (self.source_store or source_evidence_store, self.structure_store or source_structure_store)
        checked = set()
        for item in self.evidence_items:
            if self.is_cancelled and self.is_cancelled():
                raise GoogleSourceError('Original source delivery was cancelled.')
            receipt = item.metadata['source_read_receipt']
            if receipt.get('authorization') == 'automatic' and (not auto_source_retrieval_enabled(self.owner_user_id, store.path)):
                raise GoogleSourceError('Automatic source access was switched off.')
            identity = (item.source_ingestion_id, item.metadata.get('source_content_hash'), item.metadata.get('source_version'), item.metadata.get('catalog_version'))
            provenance = item.metadata.get('provenance') or {}
            if provenance.get('input_method') == 'basic_source_text':
                source = store.get_source(owner_user_id=self.owner_user_id, package_id=self.package_id, source_id=item.source_ingestion_id)
                if source is None or (source.metadata.get('basic_content_artifact') or {}).get('sha256') != provenance.get('parsed_artifact_sha256'):
                    raise GoogleSourceError('The faithful native source cache changed before delivery.')
            if identity in checked:
                continue
            source = store.get_source(owner_user_id=self.owner_user_id, package_id=self.package_id, source_id=item.source_ingestion_id)
            if source is None or source.metadata.get('external_turn_temporary') or source.metadata.get('content_hash') != identity[1]:
                raise GoogleSourceError('The original source is no longer authorized at this hash.')
            if source.metadata.get('media_kind') in {'caption_transcript', 'audio_transcript'} or ((source.metadata.get('origin') or {}).get('snapshot_kind') in {'caption_transcript', 'public_audio'} and item.metadata.get('input_kind') == 'text'):
                raise GoogleSourceError('A generated transcript cannot replace the referenced original media.')
            version = source.metadata.get('source_version')
            if receipt.get('role') in {'original_range_reader', 'legacy_native_reader'}:
                structure = structures.get_structure(owner_user_id=self.owner_user_id, package_id=self.package_id, source_id=source.id)
                if structure is None or _catalog_version(structure) != identity[3]:
                    raise GoogleSourceError('The original source catalog changed before delivery.')
                version = version or f'original:{identity[1]}:catalog:{identity[3]}'
            elif identity[3] is not None and source.metadata.get('catalog_version', 1) != identity[3]:
                raise GoogleSourceError('The original source catalog changed before delivery.')
            if version != identity[2]:
                raise GoogleSourceError('The original source version changed before delivery.')
            path = source_download_path(source)
            if path is None:
                raise GoogleSourceError('The authorized original file is unavailable.')
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != identity[1]:
                    raise GoogleSourceError('The original file bytes changed before delivery.')
            checked.add(identity)
        return self

    def iter_batches(self, unit_ids=None):
        from openclass_core.services.original_source_rehydration import rehydrate_original_batches
        self.validate_current()
        known = {item.id for item in self.evidence_items}
        if unit_ids is not None and (not set(unit_ids) <= known):
            raise GoogleSourceError('The requested original units exceed this delivery scope.')
        for result in rehydrate_original_batches(self, unit_ids=unit_ids):
            self.validate_current()
            yield original_batch(result, owner_user_id=self.owner_user_id, package_id=self.package_id, deliveries=(self,))

def original_batch(result, *, owner_user_id, package_id, deliveries=()):
    batch = OriginalSourceBatch(**{name: deepcopy(getattr(result, name)) for name in SourceReadResult.__dataclass_fields__}, owner_user_id=owner_user_id, package_id=package_id, deliveries=tuple(deliveries))
    return batch.validate_current()

def combine_original_batches(batches):
    batches = list(batches)
    if not batches:
        raise GoogleSourceError('An original model call cannot bind an empty batch.')
    first = batches[0]
    evidence, images, manifest, coverage, deliveries, visuals = ({}, [], [], [], [], {})
    for batch in batches:
        batch.validate_current()
        if (batch.owner_user_id, batch.package_id) != (first.owner_user_id, first.package_id):
            raise GoogleSourceError('Original batches from different authorization scopes cannot be combined.')
        for item in batch.evidence_items:
            from openclass_core.services.original_source_rehydration import _unit_key
            previous = evidence.get(item.id)
            if previous is not None and _unit_key(previous) != _unit_key(item):
                raise GoogleSourceError('A repeated original evidence identity has conflicting source bytes or coordinates.')
            evidence.setdefault(item.id, item)
        for visual, image in batch.visual_inputs:
            existing = visuals.get(visual.visual_id)
            if existing is not None and existing[0].content_hash != visual.content_hash:
                raise GoogleSourceError('A repeated original visual identity has conflicting crop bytes.')
            visuals.setdefault(visual.visual_id, (visual, image))
        for image, entry in zip(batch.image_inputs, batch.image_manifest, strict=True):
            if any((previous['evidence_id'] == entry['evidence_id'] for previous in manifest)):
                continue
            manifest.append({**entry, 'image_index': len(images)})
            images.append(image)
        coverage.extend(batch.coverage)
        deliveries.extend((item for item in batch.deliveries if item not in deliveries))
    return OriginalSourceBatch(evidence_items=list(evidence.values()), image_inputs=images, image_manifest=manifest, coverage=coverage, complete=all((item.complete for item in batches)), owner_user_id=first.owner_user_id, package_id=first.package_id, deliveries=tuple(deliveries), visual_inputs=tuple(visuals.values())).validate_current()

def from_frozen_requirement(requirement, owner_user_id, is_cancelled=None):
    return from_grounding(requirement.source_grounding, owner_user_id=owner_user_id, is_cancelled=is_cancelled)

def from_grounding(grounding, *, owner_user_id, is_cancelled=None):
    items = grounding.frozen_evidence
    if not items:
        raise GoogleSourceError('The frozen request has no original source identities to reauthorize.')
    packages = {(item.metadata.get('source_read_receipt') or {}).get('package_id') for item in items}
    if len(packages) != 1 or not next(iter(packages)):
        raise GoogleSourceError('The frozen original input has no unique course authorization.')
    return OriginalSourceDelivery(owner_user_id=owner_user_id, package_id=next(iter(packages)), evidence_items=items, is_cancelled=is_cancelled)

def iter_validated_original_batches(grounding, *, owner_user_id, is_cancelled=None):
    yield from from_grounding(grounding, owner_user_id=owner_user_id, is_cancelled=is_cancelled).iter_batches()

def original_context_for_model(batch, citation_style='numbers'):
    from openclass_core.services.source_prompt_projection import context_for_model
    batch.validate_current()
    items = batch.evidence_items
    if citation_style == 'identity':
        items = [item.model_copy(update={'metadata': {key: value for key, value in item.metadata.items() if key not in {'citation_number', 'citation_numbers'}}}) for item in items]
    return {'source_delivery_contract': ORIGINAL_DELIVERY_VERSION, **context_for_model(items, image_manifest=batch.image_manifest, coverage=batch.coverage)}

@contextmanager
def bind_original_source_batch(batch):
    batch.validate_current()
    token = _ACTIVE.set(batch)
    try:
        yield batch
    finally:
        _ACTIVE.reset(token)

def _forbidden_source_payload(value, source_envelope=False):
    if isinstance(value, dict):
        source_envelope = source_envelope or bool({'source_grounding', 'verified_evidence', 'evidence_bundle_id', 'source_delivery_contract', 'original_delivery_required', 'reading_representation'}.intersection(value))
        return any((source_envelope and key in _FORBIDDEN_KEYS and bool(child) or (key not in _OPAQUE_FIELDS and _forbidden_source_payload(_decode_context(child) if key in _SOURCE_CONTEXT_FIELDS else child, source_envelope=source_envelope or key in _SOURCE_CONTEXT_FIELDS or key == 'source_grounding')) for key, child in value.items()))
    if isinstance(value, list):
        return any((_forbidden_source_payload(child, source_envelope=source_envelope) for child in value))
    return False

def _decode_context(value):
    if not isinstance(value, str):
        return value
    for candidate in (value, value.split('\n', 1)[-1]):
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            pass
    return value

def _claims_source_input(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _OPAQUE_FIELDS:
                continue
            if key in {'source_delivery_contract', 'original_delivery_required'} and child:
                return True
            if key in _SOURCE_CONTEXT_FIELDS and child:
                return True
            if key in {'frozen_evidence', 'verified_evidence'} and child:
                return True
            if key == 'evidence' and isinstance(child, list) and any((isinstance(item, dict) and {'source_id', 'source_ingestion_id'}.intersection(item) for item in child)):
                return True
            if _claims_source_input(child):
                return True
    elif isinstance(value, list):
        return any((_claims_source_input(child) for child in value))
    return False

def _all_strings(value):
    if isinstance(value, str):
        yield value
        parsed = _decode_context(value)
        if not isinstance(parsed, str):
            yield from _all_strings(parsed)
    elif isinstance(value, dict):
        for child in value.values():
            yield from _all_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_strings(child)

def current_original_source_batch():
    return _ACTIVE.get()

def validate_original_source_call(*, user_prompt, image_inputs=None, document_path=None, document_bytes=None, source_delivery=None, system_prompt=''):
    batch = source_delivery if isinstance(source_delivery, OriginalSourceBatch) else _ACTIVE.get()
    if source_delivery is not None and batch is None:
        raise GoogleSourceError('Bind the concrete original batch before this model call.')
    try:
        payload = json.loads(user_prompt)
    except (ValueError, TypeError):
        payload = None
        if isinstance(user_prompt, str) and '\n' in user_prompt:
            try:
                payload = json.loads(user_prompt.split('\n', 1)[1])
            except (ValueError, TypeError):
                pass
    exact_native_document = batch is not None and any((item.metadata.get('input_kind') == 'text' and item.expanded_text == user_prompt for item in batch.evidence_items))
    if not exact_native_document and _forbidden_source_payload(payload, source_envelope=batch is not None):
        raise GoogleSourceError('Derived source notes, OCR and summaries cannot be sent as source facts.')
    if batch is None:
        if _claims_source_input(payload):
            raise GoogleSourceError('This source-fact model request lost its original delivery capability.')
        return
    batch.validate_current()
    file_sources = set()
    if document_path is not None or document_bytes is not None:
        if document_bytes is not None:
            digest = hashlib.sha256(document_bytes).hexdigest()
        else:
            with Path(document_path).open('rb') as raw:
                digest = hashlib.file_digest(raw, 'sha256').hexdigest()
        for item in batch.evidence_items:
            receipt = item.metadata.get('source_read_receipt') or {}
            authorized = receipt.get('authorized_range')
            explicit_whole = receipt.get('authorization') == 'explicit' and (authorized is None and item.metadata.get('source_read_request') is None or (isinstance(authorized, dict) and authorized.get('kind') == 'whole_source'))
            if item.metadata.get('source_content_hash') == digest and explicit_whole:
                file_sources.add(item.source_ingestion_id)
        if not file_sources:
            raise GoogleSourceError('A whole original file cannot be attached under a page or chapter-only authorization.')
    actual = Counter((hashlib.sha256(_image_bytes(image)).hexdigest() for image in image_inputs or []))
    expected = Counter((hashlib.sha256(_image_bytes(image)).hexdigest() for image in batch.model_image_inputs))
    if file_sources and (not actual):
        expected = Counter((hashlib.sha256(_image_bytes(image)).hexdigest() for image, entry in zip(batch.image_inputs, batch.image_manifest, strict=True) if next((item for item in batch.evidence_items if item.id == entry['evidence_id'])).source_ingestion_id not in file_sources))
    if actual != expected:
        raise GoogleSourceError('The final model request omitted or replaced an authorized original image.')
    strings = [str(user_prompt), *list(_all_strings(payload if payload is not None else user_prompt))]
    for item in batch.evidence_items:
        if item.source_ingestion_id not in file_sources and item.metadata.get('input_kind') == 'text' and (not any((item.expanded_text in text for text in strings))):
            raise GoogleSourceError('The final model request omitted or substituted the exact native original text.')

def validate_original_source_wire(*, user_prompt, image_inputs=None, document_path=None, document_bytes=None):
    return validate_original_source_call(user_prompt=user_prompt, image_inputs=image_inputs, document_path=document_path, document_bytes=document_bytes)

def evidence_metadata_for_plan(items):
    from openclass_core.services.source_prompt_projection import range_for_model
    return [{'unit_id': item.id, 'source_id': item.source_ingestion_id, 'source_title': item.source_title, 'source_range': range_for_model(item.metadata.get('source_range')), 'page_start': item.metadata.get('page_start'), 'page_end': item.metadata.get('page_end'), 'input_kind': item.metadata.get('input_kind')} for item in items]

def collect_original_inventory(batches, *, owner_user_id, package_id, is_cancelled=None, source_store=None, structure_store=None, google_service=None):
    """Keep complete backend identities and only a bounded original preview.

    The preview is a convenience for one small call, never a partial substitute
    for a larger scope. Overflow discards its image buffer and requires iterating
    the delivery again for final source-consuming role calls.
    """
    evidence, coverage, images, manifest = ({}, [], [], [])
    small = True
    text_chars = 0
    byte_count = 0
    for result in batches:
        if is_cancelled and is_cancelled():
            raise GoogleSourceError('Original source reading was cancelled.')
        if isinstance(result, SourceReadResult):
            batch_coverage = result.coverage
        else:
            batch_coverage = [{'source_id': item.source_ingestion_id, 'read_range': result.source_range, 'complete': result.complete, 'missing': []} for item in result.evidence_items[:1]]
        coverage.extend(batch_coverage)
        for item in result.evidence_items:
            require_original_evidence(item, owner_user_id=owner_user_id, package_id=package_id)
            previous = evidence.get(item.id)
            if previous is not None and previous.metadata.get('content_unit_sha256') != item.metadata.get('content_unit_sha256'):
                raise GoogleSourceError('The original source unit changed during reading.')
            evidence[item.id] = item
            text_chars += len(item.expanded_text)
        batch_bytes = sum((len(_image_bytes(image)) for image in result.image_inputs))
        if small and (len(images) + len(result.image_inputs) > 6 or byte_count + batch_bytes > 40 * 1024 * 1024 or text_chars > 18000):
            small = False
            images, manifest = ([], [])
        if small:
            manifest.extend(({**entry, 'image_index': len(images) + index} for index, entry in enumerate(result.image_manifest)))
            images.extend(result.image_inputs)
            byte_count += batch_bytes
    items = list(evidence.values())
    if not items:
        raise GoogleSourceError('No original source units are available for this request.')
    from openclass_core.services.source_board_coverage import require_complete_board_evidence
    if coverage:
        require_complete_board_evidence([items[0].model_copy(update={'metadata': {**items[0].metadata, 'source_coverage': coverage}})])
    delivery = OriginalSourceDelivery(owner_user_id=owner_user_id, package_id=package_id, evidence_items=items, is_cancelled=is_cancelled, source_store=source_store, structure_store=structure_store, google_service=google_service).validate_current()
    return (items, images, manifest, coverage, delivery)
