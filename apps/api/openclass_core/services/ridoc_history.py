from __future__ import annotations
from typing import Any, Literal
from openclass_core.models import BoardDocument, RidocHistoryBranchView, RidocHistoryDocumentView, RidocHistoryNodeView, RidocHistoryPageView
from openclass_core.services.ridoc_v3 import RidocArchiveV3, RidocDocumentNode, RidocMergeEventNode, RidocMessageNode, RidocV3Error, merge_parent_roles
from openclass_core.services.ridoc_workspace_store import resolve_history_branch_parent
from openclass_core.services.ridoc_learning_sequence import branch_sequence
from openclass_core.services.ridoc_v34_states import branch_refs_view, is_v34, node_refs_view, saved_replay
from openclass_core.services.ridoc_document_history import document_history_view

def _graph(archive: RidocArchiveV3, lesson_id: str):
    graph = archive.package.lessons.get(lesson_id)
    if graph is None:
        raise RidocV3Error('RIDOC lesson is unavailable')
    return graph

def _node(archive: RidocArchiveV3, lesson_id: str, node_id: str):
    try:
        return archive.nodes[f'{lesson_id}/{node_id}']
    except KeyError as exc:
        raise RidocV3Error('RIDOC history node is unavailable') from exc

def _document_ref(node: RidocMessageNode | RidocDocumentNode | RidocMergeEventNode) -> str:
    return node.after_document_ref if isinstance(node, RidocDocumentNode) else node.document_ref

def _reachable_ids(archive: RidocArchiveV3, lesson_id: str, branch_name: str) -> list[str]:
    graph = _graph(archive, lesson_id)
    branch = graph.branches.get(branch_name)
    if branch is None:
        raise RidocV3Error('RIDOC branch is unavailable')
    return branch_sequence(archive, lesson_id, branch_name)

def _node_view(archive: RidocArchiveV3, lesson_id: str, node_id: str, *, head_ids: set[str]) -> RidocHistoryNodeView:
    node = _node(archive, lesson_id, node_id)
    references = node_refs_view(archive, node) if is_v34(archive) else []
    parents = [identity for identity in node.parent_ids if not is_v34(archive) or f'{lesson_id}/{identity}' in archive.nodes]
    continuation_node_id = resolve_history_branch_parent(archive, lesson_id, 'fork', node.id)[1] if isinstance(node, RidocMessageNode) else node.id
    if isinstance(node, RidocMessageNode):
        origin_label = ''
        if node.branch_origin_deleted:
            origin_label = 'Original source deleted · '
        elif node.branch_origin_kind and node.branch_origin_node_id:
            origin_label = ('Regenerated from' if node.branch_origin_kind == 'regenerate' else 'Branched from') + f' {node.branch_origin_node_id[-10:]} · '
        return RidocHistoryNodeView(id=node.id, continuation_node_id=continuation_node_id, kind='M', parent_ids=parents, node_refs=references, branch_name=node.branch_name, created_at=node.created_at, title=f'{origin_label}{node.user_message}', summary=node.assistant_message, user_message=node.user_message, assistant_message=node.assistant_message, after_document_ref=node.document_ref, is_head=node.id in head_ids, branch_origin_deleted=node.branch_origin_deleted)
    if isinstance(node, RidocMergeEventNode):
        label = node.label or f'Merge {node.merge_source_branch} into {node.branch_name}'
        return RidocHistoryNodeView(id=node.id, continuation_node_id=continuation_node_id, kind='E', parent_ids=parents, node_refs=references, branch_name=node.branch_name, created_at=node.created_at, title=label, summary=label, after_document_ref=node.document_ref, is_head=node.id in head_ids, merge_target_node_id=merge_parent_roles(node)[0], merge_source_node_id=merge_parent_roles(node)[1], merge_source_branch=node.merge_source_branch)
    return RidocHistoryNodeView(id=node.id, continuation_node_id=continuation_node_id, kind='C', initial=node.initial, parent_ids=parents, node_refs=references, branch_name=node.branch_name, created_at=node.created_at, title='Initial document' if node.initial else node.label or 'Document update', summary=('Original conversation deleted · ' if node.source_message_deleted else '') + ('' if node.initial else node.label or 'Document update'), before_document_ref=node.before_document_ref, after_document_ref=node.after_document_ref, is_head=node.id in head_ids, source_message_deleted=node.source_message_deleted, branch_origin_deleted=node.branch_origin_deleted, merge_target_node_id=merge_parent_roles(node)[0], merge_source_node_id=merge_parent_roles(node)[1], merge_source_branch=node.merge_source_branch)

