"""Atomic selective replay over snapshot-derived text and gap identities.

This is a pure engine: no course files, SQLite writes, migrations or AI calls.
Equal text with a different producer is never substituted for an expected input.
Independent saved-state discontinuities are explicit conservative checkpoints.
"""
from __future__ import annotations
from copy import deepcopy
from typing import Any
from openclass_core.models import BoardDocument
from openclass_core.services.ridoc_document_actions import DocumentActionBlocked
from openclass_core.services.ridoc_range_actions import compile_range_action, document_layout, rebuild_with_tapes, same_document_content, tokens_inline, validate_compiled_action
from openclass_core.services.rich_document import rebuild_document_from_content_json
from openclass_core.services.ridoc_v3 import content_hash
from openclass_core.services.ridoc_range_compilation_cache import verified_replay

def _identity(*parts: Any) -> str:
    return content_hash(list(parts))

def create_range_trace(document: BoardDocument, *, origin: str='', checkpoint_dependencies: list[str] | None=None) -> dict[str, Any]:
    layout = document_layout(document)
    blocks = {}
    seed = _identity('base', content_hash(document), origin)
    for key, values in layout['tapes'].items():
        blocks[key] = {'producer': origin, 'shell': deepcopy(layout['shells'][key]), 'tokens': [{'id': _identity(seed, key, 'text', index), 'producer': origin, 'value': deepcopy(value)} for index, value in enumerate(values)], 'gaps': [{'id': _identity(seed, key, 'gap', index), 'producer': origin, 'kind': 'base'} for index in range(len(values) + 1)]}
    return {'version': 2, 'document_hash': content_hash(document), 'layout_hash': layout['layout_hash'], 'structure_producer': origin, 'checkpoint_dependencies': sorted(set(checkpoint_dependencies or [])), 'root_hash': layout['root_hash'], 'flat': layout['flat'], 'block_order': list(blocks), 'block_gaps': [{'id': _identity(seed, 'block-gap', index), 'producer': origin, 'kind': 'base'} for index in range(len(blocks) + 1)], 'blocks': blocks}

def validate_range_trace(document: BoardDocument, trace: dict[str, Any]) -> None:
    try:
        layout = document_layout(document)
        if set(trace) != {'version', 'document_hash', 'layout_hash', 'structure_producer', 'checkpoint_dependencies', 'blocks', 'root_hash', 'flat', 'block_order', 'block_gaps'} or trace['version'] != 2 or trace['document_hash'] != content_hash(document) or (trace['layout_hash'] != layout['layout_hash']) or (trace['root_hash'] != layout['root_hash']) or (trace['flat'] != layout['flat']) or (len(trace['block_order']) != len(set(trace['block_order']))) or (set(trace['block_order']) != set(trace['blocks'])) or (len(trace['blocks']) != len(layout['tapes'])) or (len(trace['block_gaps']) != len(trace['blocks']) + 1) or (not isinstance(trace['structure_producer'], str)) or (not isinstance(trace['checkpoint_dependencies'], list)) or any((not isinstance(value, str) for value in trace['checkpoint_dependencies'])) or (trace['checkpoint_dependencies'] != sorted(set(trace['checkpoint_dependencies']))):
            raise ValueError('shape')
        identities: set[str] = set()
        for key, actual_key in zip(trace['block_order'], layout['tapes']):
            block = trace['blocks'][key]
            if set(block) != {'tokens', 'gaps', 'producer', 'shell'} or len(block['gaps']) != len(block['tokens']) + 1 or block['shell'] != layout['shells'][actual_key] or (not isinstance(block['producer'], str)):
                raise ValueError('block')
            if [item['value'] for item in block['tokens']] != layout['tapes'][actual_key]:
                raise ValueError('text')
            for item in [*block['tokens'], *block['gaps']]:
                identity = item['id']
                if not isinstance(identity, str) or len(identity) != 64 or identity in identities or (not isinstance(item['producer'], str)):
                    raise ValueError('identity')
                identities.add(identity)
        for gap in trace['block_gaps']:
            if not isinstance(gap['id'], str) or len(gap['id']) != 64 or gap['id'] in identities or (not isinstance(gap['producer'], str)):
                raise ValueError('block gap')
            identities.add(gap['id'])
    except (KeyError, TypeError, ValueError) as exc:
        raise DocumentActionBlocked('The document fragment lineage does not match the saved document') from exc

