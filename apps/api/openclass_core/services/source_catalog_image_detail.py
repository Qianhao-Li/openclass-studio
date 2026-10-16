"""Transient detail views for a file-authorized catalog reader; no OCR or new scope."""
import base64
import hashlib
import io
import math
from PIL import Image
from openclass_core.services.google_sources.config import GoogleSourceError

class ImageDetailCapacityError(GoogleSourceError):
    pass

def validate_region(region):
    if region is None:
        return None
    if not isinstance(region, list) or len(region) != 4 or any((type(value) not in (float, int) or not math.isfinite(value) for value in region)) or (not (0 <= region[0] < region[2] <= 1 and 0 <= region[1] < region[3] <= 1)):
        raise GoogleSourceError('The original-page detail region requires four ordered normalized coordinates.')
    return region

def add_original_detail(payload, region, limits, max_image_bytes):
    """Keep the delivered whole page; identify the additional view by its parent hash."""
    images, manifests = (payload['images'], payload['image_manifest'])
    if not images or len(images) != len(manifests) or any((item.get('parent_sha256') != manifests[0]['sha256'] for item in manifests[1:])):
        raise GoogleSourceError('An original-page detail requires exactly one verified page image.')
    if len(images) + 1 > limits.count:
        raise ImageDetailCapacityError("The original-page detail exceeds the selected model's image capacity.")
    raw = base64.b64decode(images[0]['data'], validate=True)
    parent = manifests[0]
    if hashlib.sha256(raw).hexdigest() != parent['sha256']:
        raise GoogleSourceError('The original-page detail has a mismatching parent image hash.')
    with Image.open(io.BytesIO(raw)) as full:
        box = (math.ceil(region[0] * full.width), math.ceil(region[1] * full.height), math.floor(region[2] * full.width), math.floor(region[3] * full.height))
        if box[2] <= box[0] or box[3] <= box[1]:
            raise GoogleSourceError('The detail region contains no original pixels.')
        output = io.BytesIO()
        full.crop(box).save(output, format='PNG')
    detail = output.getvalue()
    used_bytes = sum((len(base64.b64decode(image['data'], validate=True)) for image in images))
    if len(detail) > limits.per_image or used_bytes + len(detail) > max_image_bytes:
        raise ImageDetailCapacityError("The original-page detail exceeds the selected model's image capacity.")
    proof = {**parent, 'sha256': hashlib.sha256(detail).hexdigest(), 'representation': 'original_page_detail', 'parent_sha256': parent['sha256'], 'region': region, 'width': box[2] - box[0], 'height': box[3] - box[1], 'purpose': 'directory_inspection'}
    return {**payload, 'images': [*images, {'data': base64.b64encode(detail).decode(), 'mimeType': 'image/png', **proof}], 'image_manifest': [*manifests, proof]}

def add_original_pdf_detail(payload, region, limits, max_image_bytes, *, source_path, source_content_hash, source_version, physical_page, is_cancelled=lambda: False):
    """Keep the authenticated whole page and render new detail from its original PDF."""
    from openclass_core.services.pdf_page_visual_input import DEFAULT_PDF_DETAIL_DPI, PdfPageDetailTooLargeError, PdfPageVisualInputError, PdfPageVisualReader
    from openclass_core.services.source_content_contract import verified_read_evidence, verified_read_receipt
    region = validate_region(region)
    if region is None:
        raise GoogleSourceError('An original PDF detail requires an explicit region.')
    if is_cancelled():
        raise GoogleSourceError('Original PDF detail reading was cancelled.')
    images, manifests = (payload.get('images', []), payload.get('image_manifest', []))
    if len(images) != 1 or len(manifests) != 1:
        raise GoogleSourceError('An original PDF detail requires exactly one authenticated whole-page image.')
    if len(images) + 1 > limits.count:
        raise ImageDetailCapacityError("The original PDF detail exceeds the selected model's image capacity.")
    parent = manifests[0]
    scope = parent.get('source_range', {})
    receipt = payload.get('receipt', {})
    matching = [item for item in payload.get('items', []) if item.get('id') == parent.get('evidence_id')]
    evidence = matching[0] if len(matching) == 1 else {}
    metadata = evidence.get('metadata', {})
    evidence_range = metadata.get('source_range', {})
    if type(physical_page) is not int or physical_page < 1 or any((item.get('source_content_hash') != source_content_hash or item.get('source_version') != source_version for item in (payload, parent))) or (scope.get('kind') != 'pdf_pages') or (scope.get('start') != physical_page) or (scope.get('end') != physical_page) or scope.get('start_anchor') or scope.get('end_anchor') or (parent.get('physical_page') != physical_page) or (receipt.get('content_hash') != source_content_hash) or (receipt.get('source_version') != source_version) or (payload.get('source_id') != parent.get('source_id')) or (receipt.get('source_id') != parent.get('source_id')) or (evidence.get('source_ingestion_id') != parent.get('source_id')) or (metadata.get('source_content_hash') != source_content_hash) or (metadata.get('source_version') != source_version) or (metadata.get('source_read_receipt', {}).get('read_id') != receipt.get('read_id')) or (evidence_range.get('kind') != 'pdf_pages') or (evidence_range.get('start') != physical_page) or (evidence_range.get('end') != physical_page) or (not verified_read_receipt(receipt)) or (len(matching) != 1) or (not verified_read_evidence(evidence)) or (metadata.get('page_image_sha256') != parent.get('sha256')) or metadata.get('crop_bbox'):
        raise GoogleSourceError('The PDF detail does not match the authorized original page and read proof.')
    try:
        original = base64.b64decode(images[0]['data'], validate=True)
    except (ValueError, KeyError):
        raise GoogleSourceError('The PDF detail parent image is invalid.') from None
    if hashlib.sha256(original).hexdigest() != parent.get('sha256'):
        raise GoogleSourceError('The PDF detail parent image hash does not match.')
    remaining = min(max_image_bytes, limits.total_images) - len(original)
    if remaining < 1:
        raise ImageDetailCapacityError("The original PDF detail exceeds the selected model's image capacity.")
    try:
        with PdfPageVisualReader(source_path, expected_sha256=source_content_hash, is_cancelled=is_cancelled) as reader:
            detail = reader.render_region(physical_page, region, dpi=DEFAULT_PDF_DETAIL_DPI, max_png_bytes=min(remaining, limits.per_image), is_cancelled=is_cancelled)
    except PdfPageDetailTooLargeError as exc:
        raise ImageDetailCapacityError(str(exc)) from None
    except PdfPageVisualInputError as exc:
        raise GoogleSourceError(str(exc)) from None
    if is_cancelled():
        raise GoogleSourceError('Original PDF detail reading was cancelled.')
    data = detail.data_url.partition(',')[2]
    size = len(base64.b64decode(data, validate=True))
    proof = {**parent, 'image_index': len(images), 'sha256': detail.sha256, 'size_bytes': size, 'representation': 'original_page_detail', 'detail_source': 'original_pdf', 'render_dpi': DEFAULT_PDF_DETAIL_DPI, 'parent_sha256': parent['sha256'], 'region': region, 'width': detail.width, 'height': detail.height, 'purpose': 'directory_inspection', 'source_content_hash': source_content_hash, 'source_version': source_version, 'physical_page': physical_page}
    return {**payload, 'images': [*images, {'data': data, 'mimeType': detail.mime_type, **proof}], 'image_manifest': [*manifests, proof]}
