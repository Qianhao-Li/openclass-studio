"""Format-native source units must stay inside the grant used by retrieval."""
from openclass_core.services.google_sources.config import GoogleSourceError

def ensure_unit_authorized(unit, authorized):
    unit, authorized = (dict(unit), dict(authorized))
    for value in (unit, authorized):
        base = value.get('index_base', value.get('metadata', {}).get('index_base', 1))
        if value.get('kind') in {'docx_paragraphs', 'epub_spine'} and base == 0:
            if type(value.get('start')) is not int or type(value.get('end')) is not int:
                raise GoogleSourceError('The legacy paragraph range has invalid coordinates.')
            value['start'] += 1
            value['end'] += 1
    if unit.get('kind') != authorized.get('kind'):
        if authorized.get('kind') == 'structured_path' and unit.get('container') == authorized.get('container'):
            return
        raise GoogleSourceError('The original source unit cannot be mapped inside the authorized range.')
    if unit.get('kind') == 'epub_spine':
        coordinates = (authorized.get('start'), unit.get('start'), unit.get('end'), authorized.get('end'))
        if not all((type(value) is int for value in coordinates)) or not 1 <= coordinates[0] <= coordinates[1] <= coordinates[2] <= coordinates[3]:
            raise GoogleSourceError('The original EPUB unit is outside its authorized spine range.')
        if unit['start'] == authorized['start']:
            if authorized.get('container') and unit.get('container') != authorized['container']:
                raise GoogleSourceError('The EPUB start container does not match its authorized spine.')
            if authorized.get('start_anchor') and unit.get('start_anchor') != authorized['start_anchor']:
                raise GoogleSourceError('The EPUB unit does not preserve its authorized starting boundary.')
        if unit['end'] == authorized['end'] and authorized.get('end_anchor'):
            if unit.get('end_anchor') != authorized['end_anchor']:
                raise GoogleSourceError('The EPUB unit does not preserve its authorized ending boundary.')
            expected = authorized.get('metadata', {}).get('end_anchor_exclusive', True)
            if unit.get('metadata', {}).get('end_anchor_exclusive', True) != expected:
                raise GoogleSourceError('The EPUB unit changes its authorized ending boundary.')
        return
    if authorized.get('container') and unit.get('container') != authorized['container']:
        raise GoogleSourceError('The source unit belongs to a different authorized container.')
    if unit.get('kind') == 'sheet_cells':
        import re

        def cell(value):
            match = re.fullmatch('([A-Z]{1,3})([1-9][0-9]*)', str(value))
            if not match:
                raise GoogleSourceError('The cell range must use explicit A1 coordinates.')
            column = 0
            for letter in match[1]:
                column = column * 26 + ord(letter) - 64
            row = int(match[2])
            if column > 1000 or row > 100000:
                raise GoogleSourceError('The cell range exceeds supported source coordinates.')
            return (row, column)
        a, b, c, d = (cell(authorized.get('start')), cell(unit.get('start')), cell(unit.get('end')), cell(authorized.get('end')))
        if all((a[index] <= b[index] <= c[index] <= d[index] for index in (0, 1))):
            return
        raise GoogleSourceError('The original cells extend outside the authorized rectangle.')
    if any((authorized.get(key) and unit.get(key) != authorized.get(key) for key in ('start_anchor', 'end_anchor'))):
        raise GoogleSourceError('The source unit does not preserve its authorized content boundary.')
    for key in ('end_anchor_exclusive', 'page_clips', 'boundary_precision'):
        expected = authorized.get('metadata', {}).get(key)
        if expected and unit.get('metadata', {}).get(key) != expected:
            raise GoogleSourceError('The source unit changes a verified boundary constraint.')
    if unit.get('kind') == 'media_time':
        try:
            import math
            raw = [authorized['start'], unit['start'], unit['end'], authorized['end']]
            values = [float(value) for value in raw]
            if not any((isinstance(value, bool) for value in raw)) and all((math.isfinite(value) for value in values)) and (0 <= values[0] <= values[1] < values[2] <= values[3]):
                return
        except (KeyError, ValueError, TypeError):
            pass
        raise GoogleSourceError('The transcript interval exceeds the authorized media range.')
    if unit.get('kind') in {'structured_path', 'dom_anchor'}:
        if all(((unit.get(key) or ([] if key == 'path' else '')) == (authorized.get(key) or ([] if key == 'path' else '')) for key in ('container', 'path', 'start_anchor', 'end_anchor'))):
            return
    if all((type(value) is int for value in (unit.get('start'), unit.get('end'), authorized.get('start'), authorized.get('end')))):
        if authorized['start'] <= unit['start'] <= unit['end'] <= authorized['end']:
            return
    elif unit == authorized:
        return
    raise GoogleSourceError('The original source unit is outside the selected range.')
