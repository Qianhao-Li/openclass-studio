"""Snapshot-constrained, domain-independent rich-document net changes.

Offsets are Unicode scalar positions in typed inline tokens, never browser UTF-16
offsets. This compiler records *changes*, not what an editor or AI read. Historical
and new actions use exactly the same compiler. Repeated-text alignment is a
separate uncertainty check; a successful patch round trip does not prove it.
"""
from __future__ import annotations
from copy import deepcopy
from difflib import SequenceMatcher
import json
from typing import Any
from openclass_core.models import BoardDocument
from openclass_core.services.rich_document import rebuild_document_from_content_json
from openclass_core.services.ridoc_document_actions import DocumentActionBlocked
from openclass_core.services.ridoc_v3 import content_hash
COMPILER_VERSION = 'range-net-diff-1'
_TEXT_BLOCKS = {'paragraph', 'heading', 'codeBlock'}
_LCS_CELL_LIMIT = 250000

def _key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)

def inline_tokens(children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for child in children:
        if child.get('type') == 'text' and isinstance(child.get('text'), str):
            shell = {key: deepcopy(value) for key, value in child.items() if key != 'text'}
            result.extend(({'kind': 'text', 'char': char, 'shell': shell} for char in child['text']))
        else:
            result.append({'kind': 'inline', 'node': deepcopy(child)})
    return result

def tokens_inline(tokens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for token in tokens:
        if token['kind'] == 'inline':
            result.append(deepcopy(token['node']))
        elif token['kind'] == 'text':
            shell = token['shell']
            if result and result[-1].get('type') == 'text' and ({key: value for key, value in result[-1].items() if key != 'text'} == shell):
                result[-1]['text'] += token['char']
            else:
                result.append({**deepcopy(shell), 'text': token['char']})
        else:
            raise DocumentActionBlocked('不支持的文档片段类型')
    return result

def document_layout(document: BoardDocument) -> dict[str, Any]:
    template = deepcopy(document.content_json)
    tapes: dict[str, list[dict[str, Any]]] = {}
    paths: dict[str, list[int]] = {}
    identities: set[str] = set()

    def visit(node: dict[str, Any], path: list[int]) -> None:
        children = node.get('content', [])
        if not isinstance(children, list) or any((not isinstance(child, dict) for child in children)):
            raise DocumentActionBlocked('文档结构无法可靠定位文本范围')
        if node.get('type') in _TEXT_BLOCKS or any((child.get('type') == 'text' for child in children)):
            attrs = node.get('attrs', {})
            identity = attrs.get('blockId') if isinstance(attrs, dict) else None
            key = f'block:{identity}' if isinstance(identity, str) and identity.strip() else 'path:' + '/'.join(map(str, path))
            if key in identities:
                raise DocumentActionBlocked('文档块 ID 重复，无法可靠定位文本范围', key)
            identities.add(key)
            tapes[key], paths[key] = (inline_tokens(children), path)
            node['content'] = []
        else:
            for index, child in enumerate(children):
                visit(child, [*path, index])
    visit(template, [])
    layout_hash = content_hash({'id': document.id, 'title': document.title, 'page_settings': document.page_settings.model_dump(mode='json'), 'template': template, 'paths': paths})
    flat = len(paths) == len(template.get('content', [])) and list(paths.values()) == [[index] for index in range(len(paths))]
    shells = {}
    for key, path in paths.items():
        node = template
        for index in path:
            node = node['content'][index]
        shells[key] = {name: deepcopy(value) for name, value in node.items() if name != 'content'}
    root_hash = content_hash({'id': document.id, 'title': document.title, 'page_settings': document.page_settings.model_dump(mode='json'), 'root': {key: value for key, value in template.items() if key != 'content'}})
    return {'template': template, 'paths': paths, 'tapes': tapes, 'layout_hash': layout_hash, 'flat': flat, 'shells': shells, 'root_hash': root_hash}

def rebuild_with_tapes(document: BoardDocument, layout: dict[str, Any], tapes: dict[str, list[dict[str, Any]]]) -> BoardDocument:
    root = deepcopy(layout['template'])
    for key, path in layout['paths'].items():
        node = root
        for index in path:
            node = node['content'][index]
        node['content'] = tokens_inline(tapes[key])
    return rebuild_document_from_content_json(document, root)

def same_document_content(left: BoardDocument, right: BoardDocument) -> bool:
    """Ignore only derived HTML/text and equivalent splitting of text runs."""
    try:
        old, new = (document_layout(left), document_layout(right))
        return old['layout_hash'] == new['layout_hash'] and old['tapes'] == new['tapes']
    except DocumentActionBlocked:
        return (left.id, left.title, left.page_settings, left.content_json) == (right.id, right.title, right.page_settings, right.content_json)

def _alignment(a: list[str], b: list[str]) -> tuple[list[tuple[int, int]], list[tuple[int, int]]] | None:
    if len(a) * len(b) > _LCS_CELL_LIMIT:
        return None
    rows = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) - 1, -1, -1):
        for j in range(len(b) - 1, -1, -1):
            rows[i][j] = max(rows[i + 1][j], rows[i][j + 1], 1 + rows[i + 1][j + 1] if a[i] == b[j] else 0)

    def walk(reverse: bool) -> list[tuple[int, int]]:
        result, i, j = ([], 0, 0)
        while i < len(a) and j < len(b):
            choices = []
            if a[i] == b[j] and rows[i][j] == 1 + rows[i + 1][j + 1]:
                choices.append('match')
            if rows[i][j] == rows[i + 1][j]:
                choices.append('delete')
            if rows[i][j] == rows[i][j + 1]:
                choices.append('insert')
            choice = choices[-1] if reverse else choices[0]
            if choice == 'match':
                result.append((i, j))
                i, j = (i + 1, j + 1)
            elif choice == 'delete':
                i += 1
            else:
                j += 1
        return result
    return (walk(False), walk(True))

