"""Independent, content-addressed states and actual branch C occurrences."""
from __future__ import annotations
from copy import deepcopy
from typing import Any
from openclass_core.models import BoardDocument
from openclass_core.services.ridoc_document_actions import apply_document_action, initial_versions
from openclass_core.services.ridoc_v3 import RIDOC_V34_SPEC_VERSION, RIDOC_V35_SPEC_VERSION, RIDOC_INDEPENDENT_STATE_SPEC_VERSIONS, RidocArchiveV3, RidocDocumentNode, RidocMergeEventNode, RidocMessageNode, RidocV3Error, content_hash, store_document
from openclass_core.services.ridoc_v34_models import DocumentOccurrence, DocumentState, NodeRef

def is_v34(archive: RidocArchiveV3) -> bool:
    return archive.manifest.get('spec_version') in RIDOC_INDEPENDENT_STATE_SPEC_VERSIONS

def resolve_ref(archive: RidocArchiveV3, lesson_id: str, node_id: str) -> NodeRef:
    ref = archive.node_refs.get(f'{lesson_id}/{node_id}')
    if ref is None:
        raise RidocV3Error(f'Unregistered node reference: {node_id}')
    return ref

def continuation_node_id(archive: RidocArchiveV3, lesson_id: str, branch_name: str | None=None) -> str:
    graph = archive.package.lessons[lesson_id]
    branch = graph.branches[branch_name or graph.current_branch]
    if f'{lesson_id}/{branch.head_node_id}' in archive.nodes:
        return branch.head_node_id
    if not branch.learning_node_ids:
        raise RidocV3Error('The branch has no live node to continue from')
    return branch.learning_node_ids[-1]

def node_refs_view(archive: RidocArchiveV3, node) -> list[dict[str, Any]]:
    from openclass_core.services.ridoc_v34_references import node_reference_ids
    return [{**resolve_ref(archive, node.lesson_id, identity).model_dump(mode='json'), 'role': role} for role, identity in node_reference_ids(node)]

def branch_refs_view(archive: RidocArchiveV3, lesson_id: str, branch) -> list[dict[str, Any]]:
    return [{**resolve_ref(archive, lesson_id, identity).model_dump(mode='json'), 'role': role} for role, identity in (('head', branch.head_node_id), ('base', branch.base_node_id))]

def state(archive: RidocArchiveV3, digest: str) -> DocumentState:
    value = archive.document_states.get(digest)
    if value is None or content_hash(value) != digest:
        raise RidocV3Error('DocumentState is missing or its hash does not match')
    return value

def state_result(archive: RidocArchiveV3, digest: str) -> dict[str, Any]:
    value = state(archive, digest)
    return {'document': BoardDocument.model_validate(archive.documents[value.document_ref]), 'document_ref': value.document_ref, 'document_version': value.document_version, 'structure_version': value.structure_version, 'versions': dict(value.versions), 'resource_refs': list(value.resource_refs), 'evidence_ids': list(value.evidence_ids), 'state_ref': digest, 'legacy': False}

def put_state(archive: RidocArchiveV3, lesson_id: str, value: dict[str, Any], *, resource_refs: list[str] | None=None, evidence_ids: list[str] | None=None) -> str:
    document_ref = store_document(archive.documents, value['document'])
    saved = DocumentState(lesson_id=lesson_id, document_ref=document_ref, document_version=value['document_version'], structure_version=value['structure_version'], versions=dict(value['versions']), resource_refs=sorted(set(resource_refs if resource_refs is not None else value.get('resource_refs', []))), evidence_ids=sorted(set(evidence_ids if evidence_ids is not None else value.get('evidence_ids', []))))
    digest = content_hash(saved)
    archive.document_states[digest] = saved
    return digest

def initial_state(archive: RidocArchiveV3, node) -> str:
    digest = node.after_document_ref if isinstance(node, RidocDocumentNode) else node.document_ref
    document = BoardDocument.model_validate(archive.documents[digest])
    return put_state(archive, node.lesson_id, {'document': document, 'document_version': node.id, 'structure_version': node.id, 'versions': initial_versions(document, node.id)}, resource_refs=node.resource_refs, evidence_ids=node.evidence_ids)

