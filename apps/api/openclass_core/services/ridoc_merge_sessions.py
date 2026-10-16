"""Runtime merge drafts over a revision-bound, authoritative RIDOC sequence.

Drafts never write course history. Submission replays the complete plan under
the package lock; a failed C cannot be skipped or resolved by replacing text.
"""
from __future__ import annotations
from typing import Any
from openclass_core.models import LessonMergeEventPlan, LessonMergeSession, LessonMergeSessionView, now_iso
from openclass_core.services.lesson_merge import LessonMergeConflictError, LessonMergeError, LessonMergeStaleError, merge_session_view
from openclass_core.services.ridoc_learning_sequence import build_merge_plan
from openclass_core.services.ridoc_v3 import RidocArchiveV3, RidocV3ConflictError, RidocV3Error

def _plan(archive: RidocArchiveV3, lesson_id: str, source: str, ordered: list[str] | None=None) -> tuple[LessonMergeEventPlan, Any]:
    if archive.revision is None:
        raise RidocV3Error('The course is missing a verifiable RIDOC revision')
    result = build_merge_plan(archive, lesson_id, source, ordered_node_ids=ordered)
    payload = {**result, 'ridoc_revision': archive.revision.value}
    return (LessonMergeEventPlan.model_validate(payload), result['document'])

def create_ridoc_merge_session(archive: RidocArchiveV3, *, owner_user_id: str, lesson_id: str, source_branch_name: str, supersedes_session_id: str | None=None) -> LessonMergeSession:
    graph = archive.package.lessons[lesson_id]
    plan, document = _plan(archive, lesson_id, source_branch_name)
    if document is None:
        raise RidocV3Error('The merge is missing a verifiable initial board')
    return LessonMergeSession(owner_user_id=owner_user_id, lesson_id=lesson_id, source_branch_name=source_branch_name, target_branch_name=graph.current_branch, base_commit_id=plan.base_node_id, source_head_commit_id=graph.branches[source_branch_name].head_node_id, target_head_commit_id=graph.branches[graph.current_branch].head_node_id, status='draft' if plan.blockers else 'ready', mode='manual', draft_document=document, event_plan=plan, supersedes_session_id=supersedes_session_id)

def _assert_current(archive: RidocArchiveV3, session: LessonMergeSession) -> None:
    plan = session.event_plan
    if plan is None:
        raise LessonMergeStaleError('This legacy merge draft must be recalculated using the current event stream')
    graph = archive.package.lessons.get(session.lesson_id)
    if graph is None or archive.revision is None:
        raise LessonMergeStaleError('The course has changed. Recalculate the merge')
    source = graph.branches.get(session.source_branch_name)
    target = graph.branches.get(session.target_branch_name)
    if archive.revision.value != plan.ridoc_revision or source is None or target is None or (graph.current_branch != session.target_branch_name) or (source.head_node_id != session.source_head_commit_id) or (target.head_node_id != session.target_head_commit_id):
        raise LessonMergeStaleError('The course or branch has changed. The original draft cannot be submitted; recalculate the merge')

def ridoc_merge_session_view(archive: RidocArchiveV3, session: LessonMergeSession) -> LessonMergeSessionView:
    copied = session.model_copy(deep=True)
    if copied.status not in {'committed', 'abandoned'}:
        try:
            _assert_current(archive, copied)
        except LessonMergeStaleError:
            copied.status = 'stale'
    if copied.event_plan:
        existing = {node.id for node in archive.nodes.values() if node.lesson_id == session.lesson_id}
        copied.event_plan.nodes = [node for node in copied.event_plan.nodes if node.id in existing]
        for name in ('target_node_ids', 'source_node_ids', 'default_node_ids', 'ordered_node_ids'):
            setattr(copied.event_plan, name, [identity for identity in getattr(copied.event_plan, name) if identity in existing])
    return merge_session_view(copied)

def update_ridoc_merge_session(archive: RidocArchiveV3, session: LessonMergeSession, *, expected_version: int, ordered_node_ids: list[str] | None) -> None:
    if session.version != expected_version:
        raise LessonMergeStaleError('The merge draft has been updated. Refresh and retry')
    if session.status not in {'ready', 'draft', 'failed'}:
        raise LessonMergeError('The current merge draft cannot be edited')
    _assert_current(archive, session)
    assert session.event_plan is not None
    ordered = session.event_plan.ordered_node_ids if ordered_node_ids is None else ordered_node_ids
    plan, document = _plan(archive, session.lesson_id, session.source_branch_name, ordered)
    session.event_plan = plan
    session.draft_document = document
    session.status = 'draft' if plan.blockers else 'ready'
    session.version += 1
    session.updated_at = now_iso()

def submit_ridoc_merge_session(store: Any, *, owner_user_id: str, package_id: str, session: LessonMergeSession, expected_version: int, expected_ridoc_revision: str) -> Any:
    """Caller retains the package lock through draft persistence and response."""
    archive = store.load_archive(owner_user_id, package_id=package_id)
    graph = archive.package.lessons[session.lesson_id]
    identity = f'merge:{session.id}'
    previous = next((record for record in graph.merge_records if record.get('transaction_id') == identity), None)
    if previous is not None:
        if session.event_plan is None or expected_ridoc_revision != session.event_plan.ridoc_revision or expected_version not in {session.version, session.version - 1}:
            raise LessonMergeStaleError('The merge submission identity does not match the original request')
        if session.status != 'committed':
            session.status = 'committed'
            session.version += 1
        session.committed_commit_id = graph.branches[session.target_branch_name].head_node_id
        return store.load_package(owner_user_id, package_id=package_id)
    if session.version != expected_version:
        raise LessonMergeStaleError('The merge draft has been updated. Refresh and retry')
    if session.status not in {'ready', 'draft'}:
        raise LessonMergeError('The current merge draft cannot be submitted')
    _assert_current(archive, session)
    assert session.event_plan is not None
    if expected_ridoc_revision != session.event_plan.ridoc_revision:
        raise LessonMergeStaleError('The submitted version does not match the preview. Recalculate it')
    plan, document = _plan(archive, session.lesson_id, session.source_branch_name, session.event_plan.ordered_node_ids)
    if plan.blockers:
        blocker = plan.blockers[0]
        raise LessonMergeConflictError(f'Node {blocker.node_id} cannot be merged: {blocker.reason}; the entire merge was not saved')
    store.merge_branches(owner_user_id=owner_user_id, package_id=package_id, lesson_id=session.lesson_id, expected_revision=expected_ridoc_revision, transaction_id=identity, source_branch_name=session.source_branch_name, target_branch_name=session.target_branch_name, expected_source_head_id=session.source_head_commit_id, expected_target_head_id=session.target_head_commit_id, document=document, ordered_node_ids=plan.ordered_node_ids, label=f'Merge {session.source_branch_name} into {session.target_branch_name}', decision_metadata={'merge_session_id': session.id})
    refreshed = store.load_package(owner_user_id, package_id=package_id)
    lesson = next((item for item in refreshed.lessons if item.id == session.lesson_id))
    session.committed_commit_id = lesson.history_graph.branches[session.target_branch_name].head_commit_id
    session.status = 'committed'
    session.version += 1
    session.updated_at = now_iso()
    return refreshed
