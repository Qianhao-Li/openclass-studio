"""Printed TOC discovery from cached native text. No PDF reopening or OCR."""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass, field
import unicodedata

def title_key(text):
    return ''.join((c for c in unicodedata.normalize('NFKC', text).casefold() if not c.isspace() and unicodedata.category(c) != 'Cf'))

@dataclass
class PrintedDirectory:
    pages: list[int] = field(default_factory=list)
    rows: list = field(default_factory=list)
    unit_ids: list[int] = field(default_factory=list)
    page_text: dict[int, str] = field(default_factory=dict)

def discover_printed_directory(data):
    from openclass_core.services.source_directory_extractor import _parse_native_toc_text
    from openclass_core.services.pdf_toc_parser import is_toc_heading
    by_page = defaultdict(list)
    for unit in data['units']:
        if unit['locator']['kind'] == 'pdf_pages':
            by_page[unit['locator']['start']].append(unit)
    found = PrintedDirectory()
    active = False
    body_start = None
    for page, units in sorted(by_page.items()):
        if active and body_start is not None and (page >= body_start):
            break
        text = '\n'.join((unit['text'] for unit in units))
        header = any((is_toc_heading(line) for line in text.splitlines()))
        if not active and (not header):
            continue
        rows = _parse_native_toc_text(text, toc_page=page)
        if active and (not rows):
            break
        if not rows:
            return found
        if not active:
            from openclass_core.services.source_directory_extractor import _heading_number_and_level
            roots = [entry['range']['start'] for entry in data.get('native_entries', []) if entry['range'].get('kind') == 'pdf_pages' and entry['range'].get('start', 0) > page and _heading_number_and_level(entry['title'])[0] and (_heading_number_and_level(entry['title'])[1] == 1)]
            body_start = min(roots) if roots else None
        active = True
        found.pages.append(page)
        found.rows.extend(rows)
        found.unit_ids.extend((unit['id'] for unit in units))
        found.page_text[page] = text
    return found

class BodyHeadingIndex:

    def __init__(self, data):
        self.data = data
        self.pages = defaultdict(list)
        self.folded = {}
        for unit in data['units']:
            if unit['locator']['kind'] == 'pdf_pages':
                self.pages[unit['locator']['start']].append(unit)

    def find(self, title, *, page=None, after=0):
        from openclass_core.services.source_basic_catalog import _heading_position
        keys = [page] if page is not None else sorted((p for p in self.pages if p > after))
        matches = []
        needle = title_key(title)
        for physical in keys:
            units = self.pages.get(physical, [])
            if physical not in self.folded:
                self.folded[physical] = title_key('\n'.join((unit['text'] for unit in units)))
            joined = self.folded[physical]
            if needle not in joined:
                continue
            for unit in units:
                offset = _heading_position(self.data, unit['id'], title)
                if offset is None:
                    continue
                prefix = unit['text'][:offset].strip()
                if prefix:
                    continue
                matches.append((unit['id'], offset))
        return matches
