"""Locate headings whose differently sized PDF font blocks sort out of order."""
import unicodedata

def heading_layout_characters(units):
    blocks = []
    for unit in units:
        box = unit['locator'].get('metadata', {}).get('native_bbox')
        if isinstance(box, (list, tuple)) and len(box) == 4:
            blocks.append((unit, box))
    if not blocks:
        return ([], [])
    lines = []
    for unit, box in sorted(blocks, key=lambda value: ((value[1][1] + value[1][3]) / 2, value[1][0])):
        center = (box[1] + box[3]) / 2
        height = max(1, box[3] - box[1])
        found = next((line for line in lines if abs(line['center'] - center) <= 0.65 * min(height, line['height'])), None)
        if found is None:
            found = {'center': center, 'height': height, 'blocks': []}
            lines.append(found)
        found['blocks'].append((unit, box))
    characters = []
    positions = []
    for line in lines:
        for unit, box in sorted(line['blocks'], key=lambda value: (value[1][0], value[0]['id'])):
            for offset, char in enumerate(unit['text']):
                for value in unicodedata.normalize('NFKC', char).casefold():
                    if not value.isspace():
                        characters.append(value)
                        positions.append((unit['id'], offset))
    return (characters, positions)

def heading_layout_text(units):
    characters, _ = heading_layout_characters(units)
    return ''.join(characters)

def heading_layout_offset(units, unit_id, title):
    characters, positions = heading_layout_characters(units)
    needle = ''.join(unicodedata.normalize('NFKC', title).casefold().split())
    start = ''.join(characters).find(needle)
    if start < 0:
        return None
    matched = positions[start:start + len(needle)]
    if not matched or min((position[0] for position in matched)) != unit_id:
        return None
    return min((offset for identifier, offset in matched if identifier == unit_id))