def _diff(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> tuple[list[tuple[int, int, int, int]], list[dict[str, Any]]]:
    a, b = (list(map(_key, old)), list(map(_key, new)))
    if a == b:
        return ([], [])
    matches = _alignment(a, b)
    if matches is None:
        return ([(0, len(a), 0, len(b))], [{'start': 0, 'end': len(a), 'reason': '范围对齐超过验证预算，按整个文本块处理'}])
    first, second = matches
    boundaries = [(-1, -1), *first, (len(a), len(b))]
    edits = [(left[0] + 1, right[0], left[1] + 1, right[1]) for left, right in zip(boundaries, boundaries[1:]) if left[0] + 1 != right[0] or left[1] + 1 != right[1]]
    if first == second:
        return (edits, [])
    prefix = 0
    while prefix < len(first) and first[prefix] == second[prefix]:
        prefix += 1
    suffix = 0
    while suffix < len(first) - prefix and first[-1 - suffix] == second[-1 - suffix]:
        suffix += 1
    left = first[prefix - 1] if prefix else (-1, -1)
    right = first[len(first) - suffix] if suffix else (len(a), len(b))
    expanded = (left[0] + 1, right[0], left[1] + 1, right[1])
    retained = [edit for edit in edits if edit[1] <= expanded[0] and edit[3] <= expanded[2] or (edit[0] >= expanded[1] and edit[2] >= expanded[3])]
    return (sorted([*retained, expanded]), [{'start': expanded[0], 'end': expanded[1], 'reason': '重复内容存在多种对齐，已扩大到能够验证的范围'}])

def _block_matches(old: dict[str, Any], new: dict[str, Any]) -> list[tuple[int, int]] | None:
    old_keys, new_keys = (list(old['tapes']), list(new['tapes']))

    def signature(layout, key):
        return _key([layout['shells'][key], layout['tapes'][key]])
    old_signatures = [signature(old, key) for key in old_keys]
    new_signatures = [signature(new, key) for key in new_keys]
    if old_signatures == new_signatures:
        return list(zip(range(len(old_keys)), range(len(new_keys))))
    anchors = []
    for i, key in enumerate(old_keys):
        if key.startswith('block:') and key in new_keys and (old['shells'][key] == new['shells'][key]):
            anchors.append((i, new_keys.index(key)))
        elif not key.startswith('block:'):
            sig = old_signatures[i]
            if old_signatures.count(sig) == 1 and new_signatures.count(sig) == 1:
                anchors.append((i, new_signatures.index(sig)))
    aligned = _alignment(old_signatures, new_signatures)
    if aligned is not None and aligned[0] == aligned[1]:
        anchors.extend(aligned[0])
    anchors = sorted(set(anchors))
    if any((left[1] >= right[1] for left, right in zip(anchors, anchors[1:]))):
        return None
    result = list(anchors)
    edges = [(-1, -1), *anchors, (len(old_keys), len(new_keys))]
    for left, right in zip(edges, edges[1:]):
        i, j = (left[0] + 1, left[1] + 1)
        if right[0] - i == right[1] - j == 1:
            old_key, new_key = (old_keys[i], new_keys[j])
            if old['shells'][old_key] == new['shells'][new_key] and (old_key == new_key or (not old_key.startswith('block:') and (not new_key.startswith('block:')))):
                result.append((i, j))
    return sorted(result)

def compile_range_action(before: BoardDocument, after: BoardDocument) -> dict[str, Any]:
    common = {'version': 2, 'compiler': COMPILER_VERSION, 'before_hash': content_hash(before), 'after_hash': content_hash(after)}
    try:
        old, new = (document_layout(before), document_layout(after))
    except DocumentActionBlocked as exc:
        return {**common, 'scope': 'document', 'reason': str(exc), 'operations': [], 'ambiguities': []}
    matched = _block_matches(old, new) if old['flat'] and new['flat'] and (old['root_hash'] == new['root_hash']) else list(zip(range(len(old['tapes'])), range(len(new['tapes'])))) if old['layout_hash'] == new['layout_hash'] else None
    if matched is None:
        return {**common, 'scope': 'document', 'reason': '文档结构或属性变化，按整文档范围处理', 'operations': [], 'ambiguities': []}
    operations, ambiguities, block_operations = ([], [], [])
    old_keys, new_keys = (list(old['tapes']), list(new['tapes']))
    for old_index, new_index in matched:
        block, target = (old_keys[old_index], new_keys[new_index])
        tokens = old['tapes'][block]
        changes, uncertain = _diff(tokens, new['tapes'][target])
        ambiguities.extend(({'block_id': block, **item} for item in uncertain))
        for start, end, output_start, output_end in changes:
            operations.append({'block_id': block, 'start': start, 'end': end, 'removed': deepcopy(tokens[start:end]), 'inserted': deepcopy(new['tapes'][target][output_start:output_end])})
    edges = [(-1, -1), *matched, (len(old_keys), len(new_keys))]

    def block_spec(layout, key):
        return {'key': key, 'shell': deepcopy(layout['shells'][key]), 'tokens': deepcopy(layout['tapes'][key])}
    for left, right in zip(edges, edges[1:]):
        start, end, new_start, new_end = (left[0] + 1, right[0], left[1] + 1, right[1])
        if start != end or new_start != new_end:
            block_operations.append({'start': start, 'end': end, 'removed': [block_spec(old, key) for key in old_keys[start:end]], 'inserted': [block_spec(new, key) for key in new_keys[new_start:new_end]]})
            if end - start > 1 and new_end - new_start > 1:
                ambiguities.append({'block_id': 'document/blocks', 'start': start, 'end': end, 'reason': '段落对应位置存在歧义，按完整受影响段落组处理'})
    return {**common, 'scope': 'ranges', 'layout_hash': old['layout_hash'], 'operations': operations, 'block_operations': block_operations, 'source_order': old_keys, 'target_order': new_keys, 'block_matches': [[old_keys[i], new_keys[j]] for i, j in matched], 'ambiguities': ambiguities}

def validate_compiled_action(before: BoardDocument, after: BoardDocument, action: dict[str, Any]) -> None:
    if compile_range_action(before, after) != action:
        raise DocumentActionBlocked('The range change proof does not match the original before and after snapshots')
    if action['scope'] == 'document':
        return
    old, new = (document_layout(before), document_layout(after))
    forward, inverse = (deepcopy(old['tapes']), deepcopy(new['tapes']))
    by_block: dict[str, list[dict[str, Any]]] = {}
    for operation in action['operations']:
        by_block.setdefault(operation['block_id'], []).append(operation)
    for block, operations in by_block.items():
        target = dict(action['block_matches'])[block]
        delta = 0
        inverses = []
        for operation in operations:
            start, end = (operation['start'], operation['end'])
            inverses.append((start + delta, start + delta + len(operation['inserted']), operation['removed']))
            delta += len(operation['inserted']) - (end - start)
        for operation in reversed(operations):
            forward[block][operation['start']:operation['end']] = operation['inserted']
        for start, end, replacement in reversed(inverses):
            inverse[target][start:end] = replacement
    mapped_forward = {target: forward[source] for source, target in action['block_matches']}
    for operation in action['block_operations']:
        mapped_forward.update({item['key']: item['tokens'] for item in operation['inserted']})
    mapped_inverse = {source: inverse[target] for source, target in action['block_matches']}
    for operation in action['block_operations']:
        mapped_inverse.update({item['key']: item['tokens'] for item in operation['removed']})
    if mapped_forward != new['tapes'] or mapped_inverse != old['tapes']:
        raise DocumentActionBlocked('The range change failed complete forward and reverse replay verification')

def apply_range_action(current: BoardDocument, before: BoardDocument, after: BoardDocument, action: dict[str, Any]) -> BoardDocument:
    """Exact original-state application; divergent replay requires range lineage."""
    validate_compiled_action(before, after, action)
    if not same_document_content(current, before):
        raise DocumentActionBlocked('The current document differs from the original input. Replay requires verified fragment lineage')
    return after.model_copy(deep=True)