def advance(archive: RidocArchiveV3, node, before_ref: str) -> str:
    before = state_result(archive, before_ref)
    if isinstance(node, RidocMergeEventNode):
        document = BoardDocument.model_validate(archive.documents[node.document_ref])
        if document != before['document']:
            raise RidocV3Error('Legacy merge events cannot create artificial document changes')
        result = {**before, 'document_version': node.id}
    else:
        source_before = BoardDocument.model_validate(archive.documents[node.before_document_ref])
        source_after = BoardDocument.model_validate(archive.documents[node.after_document_ref])
        document, versions, structure = apply_document_action(before['document'], source_before, source_after, node.document_action, node_id=node.id, versions=before['versions'], document_version=before['document_version'], structure_version=before['structure_version'], legacy_input_version=before['document_version'])
        result = {'document': document, 'versions': versions, 'document_version': node.id, 'structure_version': structure}
    return put_state(archive, node.lesson_id, result, resource_refs=[*before['resource_refs'], *node.resource_refs], evidence_ids=[*before['evidence_ids'], *node.evidence_ids])

def capture_node(archive: RidocArchiveV3, node, input_state: dict[str, Any]) -> None:
    existing = archive.node_refs.get(f'{node.lesson_id}/{node.id}')
    if existing is not None and existing.status == 'deleted':
        raise RidocV3Error('Deleted nodes cannot be recreated through an old transaction')
    archive.node_refs[f'{node.lesson_id}/{node.id}'] = NodeRef(lesson_id=node.lesson_id, node_id=node.id, kind=node.kind)
    before = input_state.get('state_ref') or put_state(archive, node.lesson_id, input_state)
    node.input_state_ref = before
    node.output_state_ref = before if isinstance(node, RidocMessageNode) else advance(archive, node, before)

def _resources(archive: RidocArchiveV3, lesson_id: str, ids: list[str]) -> tuple[list[str], list[str]]:
    changes = [archive.nodes[f'{lesson_id}/{identity}'] for identity in ids if not isinstance(archive.nodes[f'{lesson_id}/{identity}'], RidocMessageNode)]
    return (sorted({value for node in changes for value in node.resource_refs}), sorted({value for node in changes for value in node.evidence_ids}))

def upgrade_archive(archive: RidocArchiveV3) -> None:
    from openclass_core.services.ridoc_learning_sequence import branch_sequence, replay_sequence, require_replay
    from openclass_core.services.ridoc_v3 import validate_ridoc_v3
    validate_ridoc_v3(archive)
    if is_v34(archive):
        return
    edited = deepcopy(archive)
    for node in edited.nodes.values():
        edited.node_refs[f'{node.lesson_id}/{node.id}'] = NodeRef(lesson_id=node.lesson_id, node_id=node.id, kind=node.kind)
    resolving: set[str] = set()

    def source_state(lesson_id: str, node_id: str) -> str:
        key = f'{lesson_id}/{node_id}'
        node = edited.nodes[key]
        if node.output_state_ref:
            return node.output_state_ref
        if key in resolving:
            raise RidocV3Error('The original state references contain a cycle')
        resolving.add(key)
        if isinstance(node, RidocDocumentNode) and node.initial:
            node.output_state_ref = initial_state(edited, node)
        else:
            if node.learning_context_node_ids:
                result = require_replay(replay_sequence(archive, lesson_id, node.learning_context_node_ids))
                resources, evidence = _resources(archive, lesson_id, node.learning_context_node_ids)
                before = put_state(edited, lesson_id, result, resource_refs=resources, evidence_ids=evidence)
            else:
                before = source_state(lesson_id, node.parent_ids[0])
            node.input_state_ref = before
            node.output_state_ref = before if isinstance(node, RidocMessageNode) else advance(edited, node, before)
        resolving.remove(key)
        return node.output_state_ref
    for lesson_id, graph in edited.package.lessons.items():
        for identity in graph.node_order:
            source_state(lesson_id, identity)
        for name, branch in graph.branches.items():
            ids = branch_sequence(archive, lesson_id, name)
            branch.learning_node_ids = list(ids)
            legacy = any((isinstance(edited.nodes[f'{lesson_id}/{identity}'], RidocMergeEventNode) for identity in ids))
            previous: str | None = None
            for index, identity in enumerate(ids):
                node = edited.nodes[f'{lesson_id}/{identity}']
                if isinstance(node, RidocMessageNode):
                    continue
                if legacy:
                    after = node.output_state_ref
                    before = node.input_state_ref or None
                else:
                    result = require_replay(replay_sequence(archive, lesson_id, ids[:index + 1]))
                    resources, evidence = _resources(archive, lesson_id, ids[:index + 1])
                    after = put_state(edited, lesson_id, result, resource_refs=resources, evidence_ids=evidence)
                    before = previous
                branch.document_occurrences[identity] = DocumentOccurrence(before_state_ref=before, after_state_ref=after)
                previous = after
            branch.replay_document_refs = {}
    edited.manifest['spec_version'] = RIDOC_V34_SPEC_VERSION
    edited.manifest['replay_mode'] = 'independent_document_states'
    from openclass_core.services.ridoc_v34_references import prune_objects
    prune_objects(edited)
    validate_ridoc_v3(edited)
    archive.manifest, archive.package, archive.nodes = (edited.manifest, edited.package, edited.nodes)
    archive.documents, archive.document_states, archive.node_refs = (edited.documents, edited.document_states, edited.node_refs)

