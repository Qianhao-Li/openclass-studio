"""Verified range applications over immutable C facts and independent branch state.

The objects below are content-addressed replay proofs, not another history. A
proof can only derive from the original C or an already verified C occurrence.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import replace
from typing import Any
from openclass_core.models import BoardDocument
from openclass_core.services.ridoc_document_actions import initial_versions
from openclass_core.services.ridoc_v3 import RIDOC_V35_SPEC_VERSION, RidocArchiveV3, RidocDocumentNode, RidocMessageNode, RidocV3Error, content_hash, validate_ridoc_v3
from openclass_core.services.ridoc_v34_models import DocumentOccurrence
from openclass_core.services.ridoc_v34_states import advance, archive_state, put_state, state, state_result, upgrade_archive

def _copy_back(target: RidocArchiveV3, source: RidocArchiveV3) -> None:
    for field in ('manifest', 'package', 'nodes', 'documents', 'document_states', 'node_refs', 'assets', 'evidence', 'range_proofs', 'range_traces'):
        setattr(target, field, getattr(source, field))

def prepare_range_archive(archive: RidocArchiveV3) -> None:
    """Upgrade only the supplied in-memory copy; readers never call this."""
    validate_ridoc_v3(archive)
    if archive.manifest.get('spec_version') == RIDOC_V35_SPEC_VERSION:
        return
    edited = deepcopy(archive)
    upgrade_archive(edited)
    for lesson_id, graph in edited.package.lessons.items():
        for name, branch in graph.branches.items():
            branch.current_state_ref = archive_state(edited, lesson_id, name)['state_ref']
    edited.manifest['spec_version'] = RIDOC_V35_SPEC_VERSION
    validate_ridoc_v3(edited)
    _copy_back(archive, edited)

def _object(values: dict, digest: str | None, kind: str) -> dict:
    value = values.get(digest)
    if not isinstance(value, dict) or content_hash(value) != digest:
        raise RidocV3Error(f'The range {kind} is missing or its hash does not match')
    return value

def _document(archive: RidocArchiveV3, ref: str) -> BoardDocument:
    return state_result(archive, ref)['document']

def _put_trace(archive: RidocArchiveV3, trace: dict) -> str:
    """Persist identities only; the bound document is the sole text carrier."""
    saved = _packed_trace(trace)
    digest = content_hash(saved)
    archive.range_traces[digest] = saved
    return digest

def _packed_trace(trace: dict) -> dict:
    saved = deepcopy(trace)
    for block in saved['blocks'].values():
        block.pop('shell', None)
        for token in block['tokens']:
            token.pop('value')
    return saved

def _trace(archive: RidocArchiveV3, ref: str, document: BoardDocument) -> dict:
    from openclass_core.services.ridoc_range_actions import document_layout
    from openclass_core.services.ridoc_range_history import validate_range_trace
    saved = deepcopy(_object(archive.range_traces, ref, 'lineage'))
    layout = document_layout(document)
    order = saved.get('block_order', list(saved['blocks']))
    if len(order) != len(layout['tapes']) or set(order) != set(saved['blocks']):
        raise RidocV3Error('The range lineage does not match the document blocks')
    for key, actual_key in zip(order, layout['tapes'], strict=True):
        block = saved['blocks'][key]
        if 'shell' in block:
            raise RidocV3Error('The document structure in range lineage must reference the original snapshot')
        if saved['version'] == 2:
            block['shell'] = layout['shells'][actual_key]
        values = layout['tapes'][actual_key]
        if len(block['tokens']) != len(values):
            raise RidocV3Error('The range lineage does not match the number of document fragments')
        for token, value in zip(block['tokens'], values, strict=True):
            if set(token) != {'id', 'producer'}:
                raise RidocV3Error('Range lineage may store fragment identities only; content must reference the document')
            token['value'] = value
    validate_range_trace(document, saved)
    return saved

def _put_action(archive: RidocArchiveV3, proof: dict) -> dict:
    return {'version': proof['version'], 'node_id': proof['node_id'], 'compiled_action_hash': content_hash(proof['action']), 'source_trace_ref': _put_trace(archive, proof['source_trace']), 'checkpoint': proof['checkpoint']}

def _action(archive: RidocArchiveV3, proof: dict, before: BoardDocument, after: BoardDocument) -> dict:
    from openclass_core.services.ridoc_range_actions import compile_range_action
    if set(proof) != {'version', 'node_id', 'compiled_action_hash', 'source_trace_ref', 'checkpoint'}:
        raise RidocV3Error('Range proofs may only store differences and lineage references bound to source snapshots')
    compiled = compile_range_action(before, after)
    if content_hash(compiled) != proof['compiled_action_hash']:
        raise RidocV3Error('The range difference proof does not match the original snapshots')
    return {'version': proof['version'], 'node_id': proof['node_id'], 'action': compiled, 'source_trace': _trace(archive, proof['source_trace_ref'], before), 'checkpoint': proof['checkpoint']}

def validate_occurrence(archive: RidocArchiveV3, node, occurrence: DocumentOccurrence, _visiting: set[str] | None=None) -> None:
    from openclass_core.services.ridoc_range_history import validate_range_occurrence
    if not isinstance(node, RidocDocumentNode) or node.initial or (not occurrence.before_state_ref):
        raise RidocV3Error('Range proofs require a real C change with complete input')
    for ref in (occurrence.before_state_ref, occurrence.after_state_ref):
        if state(archive, ref).lesson_id != node.lesson_id:
            raise RidocV3Error("Range changes cannot reference another course's state")
    proof = _object(archive.range_proofs, occurrence.range_proof_ref, 'proof')
    if set(proof) != {'lesson_id', 'node_id', 'source_occurrence', 'action'} or proof['lesson_id'] != node.lesson_id or proof['node_id'] != node.id:
        raise RidocV3Error('The range proof must be bound to the original C identity')
    visiting = set(_visiting or ())
    if occurrence.range_proof_ref in visiting:
        raise RidocV3Error('The range proof sources contain a cycle')
    visiting.add(occurrence.range_proof_ref)
    if len(visiting) > 128:
        raise RidocV3Error('The range proof source chain is too deep')
    source = DocumentOccurrence.model_validate(proof['source_occurrence'])
    if not source.before_state_ref:
        raise RidocV3Error("The range proof is missing the original action's source input")
    for ref in (source.before_state_ref, source.after_state_ref):
        if state(archive, ref).lesson_id != node.lesson_id:
            raise RidocV3Error('The range proof source belongs to another course')
    if source.range_proof_ref or source.input_trace_ref or source.output_trace_ref:
        validate_occurrence(archive, node, source, visiting)
    else:
        scratch = replace(archive, documents=dict(archive.documents), document_states=dict(archive.document_states))
        if advance(scratch, node, source.before_state_ref) != source.after_state_ref:
            raise RidocV3Error('The range proof source failed validation against the original C change')
    source_before, source_after = (_document(archive, source.before_state_ref), _document(archive, source.after_state_ref))
    before, after = (_document(archive, occurrence.before_state_ref), _document(archive, occurrence.after_state_ref))
    before_trace = _trace(archive, occurrence.input_trace_ref, before)
    after_trace = _trace(archive, occurrence.output_trace_ref, after)
    validate_range_occurrence(_action(archive, proof['action'], source_before, source_after), source_before, source_after, before, after, before_trace, after_trace)

def range_object_references(archive: RidocArchiveV3) -> tuple[set[str], set[str], set[str]]:
    """Walk only reachable proof ancestry; retain its immutable source states."""
    proofs: set[str] = set()
    traces: set[str] = set()
    states: set[str] = set()
    pending = [occurrence for graph in archive.package.lessons.values() for branch in graph.branches.values() for occurrence in branch.document_occurrences.values()]
    traces.update((branch.current_trace_ref for graph in archive.package.lessons.values() for branch in graph.branches.values() if branch.current_trace_ref))
    while pending:
        occurrence = pending.pop()
        states.update((ref for ref in (occurrence.before_state_ref, occurrence.after_state_ref) if ref))
        traces.update((ref for ref in (occurrence.input_trace_ref, occurrence.output_trace_ref) if ref))
        digest = occurrence.range_proof_ref
        if digest and digest not in proofs:
            proofs.add(digest)
            proof = _object(archive.range_proofs, digest, 'proof')
            traces.add(proof['action']['source_trace_ref'])
            pending.append(DocumentOccurrence.model_validate(proof['source_occurrence']))
    return (proofs, traces, states)

def validate_range_objects(archive: RidocArchiveV3) -> None:
    proofs, traces, _states = range_object_references(archive)
    if set(archive.range_proofs) != proofs or set(archive.range_traces) != traces:
        raise RidocV3Error('RIDOC may only store range proofs and lineage referenced by valid applied changes')
    for digest in proofs:
        _object(archive.range_proofs, digest, 'proof')
    for digest in traces:
        _object(archive.range_traces, digest, 'lineage')
    for graph in archive.package.lessons.values():
        for branch in graph.branches.values():
            validate_branch_trace_chain(archive, graph.id, branch)
            if branch.current_trace_ref:
                _trace(archive, branch.current_trace_ref, _document(archive, branch.current_state_ref))

def validate_branch_trace_chain(archive: RidocArchiveV3, lesson_id: str, branch) -> None:
    """A continuous application must consume the preceding proven identities."""
    from openclass_core.services.ridoc_range_history import create_range_trace
    previous = None
    for identity in branch.learning_node_ids:
        node = archive.nodes[f'{lesson_id}/{identity}']
        if isinstance(node, RidocMessageNode):
            continue
        occurrence = branch.document_occurrences[identity]
        if occurrence.range_proof_ref:
            proof = archive.range_proofs[occurrence.range_proof_ref]
            source = DocumentOccurrence.model_validate(proof['source_occurrence'])
            before = _document(archive, occurrence.before_state_ref)
            verified_source = all((state(archive, actual).document_ref == state(archive, expected).document_ref for actual, expected in ((occurrence.before_state_ref, source.before_state_ref), (occurrence.after_state_ref, source.after_state_ref))))
            source_trace = proof['action']['source_trace_ref']
            if previous and state(archive, previous.after_state_ref).document_ref == state(archive, occurrence.before_state_ref).document_ref:
                expected = previous.output_trace_ref
                if not expected and previous.before_state_ref is None:
                    expected = content_hash(_packed_trace(create_range_trace(before)))
                if expected and expected != occurrence.input_trace_ref:
                    raise RidocV3Error("The range change input lineage does not connect to the previous valid change's output")
                if not expected and (not (verified_source and source_trace == occurrence.input_trace_ref)):
                    raise RidocV3Error('The first range change is missing a trusted input lineage source')
            else:
                saved = state(archive, occurrence.before_state_ref)
                deleted_source = archive.node_refs[f'{lesson_id}/{saved.document_version}'].status == 'deleted'
                if not deleted_source and (not (verified_source and source_trace == occurrence.input_trace_ref)):
                    raise RidocV3Error('A break in range change lineage is missing a verified independently saved source')
        previous = occurrence

def remap_range_proofs(archive: RidocArchiveV3, lesson_map: dict[str, str], state_map: dict[str, str]) -> None:
    mapping: dict[str, str] = {}
    saved: dict[str, dict] = {}

    def remap(digest: str) -> str:
        if digest in mapping:
            return mapping[digest]
        proof = deepcopy(_object(archive.range_proofs, digest, 'proof'))
        proof['lesson_id'] = lesson_map[proof['lesson_id']]
        source = proof['source_occurrence']
        source['before_state_ref'] = state_map[source['before_state_ref']]
        source['after_state_ref'] = state_map[source['after_state_ref']]
        if source.get('range_proof_ref'):
            source['range_proof_ref'] = remap(source['range_proof_ref'])
        identity = content_hash(proof)
        mapping[digest] = identity
        saved[identity] = proof
        return identity
    for graph in archive.package.lessons.values():
        for branch in graph.branches.values():
            for occurrence in branch.document_occurrences.values():
                if occurrence.range_proof_ref:
                    occurrence.range_proof_ref = remap(occurrence.range_proof_ref)
    archive.range_proofs = saved

def install_range_replay(archive: RidocArchiveV3, lesson_id: str, branch_name: str, replay: dict[str, Any]) -> None:
    """Install an entirely verified replay or leave the input copy untouched."""
    edited = deepcopy(archive)
    prepare_range_archive(edited)
    branch = edited.package.lessons[lesson_id].branches[branch_name]
    original = dict(branch.document_occurrences)
    c0_id = next((identity for identity in branch.learning_node_ids if isinstance(edited.nodes[f'{lesson_id}/{identity}'], RidocDocumentNode) and edited.nodes[f'{lesson_id}/{identity}'].initial))
    installed = {c0_id: original[c0_id].model_copy(deep=True)}
    before_ref = installed[c0_id].after_state_ref
    for item in replay['occurrences']:
        identity = item['node_id']
        if identity == c0_id:
            continue
        node = edited.nodes.get(f'{lesson_id}/{identity}')
        if not isinstance(node, RidocDocumentNode) or node.initial or identity not in original or (identity in installed):
            raise RidocV3Error('Range replay must use exactly the complete C changes already in the branch')
        if _document(edited, before_ref) != item['before']:
            saved_before = original[identity].before_state_ref
            if item['proof'].get('checkpoint') is not True or not saved_before or _document(edited, saved_before) != item['before']:
                raise RidocV3Error('The range replay input does not connect to the previous document state or a verified saved state')
            before_ref = saved_before
        previous = state_result(edited, before_ref)
        after_ref = put_state(edited, lesson_id, {'document': item['after'], 'document_version': identity, 'structure_version': identity, 'versions': initial_versions(item['after'], identity)}, resource_refs=[*previous['resource_refs'], *node.resource_refs], evidence_ids=[*previous['evidence_ids'], *node.evidence_ids])
        input_trace_ref, output_trace_ref = (_put_trace(edited, item['input_trace']), _put_trace(edited, item['output_trace']))
        action_proof = _put_action(edited, item['proof'])
        sources = [DocumentOccurrence(before_state_ref=node.input_state_ref, after_state_ref=node.output_state_ref), original[identity]]
        failure: Exception | None = None
        for source in sources:
            proof = {'lesson_id': lesson_id, 'node_id': identity, 'source_occurrence': source.model_dump(mode='json'), 'action': action_proof}
            digest = content_hash(proof)
            edited.range_proofs[digest] = proof
            occurrence = DocumentOccurrence(before_state_ref=before_ref, after_state_ref=after_ref, range_proof_ref=digest, input_trace_ref=input_trace_ref, output_trace_ref=output_trace_ref)
            try:
                validate_occurrence(edited, node, occurrence)
                installed[identity] = occurrence
                break
            except (ValueError, KeyError, TypeError) as exc:
                failure = exc
        if identity not in installed:
            raise RidocV3Error(f'Range replay cannot verify source change {identity}: {failure}')
        before_ref = after_ref
    if _document(edited, before_ref) != replay['document']:
        raise RidocV3Error('The final range replay document does not match the change result')
    last_active = next(reversed(installed))
    preserve = set(replay.get('preserve_withdrawn_node_ids', ()))
    original_changes = list(original)
    if preserve:
        if not preserve <= set(original) or preserve & set(installed):
            raise RidocV3Error('A legacy sequentially undone range must be a complete inactive C change in the current branch')
        first_preserved = min((original_changes.index(identity) for identity in preserve))
        if set(original_changes[first_preserved:]) != preserve:
            raise RidocV3Error('Legacy sequentially undone ranges must retain the complete suffix')
        installed.update({identity: original[identity].model_copy(deep=True) for identity in original_changes if identity in preserve})
    branch.document_occurrences = installed
    branch.learning_node_ids = [identity for identity in branch.learning_node_ids if isinstance(edited.nodes[f'{lesson_id}/{identity}'], RidocMessageNode) or identity in installed]
    branch.current_state_ref = before_ref
    branch.current_trace_ref = _put_trace(edited, replay['state_trace'])
    branch.document_cursor_node_id = last_active if preserve else None
    from openclass_core.services.ridoc_v34_references import prune_objects
    prune_objects(edited)
    validate_ridoc_v3(edited)
    _copy_back(archive, edited)
