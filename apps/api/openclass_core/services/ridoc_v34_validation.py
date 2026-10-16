"""Validate independent saved facts without inventing deleted producers."""
from __future__ import annotations
from dataclasses import replace
from openclass_core.models import BoardDocument
from openclass_core.services.ridoc_document_actions import initial_versions, objects
from openclass_core.services.ridoc_v3 import RIDOC_V35_SPEC_VERSION, RidocArchiveV3, RidocDocumentNode, RidocMessageNode, RidocV3Error, _reject_runtime_metadata, content_hash
from openclass_core.services.ridoc_v34_references import node_reference_ids, state_producers
from openclass_core.services.ridoc_v34_states import advance, resolve_ref, saved_replay, state

def validate_archive(archive: RidocArchiveV3) -> None:
    range_format = archive.manifest.get('spec_version') == RIDOC_V35_SPEC_VERSION
    if not range_format and (archive.range_proofs or archive.range_traces or any((branch.current_state_ref or branch.current_trace_ref or any((occurrence.range_proof_ref or occurrence.input_trace_ref or occurrence.output_trace_ref for occurrence in branch.document_occurrences.values())) for graph in archive.package.lessons.values() for branch in graph.branches.values()))):
        raise RidocV3Error('Range replay proofs and independent current branch states require RIDOC 3.5')
    if archive.manifest.get('replay_mode') != 'independent_document_states':
        raise RidocV3Error('RIDOC 3.4 must explicitly declare independent state replay')
    if len(archive.package.lesson_order) != len(set(archive.package.lesson_order)) or set(archive.package.lesson_order) != set(archive.package.lessons):
        raise RidocV3Error('RIDOC lesson order must cover every lesson exactly once')
    for key, ref in archive.node_refs.items():
        type(ref).model_validate(ref.model_dump(mode='json'))
        if key != f'{ref.lesson_id}/{ref.node_id}' or ref.lesson_id not in archive.package.lessons:
            raise RidocV3Error('The NodeRef identity or course ownership is invalid')
        node = archive.nodes.get(key)
        if ref.status == 'live' and (node is None or node.kind != ref.kind):
            raise RidocV3Error('A live NodeRef is missing its corresponding node')
        if ref.status == 'deleted' and node is not None:
            raise RidocV3Error('A deleted NodeRef cannot retain a node object')
    needed_states: set[str] = set()
    needed_resources: set[str] = set()
    needed_evidence: set[str] = set()
    for digest, value in archive.document_states.items():
        type(value).model_validate(value.model_dump(mode='json'))
        if content_hash(value) != digest or value.lesson_id not in archive.package.lessons:
            raise RidocV3Error('The DocumentState hash or course ownership is invalid')
        payload = archive.documents.get(value.document_ref)
        if payload is None or content_hash(payload) != value.document_ref:
            raise RidocV3Error('DocumentState is missing a valid document snapshot')
        document = BoardDocument.model_validate(payload)
        found = objects(document)
        if set(value.versions) != (set(found[0]) if found is not None else set()):
            raise RidocV3Error('The DocumentState object version does not match the actual object')
        for identity in state_producers(value):
            resolve_ref(archive, value.lesson_id, identity)
        needed_resources.update(value.resource_refs)
        needed_evidence.update(value.evidence_ids)
    scratch = replace(archive, document_states=dict(archive.document_states), documents=dict(archive.documents))
    for lesson_id, graph in archive.package.lessons.items():
        if graph.id != lesson_id or not graph.node_order or len(graph.node_order) != len(set(graph.node_order)):
            raise RidocV3Error('The RIDOC lesson or node order is invalid')
        keys = {f'{lesson_id}/{identity}' for identity in graph.node_order}
        if keys != {key for key, node in archive.nodes.items() if node.lesson_id == lesson_id}:
            raise RidocV3Error('The RIDOC node order does not cover every live node')
        initial = [identity for identity in graph.node_order if isinstance(archive.nodes[f'{lesson_id}/{identity}'], RidocDocumentNode) and archive.nodes[f'{lesson_id}/{identity}'].initial]
        if len(initial) != 1:
            raise RidocV3Error('A RIDOC course must retain exactly one C0')
        positions = {identity: index for index, identity in enumerate(graph.node_order)}
        for identity in graph.node_order:
            node = archive.nodes[f'{lesson_id}/{identity}']
            if node.id != identity or node.lesson_id != lesson_id:
                raise RidocV3Error('The RIDOC node identity is invalid')
            type(node).model_validate(node.model_dump(mode='json'))
            if resolve_ref(archive, lesson_id, identity).status != 'live':
                raise RidocV3Error('A live node is missing its live NodeRef')
            _reject_runtime_metadata(node.decision_metadata)
            if len(node.parent_ids) != len(set(node.parent_ids)):
                raise RidocV3Error('Node parent references must be unique')
            for _role, target in node_reference_ids(node):
                resolve_ref(archive, lesson_id, target)
            for target in node.learning_context_node_ids:
                ref = resolve_ref(archive, lesson_id, target)
                if ref.status == 'live' and positions[target] >= positions[identity]:
                    raise RidocV3Error('Learning context can only reference existing nodes')
            if isinstance(node, RidocMessageNode) and len(node.parent_ids) != 1:
                raise RidocV3Error('M must retain exactly one original parent reference')
            output = state(archive, node.output_state_ref)
            needed_states.add(node.output_state_ref)
            if output.lesson_id != lesson_id:
                raise RidocV3Error("The node references another course's DocumentState")
            expected_document = node.after_document_ref if isinstance(node, RidocDocumentNode) else node.document_ref
            if output.document_ref != expected_document:
                raise RidocV3Error("The node's original output does not match the saved state")
            if identity == initial[0]:
                document = BoardDocument.model_validate(archive.documents[output.document_ref])
                if node.input_state_ref or output.document_version != identity or output.structure_version != identity or (output.versions != initial_versions(document, identity)):
                    raise RidocV3Error('The C0 initial state is invalid')
            else:
                before = state(archive, node.input_state_ref)
                needed_states.add(node.input_state_ref)
                if before.lesson_id != lesson_id:
                    raise RidocV3Error('The node input belongs to another course')
                if isinstance(node, RidocMessageNode):
                    if node.input_state_ref != node.output_state_ref or before.document_ref != node.document_ref:
                        raise RidocV3Error('The original M document snapshot cannot change the board')
                else:
                    if isinstance(node, RidocDocumentNode) and before.document_ref != node.before_document_ref:
                        raise RidocV3Error('The original C input does not match the saved state')
                    if isinstance(node, RidocDocumentNode) and node.document_action:
                        if node.document_action.get('input_document_version') != before.document_version:
                            raise RidocV3Error('The original C input document version does not match the saved state')
                        if 'input_structure_version' in node.document_action and node.document_action['input_structure_version'] != before.structure_version:
                            raise RidocV3Error('The original C structure version does not match the saved state')
                    if advance(scratch, node, node.input_state_ref) != node.output_state_ref:
                        raise RidocV3Error("The node's original change does not match the independent input and output states")
            needed_resources.update(node.resource_refs)
            needed_evidence.update(node.evidence_ids)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(identity: str) -> None:
            if identity in visiting:
                raise RidocV3Error('The RIDOC live graph contains a cycle')
            if identity in visited:
                return
            visiting.add(identity)
            for target in archive.nodes[f'{lesson_id}/{identity}'].parent_ids:
                if resolve_ref(archive, lesson_id, target).status == 'live':
                    visit(target)
            visiting.remove(identity)
            visited.add(identity)
        for identity in graph.node_order:
            visit(identity)
        if graph.current_branch not in graph.branches:
            raise RidocV3Error('The current branch does not exist')
        for name, branch in graph.branches.items():
            if name != branch.name:
                raise RidocV3Error('The branch identity is invalid')
            if range_format:
                if not branch.current_state_ref or state(archive, branch.current_state_ref).lesson_id != lesson_id:
                    raise RidocV3Error('The RIDOC 3.5 branch is missing an independent current state from the same course')
                needed_states.add(branch.current_state_ref)
            resolve_ref(archive, lesson_id, branch.head_node_id)
            resolve_ref(archive, lesson_id, branch.base_node_id)
            ids = branch.learning_node_ids
            if not ids or ids[0] != initial[0] or len(ids) != len(set(ids)) or (not set(ids) <= set(graph.node_order)):
                raise RidocV3Error('The branch must contain C0 and a unique live learning sequence')
            changes = [identity for identity in ids if not isinstance(archive.nodes[f'{lesson_id}/{identity}'], RidocMessageNode)]
            if set(branch.document_occurrences) != set(changes) or branch.replay_document_refs:
                raise RidocV3Error('Branch document states must cover exactly the C changes and cannot include legacy replay caches')
            for identity, occurrence in branch.document_occurrences.items():
                type(occurrence).model_validate(occurrence.model_dump(mode='json'))
                node = archive.nodes[f'{lesson_id}/{identity}']
                needed_states.add(occurrence.after_state_ref)
                if identity == initial[0]:
                    if occurrence.before_state_ref is not None or occurrence.after_state_ref != node.output_state_ref:
                        raise RidocV3Error('The branch C0 state does not match')
                else:
                    if not occurrence.before_state_ref:
                        raise RidocV3Error('The applied C state is missing its original input')
                    needed_states.add(occurrence.before_state_ref)
                    if state(archive, occurrence.before_state_ref).lesson_id != lesson_id or state(archive, occurrence.after_state_ref).lesson_id != lesson_id:
                        raise RidocV3Error('The applied branch state belongs to another course')
                    if occurrence.range_proof_ref or occurrence.input_trace_ref or occurrence.output_trace_ref:
                        if not range_format:
                            raise RidocV3Error('Range application proofs require RIDOC 3.5')
                        from openclass_core.services.ridoc_v35 import validate_occurrence
                        validate_occurrence(archive, node, occurrence)
                    elif advance(scratch, node, occurrence.before_state_ref) != occurrence.after_state_ref:
                        raise RidocV3Error('The branch change failed input, hash, or version verification')
                if identity == initial[0] and (occurrence.range_proof_ref or occurrence.input_trace_ref or occurrence.output_trace_ref):
                    raise RidocV3Error('C0 cannot be represented as a range change')
            if branch.document_cursor_node_id is not None and branch.document_cursor_node_id not in changes:
                raise RidocV3Error('The board cursor may only reference a live C change in the current branch')
            saved_replay(archive, lesson_id, ids, branch_name=name)
        transactions: set[str] = set()
        for record in graph.merge_records:
            identity = record.get('transaction_id')
            if not isinstance(identity, str) or not identity or identity in transactions:
                raise RidocV3Error('Each merge must have a unique transaction identity')
            transactions.add(identity)
            for field in ('source_head_id', 'target_head_id', 'base_node_id'):
                resolve_ref(archive, lesson_id, record.get(field, ''))
    if range_format:
        from openclass_core.services.ridoc_v35 import range_object_references
        needed_states.update(range_object_references(archive)[2])
    if set(archive.document_states) != needed_states:
        raise RidocV3Error('RIDOC may only store live facts and the DocumentState objects needed for replay')
    if set(archive.documents) != {value.document_ref for value in archive.document_states.values()}:
        raise RidocV3Error('RIDOC 3.4 document objects must exactly cover the independent state references')
    if set(archive.assets) != needed_resources or set(archive.evidence) != needed_evidence:
        raise RidocV3Error('RIDOC assets do not match the node and independent state references')
    for identity in needed_resources:
        if content_hash(archive.assets[identity]) != identity:
            raise RidocV3Error('The RIDOC asset hash does not match')
    if range_format:
        from openclass_core.services.ridoc_v35 import validate_range_objects
        validate_range_objects(archive)