def saved_replay(archive: RidocArchiveV3, lesson_id: str, ids: list[str], *, branch_name: str | None=None) -> dict[str, Any]:
    graph = archive.package.lessons[lesson_id]
    branch = graph.branches[branch_name or graph.current_branch]
    if len(ids) != len(set(ids)):
        raise RidocV3Error('The learning sequence cannot contain duplicate nodes')
    current = None
    refs: dict[str, str] = {}
    documents: dict[str, Any] = {}
    for identity in ids:
        node = archive.nodes.get(f'{lesson_id}/{identity}')
        if node is None:
            raise RidocV3Error('The branch learning sequence may only contain live nodes')
        if not isinstance(node, RidocMessageNode):
            occurrence = branch.document_occurrences.get(identity)
            if occurrence is None:
                raise RidocV3Error('The branch is missing the applied state of a document change')
            current = state_result(archive, occurrence.after_state_ref)
            documents[current['document_ref']] = current['document'].model_dump(mode='json')
        if current is None:
            raise RidocV3Error('The learning sequence must start with C0')
        refs[identity] = current['document_ref']
    if current is None:
        raise RidocV3Error('The learning sequence is missing C0')
    if archive.manifest.get('spec_version') == RIDOC_V35_SPEC_VERSION and ids == branch.learning_node_ids and branch.current_state_ref and (branch.document_cursor_node_id is None):
        current = state_result(archive, branch.current_state_ref)
        documents[current['document_ref']] = current['document'].model_dump(mode='json')
    return {**current, 'document_refs': refs, 'documents': documents, 'blockers': []}

def set_sequence(archive: RidocArchiveV3, lesson_id: str, name: str, ids: list[str]) -> None:
    branch = archive.package.lessons[lesson_id].branches[name]
    original_occurrences = branch.document_occurrences
    occurrences: dict[str, DocumentOccurrence] = {}
    for identity in ids:
        node = archive.nodes[f'{lesson_id}/{identity}']
        if isinstance(node, RidocMessageNode):
            continue
        occurrence = branch.document_occurrences.get(identity)
        if occurrence is None:
            if not node.output_state_ref:
                raise RidocV3Error('The document change is missing its captured state')
            occurrence = DocumentOccurrence(before_state_ref=node.input_state_ref or None, after_state_ref=node.output_state_ref)
        occurrences[identity] = occurrence
    branch.learning_node_ids = list(ids)
    branch.document_occurrences = occurrences
    branch.replay_document_refs = {}
    if archive.manifest.get('spec_version') == RIDOC_V35_SPEC_VERSION:
        added = [identity for identity in occurrences if identity not in original_occurrences]
        if added or not branch.current_state_ref:
            branch.current_state_ref = occurrences[next(reversed(occurrences))].after_state_ref
            branch.current_trace_ref = occurrences[next(reversed(occurrences))].output_trace_ref
    saved_replay(archive, lesson_id, ids, branch_name=name)