def history_page(archive: RidocArchiveV3, lesson_id: str, *, branch_name: str | None=None, scope: Literal['branch', 'all']='branch', cursor: str | None=None, limit: int=100, query: str='', store: Any=None, owner_user_id: str='', include_replay_documents: bool=True) -> RidocHistoryPageView:
    if archive.revision is None:
        raise RidocV3Error('RIDOC revision is unavailable')
    graph = _graph(archive, lesson_id)
    selected_branch = branch_name or graph.current_branch
    if selected_branch not in graph.branches:
        raise RidocV3Error('RIDOC branch is unavailable')
    branch = graph.branches[selected_branch]
    node_ids = list(graph.node_order) if scope == 'all' else _reachable_ids(archive, lesson_id, selected_branch)
    normalized_query = query.strip().casefold()
    if normalized_query:
        node_ids = [node_id for node_id in node_ids if normalized_query in ' '.join(filter(None, (getattr(_node(archive, lesson_id, node_id), 'user_message', ''), getattr(_node(archive, lesson_id, node_id), 'assistant_message', ''), getattr(_node(archive, lesson_id, node_id), 'label', '')))).casefold()]
    start = 0
    if cursor:
        try:
            start = node_ids.index(cursor) + 1
        except ValueError as exc:
            raise RidocV3Error('RIDOC history cursor is unavailable') from exc
    page_ids = node_ids[start:start + limit]
    next_cursor = page_ids[-1] if page_ids and start + len(page_ids) < len(node_ids) else None
    head_ids = {branch.head_node_id for branch in graph.branches.values()}
    return RidocHistoryPageView(lesson_id=lesson_id, ridoc_revision=archive.revision.value, current_branch=graph.current_branch, branches=[RidocHistoryBranchView(name=branch.name, head_node_id=branch.head_node_id, base_node_id=branch.base_node_id, archived=branch.archived, node_refs=branch_refs_view(archive, lesson_id, branch) if is_v34(archive) else []) for branch in graph.branches.values()], nodes=[_node_view(archive, lesson_id, node_id, head_ids=head_ids) for node_id in page_ids], next_cursor=next_cursor, learning_node_ids=list(branch.learning_node_ids), replay_documents={identity: BoardDocument.model_validate(archive.documents[digest]) for identity, digest in (saved_replay(archive, lesson_id, branch.learning_node_ids, branch_name=selected_branch)['document_refs'] if is_v34(archive) else branch.replay_document_refs).items()} if include_replay_documents else {}, learning_order_merged=branch.learning_order_merged or any((record.get('target_branch') == selected_branch for record in graph.merge_records)), document_history=document_history_view(archive, lesson_id, branch_name=selected_branch, store=store, owner_user_id=owner_user_id), expected_version=store._repository_row(owner_user_id)['version'] if store is not None and hasattr(store, 'repository_id') else None)

def history_document(archive: RidocArchiveV3, lesson_id: str, node_id: str) -> RidocHistoryDocumentView:
    if archive.revision is None:
        raise RidocV3Error('RIDOC revision is unavailable')
    node = _node(archive, lesson_id, node_id)
    digest = _document_ref(node)
    return RidocHistoryDocumentView(lesson_id=lesson_id, node_id=node_id, ridoc_revision=archive.revision.value, document=BoardDocument.model_validate(archive.documents[digest]))
