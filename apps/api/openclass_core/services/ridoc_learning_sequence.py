"""RIDOC branch learning order and strict C-by-C replay.

Ordering metadata references the original facts; it never rewrites their causal
parents, copies an exchange, creates a merge event, or resolves a stale action.
"""
from __future__ import annotations
from typing import Any
from openclass_core.models import BoardDocument
from openclass_core.services.ridoc_document_actions import DocumentActionBlocked, apply_document_action, initial_versions
from openclass_core.services.ridoc_v3 import RIDOC_V3_LEARNING_SEQUENCE_SPEC_VERSION, RIDOC_V3_SEQUENCE_SPEC_VERSIONS, RidocArchiveV3, RidocDocumentNode, RidocMergeEventNode, RidocMessageNode, RidocV3Error, content_hash

def _node(archive: RidocArchiveV3, lesson_id: str, node_id: str):
    node = archive.nodes.get(f'{lesson_id}/{node_id}')
    if node is None:
        raise RidocV3Error('A node referenced by the learning sequence does not exist')
    return node

def causal_ids(archive: RidocArchiveV3, lesson_id: str, heads: list[str]) -> set[str]:
    pending, found = (list(heads), set())
    while pending:
        identity = pending.pop()
        if identity in found:
            continue
        if archive.manifest.get('spec_version') in {'3.4', '3.5'}:
            from openclass_core.services.ridoc_v34_states import resolve_ref
            if resolve_ref(archive, lesson_id, identity).status == 'deleted':
                continue
        node = _node(archive, lesson_id, identity)
        found.add(identity)
        pending.extend(node.parent_ids)
        pending.extend(node.learning_context_node_ids)
    return found

def branch_sequence(archive: RidocArchiveV3, lesson_id: str, branch_name: str | None=None) -> list[str]:
    graph = archive.package.lessons[lesson_id]
    branch = graph.branches[branch_name or graph.current_branch]
    if branch.learning_node_ids:
        return list(branch.learning_node_ids)
    reachable = causal_ids(archive, lesson_id, [branch.head_node_id])
    return [identity for identity in graph.node_order if identity in reachable]

def sequence_at_node(archive: RidocArchiveV3, lesson_id: str, node_id: str) -> list[str]:
    graph = archive.package.lessons[lesson_id]
    current = branch_sequence(archive, lesson_id)
    if node_id in current:
        return current[:current.index(node_id) + 1]
    reachable = causal_ids(archive, lesson_id, [node_id])
    return [identity for identity in graph.node_order if identity in reachable]

def _legacy_input_version(archive: RidocArchiveV3, lesson_id: str, node) -> str:
    if node.learning_context_node_ids:
        for identity in reversed(node.learning_context_node_ids):
            if isinstance(_node(archive, lesson_id, identity), RidocDocumentNode):
                return identity
    cursor = node.parent_ids[0] if node.parent_ids else ''
    seen: set[str] = set()
    while cursor and cursor not in seen:
        seen.add(cursor)
        parent = _node(archive, lesson_id, cursor)
        if isinstance(parent, RidocDocumentNode):
            return parent.id
        cursor = parent.parent_ids[0] if parent.parent_ids else ''
    return ''

def replay_sequence(archive: RidocArchiveV3, lesson_id: str, node_ids: list[str], *, _memo: dict[tuple[str, ...], dict[str, Any]] | None=None) -> dict[str, Any]:
    if archive.manifest.get('spec_version') in {'3.4', '3.5'}:
        from openclass_core.services.ridoc_v34_states import saved_replay
        return saved_replay(archive, lesson_id, node_ids)
    documents: dict[str, dict[str, Any]] = {}
    refs: dict[str, str] = {}
    blockers: list[dict[str, str]] = []
    current: BoardDocument | None = None
    versions: dict[str, str] = {}
    document_version = structure_version = ''
    digest = ''
    document_prefix = [lesson_id]
    if len(node_ids) != len(set(node_ids)):
        raise RidocV3Error('The learning sequence cannot contain duplicate nodes')
    for identity in node_ids:
        node = _node(archive, lesson_id, identity)
        try:
            if isinstance(node, RidocMergeEventNode):
                raise DocumentActionBlocked('The legacy E merge has no verifiable action input version. Migrate it explicitly before merging')
            if isinstance(node, RidocDocumentNode):
                if node.initial:
                    if current is not None:
                        raise DocumentActionBlocked('C0 must be the first node in the learning sequence')
                elif current is None:
                    raise DocumentActionBlocked('The learning sequence is missing its initial document')
                document_prefix.append(node.id)
                key = tuple(document_prefix)
                cached = _memo.get(key) if _memo is not None else None
                if cached is not None:
                    current, versions = (cached['document'], cached['versions'])
                    document_version, structure_version = (cached['document_version'], cached['structure_version'])
                    digest, payload = (cached['digest'], cached['payload'])
                else:
                    after = BoardDocument.model_validate(archive.documents[node.after_document_ref])
                    if node.initial:
                        current = after
                        versions = initial_versions(current, node.id)
                        structure_version = document_version = node.id
                    else:
                        before = BoardDocument.model_validate(archive.documents[node.before_document_ref])
                        current, versions, structure_version = apply_document_action(current, before, after, node.document_action, node_id=node.id, versions=versions, document_version=document_version, structure_version=structure_version, legacy_input_version=_legacy_input_version(archive, lesson_id, node))
                        document_version = node.id
                    payload = current.model_dump(mode='json')
                    digest = content_hash(payload)
                    if _memo is not None:
                        _memo[key] = {'document': current, 'versions': versions, 'document_version': document_version, 'structure_version': structure_version, 'digest': digest, 'payload': payload}
                documents[digest] = payload
            if current is None:
                raise DocumentActionBlocked('The learning sequence must include C0 first')
            refs[identity] = digest
        except DocumentActionBlocked as exc:
            blockers.append({'node_id': identity, 'reason': str(exc), 'object_id': exc.object_id})
            break
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise RidocV3Error(f'The document change format for node {identity} is invalid') from exc
    return {'document': current, 'document_ref': digest, 'document_refs': refs, 'documents': documents, 'blockers': blockers, 'versions': versions, 'document_version': document_version, 'structure_version': structure_version}

