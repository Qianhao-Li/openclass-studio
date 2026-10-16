from __future__ import annotations

from copy import deepcopy

import pytest

from openclass_core.services.ridoc_document_cursor import applied_branch_replay, document_history_state
from openclass_core.services.ridoc_edit_operations import apply_edit_operations, merge_edit_plan
from openclass_core.services.ridoc_learning_sequence import branch_sequence, build_merge_plan
from openclass_core.services.ridoc_v3 import (
    RidocDocumentNode, RidocMergeEventNode, RidocV3ConflictError, RidocV3Error,
    ridoc_revision_for_archive, validate_ridoc_v3,
)
from test_ridoc_learning_sequence import Course, document

CREATED_AT = "2026-09-06T13:00:00+08:00"


def operation(kind, identity=None, **fields):
    return {"operation_id": identity or kind, "operation": {"kind": kind, **fields}}


def apply(archive, lesson, operations, session="edit-session"):
    return apply_edit_operations(archive, lesson, operations, session_id=session, created_at=CREATED_AT)


def course_with_changes(tmp_path):
    course = Course(tmp_path)
    changes = [course.chat(f"question-{index}", document(f"A{index}")) for index in range(1, 4)]
    archive = course.archive()
    messages = [archive.nodes[f"{course.lesson.id}/{change}"].parent_ids[0] for change in changes]
    return course, changes, messages


def legacy_linear(archive, version):
    archive = deepcopy(archive)
    archive.manifest["spec_version"] = version
    for graph in archive.package.lessons.values():
        for branch in graph.branches.values():
            branch.learning_node_ids = []
            branch.replay_document_refs = {}
    for node in archive.nodes.values():
        node.learning_context_node_ids = []
        if isinstance(node, RidocDocumentNode):
            node.document_action = {}
    validate_ridoc_v3(archive)
    return archive


def test_mixed_operations_apply_sequentially_without_mutating_base_or_disk(tmp_path):
    course, changes, messages = course_with_changes(tmp_path)
    archive = course.archive()
    base = deepcopy(archive)
    path = course.store.package_path("owner", course.package.id)
    disk = path.read_bytes()
    c0 = f"c0_{course.lesson.id}"
    operations = [
        operation("reorder_messages", branch_name="main", ordered_node_ids=[c0, *reversed(messages), *changes]),
        operation("step_document", branch_name="main", direction="undo", target_node_id=changes[-1]),
        operation("delete_message", node_id=messages[1]),
        operation("create_branch", name="continue"),
    ]
    edited, deleted, changed = apply(archive, course.lesson.id, operations)
    assert changed and edited is not archive
    assert archive == base and path.read_bytes() == disk
    assert deleted == [{"node_id": messages[1], "transaction_id": base.nodes[f"{course.lesson.id}/{messages[1]}"].transaction_id}]
    assert edited.package.lessons[course.lesson.id].current_branch == "continue"
    assert branch_sequence(edited, course.lesson.id) == [c0, messages[2], messages[0], *changes[:2]]
    assert document_history_state(edited, course.lesson.id, "main")["withdrawn_node_ids"] == [changes[-1]]
    assert applied_branch_replay(edited, course.lesson.id)["document"] == document("A2")
    assert edited.nodes[f"{course.lesson.id}/{changes[1]}"].source_message_deleted
    assert set(edited.nodes) == set(base.nodes) - {f"{course.lesson.id}/{messages[1]}"}
    for identity in changes:
        assert edited.nodes[f"{course.lesson.id}/{identity}"].after_document_ref == base.nodes[f"{course.lesson.id}/{identity}"].after_document_ref
    assert edited.package.lessons[course.lesson.id].updated_at == CREATED_AT
    assert edited.revision == base.revision


