"""Reproducible native line coordinates for clipping cited original PDF pages."""
import hashlib
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_pdf_native import _page_text

def _same_box(first, second):
    return len(first) == len(second) == 4 and all((abs(a - b) < 0.001 for a, b in zip(first, second, strict=True)))

def _line_point(page, *, block_bbox, block_sha256, line_index):
    from openclass_core.services import pdf_backend as fitz
    flags = fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES
    blocks = page.get_text('dict', sort=True, flags=flags)['blocks']
    matches = [block for block in blocks if block.get('type') == 0 and _same_box(block['bbox'], block_bbox)]
    if len(matches) != 1:
        raise GoogleSourceError('A native PDF boundary has no unique original block.')
    block = matches[0]
    texts = [''.join((span['text'] for span in line['spans'])) for line in block['lines']]
    if hashlib.sha256(('\n'.join(texts) + '\n').encode()).hexdigest() != block_sha256:
        raise GoogleSourceError('The native PDF boundary block no longer matches its original text.')
    if type(line_index) is not int or not 0 <= line_index < len(texts):
        raise GoogleSourceError('The native PDF boundary line is outside its original block.')
    box = fitz.Rect(block['lines'][line_index]['bbox'])
    actual = _page_text(page, box)
    if actual is None or actual.strip() != texts[line_index].strip():
        raise GoogleSourceError('The chapter boundary cannot be verified against visible original glyphs.')
    return {'proof_kind': 'verified_basic_native_line', 'physical_page': page.number + 1, 'block_bbox': list(block_bbox), 'block_sha256': block_sha256, 'line_index': line_index, 'bbox': [box.x0 / page.rect.width, box.y0 / page.rect.height, box.x1 / page.rect.width, box.y1 / page.rect.height]}

def point_for_unit(document, unit, offset):
    locator = unit['locator']
    number = locator['start']
    bbox = locator.get('metadata', {}).get('native_bbox')
    if not bbox:
        raise GoogleSourceError('A PDF chapter boundary has no exact native text block.')
    page = document[number - 1]
    from openclass_core.services import pdf_backend as fitz
    blocks = page.get_text('blocks', sort=True, flags=fitz.TEXTFLAGS_BLOCKS & ~fitz.TEXT_PRESERVE_IMAGES)
    matches = [block for block in blocks if len(block) > 6 and block[6] == 0 and _same_box(block[:4], bbox)]
    if len(matches) != 1 or matches[0][4][unit['text_start']:unit['text_end']] != unit['text']:
        raise GoogleSourceError('The cached PDF boundary does not match its original block bytes.')
    text = matches[0][4]
    position = unit['text_start'] + offset
    cursor = 0
    for index, line in enumerate(text.splitlines(keepends=True)):
        if cursor <= position < cursor + len(line):
            if line[:position - cursor].strip():
                raise GoogleSourceError('A chapter boundary falls inside an unverified original text line.')
            return _line_point(page, block_bbox=bbox, block_sha256=hashlib.sha256(text.encode()).hexdigest(), line_index=index)
        cursor += len(line)
    raise GoogleSourceError('The chapter character boundary is outside its original block.')

def verify_point(page, point):
    return _line_point(page, block_bbox=point.get('block_bbox', []), block_sha256=point.get('block_sha256'), line_index=point.get('line_index'))

def bind_original_pdf_boundaries(path, data, chapters):
    from openclass_core.services import pdf_backend as fitz
    result = []
    with fitz.open(path) as document:
        for chapter in chapters:
            value = chapter.range
            metadata = dict(value.metadata)
            points = {'start': point_for_unit(document, data['units'][metadata['basic_unit_start'] - 1], metadata.get('basic_start_char', 0))}
            end_id = metadata['basic_unit_end']
            end_char = metadata.get('basic_end_char')
            if end_char is not None:
                points['end'] = point_for_unit(document, data['units'][end_id - 1], end_char)
            elif end_id < len(data['units']) and data['units'][end_id]['locator']['start'] <= value.end:
                points['end'] = point_for_unit(document, data['units'][end_id], 0)
            metadata.update(boundary_precision='verified_native_region', native_boundary_points=points, native_boundary_range={'start': value.start, 'end': value.end})
            result.append(chapter.model_copy(update={'range': value.model_copy(update={'metadata': metadata})}))
    return result