def require_replay(result: dict[str, Any]) -> dict[str, Any]:
    if result['blockers']:
        blocker = result['blockers'][0]
        raise RidocV3Error(f"Node {blocker['node_id']} cannot be merged: {blocker['reason']}")
    if result['document'] is None:
        raise RidocV3Error('The learning sequence is missing a document')
    return result

def branch_replay(archive: RidocArchiveV3, lesson_id: str, branch_name: str | None=None) -> dict[str, Any]:
    if archive.manifest.get('spec_version') in {'3.4', '3.5'}:
        from openclass_core.services.ridoc_v34_states import saved_replay
        return saved_replay(archive, lesson_id, branch_sequence(archive, lesson_id, branch_name), branch_name=branch_name)
    return require_replay(replay_sequence(archive, lesson_id, branch_sequence(archive, lesson_id, branch_name)))

def set_branch_sequence(archive: RidocArchiveV3, lesson_id: str, branch_name: str, node_ids: list[str]) -> None:
    if archive.manifest.get('spec_version') in {'3.4', '3.5'}:
        from openclass_core.services.ridoc_v34_states import set_sequence
        set_sequence(archive, lesson_id, branch_name, node_ids)
        return
    replay = require_replay(replay_sequence(archive, lesson_id, node_ids))
    branch = archive.package.lessons[lesson_id].branches[branch_name]
    branch.learning_node_ids = list(node_ids)
    branch.replay_document_refs = replay['document_refs']
    archive.documents.update(replay['documents'])
    if archive.manifest['spec_version'] not in RIDOC_V3_SEQUENCE_SPEC_VERSIONS:
        archive.manifest['spec_version'] = RIDOC_V3_LEARNING_SEQUENCE_SPEC_VERSION

def sequence_document_refs(archive: RidocArchiveV3) -> set[str]:
    if archive.manifest.get('spec_version') in {'3.4', '3.5'}:
        return {value.document_ref for value in archive.document_states.values()}
    return {ref for graph in archive.package.lessons.values() for branch in graph.branches.values() for ref in branch.replay_document_refs.values()}