def _dependencies(proof: dict[str, Any]) -> set[str]:
    action, trace = (proof['action'], proof['source_trace'])
    result = {trace['structure_producer'], *trace['checkpoint_dependencies']}
    if action['scope'] == 'document':
        result.update((item['producer'] for item in trace['block_gaps']))
        for block in trace['blocks'].values():
            result.add(block['producer'])
            result.update((item['producer'] for item in [*block['tokens'], *block['gaps']]))
    else:
        for operation in action['operations']:
            block = trace['blocks'][_source_key(proof, operation['block_id'])]
            result.add(block['producer'])
            start, end = (operation['start'], operation['end'])
            result.update((item['producer'] for item in block['tokens'][start:end]))
            gaps = block['gaps'][start:end + 1] if start == end else block['gaps'][start + 1:end]
            result.update((item['producer'] for item in gaps))
        for operation in action['block_operations']:
            start, end = (operation['start'], operation['end'])
            for key in trace['block_order'][start:end]:
                block = trace['blocks'][key]
                result.add(block['producer'])
                result.update((item['producer'] for item in [*block['tokens'], *block['gaps']]))
            gaps = trace['block_gaps'][start:end + 1] if start == end else trace['block_gaps'][start + 1:end]
            result.update((item['producer'] for item in gaps))
    return result - {'', proof['node_id']}

def _source_key(proof: dict[str, Any], key: str) -> str:
    return proof['source_trace']['block_order'][proof['action']['source_order'].index(key)]

def _match_blocks(operation: dict[str, Any], source: dict[str, Any], current: dict[str, Any]) -> tuple[int, int]:
    start, end = (operation['start'], operation['end'])
    if start == end:
        found = [i for i, gap in enumerate(current['block_gaps']) if gap == source['block_gaps'][start]]
        if len(found) != 1:
            raise DocumentActionBlocked('The original paragraph insertion position or gap left by deletion has changed', 'document/blocks')
        return (found[0], found[0])
    wanted = source['block_order'][start:end]
    found = [i for i, key in enumerate(current['block_order']) if key == wanted[0]]
    if len(found) != 1:
        raise DocumentActionBlocked('The original paragraph was deleted or replaced', 'document/blocks')
    position = found[0]
    if current['block_order'][position:position + len(wanted)] != wanted or any((current['blocks'].get(key) != source['blocks'][key] for key in wanted)) or current['block_gaps'][position + 1:position + len(wanted)] != source['block_gaps'][start + 1:end]:
        raise DocumentActionBlocked('The content or source version of the original paragraph range has changed', 'document/blocks')
    return (position, position + len(wanted))

def _rebuild_trace(document: BoardDocument, trace: dict[str, Any]) -> BoardDocument:
    if trace['flat']:
        root = {key: deepcopy(value) for key, value in document.content_json.items() if key != 'content'}
        root['content'] = [{**deepcopy(trace['blocks'][key]['shell']), 'content': tokens_inline([item['value'] for item in trace['blocks'][key]['tokens']])} for key in trace['block_order']]
        return rebuild_document_from_content_json(document, root)
    layout = document_layout(document)
    return rebuild_with_tapes(document, layout, {actual: [item['value'] for item in trace['blocks'][key]['tokens']] for key, actual in zip(trace['block_order'], layout['tapes'])})

def _mapped_position(start: int, end: int, changes: list[dict[str, Any]]) -> tuple[int, int]:
    """A renamed/rehashed lineage cannot move a net change to equal text elsewhere."""
    shift = 0
    for change in changes:
        left, right = (change['start'], change['end'])
        overlaps = max(start, left) < min(end, right) or (left == right and start < left < end) if start < end else left < start < right or left == right == start
        if overlaps:
            raise DocumentActionBlocked('The current snapshot contains changes or ambiguous alignment within the original change range')
        if right <= start:
            shift += len(change['inserted']) - (right - left)
    return (start + shift, end + shift)

