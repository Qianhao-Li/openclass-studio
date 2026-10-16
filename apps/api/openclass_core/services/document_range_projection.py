"""Read-only rich-document geometry. Never used to authorize history mutations.

Generated block IDs are locators, not content. This projection ignores only
blockId during comparison; saved proofs and the range replay compiler stay intact.
Anchors use child-index paths and Unicode scalar inline offsets (atoms count one).
"""
from __future__ import annotations
from copy import deepcopy
from openclass_core.models import BoardDocument
from openclass_core.services.ridoc_range_actions import _alignment, _diff, inline_tokens
from openclass_core.services.ridoc_v3 import canonical_json_bytes, content_hash

def canonical_node(value):
    if isinstance(value, list):
        return [canonical_node(item) for item in value]
    if isinstance(value, dict):
        return {key: canonical_node({name: attr for name, attr in item.items() if name != 'blockId'}) if key == 'attrs' and isinstance(item, dict) and ('type' in value) else canonical_node(item) for key, item in value.items()}
    return value

def anchor(path, kind='node', start=0, end=0):
    return {'path': list(path), 'kind': kind, 'start': start, 'end': end}

def _shell(node):
    return {key: value for key, value in node.items() if key != 'content'}

def _inline(node):
    return node.get('type') in {'paragraph', 'heading', 'codeBlock'}

def _text_diff(old, new):

    def group(tokens):
        groups, offsets = ([], [0])
        index = 0
        while index < len(tokens):
            end = index + 1
            if tokens[index].get('char', '').isdecimal():
                while end < len(tokens):
                    char = tokens[end].get('char', '')
                    if char.isdecimal() or (char in {',', '.'} and end + 1 < len(tokens) and tokens[end + 1].get('char', '').isdecimal()):
                        end += 1
                    else:
                        break
            elif tokens[index].get('char', '').isascii() and tokens[index].get('char', '').isalpha():
                while end < len(tokens) and tokens[end].get('char', '').isascii() and tokens[end].get('char', '').isalpha():
                    end += 1
            while end < len(tokens) and tokens[end].get('char', '').isspace():
                end += 1
            groups.append({'tokens': tokens[index:end]})
            offsets.append(end)
            index = end
        return (groups, offsets)
    a, ai = group(old)
    b, bi = group(new)
    edits, ambiguous = _diff(a, b)
    return ([(ai[i], ai[j], bi[k], bi[l]) for i, j, k, l in edits], ambiguous)

class Alignment:

    def __init__(self, before, after):
        self.before, self.after = (canonical_node(before), canonical_node(after))
        self.nodes: dict[tuple, tuple] = {}
        self.text_edits: dict[tuple, list] = {}
        self.changes: list[dict] = []
        self.uncertain: set[tuple] = set()
        self.uncertain_text: set[tuple] = set()
        self._visit(self.before, self.after, (), ())

    def _change(self, before, after, reason=None):
        self.changes.append({'kind': 'replace' if before and after else 'delete' if before else 'insert', 'before': before, 'after': after, 'precision': 'unknown' if reason else 'exact', 'reason': reason})

    def _equal(self, node, left, right):
        self.nodes[left] = right
        for index, child in enumerate(node.get('content', [])):
            self._equal(child, (*left, index), (*right, index))

    def _visit(self, old, new, left, right):
        if old == new:
            self._equal(old, left, right)
            return
        if _shell(old) != _shell(new):
            self.nodes[left] = right
            self._change([anchor(left)], [anchor(right)])
            return
        self.nodes[left] = right
        if _inline(old):
            edits, ambiguity = _text_diff(inline_tokens(old.get('content', [])), inline_tokens(new.get('content', [])))
            self.text_edits[left] = edits
            if ambiguity:
                self.uncertain_text.add(left)
            for start, end, output_start, output_end in edits:
                self._change([anchor(left, 'text', start, end)] if start < end else [], [anchor(right, 'text', output_start, output_end)] if output_start < output_end else [], '重复文本或超出对齐预算，无法证明精确范围' if ambiguity else None)
                if start == end:
                    self.changes[-1]['before_marker'] = anchor(left, 'text', start, start)
                if output_start == output_end:
                    self.changes[-1]['after_marker'] = anchor(right, 'text', output_start, output_start)
            return
        a, b = (old.get('content', []), new.get('content', []))
        signatures = lambda nodes: [canonical_json_bytes(node).decode() for node in nodes]
        matches = _alignment(signatures(a), signatures(b))
        if matches is None or matches[0] != matches[1]:
            self.uncertain.add(left)
            self._change([anchor((*left, i)) for i in range(len(a))], [anchor((*right, i)) for i in range(len(b))], '结构存在多种对应位置，无法证明精确范围')
            return
        pairs = matches[0]
        for i, j in pairs:
            self._equal(a[i], (*left, i), (*right, j))
        bounds = [(-1, -1), *pairs, (len(a), len(b))]
        for previous, following in zip(bounds, bounds[1:]):
            i, j, k, end = (previous[0] + 1, following[0], previous[1] + 1, following[1])
            if i == j and k == end:
                continue
            if j - i == end - k and all((x.get('type') == y.get('type') for x, y in zip(a[i:j], b[k:end]))):
                for offset in range(j - i):
                    self._visit(a[i + offset], b[k + offset], (*left, i + offset), (*right, k + offset))
            else:
                self._change([anchor((*left, x)) for x in range(i, j)], [anchor((*right, x)) for x in range(k, end)])
                self.changes[-1]['before_marker'] = anchor(left, 'gap', i, i)
                self.changes[-1]['after_marker'] = anchor(right, 'gap', k, k)

    def map(self, source):
        path = tuple(source['path'])
        if any((path[:len(parent)] == parent for parent in self.uncertain)):
            return []
        if source['kind'] == 'text' and path in self.uncertain_text:
            return []
        target = self.nodes.get(path)
        if target is None:
            parent, index = (path[:-1], path[-1] if path else 0)
            if parent not in self.nodes:
                return []
            siblings = sorted(((p[-1], q[-1]) for p, q in self.nodes.items() if p[:-1] == parent and len(p) == len(path)))
            following = next((q for p, q in siblings if p > index), None)
            previous = next((q for p, q in reversed(siblings) if p < index), None)
            if following is None and previous is None:
                return []
            position = following if following is not None else previous + 1
            return [anchor(self.nodes[parent], 'gap', position, position)]
        if source['kind'] != 'text':
            if source['kind'] == 'gap':
                old_child = (*path, source['start'])
                if old_child in self.nodes:
                    position = self.nodes[old_child][-1]
                else:
                    previous = self.nodes.get((*path, source['start'] - 1))
                    position = previous[-1] + 1 if previous else 0
                return [anchor(target, 'gap', position, position)]
            insertions = [change for change in self.changes if change['kind'] == 'insert' and tuple(change.get('before_marker', {}).get('path', []))[:len(path)] == path]
            if insertions:
                node = self.before
                for index in path:
                    node = node['content'][index]
                if _inline(node):
                    total = len(inline_tokens(node.get('content', [])))
                    base = self.map(anchor(path, 'text', 0, total))
                    if not base:
                        return []
                    parts = [(base[0]['start'], base[0]['end'])]
                    for a, b, c, d in self.text_edits.get(path, []):
                        if a == b:
                            parts = [(x, y) for left, right in parts for x, y in [(left, min(right, c)), (max(left, d), right)] if x < y]
                    return [anchor(target, 'text', left, right) for left, right in parts]
                if node.get('content'):
                    return [mapped for i in range(len(node['content'])) for mapped in self.map(anchor((*path, i)))]
            return [dict(source, path=list(target))]
        start, end = (source['start'], source['end'])
        edits = self.text_edits.get(path, [])

        def offset(value, end_edge=False):
            shift = 0
            for a, b, c, d in edits:
                if value < a or (value == a and (not end_edge)):
                    break
                if a <= value < b:
                    return d if end_edge else c
                shift += d - c - (b - a)
            return value + shift
        return [anchor(target, 'text', offset(start), offset(end, True))]

