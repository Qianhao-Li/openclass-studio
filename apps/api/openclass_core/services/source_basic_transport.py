"""Coalesce already-authorized native text while retaining every original span."""

def native_separator(previous, text, metadata):
    if not previous:
        return ''
    prior = previous['locator']['metadata']
    last = prior.get('original_unit_spans', [-1])[-1]
    if isinstance(last, dict):
        prior = last
    if prior.get('text_end') == metadata.get('text_start') or previous['text'].endswith('\n') or text.startswith('\n'):
        return ''
    return '\n'

def coalesce_native_parts(parts):
    result = []
    for part in parts:
        locator = part['locator']
        key = tuple((locator.get(name) for name in ('kind', 'start', 'end', 'container')))
        previous = result[-1] if result else None
        previous_key = tuple((previous['locator'].get(name) for name in ('kind', 'start', 'end', 'container'))) if previous else None
        if part['kind'] != 'text' or previous is None or previous['kind'] != 'text' or (key != previous_key) or (part['provenance'] != previous['provenance']):
            metadata = locator.get('metadata', {})
            copy = {**part, 'locator': {**locator, 'metadata': {**metadata, 'original_unit_spans': [{'unit_id': metadata['basic_unit_id'], 'text_start': metadata['text_start'], 'text_end': metadata['text_end'], 'transport_start': 0, 'transport_end': len(part['text'])}]}}}
            result.append(copy)
            continue
        separator = native_separator(previous, part['text'], part['locator']['metadata'])
        offset = len(previous['text']) + len(separator)
        metadata = part['locator']['metadata']
        previous['text'] += separator + part['text']
        previous['locator']['metadata']['original_unit_spans'].append({'unit_id': metadata['basic_unit_id'], 'text_start': metadata['text_start'], 'text_end': metadata['text_end'], 'transport_start': offset, 'transport_end': offset + len(part['text'])})
    return result
