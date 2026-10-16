"""Read original archive assets at the collector's immutable character positions."""
from __future__ import annotations
import hashlib
import zipfile
from pathlib import PurePosixPath
from openclass_core.services.google_sources.config import GoogleSourceError

def positioned_image_units(unit, bounds):
    start = bounds.get('basic_start_char', 0) if unit['id'] == bounds.get('basic_unit_start') else 0
    end = bounds.get('basic_end_char') if unit['id'] == bounds.get('basic_unit_end') else None
    end = len(unit['text']) if end is None else end
    selected = [image for image in unit.get('original_images', []) if start <= image['offset'] - unit['text_start'] < end or (image['offset'] == unit['text_end'] and end == len(unit['text']) and (unit['id'] != bounds.get('basic_unit_end') or bounds.get('basic_end_char') is None))]
    if not selected:
        return [unit]
    result, point = ([], start)
    for image in sorted(selected, key=lambda image: image['offset']):
        relative = image['offset'] - unit['text_start']
        if relative > point:
            result.append({**unit, 'text': unit['text'][point:relative], 'text_start': unit['text_start'] + point, 'text_end': image['offset'], '_scope_applied': True})
        result.append({**unit, 'text': '', 'text_start': image['offset'], 'text_end': image['offset'], '_scope_applied': True, '_original_asset': image})
        point = relative
    if point < end:
        result.append({**unit, 'text': unit['text'][point:end], 'text_start': unit['text_start'] + point, 'text_end': unit['text_start'] + end, '_scope_applied': True})
    return result

def read_archive_image(path, source, artifact, part):
    asset = part['asset']
    name = asset['container']
    target = PurePosixPath(name)
    if target.is_absolute() or '..' in target.parts or '\\' in name:
        raise GoogleSourceError('The original image is outside its archive.')
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(name)
        if info.file_size > 20 * 1024 * 1024:
            raise GoogleSourceError("The original archive image exceeds this channel's transfer capacity.")
        raw = archive.read(info)
    if hashlib.sha256(raw).hexdigest() != asset['sha256']:
        raise GoogleSourceError('The original archive image hash changed.')
    from openclass_core.services.source_content_media import image_part
    return image_part(raw, locator={**part['locator'], 'object': name}, artifact=artifact, original_name=name, source_hash=source.metadata['content_hash'], native_container=True)['_load']()
