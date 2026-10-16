from __future__ import annotations

from copy import deepcopy

import pytest

from openclass_core.services.ridoc_document_cursor import (
    applied_branch_replay, applied_branch_sequence, document_history_state,
)
from openclass_core.services.ridoc_learning_sequence import branch_sequence, build_merge_plan, set_branch_sequence
from openclass_core.services.ridoc_v3 import (
    RidocMessageNode, RidocV3ConflictError, RidocV3Error,
    read_ridoc_v3, validate_ridoc_v3,
)
from test_ridoc_learning_sequence import Course, document


def position(course, node_id, branch="main"):
    def update(archive):
        archive.manifest["spec_version"] = "3.3"
        archive.package.lessons[course.lesson.id].branches[branch].document_cursor_node_id = node_id
    course.record = course.store.mutate("owner", course.package.id, course.record.revision, update)


def three_changes(tmp_path):
    course = Course(tmp_path)
    changes = [course.chat(f"turn-{index}", document(f"A{index}")) for index in range(1, 4)]
    return course, changes


def test_latest_first_cursor_keeps_all_original_facts_and_round_trips(tmp_path):
    course, changes = three_changes(tmp_path)
    original = course.archive()
    sequence = branch_sequence(original, course.lesson.id)
    original_branch = deepcopy(original.package.lessons[course.lesson.id].branches["main"])
    c0 = sequence[0]
    assert document_history_state(original, course.lesson.id) == {
        "current_document_node_id": changes[2], "undo_node_id": changes[2],
        "redo_node_id": None, "withdrawn_node_ids": [],
    }
    for cursor, count in ((changes[1], 1), (changes[0], 2), (c0, 3)):
        position(course, cursor)
        archive = course.archive()
        state = document_history_state(archive, course.lesson.id)
        assert state["current_document_node_id"] == cursor
        assert state["withdrawn_node_ids"] == changes[-count:]
        assert state["redo_node_id"] == changes[-count]
        assert state["undo_node_id"] == (None if cursor == c0 else cursor)
        assert archive.nodes == original.nodes
        branch = archive.package.lessons[course.lesson.id].branches["main"]
        assert branch.head_node_id == original_branch.head_node_id
        assert branch.learning_node_ids == original_branch.learning_node_ids
        assert branch.replay_document_refs == original_branch.replay_document_refs
        loaded = course.store.load_package("owner", package_id=course.package.id).lessons[0]
        assert loaded.board_document == applied_branch_replay(archive, course.lesson.id)["document"]
        assert loaded.history_graph.branches["main"].document_cursor_commit_id == cursor
        assert loaded.history_graph.branches["main"].document_redo_commit_ids == changes[-count:]
        assert branch_sequence(archive, course.lesson.id) == sequence
    for cursor in (changes[0], changes[1], None):
        position(course, cursor)
    assert course.store.load_package("owner", package_id=course.package.id).lessons[0].board_document == document("A3")
    assert course.archive().manifest["spec_version"] == "3.3"


def test_m_reorder_never_changes_document_order_or_cursor(tmp_path):
    course, changes = three_changes(tmp_path)
    position(course, changes[0])
    archive = course.archive()
    before = document_history_state(archive, course.lesson.id)
    sequence = branch_sequence(archive, course.lesson.id)
    messages = [identity for identity in sequence if isinstance(archive.nodes[f"{course.lesson.id}/{identity}"], RidocMessageNode)]
    reordered = [sequence[0], *reversed(messages), *changes]
    set_branch_sequence(archive, course.lesson.id, "main", reordered)
    validate_ridoc_v3(archive)
    assert archive.manifest["spec_version"] == "3.3"
    assert document_history_state(archive, course.lesson.id) == before
    assert [identity for identity in applied_branch_sequence(archive, course.lesson.id) if identity in messages] == list(reversed(messages))


@pytest.mark.parametrize("outcome", ["chat", "chat-and-document", "manual"])
def test_successful_write_after_undo_forks_applied_state_without_changing_source(tmp_path, outcome):
    course, changes = three_changes(tmp_path)
    position(course, changes[0])
    before = course.archive()
    source = deepcopy(before.package.lessons[course.lesson.id].branches["main"])
    expected_input = applied_branch_sequence(before, course.lesson.id)
    if outcome == "manual":
        head = course.write("commit_document", transaction_id="new-edit", document=document("new"))
    else:
        head = course.chat("new-question", document("new") if outcome == "chat-and-document" else None)
    archive = course.archive()
    graph = archive.package.lessons[course.lesson.id]
    assert graph.current_branch != "main"
    assert graph.branches["main"] == source
    assert all(archive.nodes[key] == value for key, value in before.nodes.items())
    assert graph.branches[graph.current_branch].document_cursor_node_id is None
    sequence = branch_sequence(archive, course.lesson.id)
    assert sequence[:len(expected_input)] == expected_input
    assert not set(changes[1:]) & set(sequence)
    assert archive.nodes[f"{course.lesson.id}/{head}"].learning_context_node_ids == expected_input
    expected = document("A1") if outcome == "chat" else document("new")
    assert course.store.load_package("owner", package_id=course.package.id).lessons[0].board_document == expected
    validate_ridoc_v3(archive)


