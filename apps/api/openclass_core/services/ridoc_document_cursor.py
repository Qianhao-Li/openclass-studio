"""Branch-local document position over immutable, complete learning history."""
from __future__ import annotations
import hashlib
from typing import Any
from openclass_core.services.ridoc_learning_sequence import branch_sequence, replay_sequence, require_replay
from openclass_core.services.ridoc_v3 import RIDOC_V3_CURSOR_SPEC_VERSIONS, RidocArchiveV3, RidocBranch, RidocDocumentNode, RidocLessonGraph, RidocV3Error

def set_document_cursor(archive: RidocArchiveV3, lesson_id: str, branch_name: str, cursor: str, last: str) -> None:
    branch = archive.package.lessons[lesson_id].branches[branch_name]
    branch.document_cursor_node_id = None if cursor == last else cursor
    if archive.manifest.get('spec_version') == '3.5':
        occurrence = branch.document_occurrences[cursor]
        branch.current_state_ref = occurrence.after_state_ref
        branch.current_trace_ref = occurrence.output_trace_ref

def document_history_state(archive: RidocArchiveV3, lesson_id: str, branch_name: str | None=None) -> dict[str, Any]:
    graph = archive.package.lessons.get(lesson_id)
    if graph is None:
        raise RidocV3Error('RIDOC lesson is unavailable')
    name = branch_name or graph.current_branch
    branch = graph.branches.get(name)
    if branch is None:
        raise RidocV3Error('RIDOC branch is unavailable')
    sequence = branch_sequence(archive, lesson_id, name)
    if archive.manifest.get('spec_version') in {'3.4', '3.5'}:
        from openclass_core.services.ridoc_v34_states import saved_replay
        saved_replay(archive, lesson_id, sequence, branch_name=name)
    else:
        require_replay(replay_sequence(archive, lesson_id, sequence))
    changes = [identity for identity in sequence if isinstance(archive.nodes[f'{lesson_id}/{identity}'], RidocDocumentNode)]
    cursor = branch.document_cursor_node_id
    if cursor is not None:
        if archive.manifest.get('spec_version') not in RIDOC_V3_CURSOR_SPEC_VERSIONS:
            raise RidocV3Error('Board undo state requires RIDOC 3.3')
        if cursor not in changes:
            raise RidocV3Error('The board cursor must reference a C or C0 node in the current branch')
    position = changes.index(cursor) if cursor is not None else len(changes) - 1
    current = changes[position]
    return {'current_document_node_id': current, 'undo_node_id': current if position > 0 else None, 'redo_node_id': changes[position + 1] if position + 1 < len(changes) else None, 'withdrawn_node_ids': changes[position + 1:]}

def applied_branch_sequence(archive: RidocArchiveV3, lesson_id: str, branch_name: str | None=None) -> list[str]:
    withdrawn = set(document_history_state(archive, lesson_id, branch_name)['withdrawn_node_ids'])
    return [identity for identity in branch_sequence(archive, lesson_id, branch_name) if identity not in withdrawn]

def applied_branch_replay(archive: RidocArchiveV3, lesson_id: str, branch_name: str | None=None) -> dict[str, Any]:
    if archive.manifest.get('spec_version') in {'3.4', '3.5'}:
        from openclass_core.services.ridoc_v34_states import saved_replay
        return saved_replay(archive, lesson_id, applied_branch_sequence(archive, lesson_id, branch_name), branch_name=branch_name)
    return require_replay(replay_sequence(archive, lesson_id, applied_branch_sequence(archive, lesson_id, branch_name)))

def create_document_divergence_branch(graph: RidocLessonGraph, transaction_id: str) -> tuple[str, RidocBranch]:
    """Create the in-memory branch only inside a successful outcome transaction."""
    source = graph.branches[graph.current_branch]
    identity = hashlib.sha256(transaction_id.encode('utf-8')).hexdigest()[:8]
    base_name = f'{source.name}-document-branch-{identity}'
    name, suffix = (base_name, 2)
    while name in graph.branches:
        name, suffix = (f'{base_name}-{suffix}', suffix + 1)
    branch = RidocBranch(name=name, head_node_id=source.head_node_id, base_node_id=source.head_node_id, learning_order_merged=source.learning_order_merged, document_occurrences={key: value.model_copy(deep=True) for key, value in source.document_occurrences.items()})
    graph.branches[name] = branch
    graph.current_branch = name
    return (name, branch)
