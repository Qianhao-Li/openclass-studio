"""Native PDFium glyph observations. Uncertain carriers remain visual evidence."""
from ctypes import byref, c_uint, c_int, create_string_buffer
from contextlib import closing
import math
from pypdfium2 import raw
from openclass_core.services.pdf_geometry import Rect

def _font(text, index):
    flags = c_int()
    size = raw.FPDFText_GetFontInfo(text.raw, index, None, 0, byref(flags))
    if size <= 0 or size > 4096:
        return ('Unknown', 0)
    buffer = create_string_buffer(size)
    raw.FPDFText_GetFontInfo(text.raw, index, buffer, size, byref(flags))
    return (buffer.value.decode('utf-8', errors='replace'), flags.value)

def observations(page):
    pdf = page._page
    height = page.rect.height
    origin = pdf.get_bbox()
    objects = list(pdf.get_objects(max_depth=1))
    order = {int(__import__('ctypes').cast(obj.raw, __import__('ctypes').c_void_p).value): index for index, obj in enumerate(objects)}
    traces = []
    chars = []
    with closing(pdf.get_textpage()) as text:
        for index in range(text.count_chars()):
            code = int(raw.FPDFText_GetUnicode(text.raw, index))
            if code in (10, 13):
                chars.append({'text': '\n', 'bbox': None, 'size': 0, 'font': '', 'flags': 0})
                continue
            if code < 0 or code > 1114111:
                code = 65533
            box = text.get_charbox(index, loose=True)
            bounds = Rect(*[value * page.unit for value in (box[0] - origin[0], origin[3] - box[3], box[2] - origin[0], origin[3] - box[1])])
            size = float(raw.FPDFText_GetFontSize(text.raw, index)) * page.unit
            font, flags = _font(text, index)
            pointer = raw.FPDFText_GetTextObject(text.raw, index)
            address = int(__import__('ctypes').cast(pointer, __import__('ctypes').c_void_p).value or 0)
            sequence = order.get(address, -1)
            mode = int(raw.FPDFTextObj_GetTextRenderMode(pointer)) if pointer else -1
            rgba = [c_uint() for _ in range(4)]
            color = bool(pointer and raw.FPDFPageObj_GetFillColor(pointer, *[byref(v) for v in rgba]))
            opacity = rgba[3].value / 255 if color else 0
            valid = sequence >= 0 and mode in (0, 1) and (not page.rotation) and (page.first_annot is None)
            trace = {'type': mode if valid else -1, 'opacity': opacity, 'seqno': sequence, 'bbox': bounds, 'chars': [(code, code if code != 65533 else -1, (bounds.x0, bounds.y1), bounds)]}
            traces.append(trace)
            chars.append({'text': chr(code), 'bbox': bounds, 'size': size, 'font': font, 'flags': flags})
    return (chars, traces)

def lines(chars, clip=None):
    output = []
    current = []

    def flush():
        nonlocal current
        if current:
            output.append(current)
            current = []
    for char in chars:
        if char['bbox'] is None:
            flush()
            continue
        if clip is not None and (not clip.contains(char['bbox'])):
            continue
        if current and abs(current[-1]['bbox'].y0 - char['bbox'].y0) > max(2, min(current[-1]['size'], char['size']) * 0.65):
            flush()
        current.append(char)
    flush()
    return output

def union(boxes):
    boxes = list(boxes)
    return Rect(min((b[0] for b in boxes)), min((b[1] for b in boxes)), max((b[2] for b in boxes)), max((b[3] for b in boxes))) if boxes else Rect()

def text_payload(chars, kind='text', *, clip=None, sort=False):
    rows = lines(chars, clip)
    if sort:
        rows.sort(key=lambda row: (round(min((c['bbox'].y0 for c in row)), 1), min((c['bbox'].x0 for c in row))))
    if kind == 'text':
        return ''.join((''.join((c['text'] for c in row)) + '\n' for row in rows))
    if kind == 'words':
        words = []
        for line_index, row in enumerate(rows):
            word = []
            for char in [*row, {'text': ' ', 'bbox': None}]:
                if char['text'].isspace():
                    if word:
                        words.append((*union((c['bbox'] for c in word)), ''.join((c['text'] for c in word)), line_index, 0, len(words)))
                        word = []
                else:
                    word.append(char)
        return words
    blocks = []
    for index, row in enumerate(rows):
        bbox = union((c['bbox'] for c in row))
        text = ''.join((c['text'] for c in row)) + '\n'
        if kind == 'blocks':
            blocks.append((*bbox, text, index, 0))
            continue
        spans = []
        group = []
        for char in row:
            if group and (char['font'], char['size']) != (group[-1]['font'], group[-1]['size']):
                spans.append(_span(group))
                group = []
            group.append(char)
        if group:
            spans.append(_span(group))
        blocks.append({'number': index, 'type': 0, 'bbox': tuple(bbox), 'lines': [{'bbox': tuple(bbox), 'spans': spans, 'dir': (1, 0), 'wmode': 0}]})
    return blocks if kind == 'blocks' else {'blocks': blocks}

def _span(chars):
    return {'text': ''.join((c['text'] for c in chars)), 'bbox': tuple(union((c['bbox'] for c in chars))), 'size': chars[0]['size'], 'font': chars[0]['font'], 'flags': chars[0]['flags'], 'color': 0, 'chars': [{'c': c['text'], 'bbox': tuple(c['bbox']), 'origin': (c['bbox'].x0, c['bbox'].y1)} for c in chars]}
