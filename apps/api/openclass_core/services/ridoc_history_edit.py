"""Pure, validated edits of the authoritative RIDOC graph.

The caller owns permissions, revision checks, runtime identities and atomic disk
replacement. These operations never persist a partial edit or generate history.
"""
from __future__ import annotations
from copy import deepcopy
from typing import Any
from openclass_core.models import now_iso
from openclass_core.services.ridoc_learning_sequence import branch_sequence, sequence_document_refs, set_branch_sequence
from openclass_core.services.ridoc_v3 import RIDOC_V3_HISTORY_EDIT_SPEC_VERSION, RidocArchiveV3, RidocDocumentNode, RidocMessageNode, RidocV3Error, merge_parent_roles, validate_ridoc_v3
_TOPOLOGY_METADATA_REFS = {'merge_base_commit_id', 'merge_base_node_id', 'merge_target_head_commit_id', 'merge_source_head_commit_id', 'document_parent_commit_id'}
_ORIGIN_METADATA_REFS = {'history_branch_source_node_id', 'branch_origin_node_id'}

def _message(archive: RidocArchiveV3, lesson_id: str, node_id: str) -> RidocMessageNode:
    if lesson_id not in archive.package.lessons:
        raise RidocV3Error('RIDOC lesson is unavailable')
    node = archive.nodes.get(f'{lesson_id}/{node_id}')
    if node is None:
        raise RidocV3Error('RIDOC history node is unavailable')
    if not isinstance(node, RidocMessageNode):
        raise RidocV3Error('Only a complete RIDOC M exchange can be deleted')
    return node

def _affected_branches(archive: RidocArchiveV3, lesson_id: str, node_id: str) -> list[str]:
    if archive.manifest.get('spec_version') in {'3.4', '3.5'}:
        from openclass_core.services.ridoc_v34 import reference_impact
        names = {item['branch_name'] for item in reference_impact(archive, lesson_id, node_id)}
        names.update((name for name, branch in archive.package.lessons[lesson_id].branches.items() if node_id in branch.learning_node_ids))
        return sorted(names)
    affected: list[str] = []
    graph = archive.package.lessons[lesson_id]
    for name, branch in graph.branches.items():
        if node_id in branch_sequence(archive, lesson_id, name):
            affected.append(name)
            continue
        pending = [branch.head_node_id, branch.base_node_id]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            node = archive.nodes[f'{lesson_id}/{current}']
            if current == node_id or node.branch_origin_node_id == node_id:
                affected.append(name)
                break
            pending.extend(node.parent_ids)
    return affected

def preview_message_deletion(archive: RidocArchiveV3, lesson_id: str, node_id: str) -> dict[str, Any]:
    validate_ridoc_v3(archive)
    target = _message(archive, lesson_id, node_id)
    return {'lesson_id': lesson_id, 'node_id': node_id, 'ridoc_revision': archive.revision.value if archive.revision else '', 'user_message': target.user_message, 'assistant_message': target.assistant_message, 'affected_branches': _affected_branches(archive, lesson_id, node_id), 'retained_document_node_ids': [other_id for other_id in archive.package.lessons[lesson_id].node_order if isinstance((other := archive.nodes[f'{lesson_id}/{other_id}']), RidocDocumentNode) and (not other.initial) and (other.transaction_id == target.transaction_id)], 'blocked_reason': None}

def _prune_unreferenced_objects(archive: RidocArchiveV3) -> None:
    if archive.manifest.get('spec_version') in {'3.4', '3.5'}:
        from openclass_core.services.ridoc_v34_references import prune_objects
        prune_objects(archive)
        return
    documents: set[str] = set()
    evidence: set[str] = set()
    assets: set[str] = set()
    for node in archive.nodes.values():
        if isinstance(node, RidocDocumentNode):
            documents.add(node.after_document_ref)
            if node.before_document_ref:
                documents.add(node.before_document_ref)
        else:
            documents.add(node.document_ref)
        evidence.update(node.evidence_ids)
        assets.update(node.resource_refs)
    documents.update(sequence_document_refs(archive))
    archive.documents = {key: value for key, value in archive.documents.items() if key in documents}
    archive.evidence = {key: value for key, value in archive.evidence.items() if key in evidence}
    archive.assets = {key: value for key, value in archive.assets.items() if key in assets}

