"""Byte-faithful native text fragments with budget-independent continuation."""
import hashlib
from .source_content_formats import SourceContentReadError
CURSOR_FLAG = 1 << 60
CURSOR_STRIDE = 1 << 32

def decode_cursor(cursor):
    if cursor < CURSOR_FLAG:
        return (cursor, 0, False)
    unit, offset = divmod(cursor - CURSOR_FLAG, CURSOR_STRIDE)
    return (unit, offset, True)

def encode_cursor(unit, offset):
    if not 0 <= offset < CURSOR_STRIDE:
        raise SourceContentReadError('The native text cursor exceeds supported character coordinates.')
    return CURSOR_FLAG + unit * CURSOR_STRIDE + offset

def text_fragment(unit, start, end):
    text = unit['text']
    if not 0 <= start < end <= len(text):
        raise SourceContentReadError('The native text fragment is outside its original object.')
    location = dict(unit['locator'])
    metadata = dict(location.get('metadata', {}))
    origin = unit.get('_native_fragment_origin') or {'start': 0, 'sha256': hashlib.sha256(text.encode()).hexdigest()}
    absolute_start, absolute_end = (origin['start'] + start, origin['start'] + end)
    metadata['native_text_fragment'] = {'start': absolute_start, 'end': absolute_end, 'sha256': origin['sha256']}
    return {**unit, 'text': text[start:end], 'locator': {**location, 'metadata': metadata}, '_native_fragment_origin': {'start': absolute_start, 'sha256': origin['sha256']}}

def requested_text_fragment(units, scope):
    fragment = (scope or {}).get('metadata', {}).get('native_text_fragment')
    if fragment is None:
        return units
    if len(units) != 1 or units[0]['kind'] != 'text' or (not isinstance(fragment, dict)):
        raise SourceContentReadError('The frozen text fragment has no unique original object.')
    unit = units[0]
    start, end = (fragment.get('start'), fragment.get('end'))
    if type(start) is not int or type(end) is not int or hashlib.sha256(unit['text'].encode()).hexdigest() != fragment.get('sha256'):
        raise SourceContentReadError('The frozen text fragment does not match its original object.')
    return [text_fragment(unit, start, end)]