def _empty(document):

    def has_content(node):
        if node.get('type') == 'text':
            return bool(node.get('text', '').strip())
        children = node.get('content', [])
        return any((has_content(child) for child in children)) if children else node.get('type') not in {'doc', 'paragraph'}
    return not has_content(document.content_json)

def project_document_actions(initial_id: str, initial: BoardDocument, actions: list[dict], reference: BoardDocument):
    reference_ref = content_hash(reference.content_json)
    source = [{'node_id': initial_id, 'before': None, 'after': initial}, *actions]
    result = {}
    for action in source:
        before, after = (action['before'], action['after'])
        if before is None:
            changes = [{'kind': 'initial', 'before': [], 'after': [anchor((), 'gap')] if _empty(after) else [anchor((i,)) for i in range(len(after.content_json.get('content', [])))], 'precision': 'exact', 'reason': None}]
        else:
            changes = Alignment(before.content_json, after.content_json).changes
            if _empty(before) and (not _empty(after)):
                changes = [{'kind': 'insert', 'before': [], 'after': [anchor((i,)) for i in range(len(after.content_json.get('content', [])))], 'precision': 'exact', 'reason': None}]
        mapping = Alignment(after.content_json, reference.content_json)
        fragments = []
        for index, change in enumerate(changes):
            after_ranges = change['after'] or [change.get('after_marker', anchor((), 'gap'))]
            mapped_groups = [mapping.map(item) for item in after_ranges] if change['precision'] == 'exact' else []
            projected = [item for group in mapped_groups for item in group]
            fragments.append({**change, 'id': f"{action['node_id']}:{index}", 'reference': projected, 'reference_precision': 'exact' if mapped_groups and all(mapped_groups) else 'unknown'})
        result[action['node_id']] = {'version': 1, 'offset_unit': 'unicode_scalar_inline_atom', 'reference_document_ref': reference_ref, 'before_document_ref': content_hash(before.content_json) if before else None, 'after_document_ref': content_hash(after.content_json), 'fragments': fragments, 'before_document': before.model_dump(mode='json') if before and any((f['kind'] == 'delete' for f in fragments)) else None}
    return result

def attach_range_projections(archive, lesson_id, branch_name, actions):
    from openclass_core.services.ridoc_range_edit import source_actions, range_result, baseline_withdrawn
    from openclass_core.services.ridoc_v34_states import archive_state
    initial_id, initial, source = source_actions(archive, lesson_id, branch_name)
    pending = getattr(archive, '_range_pending', {}).get(lesson_id, set())
    if (lesson_id, branch_name) in getattr(archive, '_range_touched', set()) and pending:
        reference = range_result(archive, lesson_id, branch_name, pending | baseline_withdrawn(archive, lesson_id, branch_name))['document']
    else:
        reference = archive_state(archive, lesson_id, branch_name)['document']
    projections = project_document_actions(initial_id, initial, source, reference)
    return [dict(action, range_projection=projections.get(action['id'])) for action in actions]