@pytest.mark.parametrize("version", ["3.0", "3.1", "3.2"])
@pytest.mark.parametrize("round_trip", ["undo-redo", "reorder-back", "checkout-back", "empty"])
def test_net_noop_returns_original_without_format_or_timestamp_upgrade(tmp_path, version, round_trip):
    course, changes, messages = course_with_changes(tmp_path)
    course.branch("other")
    course.switch("main")
    archive = course.archive() if version == "3.2" else legacy_linear(course.archive(), version)
    before = deepcopy(archive)
    sequence = branch_sequence(archive, course.lesson.id)
    pairs = {
        "undo-redo": [
            operation("step_document", "undo", branch_name="main", direction="undo", target_node_id=changes[-1]),
            operation("step_document", "redo", branch_name="main", direction="redo", target_node_id=changes[-1]),
        ],
        "reorder-back": [
            operation("reorder_messages", "move", branch_name="main", ordered_node_ids=[sequence[0], *reversed(messages), *changes]),
            operation("reorder_messages", "restore", branch_name="main", ordered_node_ids=sequence),
        ],
        "checkout-back": [operation("checkout_branch", "there", name="other"), operation("checkout_branch", "back", name="main")],
        "empty": [],
    }
    edited, deleted, changed = apply(archive, course.lesson.id, pairs[round_trip])
    assert edited is archive and not changed and deleted == []
    assert archive == before
    assert archive.manifest["spec_version"] == version


def test_create_branch_and_merge_add_only_stable_metadata_and_replay(tmp_path):
    course, _, _ = course_with_changes(tmp_path)
    archive = course.archive()
    operations = [
        operation("create_branch", name="target", from_node_id=f"c0_{course.lesson.id}"),
        operation("merge_branch", source_branch_name="main", target_branch_name="target"),
    ]
    first, deleted, changed = apply(archive, course.lesson.id, operations)
    repeated, repeated_deleted, repeated_changed = apply(archive, course.lesson.id, operations)
    assert changed and repeated_changed and deleted == repeated_deleted == []
    assert first == repeated and first.nodes == archive.nodes
    assert ridoc_revision_for_archive(first) == ridoc_revision_for_archive(repeated)
    graph = first.package.lessons[course.lesson.id]
    assert graph.current_branch == "target"
    assert graph.branches["target"].head_node_id == f"c0_{course.lesson.id}"
    assert graph.branches["target"].learning_order_merged
    assert graph.merge_records[0]["created_at"] == CREATED_AT
    assert graph.merge_records[0]["transaction_id"].startswith("history-edit-merge:")
    assert applied_branch_replay(first, course.lesson.id)["document"] == document("A3")
    another, _, _ = apply(archive, course.lesson.id, operations, session="another-session")
    assert another.package.lessons[course.lesson.id].merge_records[0]["transaction_id"] != graph.merge_records[0]["transaction_id"]


def test_merge_preview_matches_existing_plan_and_keeps_document_blockers(tmp_path):
    course = Course(tmp_path)
    course.write("commit_document", transaction_id="target", document=document("target"))
    course.branch("other", f"c0_{course.lesson.id}")
    course.write("commit_document", transaction_id="source", document=document("source"))
    course.switch("main")
    archive = course.archive()
    before = deepcopy(archive)
    plan = merge_edit_plan(archive, course.lesson.id, "other")
    assert plan == build_merge_plan(archive, course.lesson.id, "other")
    assert plan["blockers"] and plan["blockers"][0]["object_id"] == "a"
    with pytest.raises(RidocV3ConflictError, match="cannot be merged"):
        apply(archive, course.lesson.id, [operation("merge_branch", source_branch_name="other", target_branch_name="main")])
    assert archive == before


@pytest.mark.parametrize("invalid", ["older-c", "c0", "redo-skip", "c-order", "missing-m", "duplicate-m", "other-branch", "unknown-kind", "unexpected-field", "duplicate-operation"])
def test_invalid_operation_rejects_entire_draft_without_partial_mutation(tmp_path, invalid):
    course, changes, messages = course_with_changes(tmp_path)
    course.branch("other")
    course.switch("main")
    archive = course.archive()
    before = deepcopy(archive)
    sequence = branch_sequence(archive, course.lesson.id)
    good = operation("step_document", "undo-last", branch_name="main", direction="undo", target_node_id=changes[-1])
    cases = {
        "older-c": operation("step_document", branch_name="main", direction="undo", target_node_id=changes[0]),
        "c0": operation("step_document", branch_name="main", direction="undo", target_node_id=sequence[0]),
        "redo-skip": operation("step_document", branch_name="main", direction="redo", target_node_id=changes[0]),
        "c-order": operation("reorder_messages", branch_name="main", ordered_node_ids=[sequence[0], *messages, *reversed(changes)]),
        "missing-m": operation("reorder_messages", branch_name="main", ordered_node_ids=sequence[:-1]),
        "duplicate-m": operation("reorder_messages", branch_name="main", ordered_node_ids=[*sequence, messages[0]]),
        "other-branch": operation("step_document", branch_name="other", direction="undo", target_node_id=changes[-1]),
        "unknown-kind": operation("replace_document", document={}),
        "unexpected-field": operation("checkout_branch", name="other", overwrite=True),
        "duplicate-operation": operation("checkout_branch", "undo-last", name="other"),
    }
    with pytest.raises(RidocV3Error):
        apply(archive, course.lesson.id, [good, cases[invalid]])
    assert archive == before


