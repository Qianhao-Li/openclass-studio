"""Recover numbered heading depth when native bookmarks encode a flat list."""
from openclass_core.services.source_directory_extractor import _heading_number_and_level

def normalized_native_navigation(data):
    entries = data.get('native_entries', [])
    if not entries or {entry['level'] for entry in entries} != {1}:
        return data
    markers = [_heading_number_and_level(entry['title']) for entry in entries]
    if not any((number and '.' in number for number, _ in markers)):
        return data
    normalized = []
    for entry, (number, level) in zip(entries, markers, strict=True):
        normalized.append({**entry, 'level': level if number else entry['level'], 'number': number, 'native_declared_level': entry['level']})
    return {**data, 'native_entries': normalized, 'native_hierarchy_method': 'numbered_original_headings'}