def test_empty_answer_and_noop_edit_do_not_create_branch_or_revision(tmp_path):
    course, changes = three_changes(tmp_path)
    position(course, changes[0])
    path = course.store.package_path("owner", course.package.id)
    before = path.read_bytes()
    revision = course.record.revision
    with pytest.raises(RidocV3Error, match="complete M"):
        course.write("commit_turn", transaction_id="empty", user_message="question", assistant_message=" ")
    assert path.read_bytes() == before
    course.write("commit_document", transaction_id="unchanged", document=document("A1"))
    assert course.record.revision == revision
    assert len(course.archive().package.lessons[course.lesson.id].branches) == 1


def test_failed_or_repeated_save_never_adds_extra_divergence_branches(tmp_path, monkeypatch):
    from openclass_core.services import ridoc_workspace_store
    course, changes = three_changes(tmp_path)
    position(course, changes[0])
    path = course.store.package_path("owner", course.package.id)
    before = path.read_bytes()
    with monkeypatch.context() as patch:
        def fail_write(*args, **kwargs):
            raise OSError("injected write failure")
        patch.setattr(ridoc_workspace_store, "write_ridoc_v3", fail_write)
        with pytest.raises(OSError, match="injected"):
            course.chat("continue")
    assert path.read_bytes() == before
    assert len(course.archive().package.lessons[course.lesson.id].branches) == 1
    course.chat("continue")
    committed = course.archive()
    course.chat("continue")
    assert course.archive().revision == committed.revision
    assert course.archive().package.lessons[course.lesson.id].branches == committed.package.lessons[course.lesson.id].branches


def test_undone_original_manual_action_cannot_be_extended(tmp_path):
    course = Course(tmp_path)
    course.write("commit_document", transaction_id="manual", document=document("changed"))
    position(course, f"c0_{course.lesson.id}")
    before = course.archive()
    with pytest.raises(RidocV3ConflictError, match="new editing session"):
        course.write("commit_document", transaction_id="manual", document=document("different"))
    assert course.archive().revision == before.revision
    assert course.archive().nodes == before.nodes


@pytest.mark.parametrize("bad_cursor", ["missing", "message", "foreign", "old-spec"])
def test_invalid_cursor_fails_closed(tmp_path, bad_cursor):
    course, changes = three_changes(tmp_path)
    course.branch("extra", f"c0_{course.lesson.id}")
    foreign = course.chat("other", document("other"))
    course.switch("main")
    archive = course.archive()
    archive.manifest["spec_version"] = "3.2" if bad_cursor == "old-spec" else "3.3"
    cursor = {
        "missing": "missing", "message": archive.nodes[f"{course.lesson.id}/{changes[0]}"].parent_ids[0],
        "foreign": foreign, "old-spec": changes[0],
    }[bad_cursor]
    archive.package.lessons[course.lesson.id].branches["main"].document_cursor_node_id = cursor
    with pytest.raises(RidocV3Error, match="cursor|3.3"):
        validate_ridoc_v3(archive)


def test_merged_document_cursor_uses_branch_replay_not_raw_node_snapshot(tmp_path):
    course = Course(tmp_path)
    c0 = f"c0_{course.lesson.id}"
    target = course.write("commit_document", transaction_id="target", document=document(b="B2"))
    course.branch("extra", c0)
    source = course.write("commit_document", transaction_id="source", document=document(a="A2"))
    course.switch("main")
    course.merge()
    original = course.archive()
    assert original.nodes[f"{course.lesson.id}/{target}"].after_document_ref != original.package.lessons[course.lesson.id].branches["main"].replay_document_refs[target]
    position(course, source)
    assert applied_branch_replay(course.archive(), course.lesson.id)["document"] == document("A2")
    position(course, None)
    assert course.store.load_package("owner", package_id=course.package.id).lessons[0].board_document == document("A2", "B2")


@pytest.mark.parametrize("branch", ["main", "extra"])
def test_merge_does_not_silently_restore_source_or_target(tmp_path, branch):
    course, changes = three_changes(tmp_path)
    course.branch("extra")
    course.chat("extra-question")
    course.switch("main")
    position(course, changes[0], branch)
    with pytest.raises(RidocV3Error, match="undone"):
        build_merge_plan(course.archive(), course.lesson.id, "extra")


def test_pre_cursor_format_does_not_gain_cursor_in_serialized_package(tmp_path):
    import json
    import zipfile
    course = Course(tmp_path)
    path = course.store.package_path("owner", course.package.id)
    with zipfile.ZipFile(path) as archive:
        package = json.loads(archive.read("package.json"))
    assert "document_cursor_node_id" not in package["lessons"][course.lesson.id]["branches"]["main"]
    assert read_ridoc_v3(path).package.lessons[course.lesson.id].branches["main"].document_cursor_node_id is None