def test_m_deletion_retains_its_c_and_later_learner_reply(tmp_path):
    course, changes, messages = course_with_changes(tmp_path)
    archive = course.archive()
    edited, deleted, changed = apply(archive, course.lesson.id, [operation("delete_message", node_id=messages[1])])
    assert changed and len(deleted) == 1
    assert edited.nodes[f"{course.lesson.id}/{changes[1]}"].source_message_deleted
    assert changes[1] in branch_sequence(edited, course.lesson.id)
    assert edited.nodes[f"{course.lesson.id}/{messages[2]}"].assistant_message == archive.nodes[f"{course.lesson.id}/{messages[2]}"].assistant_message
    assert applied_branch_replay(edited, course.lesson.id)["document"] == document("A3")
    with pytest.raises(RidocV3Error, match="complete RIDOC M"):
        apply(archive, course.lesson.id, [operation("delete_message", node_id=changes[1])])


def test_legacy_e_blocks_only_operations_that_require_strict_replay(tmp_path):
    course = Course(tmp_path)
    first, target = course.chat("first"), course.chat("target")
    course.branch("other", first)
    source = course.chat("source")
    course.switch("main")
    archive = legacy_linear(course.archive(), "3.1")
    graph = archive.package.lessons[course.lesson.id]
    event = RidocMergeEventNode(
        id="old-merge", lesson_id=course.lesson.id, branch_name="main",
        parent_ids=[target, source], transaction_id="old-merge", created_at=CREATED_AT,
        document_ref=archive.nodes[f"{course.lesson.id}/{target}"].document_ref,
        merge_source_branch="other",
    )
    archive.nodes[f"{course.lesson.id}/{event.id}"] = event
    graph.node_order.append(event.id)
    graph.branches["main"].head_node_id = event.id
    validate_ridoc_v3(archive)
    base = deepcopy(archive)
    checkout, _, changed = apply(archive, course.lesson.id, [operation("checkout_branch", name="other")])
    assert changed and checkout.package.lessons[course.lesson.id].current_branch == "other"
    created, _, changed = apply(archive, course.lesson.id, [operation("create_branch", name="legacy-copy")])
    assert changed and created.package.lessons[course.lesson.id].branches["legacy-copy"].head_node_id == event.id
    deleted, items, changed = apply(archive, course.lesson.id, [operation("delete_message", node_id=target)])
    assert changed and items and f"{course.lesson.id}/{target}" not in deleted.nodes
    with pytest.raises(RidocV3Error, match="legacy E"):
        apply(archive, course.lesson.id, [operation("step_document", branch_name="main", direction="undo", target_node_id=first)])
    with pytest.raises(RidocV3Error, match="legacy E"):
        apply(archive, course.lesson.id, [operation("reorder_messages", branch_name="main", ordered_node_ids=branch_sequence(archive, course.lesson.id))])
    with pytest.raises(RidocV3Error, match="legacy E"):
        merge_edit_plan(archive, course.lesson.id, "other")
    assert archive == base


def test_undo_state_rejects_merge_until_restored_in_same_operation_list(tmp_path):
    course, changes, _ = course_with_changes(tmp_path)
    course.branch("other")
    course.chat("additional")
    course.switch("main")
    archive = course.archive()
    undo = operation("step_document", "undo", branch_name="main", direction="undo", target_node_id=changes[-1])
    merge = operation("merge_branch", source_branch_name="other", target_branch_name="main")
    with pytest.raises(RidocV3Error, match="undone"):
        apply(archive, course.lesson.id, [undo, merge])
    redo = operation("step_document", "redo", branch_name="main", direction="redo", target_node_id=changes[-1])
    edited, deleted, changed = apply(archive, course.lesson.id, [undo, redo, merge])
    assert changed and not deleted and edited.nodes == archive.nodes
    assert document_history_state(edited, course.lesson.id)["withdrawn_node_ids"] == []