def _match_operation(operation: dict[str, Any], original: dict[str, Any], current: dict[str, Any]) -> tuple[int, int]:
    start, end = (operation['start'], operation['end'])
    wanted = original['tokens'][start:end]
    if start == end:
        positions = [index for index, gap in enumerate(current['gaps']) if gap == original['gaps'][start]]
        if len(positions) != 1:
            raise DocumentActionBlocked('The original insertion position or the gap version left by deletion has changed', operation['block_id'])
        return (positions[0], positions[0])
    first = wanted[0]['id']
    positions = [index for index, token in enumerate(current['tokens']) if token['id'] == first]
    if len(positions) != 1:
        raise DocumentActionBlocked('The originally changed fragment was overwritten or deleted', operation['block_id'])
    position = positions[0]
    if current['tokens'][position:position + len(wanted)] != wanted:
        raise DocumentActionBlocked('The content or source version of the originally changed fragment has changed', operation['block_id'])
    if current['gaps'][position + 1:position + len(wanted)] != original['gaps'][start + 1:end]:
        raise DocumentActionBlocked('Insertions or deletions have occurred inside the original change range', operation['block_id'])
    return (position, position + len(wanted))

@verified_replay
def replay_range_proof(proof: dict[str, Any], source_before: BoardDocument, source_after: BoardDocument, current: BoardDocument, current_trace: dict[str, Any], *, validate: bool=True) -> tuple[BoardDocument, dict[str, Any]]:
    try:
        if set(proof) != {'version', 'node_id', 'action', 'source_trace', 'checkpoint'} or proof['version'] != 1 or (not proof['node_id']):
            raise DocumentActionBlocked('The range change proof format is invalid')
        if validate:
            validate_compiled_action(source_before, source_after, proof['action'])
            validate_range_trace(source_before, proof['source_trace'])
            validate_range_trace(current, current_trace)
        action, source, node_id = (proof['action'], proof['source_trace'], proof['node_id'])
        if action['scope'] == 'document':
            if not same_document_content(current, source_before) or current_trace != source:
                raise DocumentActionBlocked('A whole-document change requires its original input state and cannot skip other active changes', 'document')
            result = source_after.model_copy(deep=True)
            return (result, create_range_trace(result, origin=node_id, checkpoint_dependencies=source['checkpoint_dependencies']))
        if current_trace['root_hash'] != source['root_hash'] or current_trace['structure_producer'] != source['structure_producer'] or (not source['flat'] and current_trace['layout_hash'] != source['layout_hash']):
            raise DocumentActionBlocked('The document structure version has changed. The original change range cannot be located', 'document')
        displacement = compile_range_action(source_before, current)
        if displacement['scope'] != 'ranges':
            raise DocumentActionBlocked('The current snapshot cannot establish a unique location for the original change range', 'document')
        current_layout_keys = list(document_layout(current)['tapes'])
        displacement_matches = dict(displacement['block_matches'])
        located: dict[str, list[tuple[int, int, int, dict[str, Any]]]] = {}
        for index, operation in enumerate(action['operations']):
            key = _source_key(proof, operation['block_id'])
            if key not in current_trace['blocks'] or current_trace['blocks'][key]['shell'] != source['blocks'][key]['shell'] or current_trace['blocks'][key]['producer'] != source['blocks'][key]['producer']:
                raise DocumentActionBlocked('The original text block identity or structure version has changed', operation['block_id'])
            start, end = _match_operation(operation, source['blocks'][key], current_trace['blocks'][key])
            expected_key = displacement_matches.get(operation['block_id'])
            if expected_key is None or current_trace['block_order'][current_layout_keys.index(expected_key)] != key:
                raise DocumentActionBlocked('The fragment lineage position does not match the paragraph correspondence in the original snapshot', operation['block_id'])
            expected_position = _mapped_position(operation['start'], operation['end'], [change for change in displacement['operations'] if change['block_id'] == operation['block_id']])
            if (start, end) != expected_position:
                raise DocumentActionBlocked('The fragment lineage position does not match the unique alignment in the original snapshot', operation['block_id'])
            located.setdefault(key, []).append((start, end, index, operation))
        located_blocks = []
        for index, operation in enumerate(action['block_operations']):
            start, end = _match_blocks(operation, source, current_trace)
            if (start, end) != _mapped_position(operation['start'], operation['end'], displacement['block_operations']):
                raise DocumentActionBlocked('The paragraph lineage position does not match the unique alignment in the original snapshot', 'document/blocks')
            located_blocks.append((start, end, index, operation))
        edited = deepcopy(current_trace)
        for key, operations in located.items():
            ordered = sorted(operations)
            if any((left[1] > right[0] or left[0] == right[0] for left, right in zip(ordered, ordered[1:]))):
                raise DocumentActionBlocked('Ranges within the same change overlap. Partial execution is not allowed', key)
            block = edited['blocks'][key]
            for start, end, index, operation in reversed(ordered):
                inserted = [{'id': _identity('text', node_id, index, offset), 'producer': node_id, 'value': deepcopy(value)} for offset, value in enumerate(operation['inserted'])]
                gap_count = len(inserted) + 1
                gaps = [{'id': _identity('gap', node_id, index, offset), 'producer': node_id, 'kind': 'deletion_join' if not inserted else 'write'} for offset in range(gap_count)]
                if start < end and inserted:
                    gaps[0], gaps[-1] = (block['gaps'][start], block['gaps'][end])
                if not inserted:
                    gaps[0].update({'left': block['gaps'][start]['id'], 'right': block['gaps'][end]['id'], 'deleted_token_ids': [item['id'] for item in block['tokens'][start:end]]})
                block['tokens'][start:end] = inserted
                block['gaps'][start:end + 1] = gaps
        for start, end, index, operation in reversed(sorted(located_blocks)):
            inserted_keys = []
            for offset, spec in enumerate(operation['inserted']):
                key = _identity('block', node_id, index, offset)
                inserted_keys.append(key)
                edited['blocks'][key] = {'producer': node_id, 'shell': deepcopy(spec['shell']), 'tokens': [{'id': _identity('block-text', node_id, index, offset, at), 'producer': node_id, 'value': deepcopy(value)} for at, value in enumerate(spec['tokens'])], 'gaps': [{'id': _identity('block-text-gap', node_id, index, offset, at), 'producer': node_id, 'kind': 'write'} for at in range(len(spec['tokens']) + 1)]}
            gaps = [{'id': _identity('block-gap', node_id, index, offset), 'producer': node_id, 'kind': 'deletion_join' if not inserted_keys else 'write'} for offset in range(len(inserted_keys) + 1)]
            if start < end and inserted_keys:
                gaps[0], gaps[-1] = (edited['block_gaps'][start], edited['block_gaps'][end])
            removed_keys = edited['block_order'][start:end]
            if not inserted_keys:
                gaps[0].update({'left': edited['block_gaps'][start]['id'], 'right': edited['block_gaps'][end]['id'], 'deleted_block_ids': list(removed_keys)})
            for key in removed_keys:
                del edited['blocks'][key]
            edited['block_order'][start:end] = inserted_keys
            edited['block_gaps'][start:end + 1] = gaps
        result = source_after.model_copy(deep=True) if same_document_content(current, source_before) and current_trace == source else _rebuild_trace(current, edited)
        edited['document_hash'] = content_hash(result)
        edited['layout_hash'] = document_layout(result)['layout_hash']
        validate_range_trace(result, edited)
        return (result, edited)
    except (KeyError, TypeError, IndexError) as exc:
        raise DocumentActionBlocked('The range change proof is missing reliable input fragments') from exc