def build_merge_plan(archive: RidocArchiveV3, lesson_id: str, source_branch: str, ordered_node_ids: list[str] | None=None) -> dict[str, Any]:
    graph = archive.package.lessons[lesson_id]
    if source_branch not in graph.branches or source_branch == graph.current_branch:
        raise RidocV3Error('Select another source branch in the current course')
    from openclass_core.services.ridoc_document_cursor import document_history_state
    for name in (graph.current_branch, source_branch):
        if graph.branches[name].document_cursor_node_id is not None and document_history_state(archive, lesson_id, name)['withdrawn_node_ids']:
            raise RidocV3Error('A branch being merged has undone board changes. Redo them first, or continue learning from the undone state in a new branch before merging')
    target_ids = branch_sequence(archive, lesson_id)
    source_ids = branch_sequence(archive, lesson_id, source_branch)
    if archive.manifest.get('spec_version') not in {'3.4', '3.5'} and any((isinstance(_node(archive, lesson_id, identity), RidocMergeEventNode) for identity in [*target_ids, *source_ids])):
        raise RidocV3Error('The legacy E merge has no verifiable action input version. Migrate it explicitly before merging')
    target_set = set(target_ids)
    common = target_set & set(source_ids)
    if not common:
        raise RidocV3Error('The branches have no verifiable common starting point')
    source_only = [identity for identity in source_ids if identity not in target_set]
    if not source_only:
        raise RidocV3Error('All nodes from the source branch have already been merged into the current learning sequence')
    base = next((identity for identity in reversed(graph.node_order) if identity in common))
    insertion = target_ids.index(base) + 1
    default = [*target_ids[:insertion], *source_only, *target_ids[insertion:]]
    ordered = list(default if ordered_node_ids is None else ordered_node_ids)
    if len(ordered) != len(set(ordered)) or set(ordered) != set(default):
        raise RidocV3Error('The merge order must preserve every node exactly once')
    fixed = lambda ids: [identity for identity in ids if not isinstance(_node(archive, lesson_id, identity), RidocMessageNode)]
    if not ordered or ordered[0] != default[0] or fixed(ordered) != fixed(default):
        raise RidocV3Error('Only the M reading order can be changed. C0 and document changes must retain their default order')
    if archive.manifest.get('spec_version') in {'3.4', '3.5'}:
        from openclass_core.services.ridoc_v34_states import replay_actions
        replay = replay_actions(archive, lesson_id, ordered)
    else:
        replay = replay_sequence(archive, lesson_id, ordered)
    nodes = []
    for identity in ordered:
        node = _node(archive, lesson_id, identity)
        nodes.append({'id': identity, 'node_id': identity, 'kind': node.kind, 'title': node.user_message if isinstance(node, RidocMessageNode) else node.label, 'summary': node.assistant_message if isinstance(node, RidocMessageNode) else '', 'branch_name': node.branch_name, 'initial': isinstance(node, RidocDocumentNode) and node.initial})
    return {'base_node_id': base, 'target_node_ids': target_ids, 'source_node_ids': source_ids, 'default_node_ids': default, 'ordered_node_ids': ordered, 'nodes': nodes, 'blockers': replay['blockers'], 'document': replay['document'], 'replay_document_refs': replay['document_refs'], 'replay_documents': replay['documents'], **({'document_states': replay['document_states'], 'document_occurrences': replay['document_occurrences'], 'state_ref': replay.get('state_ref')} if archive.manifest.get('spec_version') in {'3.4', '3.5'} else {})}

def validate_learning_sequences(archive: RidocArchiveV3, lesson_id: str) -> None:
    if archive.manifest.get('spec_version') in {'3.4', '3.5'}:
        from openclass_core.services.ridoc_v34_validation import validate_archive
        validate_archive(archive)
        return
    graph = archive.package.lessons[lesson_id]
    if (graph.merge_records or any((branch.learning_order_merged for branch in graph.branches.values()))) and archive.manifest['spec_version'] not in RIDOC_V3_SEQUENCE_SPEC_VERSIONS:
        raise RidocV3Error('Persistent merge metadata requires RIDOC 3.2')
    positions = {identity: index for index, identity in enumerate(graph.node_order)}
    memo: dict[tuple[str, ...], dict[str, Any]] = {}
    for node_id in graph.node_order:
        node = _node(archive, lesson_id, node_id)
        if node.learning_context_node_ids:
            if any((positions.get(identity, len(positions)) >= positions[node.id] for identity in node.learning_context_node_ids)):
                raise RidocV3Error('Learning context can only reference existing valid nodes')
            replay = require_replay(replay_sequence(archive, lesson_id, node.learning_context_node_ids, _memo=memo))
            expected = node.before_document_ref if isinstance(node, RidocDocumentNode) else node.document_ref
            if replay['document_ref'] != expected:
                raise RidocV3Error("The node's original input document does not match its learning context")
    for branch in graph.branches.values():
        if not branch.learning_node_ids:
            if branch.replay_document_refs:
                raise RidocV3Error('The replay snapshot is missing its learning order')
            continue
        if archive.manifest['spec_version'] not in RIDOC_V3_SEQUENCE_SPEC_VERSIONS:
            raise RidocV3Error('Persistent learning order requires RIDOC 3.2')
        result = require_replay(replay_sequence(archive, lesson_id, branch.learning_node_ids, _memo=memo))
        if result['document_refs'] != branch.replay_document_refs:
            raise RidocV3Error('The step-by-step replay snapshots do not match the learning sequence')
        for ref, payload in result['documents'].items():
            if archive.documents.get(ref) != payload:
                raise RidocV3Error('The learning sequence is missing a valid document snapshot')
    for record in graph.merge_records:
        if not isinstance(record.get('transaction_id'), str) or not record['transaction_id']:
            raise RidocV3Error('The merge source is missing a stable transaction identity')
        for field in ('source_head_id', 'target_head_id', 'base_node_id'):
            if record.get(field) not in positions:
                raise RidocV3Error('The merge source references a node that does not exist')
    identities = [record['transaction_id'] for record in graph.merge_records]
    if len(identities) != len(set(identities)):
        raise RidocV3Error('Merge transaction identities must be unique')
