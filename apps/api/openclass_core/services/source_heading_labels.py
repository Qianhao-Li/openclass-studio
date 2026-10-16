"""Conservative layout variants; original heading labels remain unchanged."""
import re
import unicodedata
_TRAILING_MARKER = re.compile('(?:\\([*†‡⇤]+\\)|[*†‡⇤]+)\\s*$')
_ORDINAL_DOT = re.compile('^(\\d+)\\.(?=\\s)')
_ACCENTS = {'´': '́', '˝': '̋'}

def canonical_pdf_label(value):
    return re.sub('([´˝])([A-Za-z])', lambda m: unicodedata.normalize('NFC', m[2] + _ACCENTS[m[1]]), value)

def spacing_pdf_label(value):
    inverse = {mark: spacing for spacing, mark in _ACCENTS.items()}
    return re.sub('([A-Za-z])([\\u0301\\u030b])', lambda m: inverse[m[2]] + m[1], unicodedata.normalize('NFD', value))

def heading_label_variants(title):
    original = title.strip()
    without_marker = _TRAILING_MARKER.sub('', original).rstrip()
    base = [original, without_marker, _ORDINAL_DOT.sub('\\1', original), _ORDINAL_DOT.sub('\\1', without_marker)]
    return list(dict.fromkeys((value for value in [*base, *(spacing_pdf_label(item) for item in base)] if value)))