def validate_range_occurrence(proof: dict[str, Any], source_before: BoardDocument, source_after: BoardDocument, current_before: BoardDocument, current_after: BoardDocument, input_trace: dict[str, Any], output_trace: dict[str, Any]) -> None:
    result, trace = replay_range_proof(proof, source_before, source_after, current_before, input_trace)
    if result != current_after or trace != output_trace:
        raise DocumentActionBlocked('The branch range change failed full-document and source-lineage replay verification')

def _text(tokens: list[dict[str, Any]]) -> str:
    return ''.join((token.get('char', '￼') for token in tokens))

def _entry(node_id: str, sequence: int, proof: dict[str, Any], after: BoardDocument) -> dict[str, Any]:
    action = proof['action']
    spans, changes = ([], [])
    if action['scope'] == 'document':
        spans = [{'start': 0.0, 'end': 1.0, 'block_id': 'document'}]
    else:
        layout = document_layout(after)
        total = max(1, sum((len(tokens) + 1 for tokens in layout['tapes'].values())))
        offsets, position = ({}, 0)
        for key, tokens in layout['tapes'].items():
            offsets[key] = position
            position += len(tokens) + 1
        deltas: dict[str, int] = {}
        for operation in action['operations']:
            key = operation['block_id']
            target = dict(action['block_matches'])[key]
            start = operation['start'] + deltas.get(key, 0)
            end = start + len(operation['inserted'])
            spans.append({'block_id': target, 'start': (offsets[target] + start) / total, 'end': (offsets[target] + end) / total, 'from': start, 'to': end, 'kind': 'insert' if not operation['removed'] else 'delete' if not operation['inserted'] else 'replace'})
            changes.append({'block_id': key, 'start': operation['start'], 'end': operation['end'], 'deleted': _text(operation['removed']), 'inserted': _text(operation['inserted'])})
            deltas[key] = deltas.get(key, 0) + len(operation['inserted']) - len(operation['removed'])
        block_delta = 0
        target_keys = action['target_order']
        for operation in action['block_operations']:
            index = operation['start'] + block_delta
            start = offsets[target_keys[index]] if index < len(target_keys) else total - 1
            size = sum((len(item['tokens']) + 1 for item in operation['inserted']))
            spans.append({'block_id': 'document/blocks', 'start': start / total, 'end': min(total, start + size) / total, 'kind': 'insert' if not operation['removed'] else 'delete' if not operation['inserted'] else 'replace'})
            changes.append({'block_id': 'document/blocks', 'start': operation['start'], 'end': operation['end'], 'deleted': '\n'.join((_text(item['tokens']) for item in operation['removed'])), 'inserted': '\n'.join((_text(item['tokens']) for item in operation['inserted']))})
            block_delta += len(operation['inserted']) - len(operation['removed'])
    return {'id': node_id, 'node_id': node_id, 'sequence': sequence, 'scope': action['scope'], 'spans': spans, 'locate_spans': deepcopy(spans), 'changes': changes, 'ambiguities': deepcopy(action['ambiguities']), 'checkpoint': proof['checkpoint'], 'scope_reason': action.get('reason')}

