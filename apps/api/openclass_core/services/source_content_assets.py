"""Represent embedded image bytes by stable source-asset references in text."""
from __future__ import annotations
import base64
import hashlib
import re
from .source_content_formats import SourceContentReadError
_DATA_IMAGE = re.compile('data:image/[A-Za-z0-9.+-]+(?:;[A-Za-z0-9_=-]+)*;base64,[^\\s\\"\'<>\\)]+')

def decode_inline_image(uri):
    try:
        header, encoded = uri.split(',', 1)
        if not header.startswith('data:image/') or not header.endswith(';base64'):
            raise ValueError()
        raw = base64.b64decode(encoded, validate=True)
        if not raw:
            raise ValueError()
        return (header[5:].split(';', 1)[0], raw)
    except (ValueError, TypeError) as exc:
        raise SourceContentReadError('The original embedded image has invalid base64 encoding.') from exc

def decode_image_bundle(mime, value):
    encoded = ''.join(value) if isinstance(value, list) else value
    if not isinstance(encoded, str):
        raise SourceContentReadError('The original image bundle has an invalid payload.')
    if mime == 'image/svg+xml':
        return encoded.encode()
    try:
        raw = base64.b64decode(encoded, validate=True)
        if not raw:
            raise ValueError()
        return raw
    except ValueError as exc:
        raise SourceContentReadError('The original image bundle has invalid base64 encoding.') from exc

def asset_reference(raw, mime):
    digest = hashlib.sha256(raw).hexdigest()
    return {'source_asset': f'sha256:{digest}', 'sha256': digest, 'declared_mime_type': mime, 'size_bytes': len(raw)}

def replace_inline_images(value, register):
    """Preserve structure and ordinary text; the callback owns image delivery."""
    if isinstance(value, str):
        return _DATA_IMAGE.sub(lambda match: register(match[0])['source_asset'], value)
    if isinstance(value, list):
        return [replace_inline_images(item, register) for item in value]
    if isinstance(value, dict):
        return {key: replace_inline_images(item, register) for key, item in value.items()}
    return value

def structured_asset_provenance(references):
    return {'representation': 'structured_original_with_asset_references', 'structure_complete': True, 'binary_payloads': 'separate_image_parts', 'embedded_assets': list(references)}
