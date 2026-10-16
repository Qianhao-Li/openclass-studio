"""Explicit reference impact and permanent entity removal without retargeting."""
from __future__ import annotations
from copy import deepcopy
from typing import Any, Iterator
from openclass_core.services.ridoc_v3 import RIDOC_V35_SPEC_VERSION, RidocArchiveV3, RidocDocumentNode, RidocMessageNode, RidocV3Error
from openclass_core.services.ridoc_v34_states import upgrade_archive
_METADATA_REFS = {'merge_base_commit_id', 'merge_base_node_id', 'merge_target_head_commit_id', 'merge_source_head_commit_id', 'document_parent_commit_id', 'history_branch_source_node_id', 'branch_origin_node_id'}

def node_reference_ids(node) -> Iterator[tuple[str, str]]:
    for role, values in (('parent', node.parent_ids), ('learning_context', node.learning_context_node_ids)):
        for value in values:
            yield (role, value)
    for field in ('branch_origin_node_id', 'merge_target_node_id', 'merge_source_node_id'):
        if (value := getattr(node, field)):
            yield (field, value)
    for field in _METADATA_REFS:
        if isinstance((value := node.decision_metadata.get(field)), str) and value:
            yield (field, value)
    if isinstance(node, RidocDocumentNode):
        action = node.document_action
        for field in ('input_document_version', 'input_structure_version'):
            if isinstance((value := action.get(field)), str) and value:
                yield (field, value)
        for identity, value in action.get('inputs', {}).items():
            if isinstance(value, dict) and isinstance((version := value.get('version')), str) and version:
                yield (f'block_version:{identity}', version)

def state_producers(value) -> set[str]:
    return {value.document_version, value.structure_version, *value.versions.values()}

def reference_impact(archive: RidocArchiveV3, lesson_id: str, node_id: str) -> list[dict[str, Any]]:
    from openclass_core.services.ridoc_v34_states import is_v34
    if not is_v34(archive):
        archive = deepcopy(archive)
        upgrade_archive(archive)
    if f'{lesson_id}/{node_id}' not in archive.nodes:
        raise RidocV3Error('RIDOC history node is unavailable')
    result: list[dict[str, Any]] = []
    for node in archive.nodes.values():
        if node.lesson_id != lesson_id or node.id == node_id:
            continue
        role_set = {role for role, identity in node_reference_ids(node) if identity == node_id}
        if any((node_id in state_producers(archive.document_states[ref]) for ref in (node.input_state_ref, node.output_state_ref) if ref)):
            role_set.add('original_state')
        roles = sorted(role_set)
        if roles:
            memberships = [name for name, branch in archive.package.lessons[lesson_id].branches.items() if node.id in branch.learning_node_ids]
            for name in memberships or [node.branch_name]:
                result.append({'lesson_id': lesson_id, 'referrer_kind': node.kind, 'referrer_id': node.id, 'branch_name': name, 'roles': roles, 'title': node.user_message if isinstance(node, RidocMessageNode) else getattr(node, 'label', '')})
    graph = archive.package.lessons[lesson_id]
    for name, branch in graph.branches.items():
        roles = [role for role, target in (('head', branch.head_node_id), ('base', branch.base_node_id)) if target == node_id]
        if roles:
            result.append({'lesson_id': lesson_id, 'referrer_kind': 'branch', 'referrer_id': name, 'branch_name': name, 'roles': roles, 'title': name})
        for identity, occurrence in branch.document_occurrences.items():
            if identity == node_id:
                continue
            referenced = [archive.document_states.get(ref) for ref in (occurrence.before_state_ref, occurrence.after_state_ref) if ref]
            if any((value and node_id in state_producers(value) for value in referenced)):
                node = archive.nodes[f'{lesson_id}/{identity}']
                result.append({'lesson_id': lesson_id, 'referrer_kind': node.kind, 'referrer_id': identity, 'branch_name': name, 'roles': ['retained_state'], 'title': getattr(node, 'label', identity)})
    for record in graph.merge_records:
        roles = [field for field in ('source_head_id', 'target_head_id', 'base_node_id') if record.get(field) == node_id]
        if roles:
            result.append({'lesson_id': lesson_id, 'referrer_kind': 'merge', 'referrer_id': record['transaction_id'], 'branch_name': record.get('target_branch', ''), 'roles': roles, 'title': record.get('label', '')})
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in result:
        key = (item['referrer_kind'], item['referrer_id'], item['branch_name'])
        if key in unique:
            unique[key]['roles'] = sorted(set(unique[key]['roles']) | set(item['roles']))
        else:
            unique[key] = item
    return list(unique.values())

