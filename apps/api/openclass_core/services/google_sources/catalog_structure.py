"""Format-level directory navigation and cloud block positioning, independent of subject."""
from __future__ import annotations
import re
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from .processor import normalized_blocks
_PREFIX = re.compile('^(?:第\\s*([\\d一二三四五六七八九十百零〇两]+)\\s*[章节篇部卷]|(?:chapter|section|part|unit|lesson)\\s+(\\d+(?:\\.\\d+)*|[ivxlcdm]+)\\b|([0-9]+(?:\\.[0-9]+)*)[.、:：\\s]+|(?:appendix|附录)\\s*([a-z])\\b)\\s*', re.I)
_TOC = {'目录', '目錄', '目次', 'contents', 'tableofcontents', 'tableofcontent'}

def normalized(value):
    return ''.join((character for character in unicodedata.normalize('NFKC', value).casefold() if character.isalnum()))

def integer_label(value):
    value = value.strip().casefold()
    if value.isdigit():
        return int(value)
    digits = {character: index for index, character in enumerate('零一二三四五六七八九')}
    digits.update({'〇': 0, '两': 2})
    if value and all((character in digits or character in '十百' for character in value)):
        total, digit = (0, 0)
        for character in value:
            if character in digits:
                digit = digits[character]
            else:
                total += (digit or 1) * (10 if character == '十' else 100)
                digit = 0
        return total + digit
    if value and re.fullmatch('(?=[ivxlcdm]+$)m{0,4}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})', value):
        nums = {'i': 1, 'v': 5, 'x': 10, 'l': 50, 'c': 100, 'd': 500, 'm': 1000}
        return sum((-nums[c] if i + 1 < len(value) and nums[c] < nums[value[i + 1]] else nums[c] for i, c in enumerate(value)))
    return None

def heading_parts(title):
    value = unicodedata.normalize('NFKC', title)
    match = _PREFIX.match(value)
    if match is None:
        return (None, title.strip())
    number = next((value for value in match.groups() if value))
    return (integer_label(number.rsplit('.', 1)[-1]), value[match.end():].lstrip(' .、:：-—'))

def heading_number(title):
    """Preserve the complete authored structural number, separate from title."""
    match = _PREFIX.match(unicodedata.normalize('NFKC', title))
    if match is None:
        return ''
    if match.group(4):
        return match.group(4).upper()
    raw = next((value for value in match.groups() if value))
    if all((part.isdigit() for part in raw.split('.'))):
        return '.'.join((str(int(part)) for part in raw.split('.')))
    ordinal = integer_label(raw)
    return str(ordinal) if ordinal is not None else raw

def page_marker(title):
    value = unicodedata.normalize('NFKC', title).strip()
    value = re.sub('^(?:page\\s*|p\\.\\s*|第\\s*)', '', value, flags=re.I)
    value = re.sub('\\s*页$', '', value).strip(' •·.()[]【】—- ')
    return integer_label(value)

def semantic_native_candidates(candidates):
    numeric = [item for item in candidates if page_marker(item['title']) is not None]
    if len(numeric) >= 6 and len(numeric) > len(candidates) * 0.6:
        return []
    return [item for item in candidates if page_marker(item['title']) is None]

def cloud_blocks(outputs):
    blocks = []
    for part_index, output in enumerate(outputs):
        for block in normalized_blocks(output['parsed']):
            offset = output.get('page_offset', 0)
            blocks.append({**block, 'ref': output['document_id'] + '/' + block['id'], 'document_id': output['document_id'], 'part_index': part_index, 'order': len(blocks), 'page_start': block['page_start'] + offset if type(block['page_start']) is int else None, 'page_end': block['page_end'] + offset if type(block['page_end']) is int else None})
    return blocks

def toc_pages(blocks, native, hints=()):
    by_page = defaultdict(list)
    for block in blocks:
        if type(block['page_start']) is int and block['text']:
            by_page[block['page_start']].append(block)
    pages = {page for page, items in by_page.items() if any((normalized(item['text']) in _TOC for item in items))}
    pages.update((item['page_start'] for item in native if normalized(item['title']) in _TOC and type(item.get('page_start')) is int))
    hinted = {page for hint in hints for page in range(int(hint.get('page_start') or 0), int(hint.get('page_end') or 0) + 1)}
    dense = {page for page, items in by_page.items() if sum((len(re.findall('(?:/|\\.{2,}|…|·{2,}|\\s{2})\\s*\\d{1,5}(?!\\d)', item['text'])) for item in items if len(item['text']) < 700)) >= 3}
    pages.update(dense & hinted)
    while True:
        extended = pages | {page for page in dense if page - 1 in pages or page + 1 in pages}
        if extended == pages:
            break
        pages = extended
    return sorted((page for page in pages if page in by_page))

def pagination_offset(blocks, directory_pages):
    anchors = defaultdict(set)
    for block in blocks:
        page = block['page_start']
        if page in directory_pages or block.get('table_id') or page != block['page_end']:
            continue
        printed = page_marker(block['text'])
        if type(page) is int and printed is not None and (printed > 0):
            anchors[page - printed].add(page)
    ranked = sorted(anchors.items(), key=lambda item: len(item[1]), reverse=True)
    if not ranked or len(ranked[0][1]) < 3 or (len(ranked) > 1 and len(ranked[0][1]) <= len(ranked[1][1])):
        return None
    return ranked[0][0]

def find_body_heading(title, blocks, directory_pages, expected_page=None):
    ordinal, content = heading_parts(title)
    wanted = normalized(content)
    if not wanted:
        return None
    candidates = []
    for block in blocks:
        if not block['text'] or block['page_start'] in directory_pages or block['type'] in {'HEADER', 'FOOTER'} or block.get('table_id'):
            continue
        number, actual_content = heading_parts(block['text'])
        actual = normalized(actual_content)
        if ordinal is not None and number is not None and (ordinal != number):
            continue
        trailing_noise = expected_page is not None and block['type'].startswith('HEADING') and actual.startswith(wanted) and (len(actual) - len(wanted) <= 80)
        if not trailing_noise and abs(len(actual) - len(wanted)) > max(3, len(wanted) * 0.18):
            continue
        ratio = 1 if trailing_noise else SequenceMatcher(None, wanted, actual).ratio()
        if ratio < 0.86 or (expected_page is None and ratio < 0.96):
            continue
        if expected_page is not None and block['page_start'] != expected_page:
            continue
        candidates.append((ratio + (0.15 if block['type'].startswith('HEADING') else 0), block))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1]['order']))
    if expected_page is None and len({item[1]['page_start'] for item in candidates if item[0] == candidates[0][0]}) > 1:
        return None
    return candidates[0][1]

def shared_boundary_page(next_heading, blocks):
    """Keep a page's preceding section content, excluding its running header."""
    seen_pages = defaultdict(set)
    for block in blocks:
        if block['text']:
            seen_pages[normalized(block['text'])].add(block['page_start'])
    for block in blocks:
        if block['order'] >= next_heading['order']:
            break
        if block['page_start'] != next_heading['page_start'] or not block['text']:
            continue
        if block['type'] in {'HEADER', 'FOOTER'} or page_marker(block['text']) is not None:
            continue
        if len(seen_pages[normalized(block['text'])]) >= 3:
            continue
        if len(normalized(block['text'])) < 12 and (not block.get('table_id')) and (not re.search('[$=<>±∑√]|\\\\(?:frac|begin|\\[|\\()', block['text'])):
            continue
        if block['type'].startswith('HEADING') and block['page_end'] != block['page_start']:
            continue
        return True
    return False
