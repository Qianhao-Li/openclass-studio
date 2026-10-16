"""Original image frames and explicitly identified complete transcript segments."""
from __future__ import annotations
import base64
import hashlib
import io
import math
import re
from .source_content_formats import SourceContentReadError, interval, text_part, decode

def image_part(raw, *, locator, artifact, original_name, source_hash='', native_container=False):
    return {'kind': 'image', 'locator': locator, 'provenance': {'original_object': original_name}, '_load': lambda: _image_part(raw, locator=locator, artifact=artifact, original_name=original_name, source_hash=source_hash, native_container=native_container)}

def _image_part(raw, *, locator, artifact, original_name, source_hash='', native_container=False):
    candidates = []
    for part in artifact.get('parts', []):
        if part.get('layout_verified') is not True or part.get('processing_complete') is not True:
            continue
        mapping = part.get('locator', {})
        mapped = mapping.get('original') == original_name or mapping.get('container') == original_name or mapping.get('asset_path') == original_name
        if native_container:
            mapped = bool(source_hash and part.get('input', {}).get('sha256') == source_hash)
        if mapped:
            candidates.append(part)
    explicit_original_exception = artifact.get('_original_read_exception') in {'explicit_switch_off', 'ordinary_source'}
    if not candidates and (not explicit_original_exception):
        raise SourceContentReadError('The original image has no verified Layout Parser mapping.')
    from PIL import Image
    original_object_hash = hashlib.sha256(raw).hexdigest()
    transformation = 'none'
    if raw.lstrip().startswith(b'<') and b'<svg' in raw[:2000].lower():
        from .source_xml import parse_untrusted_xml
        root = parse_untrusted_xml(raw)
        if re.search(b'@import|url\\(\\s*[\'\\"]?(?!#)', raw, re.I):
            raise SourceContentReadError('The SVG contains unresolved CSS resources.')
        for node in root.iter():
            if str(node.tag).rsplit('}', 1)[-1] in {'script', 'foreignObject'}:
                raise SourceContentReadError('The SVG contains active objects.')
            for key, value in node.attrib.items():
                if str(key).rsplit('}', 1)[-1] in {'href', 'src'} and (not value.startswith(('#', 'data:'))) or ('url(' in value and (not re.fullmatch('url\\(\\s*[\'\\"]?#[^)]*\\)', value))):
                    raise SourceContentReadError('The SVG contains external resources.')
        from openclass_core.services import pdf_backend as fitz
        with fitz.open(stream=raw, filetype='svg') as svg:
            with fitz.open(stream=svg.convert_to_pdf(), filetype='pdf') as rendered:
                if rendered[0].rect.width * rendered[0].rect.height * 6.25 > 12000000:
                    raise SourceContentReadError('The original SVG exceeds the pixel budget.')
                raw = rendered[0].get_pixmap(dpi=180).tobytes('png')
        transformation = 'svg_render_180dpi'
    try:
        with Image.open(io.BytesIO(raw)) as im:
            if im.width * im.height > 12000000:
                raise SourceContentReadError('The original image exceeds the pixel budget.')
            im.load()
            mime = Image.MIME.get(im.format, '')
            if mime not in {'image/png', 'image/jpeg', 'image/webp', 'image/gif'}:
                out = io.BytesIO()
                im.save(out, format='PNG')
                raw, mime = (out.getvalue(), 'image/png')
    except SourceContentReadError:
        raise
    except Exception as exc:
        raise SourceContentReadError('The original image cannot be read by the image transport.') from exc
    if len(raw) > 20 * 1024 * 1024:
        raise SourceContentReadError('The original image exceeds the 20 MiB input limit.')
    return {'kind': 'image', 'data_url': f'data:{mime};base64,' + base64.b64encode(raw).decode(), 'locator': locator, 'provenance': {'input_method': 'original_image', 'image_sha256': hashlib.sha256(raw).hexdigest(), 'layout_document_ids': [] if explicit_original_exception else [part['document_id'] for part in candidates if part.get('document_id')], 'google_layout_used': not explicit_original_exception, **({'original_read_exception': 'explicit_switch_off'} if explicit_original_exception else {}), 'original_object': original_name, 'original_object_sha256': original_object_hash, 'transformation': transformation}}