def replay_actions(archive: RidocArchiveV3, lesson_id: str, ids: list[str]) -> dict[str, Any]:
    """A new merge must prove real inputs; old producer identities may be deleted."""
    edited = deepcopy(archive)
    current: str | None = None
    refs: dict[str, str] = {}
    occurrences: dict[str, DocumentOccurrence] = {}
    blockers = []
    target = archive.package.lessons[lesson_id].branches[archive.package.lessons[lesson_id].current_branch]
    target_last_change = next(reversed(target.document_occurrences), None)
    changed_input = False
    for identity in ids:
        node = edited.nodes[f'{lesson_id}/{identity}']
        try:
            if not isinstance(node, RidocMessageNode):
                before = current
                retained = target.document_occurrences.get(identity)
                if retained is not None and (not changed_input):
                    occurrences[identity] = retained.model_copy(deep=True)
                    current = retained.after_state_ref
                    if archive.manifest.get('spec_version') == RIDOC_V35_SPEC_VERSION and identity == target_last_change and target.current_state_ref and (target.document_cursor_node_id is None):
                        current = target.current_state_ref
                else:
                    current = node.output_state_ref if isinstance(node, RidocDocumentNode) and node.initial else advance(edited, node, current or '')
                    occurrences[identity] = DocumentOccurrence(before_state_ref=before, after_state_ref=current)
                    changed_input = retained is None or current != retained.after_state_ref
            if current is None:
                raise RidocV3Error('The learning sequence must start with C0')
            refs[identity] = state(edited, current).document_ref
        except RidocV3Error as exc:
            blockers.append({'node_id': identity, 'reason': str(exc), 'object_id': getattr(exc, 'object_id', '')})
            break
    result = state_result(edited, current) if current else {'document': None, 'document_ref': ''}
    return {**result, 'document_refs': refs, 'documents': edited.documents, 'blockers': blockers, 'document_states': edited.document_states, 'document_occurrences': occurrences}

def install_merge_plan(archive: RidocArchiveV3, lesson_id: str, name: str, plan: dict[str, Any]) -> None:
    if plan.get('blockers'):
        raise RidocV3Error('The merge contains unresolved document state conflicts')
    archive.document_states.update(plan['document_states'])
    archive.documents.update(plan['replay_documents'])
    archive.package.lessons[lesson_id].branches[name].document_occurrences = plan['document_occurrences']
    if archive.manifest.get('spec_version') == RIDOC_V35_SPEC_VERSION:
        branch = archive.package.lessons[lesson_id].branches[name]
        branch.current_state_ref = plan.get('state_ref') or next(reversed(branch.document_occurrences.values())).after_state_ref
        branch.current_trace_ref = None
    set_sequence(archive, lesson_id, name, plan['ordered_node_ids'])
    from openclass_core.services.ridoc_v34_references import prune_objects
    prune_objects(archive)

def archive_state(archive: RidocArchiveV3, lesson_id: str, branch_name: str | None=None) -> dict[str, Any]:
    from openclass_core.services.ridoc_document_cursor import applied_branch_sequence
    from openclass_core.services.ridoc_learning_sequence import branch_sequence
    if not is_v34(archive):
        from openclass_core.services.ridoc_document_cursor import applied_branch_replay
        return applied_branch_replay(archive, lesson_id, branch_name)
    branch = archive.package.lessons[lesson_id].branches[branch_name or archive.package.lessons[lesson_id].current_branch]
    ids = applied_branch_sequence(archive, lesson_id, branch_name) if branch.document_cursor_node_id else branch_sequence(archive, lesson_id, branch_name)
    return saved_replay(archive, lesson_id, ids, branch_name=branch_name)