def _compile_history(initial: BoardDocument, actions: list[dict[str, Any]], ids: list[str]):
    original, original_trace = (initial.model_copy(deep=True), create_range_trace(initial))
    compiled, entries, known = ([], [], set())
    for sequence, item in enumerate(actions, 1):
        node_id, before, after = (item['node_id'], item['before'], item['after'])
        before = BoardDocument.model_validate(before) if isinstance(before, dict) else before
        after = BoardDocument.model_validate(after) if isinstance(after, dict) else after
        checkpoint = not same_document_content(original, before)
        if checkpoint:
            original_trace = create_range_trace(before, checkpoint_dependencies=sorted(known))
        elif original != before:
            original_trace = deepcopy(original_trace)
            original_trace['document_hash'] = content_hash(before)
        action = compile_range_action(before, after)
        if item.get('action') is not None and item['action'] != action:
            raise DocumentActionBlocked('The existing range change proof does not match the original snapshots')
        proof = {'version': 1, 'node_id': node_id, 'action': action, 'source_trace': deepcopy(original_trace), 'checkpoint': checkpoint}
        input_trace = deepcopy(original_trace)
        original, original_trace = replay_range_proof(proof, before, after, before, original_trace)
        dependencies = sorted(_dependencies(proof) & known, key=ids.index)
        entry = {**_entry(node_id, sequence, proof, after), 'dependencies': dependencies}
        entries.append(entry)
        occurrence = {'node_id': node_id, 'before': before.model_copy(deep=True), 'after': original.model_copy(deep=True), 'proof': proof, 'input_trace': input_trace, 'output_trace': deepcopy(original_trace)}
        compiled.append({'node_id': node_id, 'before': before, 'after': after, 'proof': proof, 'occurrence': occurrence})
        known.add(node_id)
    return {'items': compiled, 'entries': entries}

