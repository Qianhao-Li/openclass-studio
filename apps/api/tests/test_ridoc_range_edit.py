from copy import deepcopy

import pytest

from openclass_core.models import BoardDocument, CoursePackage
from openclass_core.services.lesson_factory import create_empty_lesson
from openclass_core.services.rich_document import rebuild_document_from_content_json
from openclass_core.services.ridoc_edit_operations import pending_edit_draft
from openclass_core.services import ridoc_edit_sessions as sessions
from openclass_core.services.ridoc_range_edit import range_actions_view
from openclass_core.services.ridoc_v3 import RidocV3Error, read_ridoc_v3, validate_ridoc_v3, write_ridoc_v3
from openclass_core.services.ridoc_workspace_store import RidocWorkspaceStore, materialize_course_package


def board(*paragraphs):
    return rebuild_document_from_content_json(BoardDocument(id="board", title="Ranges"), {
        "type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]} for text in paragraphs],
    })


def range_course(tmp_path):
    store = RidocWorkspaceStore(tmp_path / "ridoc", tmp_path / "runtime.sqlite3")
    lesson = create_empty_lesson("Ranges")
    lesson.board_document = board("abc", "def", "ghi")
    package = CoursePackage(title="Ranges", summary="", lessons=[lesson], active_lesson_id=lesson.id)
    record = store.create_from_package("owner", package)
    ids = []
    scope = {"owner_user_id": "owner", "package_id": package.id, "lesson_id": lesson.id}
    for index, values in enumerate([("ABC", "def", "ghi"), ("ABC", "DEF", "ghi"), ("ABC", "DEF", "GHI"),
                                   ("JKL", "MNO", "GHI"), ("JKL", "PQR", "STU")], 1):
        record = store.commit_document(**scope, expected_revision=record.revision,
                                      transaction_id=f"change-{index}", document=board(*values))
        archive = store.load_archive("owner", package_id=package.id)
        ids.append(archive.package.lessons[lesson.id].branches["main"].head_node_id)
    return store, scope, ids


def action(identity, targets, direction="undo"):
    return {"operation_id": identity, "operation": {"kind": "step_document_actions", "branch_name": "main",
                                                      "direction": direction, "target_node_ids": targets}}


def draft(archive, lesson, operations):
    return pending_edit_draft(archive, lesson, operations, session_id="ranges", created_at="2026-09-11T20:00:00+08:00")


def test_c5_then_c3_keeps_c4_and_original_facts(tmp_path):
    store, scope, ids = range_course(tmp_path)
    base = store.load_archive("owner", package_id=scope["package_id"])
    original = deepcopy(base)
    rows = {row["id"]: row for row in range_actions_view(base, scope["lesson_id"], "main")}
    assert rows[ids[2]]["blockers"] == [ids[4]]
    with pytest.raises(RidocV3Error):
        draft(base, scope["lesson_id"], [action("bad", [ids[2]])])
    display, effective, removed, affected, changed = draft(base, scope["lesson_id"], [action("five", [ids[4]]), action("three", [ids[2]])])
    assert changed and affected == [scope["lesson_id"]]
    assert {item["node_id"] for item in removed} == {ids[2], ids[4]}
    assert materialize_course_package(effective).lessons[0].board_document.content_text == board("JKL", "MNO", "ghi").content_text
    assert base == original
    assert display.nodes[f"{scope['lesson_id']}/{ids[3]}"].model_dump() == base.nodes[f"{scope['lesson_id']}/{ids[3]}"].model_dump()
    output = tmp_path / "result.ridoc"
    write_ridoc_v3(effective, output)
    reopened = read_ridoc_v3(output)
    assert reopened.manifest["spec_version"] == "3.5"
    assert materialize_course_package(reopened).lessons[0].board_document.content_text == board("JKL", "MNO", "ghi").content_text
    validate_ridoc_v3(reopened)


def test_batch_only_exposed_actions_and_noop_restore(tmp_path):
    store, scope, ids = range_course(tmp_path)
    base = store.load_archive("owner", package_id=scope["package_id"])
    one = action("five", [ids[4]])
    with pytest.raises(RidocV3Error):
        draft(base, scope["lesson_id"], [one, action("invalid-batch", [ids[1], ids[2]])])
    display, effective, removed, _, _ = draft(base, scope["lesson_id"], [one, action("two-leaves", [ids[2], ids[3]])])
    assert {item["node_id"] for item in removed} == {ids[2], ids[3], ids[4]}
    assert materialize_course_package(effective).lessons[0].board_document.content_text == board("ABC", "DEF", "ghi").content_text
    operations = [one, action("three", [ids[2]]), action("restore-three", [ids[2]], "redo"), action("restore-five", [ids[4]], "redo")]
    display, effective, removed, affected, changed = draft(base, scope["lesson_id"], operations)
    assert not changed and not removed and not affected and effective is base


def test_durable_range_draft_pause_commit_and_restart(tmp_path):
    store, scope, ids = range_course(tmp_path)
    path = store.package_path("owner", scope["package_id"])
    original = path.read_bytes()
    archive = store.load_archive("owner", package_id=scope["package_id"])
    view = sessions.begin_edit_session(store, **scope, expected_revision=archive.revision.value)
    for index in [4, 2]:
        impact = sessions.preview_edit_removal(store, **scope, session_id=view["id"], expected_draft_version=view["version"], node_ids=[ids[index]])
        view = sessions.update_edit_session(store, **scope, session_id=view["id"], expected_draft_version=view["version"],
                                            operation_id=f"remove-{index}", operation=action("unused", [ids[index]])["operation"], impact_token=impact["impact_token"])
        assert path.read_bytes() == original
    sessions.pause_edit_session(store, **scope, session_id=view["id"], expected_draft_version=view["version"])
    assert path.read_bytes() == original
    store = RidocWorkspaceStore(store.root, store.database_path)
    recovered = sessions.get_active_edit_session(store, **scope)
    assert recovered["pending"] and recovered["version"] == view["version"]
    review = sessions.review_edit_session(store, **scope, session_id=view["id"], expected_draft_version=view["version"])
    result = sessions.commit_edit_session(store, **scope, session_id=view["id"], expected_draft_version=view["version"],
                                         mutation_id="save-ranges", confirmation_token=review["confirmation_token"])
    assert result["changed"] and path.read_bytes() != original
    package = store.load_package("owner", package_id=scope["package_id"])
    assert package.lessons[0].board_document.content_text == board("JKL", "MNO", "ghi").content_text
