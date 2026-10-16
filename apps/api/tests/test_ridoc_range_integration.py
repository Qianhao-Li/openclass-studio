from __future__ import annotations

import importlib.util
from pathlib import Path

from openclass_core.services.lesson_factory import create_empty_lesson
from openclass_core.services.ridoc_v3 import validate_ridoc_v3
from openclass_core.services.ridoc_v34 import archive_state
from test_ridoc_range_edit import action, board, draft, range_course


def test_shared_other_branch_keeps_final_document_after_selective_deletion(tmp_path):
    store, scope, ids = range_course(tmp_path)
    before = store.load_archive("owner", package_id=scope["package_id"])
    record = store.create_branch(**scope, name="shared", expected_revision=before.revision.value)
    record = store.switch_branch(**scope, name="main", expected_revision=record.revision)
    before = store.load_archive("owner", package_id=scope["package_id"])
    expected = archive_state(before, scope["lesson_id"], "shared")["document"]
    _display, effective, _removed, _affected, _changed = draft(before, scope["lesson_id"], [action("five", [ids[4]]), action("three", [ids[2]])])
    assert archive_state(effective, scope["lesson_id"], "main")["document"].content_text == board("JKL", "MNO", "ghi").content_text
    assert archive_state(effective, scope["lesson_id"], "shared")["document"] == expected
    validate_ridoc_v3(effective)


def test_create_branch_from_selective_preview_inherits_composed_document(tmp_path):
    store, scope, ids = range_course(tmp_path)
    before = store.load_archive("owner", package_id=scope["package_id"])
    operations = [action("five", [ids[4]]), action("three", [ids[2]]),
                  {"operation_id": "fork-preview", "operation": {"kind": "create_branch", "name": "preview"}}]
    _display, effective, _removed, _affected, _changed = draft(before, scope["lesson_id"], operations)
    assert effective.package.lessons[scope["lesson_id"]].current_branch == "preview"
    assert archive_state(effective, scope["lesson_id"], "preview")["document"].content_text == board("JKL", "MNO", "ghi").content_text
    assert not set(ids[2::2]) & set(effective.package.lessons[scope["lesson_id"]].branches["preview"].learning_node_ids)
    validate_ridoc_v3(effective)


def test_create_branch_from_retained_other_branch_keeps_independent_final_state(tmp_path):
    store, scope, ids = range_course(tmp_path)
    before = store.load_archive("owner", package_id=scope["package_id"])
    record = store.create_branch(**scope, name="shared", expected_revision=before.revision.value)
    record = store.switch_branch(**scope, name="main", expected_revision=record.revision)
    before = store.load_archive("owner", package_id=scope["package_id"])
    _display, effective, *_ = draft(before, scope["lesson_id"], [action("five", [ids[4]])])
    effective.package.lessons[scope["lesson_id"]].current_branch = "shared"
    expected = archive_state(effective, scope["lesson_id"], "shared")["document"]
    _display, forked, *_ = draft(effective, scope["lesson_id"], [{"operation_id": "fork-saved", "operation": {"kind": "create_branch", "name": "forked"}}])
    assert archive_state(forked, scope["lesson_id"], "forked")["document"] == expected


def test_selective_changes_in_two_lessons_stay_separate(tmp_path):
    store, scope, ids = range_course(tmp_path)
    current = store.load_archive("owner", package_id=scope["package_id"])
    second = create_empty_lesson("Other lesson")
    second.board_document = board("second original")
    record = store.add_lesson(owner_user_id="owner", package_id=scope["package_id"],
                              expected_revision=current.revision.value, lesson=second)
    record = store.commit_document(owner_user_id="owner", package_id=scope["package_id"], lesson_id=second.id,
                                   expected_revision=record.revision, transaction_id="other-change", document=board("second edited"))
    before = store.load_archive("owner", package_id=scope["package_id"])
    node = before.package.lessons[second.id].branches["main"].head_node_id
    operations = [action("five", [ids[4]]), action("three", [ids[2]]),
                  {**action("other-remove", [node]), "lesson_id": second.id}]
    _display, effective, removed, affected, _changed = draft(before, scope["lesson_id"], operations)
    assert set(affected) == {scope["lesson_id"], second.id}
    assert {(item["lesson_id"], item["node_id"]) for item in removed} == {(scope["lesson_id"], ids[4]), (scope["lesson_id"], ids[2]), (second.id, node)}
    assert archive_state(effective, second.id)["document"].content_text == "second original"
    assert archive_state(effective, scope["lesson_id"])["document"].content_text == board("JKL", "MNO", "ghi").content_text
    validate_ridoc_v3(effective)


def test_audit_is_read_only_and_does_not_include_document_text(tmp_path):
    store, scope, _ids = range_course(tmp_path)
    path = store.package_path("owner", scope["package_id"])
    original = path.read_bytes()
    spec = importlib.util.spec_from_file_location("audit_document_ranges", Path(__file__).resolve().parents[3] / "scripts/audit-document-ranges.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.audit_file(path)
    assert result["status"] == "ready" and result["upgrade_dry_run"]["status"] == "passed"
    assert result["counts"]["document_actions"] == 5
    assert path.read_bytes() == original
    assert all("changes" not in action for branch in result["branches"] for action in branch["actions"])
