"""Original PDF text-layer spelling candidates, never a directory authority."""
from __future__ import annotations
import re
import unicodedata
from difflib import SequenceMatcher
from openclass_core.services.google_sources.catalog_structure import heading_number, heading_parts

def _normalized(text):
    return ''.join((character for character in unicodedata.normalize('NFKC', text) if character.isalnum()))

def native_title_hints(path, page_number, rows):
    """Flag likely spelling/number omissions for an original-image reread.

    No host OCR runs and nothing is replaced. A vision reread of the unchanged
    original page decides whether the visible title or hidden text is correct.
    """
    if not rows:
        return []
    from openclass_core.services import pdf_backend as fitz
    with fitz.open(path) as document:
        text = document[page_number - 1].get_text('text')
    if not text.strip():
        return []
    normalized = _normalized(text)
    hints = []
    for row in rows:
        title = row.title if hasattr(row, 'title') else row['title']
        key = row.key if hasattr(row, 'key') else row['key']
        supplied_number = row.number if hasattr(row, 'number') else row.get('number', '')
        number = supplied_number or heading_number(title)
        missing_number = not bool(number)
        if _normalized(title) in normalized and (not missing_number):
            continue
        match = re.search('(?<![\\d.])' + re.escape(number) + '(?![\\d.])', text) if number else None
        if match is not None:
            hints.append({'row_key': key, 'observed_title': title, 'missing_number': missing_number, 'native_fragment': text[max(0, match.start() - 16):match.end() + 180]})
        elif not number:
            wanted = _normalized(title)
            candidates = []
            for line in text.splitlines():
                if not heading_number(line):
                    continue
                _, candidate = heading_parts(line)
                candidate = re.split('\\.{2,}|…|·{2,}|\\s{2,}', candidate)[0]
                ratio = SequenceMatcher(None, wanted, _normalized(candidate)).ratio()
                if ratio >= 0.8:
                    candidates.append((ratio, line))
            candidates.sort(reverse=True)
            if candidates:
                hints.append({'row_key': key, 'observed_title': title, 'missing_number': missing_number, 'spelling_mismatch': _normalized(title) not in normalized, 'native_fragment': candidates[0][1][:200]})
    return sorted(hints, key=lambda item: not item.get('spelling_mismatch', not item['missing_number']))[:12]