def prune_objects(archive: RidocArchiveV3) -> None:
    archive.node_refs = {key: ref for key, ref in archive.node_refs.items() if ref.lesson_id in archive.package.lessons}
    state_refs = {value for node in archive.nodes.values() for value in (node.input_state_ref, node.output_state_ref) if value}
    state_refs.update((value for graph in archive.package.lessons.values() for branch in graph.branches.values() for occurrence in branch.document_occurrences.values() for value in (occurrence.before_state_ref, occurrence.after_state_ref) if value))
    state_refs.update((branch.current_state_ref for graph in archive.package.lessons.values() for branch in graph.branches.values() if branch.current_state_ref))
    from openclass_core.services.ridoc_v35 import range_object_references
    proof_refs, trace_refs, source_states = range_object_references(archive)
    state_refs.update(source_states)
    archive.document_states = {key: value for key, value in archive.document_states.items() if key in state_refs}
    documents = {value.document_ref for value in archive.document_states.values()}
    documents.update((node.after_document_ref if isinstance(node, RidocDocumentNode) else node.document_ref for node in archive.nodes.values()))
    documents.update((node.before_document_ref for node in archive.nodes.values() if isinstance(node, RidocDocumentNode) and node.before_document_ref))
    resources = {identity for node in archive.nodes.values() for identity in node.resource_refs}
    evidence = {identity for node in archive.nodes.values() for identity in node.evidence_ids}
    resources.update((identity for value in archive.document_states.values() for identity in value.resource_refs))
    evidence.update((identity for value in archive.document_states.values() for identity in value.evidence_ids))
    archive.documents = {key: value for key, value in archive.documents.items() if key in documents}
    archive.assets = {key: value for key, value in archive.assets.items() if key in resources}
    archive.evidence = {key: value for key, value in archive.evidence.items() if key in evidence}
    archive.range_proofs = {key: value for key, value in archive.range_proofs.items() if key in proof_refs}
    archive.range_traces = {key: value for key, value in archive.range_traces.items() if key in trace_refs}

def remap_lessons(archive: RidocArchiveV3, lesson_map: dict[str, str]) -> None:
    """Remap a public copy's state scope without copying or changing node facts."""
    from openclass_core.services.ridoc_v3 import content_hash
    mapping: dict[str, str] = {}
    states = {}
    for digest, value in archive.document_states.items():
        if value.lesson_id not in lesson_map:
            continue
        updated = value.model_copy(update={'lesson_id': lesson_map[value.lesson_id]})
        identity = content_hash(updated)
        mapping[digest] = identity
        states[identity] = updated
    archive.document_states = states
    archive.node_refs = {f'{lesson_map[ref.lesson_id]}/{ref.node_id}': ref.model_copy(update={'lesson_id': lesson_map[ref.lesson_id]}) for ref in archive.node_refs.values() if ref.lesson_id in lesson_map}
    for node in archive.nodes.values():
        if node.input_state_ref:
            node.input_state_ref = mapping[node.input_state_ref]
        node.output_state_ref = mapping[node.output_state_ref]
    for graph in archive.package.lessons.values():
        for branch in graph.branches.values():
            if branch.current_state_ref:
                branch.current_state_ref = mapping[branch.current_state_ref]
            for occurrence in branch.document_occurrences.values():
                if occurrence.before_state_ref:
                    occurrence.before_state_ref = mapping[occurrence.before_state_ref]
                occurrence.after_state_ref = mapping[occurrence.after_state_ref]
    from openclass_core.services.ridoc_v35 import remap_range_proofs
    remap_range_proofs(archive, lesson_map, mapping)
    prune_objects(archive)