def build_range_history(initial: BoardDocument, actions: list[dict[str, Any]], excluded_node_ids: list[str] | set[str] | tuple[str, ...]=()) -> dict[str, Any]:
    """Compile verified source lineage once; replay only the changed suffix.

    Cached inputs are complete immutable snapshot hashes, including a supplied
    action proof. A changed or corrupt source therefore cannot reuse old work.
    """
    from openclass_core.services.ridoc_range_compilation_cache import compiled_history
    excluded = set(excluded_node_ids)
    ids = [item['node_id'] for item in actions]
    if len(ids) != len(set(ids)) or any((not isinstance(value, str) or not value for value in ids)) or excluded - set(ids):
        raise DocumentActionBlocked('Range history contains duplicate, unknown, or invalid changes')
    key = (content_hash(initial), tuple(((item['node_id'], content_hash(item['before']), content_hash(item['after']), content_hash(item.get('action'))) for item in actions)))
    prepared = compiled_history(key, lambda: _compile_history(initial, actions, ids))
    compiled, entries = (prepared['items'], prepared['entries'])
    ancestors: dict[str, set[str]] = {}
    ranks: dict[str, int] = {}
    for entry in entries:
        node_id = entry['node_id']
        ancestors[node_id] = set(entry['dependencies'])
        for parent in entry['dependencies']:
            ancestors[node_id].update(ancestors[parent])
        ranks[node_id] = max((ranks[parent] + 1 for parent in entry['dependencies']), default=0)
        blockers = [candidate for candidate in ids if candidate not in excluded and node_id in ancestors.get(candidate, set())]
        entry['rank'] = ranks[node_id]
    for entry in entries:
        node_id = entry['node_id']
        active = node_id not in excluded
        blockers = [candidate for candidate in ids if candidate not in excluded and node_id in ancestors[candidate]]
        missing = [candidate for candidate in ids if candidate in excluded and candidate in ancestors[node_id]]
        entry.update({'status': 'active' if active else 'withdrawn', 'can_undo': active and (not blockers), 'can_redo': not active and (not missing), 'blockers': blockers if active else missing, 'blocking_node_ids': blockers if active else missing, 'blocked_reason': 'First undo the changes that depend on this range: ' + ', '.join(blockers) if active and blockers else 'First restore the required change ranges: ' + ', '.join(missing) if not active and missing else None})
        if active and missing:
            raise DocumentActionBlocked(f'{node_id} still depends on the ranges of undone changes: ' + ', '.join(missing), node_id)
    current, trace = (initial.model_copy(deep=True), create_range_trace(initial))
    occurrences = []
    changed_prefix = False
    for item in compiled:
        if item['node_id'] in excluded:
            changed_prefix = True
            continue
        if not changed_prefix:
            occurrence = item['occurrence']
            current, trace = (occurrence['after'], occurrence['output_trace'])
            occurrences.append(occurrence)
            continue
        proof = item['proof']
        if proof['checkpoint']:
            current, trace = (item['before'].model_copy(deep=True), deepcopy(proof['source_trace']))
        elif same_document_content(current, item['before']) and trace['document_hash'] != content_hash(item['before']):
            current = item['before'].model_copy(deep=True)
            trace = deepcopy(trace)
            trace['document_hash'] = content_hash(current)
        before, input_trace = (current.model_copy(deep=True), deepcopy(trace))
        current, trace = replay_range_proof(proof, item['before'], item['after'], current, trace)
        occurrences.append({'node_id': item['node_id'], 'before': before, 'after': current.model_copy(deep=True), 'proof': proof, 'input_trace': input_trace, 'output_trace': deepcopy(trace)})
    return {'document': current, 'state_trace': trace, 'entries': entries, 'occurrences': occurrences, 'blockers': [], 'excluded_node_ids': [node_id for node_id in ids if node_id in excluded]}