def read_image_units(source, path, artifact, scope):
    from PIL import Image, ImageOps
    if path.suffix.lower() == '.svg':
        interval(scope, 'image_frames', 1)
        return [image_part(path.read_bytes(), locator={'kind': 'image_frames', 'start': 1, 'end': 1}, artifact=artifact, original_name=source.file_name)]
    with Image.open(path) as original:
        count = getattr(original, 'n_frames', 1)
    start, end = interval(scope, 'image_frames', count)
    result = []
    for index in range(start, end + 1):
        locator = {'kind': 'image_frames', 'start': index, 'end': index}

        def load(index=index, locator=locator):
            with Image.open(path) as original:
                original.seek(index - 1)
                if original.width * original.height > 12000000:
                    raise SourceContentReadError('The original image exceeds the pixel budget.')
                out = io.BytesIO()
                ImageOps.exif_transpose(original.copy()).save(out, format='PNG')
            return _image_part(out.getvalue(), locator=locator, artifact=artifact, original_name=source.file_name)
        result.append({'kind': 'image', 'locator': locator, 'provenance': {'original_object': source.file_name}, '_load': load})
    return result

def _seconds(value):
    if isinstance(value, str):
        if ':' in value:
            fields = value.split(':')
            value = sum((float(part) * 60 ** index for index, part in enumerate(reversed(fields))))
        else:
            value = value.removesuffix('s')
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SourceContentReadError('The transcript segment has no verified timestamp.') from exc
    if not math.isfinite(number) or number < 0:
        raise SourceContentReadError('The transcript timestamp is invalid.')
    return number

def read_transcript_units(source, path, artifact, scope):
    if scope and scope.get('kind') not in {'audio_time', 'media_time', 'time_range', 'seconds', 'structured_path'}:
        raise SourceContentReadError('This source requires timestamp coordinates.')
    if scope and scope.get('kind') == 'structured_path' and (scope.get('start') is not None or scope.get('end') is not None or scope.get('path')):
        raise SourceContentReadError('A media structural locator cannot stand in for a timestamp.')
    transcript = artifact.get('transcript') or artifact.get('youtube_timeline') or {}
    segments = transcript.get('segments', [])
    method = 'google_speech_transcript' if artifact.get('transcript') else 'caption_snapshot'
    if not segments and path.suffix.lower() == '.txt':
        pattern = re.compile('^\\[([^\\]]+?)\\s*-->\\s*([^\\]]+)\\]\\s*(.*)$')
        segments = [{'start': match[1], 'end': match[2], 'text': match[3]} for line in decode(path.read_bytes()).splitlines() if (match := pattern.match(line))]
    if not segments:
        raise SourceContentReadError('The complete timestamped transcript snapshot is unavailable.')
    lo = _seconds(scope['start']) if scope and scope.get('start') is not None else 0
    hi = _seconds(scope['end']) if scope and scope.get('end') is not None else float('inf')
    if hi <= lo:
        raise SourceContentReadError('The requested media time range is invalid.')
    result = []
    for number, segment in enumerate(segments, 1):
        start = _seconds(segment.get('start_seconds', segment.get('start')))
        end = _seconds(segment.get('end_seconds', segment.get('end')))
        if end <= start:
            raise SourceContentReadError('The transcript segment duration is invalid.')
        if end <= lo or start >= hi:
            continue
        if start < lo or end > hi:
            raise SourceContentReadError('The requested range cuts a transcript segment; select its complete timestamp interval.')
        result.append(text_part(str(segment.get('text', '')), {'kind': 'media_time', 'start': start, 'end': end, 'segment': number}, input_method=method, visual_content_processed=False, original_media_read=False))
    return result