def remove_nodes(archive: RidocArchiveV3, pending: dict[str, list[str]]) -> list[dict[str, str]]:
    from openclass_core.services.ridoc_v3 import validate_ridoc_v3
    edited = deepcopy(archive)
    upgrade_archive(edited)
    deleted: list[dict[str, str]] = []
    for lesson_id, values in pending.items():
        graph = edited.package.lessons.get(lesson_id)
        if graph is None:
            raise RidocV3Error('RIDOC lesson is unavailable')
        ids = set(values)
        for identity in ids:
            node = edited.nodes.get(f'{lesson_id}/{identity}')
            if node is None or not isinstance(node, (RidocMessageNode, RidocDocumentNode)):
                raise RidocV3Error('Only live M or C nodes can be deleted')
            if isinstance(node, RidocDocumentNode) and node.initial:
                raise RidocV3Error('C0 cannot be deleted')
        for identity in graph.node_order:
            if identity not in ids:
                continue
            node = edited.nodes[f'{lesson_id}/{identity}']
            deleted.append({'kind': node.kind, 'lesson_id': lesson_id, 'node_id': identity, 'transaction_id': node.transaction_id, 'action_id': getattr(node, 'action_id', '')})
            edited.node_refs[f'{lesson_id}/{identity}'] = edited.node_refs[f'{lesson_id}/{identity}'].model_copy(update={'status': 'deleted'})
        removed_messages = {item['transaction_id'] for item in deleted if item['lesson_id'] == lesson_id and item['kind'] == 'M'}
        for node in edited.nodes.values():
            if node.lesson_id != lesson_id or node.id in ids:
                continue
            if isinstance(node, RidocDocumentNode) and node.transaction_id in removed_messages:
                node.source_message_deleted = True
            if node.branch_origin_node_id in ids:
                node.branch_origin_deleted = True
        for branch in graph.branches.values():
            if edited.manifest.get('spec_version') == RIDOC_V35_SPEC_VERSION:
                from openclass_core.services.ridoc_v34_states import archive_state
                branch.current_state_ref = archive_state(edited, lesson_id, branch.name)['state_ref']
            original = list(branch.learning_node_ids)
            if branch.document_cursor_node_id in ids:
                boundary = original.index(branch.document_cursor_node_id)
                branch.document_cursor_node_id = next((identity for identity in reversed(original[:boundary]) if identity not in ids and isinstance(edited.nodes[f'{lesson_id}/{identity}'], RidocDocumentNode)), None)
            branch.learning_node_ids = [identity for identity in original if identity not in ids]
            branch.document_occurrences = {identity: value for identity, value in branch.document_occurrences.items() if identity not in ids}
            remaining_c = [identity for identity in branch.learning_node_ids if isinstance(edited.nodes[f'{lesson_id}/{identity}'], RidocDocumentNode)]
            if branch.document_cursor_node_id == remaining_c[-1]:
                branch.document_cursor_node_id = None
        graph.node_order = [identity for identity in graph.node_order if identity not in ids]
        for identity in ids:
            del edited.nodes[f'{lesson_id}/{identity}']
    prune_objects(edited)
    validate_ridoc_v3(edited)
    archive.manifest, archive.package, archive.nodes = (edited.manifest, edited.package, edited.nodes)
    archive.documents, archive.document_states, archive.node_refs = (edited.documents, edited.document_states, edited.node_refs)
    archive.assets, archive.evidence = (edited.assets, edited.evidence)
    archive.range_proofs, archive.range_traces = (edited.range_proofs, edited.range_traces)
    return deleted
