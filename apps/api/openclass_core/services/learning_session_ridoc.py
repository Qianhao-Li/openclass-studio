"""Remap portable RIDOC identities without dropping saved state or source facts."""
from openclass_core.services.ridoc_v3 import content_hash

def remap_state_objects(archive, *, lesson_ids=None, document_refs=None, document_resources=None):
    lesson_ids, document_refs = (lesson_ids or {}, document_refs or {})
    resources = {key: set(value.resource_refs) | set((document_resources or {}).get(document_refs.get(value.document_ref, value.document_ref), ())) for key, value in archive.document_states.items()}
    if document_resources:
        edges = {}
        for node in archive.nodes.values():
            if node.kind != 'C':
                continue
            if node.output_state_ref:
                resources[node.output_state_ref].update(node.resource_refs)
            if node.input_state_ref:
                edges.setdefault(node.input_state_ref, []).append((node.output_state_ref, node.resource_refs))
        for graph in archive.package.lessons.values():
            for branch in graph.branches.values():
                for identity, occurrence in branch.document_occurrences.items():
                    if occurrence.before_state_ref:
                        node = archive.nodes[f'{graph.id}/{identity}']
                        edges.setdefault(occurrence.before_state_ref, []).append((occurrence.after_state_ref, node.resource_refs))
        pending = list(resources)
        while pending:
            source = pending.pop()
            for target, node_resources in edges.get(source, []):
                required = resources[source] | set(node_resources)
                if not required <= resources[target]:
                    resources[target].update(required)
                    pending.append(target)
    states, refs = ({}, {})
    for digest, state in archive.document_states.items():
        updated = state.model_copy(update={'lesson_id': lesson_ids.get(state.lesson_id, state.lesson_id), 'document_ref': document_refs.get(state.document_ref, state.document_ref), 'resource_refs': sorted(resources[digest])}, deep=True)
        refs[digest] = content_hash(updated)
        states[refs[digest]] = updated
    archive.document_states = states
    for node in archive.nodes.values():
        if node.input_state_ref:
            node.input_state_ref = refs[node.input_state_ref]
        if node.output_state_ref:
            node.output_state_ref = refs[node.output_state_ref]
    for graph in archive.package.lessons.values():
        for branch in graph.branches.values():
            branch.replay_document_refs = {identity: document_refs.get(ref, ref) for identity, ref in branch.replay_document_refs.items()}
            for occurrence in branch.document_occurrences.values():
                if occurrence.before_state_ref:
                    occurrence.before_state_ref = refs[occurrence.before_state_ref]
                occurrence.after_state_ref = refs[occurrence.after_state_ref]
    archive.node_refs = {f'{lesson_ids.get(ref.lesson_id, ref.lesson_id)}/{ref.node_id}': ref.model_copy(update={'lesson_id': lesson_ids.get(ref.lesson_id, ref.lesson_id)}, deep=True) for ref in archive.node_refs.values()}
