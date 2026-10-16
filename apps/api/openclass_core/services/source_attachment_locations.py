"""Bind attachment directory labels to observed original headings, independently of AI addresses."""
from __future__ import annotations
from collections import defaultdict
import re
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_basic_toc import title_key
from openclass_core.services.source_heading_labels import heading_label_variants, canonical_pdf_label
_ORDINAL = re.compile('^(?:第\\s*[0-9一二三四五六七八九十百千]+\\s*[章节篇部卷]|(?:chapter|part|section)\\s+\\d+(?:\\.\\d+)*|\\d+(?:\\.\\d+)*[.)]?)\\s+', re.I)
_JOINED_ORDINAL = re.compile('^(?:\\d+(?:\\.\\d+)+(?=[A-Za-z\\u3400-\\u9fff])|[IVXLCDM]+\\s+)')
_QUOTES = str.maketrans({'’': "'", '‘': "'", '“': '"', '”': '"'})

def heading_label_key(title):
    """Native bookmarks may omit a structural number present in the printed title."""
    title = _ORDINAL.sub('', canonical_pdf_label(title).strip(), count=1)
    return title_key(_JOINED_ORDINAL.sub('', title, count=1).translate(_QUOTES))

class OriginalHeadingLocations:

    def __init__(self, data, excluded_pages, delivered_ids):
        self.data = data
        self.delivered = set(delivered_ids)
        self.pages = defaultdict(list)
        self.matches = {}
        for unit in data['units']:
            if unit['id'] not in self.delivered:
                continue
            locator = unit['locator']
            if locator['kind'] == 'pdf_pages' and locator['start'] in excluded_pages:
                continue
            key = (locator['kind'], locator.get('container', ''), locator.get('start'))
            self.pages[key].append(unit)
        self.folded = {key: title_key('\n'.join((unit['text'] for unit in units))) for key, units in self.pages.items()}
        from openclass_core.services.source_heading_layout import heading_layout_text
        self.layout = {key: heading_layout_text(units) for key, units in self.pages.items()}

    def find(self, title):
        from openclass_core.services.source_basic_catalog import _heading_position
        if title in self.matches:
            return self.matches[title]
        result = []
        native_declared = any((heading_label_key(entry['title']) == heading_label_key(title) for entry in self.data.get('native_entries', [])))
        for label in heading_label_variants(title):
            result.extend(self._find_literal(label))
            if result and (not native_declared):
                break
        self.matches[title] = sorted(set(result))
        return self.matches[title]

    def _find_literal(self, title):
        from openclass_core.services.source_basic_catalog import _heading_position
        needle = title_key(title)
        result = []
        for key, units in self.pages.items():
            if needle not in self.folded[key] and needle not in self.layout[key]:
                continue
            groups = []
            for unit in units:
                if groups and unit['id'] == groups[-1][-1]['id'] + 1:
                    groups[-1].append(unit)
                else:
                    groups.append([unit])
            for group in groups:
                bounded = {**self.data, 'units': self.data['units'][:group[-1]['id']]}
                for unit in group:
                    start = 0
                    while start < len(unit['text']):
                        offset = _heading_position(bounded, unit['id'], title, from_char=start)
                        if offset is None or offset < start or offset >= len(unit['text']):
                            break
                        prefix = unit['text'][:offset].rsplit('\n', 1)[-1].strip()
                        required = len(title_key(title))
                        consumed, end = (0, offset)
                        for end, character in enumerate(unit['text'][offset:], offset + 1):
                            consumed += len(title_key(character))
                            if consumed >= required:
                                break
                        line_tail = unit['text'][end:].split('\n', 1)[0].strip()
                        structural_prefix = re.fullmatch('(?:\\d+(?:\\.\\d+)*[.)]?|#{1,6}|part|chapter|section|book|unit|appendix)', prefix, re.I)
                        if not line_tail and (not prefix or structural_prefix):
                            result.append((unit['id'], offset))
                        start = offset + max(1, len(title))
        return result

    def native_candidates(self, title, candidates):
        key = heading_label_key(title)
        destinations = set()
        for entry in self.data.get('native_entries', []):
            if heading_label_key(entry['title']) != key:
                continue
            location = entry['range']
            destinations.add((location['kind'], location.get('container', ''), location.get('start')))
        return [pair for pair in candidates if tuple((self.data['units'][pair[0] - 1]['locator'].get(name, '' if name == 'container' else None) for name in ('kind', 'container', 'start'))) in destinations]

    def heading_weight(self, pair):
        unit = self.data['units'][pair[0] - 1]
        box = unit['locator'].get('metadata', {}).get('native_bbox')
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            return 0
        return max(0, box[3] - box[1]) / max(1, len(unit['text'].strip().splitlines()))

    def native_image_position(self, title):
        verified = set(self.data.get('visual_verified_units', [])) & self.delivered
        entries = [entry for entry in self.data.get('native_entries', []) if heading_label_key(entry['title']) == heading_label_key(title)]
        found = []
        for unit in self.data['units']:
            if unit['id'] not in verified or unit['text'].strip():
                continue
            if any((all((unit['locator'].get(key) == entry['range'].get(key) for key in ('kind', 'start', 'container'))) for entry in entries)):
                found.append((unit['id'], 0))
        return found

def locate_attachment_rows(data, rows, *, toc_pages=(), delivered_ids=None, authored=True):
    """Only real headings determine body ranges; a TOC row is a discovery anchor."""
    delivered = set(delivered_ids if delivered_ids is not None else (unit['id'] for unit in data['units']))
    lookup = OriginalHeadingLocations(data, set(toc_pages), delivered)
    candidates = []
    preferred = []
    generated = set()
    for index, row in enumerate(rows):
        if row.start_unit not in delivered:
            raise GoogleSourceError('The attachment directory returned a position outside its delivered original content.')
        found = lookup.find(row.title)
        if not found:
            found = lookup.native_image_position(row.title)
            if found:
                generated.add(index)
        if not found and (not authored):
            found = [(row.start_unit, 0)]
            generated.add(index)
        native = lookup.native_candidates(row.title, found)
        preferred.append(native)
        if row.start_char is not None and (row.start_unit, row.start_char) in found:
            found = [(row.start_unit, row.start_char)]
        candidates.append(found)
    result = []
    previous = (0, 0)
    for index, row in enumerate(rows):
        found = [pair for pair in candidates[index] if pair >= previous]
        next_anchor = next((values[0] for future, values in zip(rows[index + 1:], candidates[index + 1:], strict=True) if future.level <= row.level and len(values) == 1 and (values[0] > previous)), None)
        if next_anchor is not None:
            found = [pair for pair in found if pair < next_anchor]
        declared = [pair for pair in preferred[index] if pair in found]
        if len(declared) == 1:
            found = declared
        if len(found) > 1:
            weighted = sorted(((lookup.heading_weight(pair), pair) for pair in found), reverse=True)
            if weighted[0][0] > 0 and weighted[0][0] >= 1.5 * max(1, weighted[1][0]):
                found = [weighted[0][1]]
        if len(found) != 1:
            raise GoogleSourceError(f'Directory entry {index + 1} has no uniquely verified original heading. Its parsed contents remain available.')
        identifier, offset = found[0]
        previous = (identifier, offset)
        result.append(row.model_copy(update={'start_unit': identifier, 'start_char': None if index in generated else offset, 'end_unit': None}))
    return result