def delete_message_node(archive: RidocArchiveV3, lesson_id: str, node_id: str) -> dict[str, Any]:
    preview = preview_message_deletion(archive, lesson_id, node_id)
    target = _message(archive, lesson_id, node_id)
    if archive.manifest.get('spec_version') in {'3.4', '3.5'}:
        from openclass_core.services.ridoc_v34 import remove_nodes
        transaction = target.transaction_id
        remove_nodes(archive, {lesson_id: [node_id]})
        archive.package.lessons[lesson_id].updated_at = now_iso()
        return {'deleted_node_id': node_id, 'deleted_transaction_id': transaction, 'affected_branches': preview['affected_branches'], 'retained_document_node_ids': preview['retained_document_node_ids']}
    parent_id = target.parent_ids[0]
    edited = deepcopy(archive)
    graph = edited.package.lessons[lesson_id]
    if edited.manifest['spec_version'] == '3.0':
        edited.manifest['spec_version'] = RIDOC_V3_HISTORY_EDIT_SPEC_VERSION
    for other_id in graph.node_order:
        if other_id == node_id:
            continue
        node = edited.nodes[f'{lesson_id}/{other_id}']
        node.learning_context_node_ids = [value for value in node.learning_context_node_ids if value != node_id]
        if node.merge_source_branch:
            target_id, source_id = merge_parent_roles(node)
            node.merge_target_node_id = parent_id if target_id == node_id else target_id
            node.merge_source_node_id = parent_id if source_id == node_id else source_id
            node.parent_ids = list(dict.fromkeys((node.merge_target_node_id, node.merge_source_node_id)))
        else:
            node.parent_ids = [parent_id if value == node_id else value for value in node.parent_ids]
        if node.branch_origin_node_id == node_id:
            node.branch_origin_node_id = ''
            node.branch_origin_deleted = True
        if isinstance(node, RidocDocumentNode) and other_id in preview['retained_document_node_ids']:
            node.source_message_deleted = True
        for key in _TOPOLOGY_METADATA_REFS:
            if node.decision_metadata.get(key) == node_id:
                node.decision_metadata[key] = parent_id
        for key in _ORIGIN_METADATA_REFS:
            if node.decision_metadata.get(key) == node_id:
                node.decision_metadata.pop(key)
                node.branch_origin_deleted = True
    for branch in graph.branches.values():
        if branch.head_node_id == node_id:
            branch.head_node_id = parent_id
        if branch.base_node_id == node_id:
            branch.base_node_id = parent_id
        branch.learning_node_ids = [value for value in branch.learning_node_ids if value != node_id]
        branch.replay_document_refs.pop(node_id, None)
    for record in graph.merge_records:
        for field in ('source_head_id', 'target_head_id', 'base_node_id'):
            if record.get(field) == node_id:
                record[field] = parent_id
    graph.node_order.remove(node_id)
    del edited.nodes[f'{lesson_id}/{node_id}']
    for name, branch in graph.branches.items():
        if branch.learning_node_ids:
            set_branch_sequence(edited, lesson_id, name, branch.learning_node_ids)
    graph.updated_at = now_iso()
    _prune_unreferenced_objects(edited)
    validate_ridoc_v3(edited)
    archive.manifest = edited.manifest
    archive.package = edited.package
    archive.nodes = edited.nodes
    archive.documents = edited.documents
    archive.evidence = edited.evidence
    archive.assets = edited.assets
    return {'deleted_node_id': node_id, 'deleted_transaction_id': target.transaction_id, 'affected_branches': preview['affected_branches'], 'retained_document_node_ids': preview['retained_document_node_ids']}
