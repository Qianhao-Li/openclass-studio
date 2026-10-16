from __future__ import annotations

from copy import deepcopy

import pytest

from openclass_core.services.ridoc_history import history_page
from openclass_core.services.ridoc_learning_sequence import branch_sequence
from openclass_core.services.ridoc_v3 import RidocV3Error, content_hash, read_ridoc_v3, validate_ridoc_v3, write_ridoc_v3
from openclass_core.services.ridoc_v34 import archive_state, reference_impact, remove_nodes, upgrade_archive
from openclass_core.services.ridoc_workspace_store import materialize_course_package
from test_ridoc_learning_sequence import Course, document


def native(course, pending=None):
    def update(archive):
        if pending:
            remove_nodes(archive, {course.lesson.id: pending})
        else:
            upgrade_archive(archive)
    course.record = course.store.mutate("owner", course.package.id, course.record.revision, update)
    return course.archive()


def chain(tmp_path):
    course = Course(tmp_path)
    changes = [course.chat(f"question-{i}", document(f"A{i}")) for i in range(1, 4)]
    return course, changes


def test_c_removal_preserves_later_original_m_and_never_restores_its_board(tmp_path):
    course, changes = chain(tmp_path)
    before = course.archive()
    messages = [before.nodes[f"{course.lesson.id}/{identity}"].parent_ids[0] for identity in changes]
    after = native(course, changes[1:])
    assert after.manifest["spec_version"] == "3.4"
    for identity in messages:
        old, new = before.nodes[f"{course.lesson.id}/{identity}"], after.nodes[f"{course.lesson.id}/{identity}"]
        assert (new.user_message, new.assistant_message, new.document_ref, new.parent_ids, new.learning_context_node_ids) == (
            old.user_message, old.assistant_message, old.document_ref, old.parent_ids, old.learning_context_node_ids)
    assert archive_state(after, course.lesson.id)["document"] == document("A1")
    assert materialize_course_package(after).lessons[0].board_document == document("A1")
    assert after.package.lessons[course.lesson.id].branches["main"].head_node_id == changes[-1]
    assert after.node_refs[f"{course.lesson.id}/{changes[-1]}"].status == "deleted"
    view = history_page(after, course.lesson.id, scope="all")
    m3 = next(node for node in view.nodes if node.id == messages[-1])
    assert any(ref["status"] == "deleted" and ref["node_id"] == changes[1] for ref in m3.node_refs)
    assert changes[1] not in m3.parent_ids
    course.chat("continue after removal")
    course.write("commit_document", transaction_id="fresh-edit", document=document("fresh"))
    assert archive_state(course.archive(), course.lesson.id)["document"] == document("fresh")
    validate_ridoc_v3(course.archive())


def test_retained_c_and_other_branch_keep_original_states_with_deleted_input(tmp_path):
    course = Course(tmp_path)
    first = course.write("commit_document", transaction_id="first", document=document("A1"))
    course.branch("other")
    later = course.write("commit_document", transaction_id="later", document=document("A1", "B2"))
    course.switch("main")
    before = course.archive()
    after = native(course, [first])
    retained = after.nodes[f"{course.lesson.id}/{later}"]
    original = before.nodes[f"{course.lesson.id}/{later}"]
    assert (retained.parent_ids, retained.before_document_ref, retained.after_document_ref, retained.document_action,
            retained.learning_context_node_ids) == (original.parent_ids, original.before_document_ref,
            original.after_document_ref, original.document_action, original.learning_context_node_ids)
    assert archive_state(after, course.lesson.id, "main")["document"] == document()
    assert archive_state(after, course.lesson.id, "other")["document"] == document("A1", "B2")
    course.switch("other")
    course.chat("question after removed producer")
    course.write("commit_document", transaction_id="new", document=document("A1", "B3"))
    assert archive_state(course.archive(), course.lesson.id)["document"] == document("A1", "B3")


def test_upgrade_freezes_actual_merged_occurrence_not_source_snapshot(tmp_path):
    course = Course(tmp_path)
    c0 = f"c0_{course.lesson.id}"
    target = course.write("commit_document", transaction_id="target", document=document(b="B2"))
    course.branch("extra", c0)
    source = course.write("commit_document", transaction_id="source", document=document(a="A2"))
    course.switch("main")
    course.merge()
    original = course.archive()
    expected = original.package.lessons[course.lesson.id].branches["main"].replay_document_refs[target]
    after = native(course, [source])
    occurrence = after.package.lessons[course.lesson.id].branches["main"].document_occurrences[target]
    assert after.document_states[occurrence.after_state_ref].document_ref == expected
    assert expected != original.nodes[f"{course.lesson.id}/{target}"].after_document_ref
    assert archive_state(after, course.lesson.id)["document"] == document("A2", "B2")
    exported = tmp_path / "v34.ridoc"
    write_ridoc_v3(after, exported)
    assert archive_state(read_ridoc_v3(exported), course.lesson.id)["document"] == document("A2", "B2")


def test_impact_includes_every_shared_referrer_branch_before_upgrade(tmp_path):
    course, changes = chain(tmp_path)
    course.branch("shared")
    course.switch("main")
    old = course.archive()
    message = old.nodes[f"{course.lesson.id}/{changes[2]}"].parent_ids[0]
    impact = reference_impact(old, course.lesson.id, changes[1])
    assert {item["branch_name"] for item in impact if item["referrer_id"] == message} == {"main", "shared"}
    assert old.manifest["spec_version"] == "3.2"


def test_retained_state_keeps_assets_and_evidence_after_producer_removal(tmp_path):
    course, changes = chain(tmp_path)
    archive = course.archive()
    asset = b"retained image bytes"
    digest = content_hash(asset)
    archive.assets[digest] = asset
    archive.evidence["evidence"] = {"asset_ref": digest}
    archive.nodes[f"{course.lesson.id}/{changes[0]}"].resource_refs = [digest]
    archive.nodes[f"{course.lesson.id}/{changes[0]}"].evidence_ids = ["evidence"]
    validate_ridoc_v3(archive)
    remove_nodes(archive, {course.lesson.id: changes})
    assert archive_state(archive, course.lesson.id)["document"] == document()
    assert archive.assets[digest] == asset and "evidence" in archive.evidence
    assert any(digest in state.resource_refs for state in archive.document_states.values())
    validate_ridoc_v3(archive)


@pytest.mark.parametrize("tamper", ["unknown-parent", "wrong-status", "bad-state", "missing-state", "missing-image", "cycle", "wrong-occurrence"])
def test_v34_unknown_gaps_hashes_assets_and_live_cycles_fail_closed(tmp_path, tamper):
    course, changes = chain(tmp_path)
    archive = native(course, [changes[1]])
    last = archive.nodes[f"{course.lesson.id}/{changes[-1]}"]
    if tamper == "unknown-parent":
        last.parent_ids = ["unregistered"]
    elif tamper == "wrong-status":
        archive.node_refs[f"{course.lesson.id}/{changes[-1]}"].status = "deleted"
    elif tamper == "bad-state":
        archive.document_states[last.input_state_ref].document_version = changes[-1]
    elif tamper == "missing-state":
        del archive.document_states[last.input_state_ref]
    elif tamper == "missing-image":
        last.resource_refs = ["f" * 64]
    elif tamper == "cycle":
        last.parent_ids = [last.id]
    else:
        occurrence = archive.package.lessons[course.lesson.id].branches["main"].document_occurrences[changes[-1]]
        occurrence.after_state_ref = archive.nodes[f"{course.lesson.id}/{changes[0]}"].output_state_ref
    with pytest.raises((RidocV3Error, ValueError)):
        validate_ridoc_v3(archive)


def test_c0_cannot_be_removed_and_bad_request_never_partially_edits(tmp_path):
    course, changes = chain(tmp_path)
    archive = course.archive()
    before = deepcopy(archive)
    with pytest.raises(RidocV3Error, match="C0"):
        remove_nodes(archive, {course.lesson.id: [changes[-1], f"c0_{course.lesson.id}"]})
    assert archive == before


def test_old_withdrawn_cursor_is_preserved_not_converted_to_deletion(tmp_path):
    course, changes = chain(tmp_path)
    archive = course.archive()
    archive.manifest["spec_version"] = "3.3"
    archive.package.lessons[course.lesson.id].branches["main"].document_cursor_node_id = changes[0]
    upgrade_archive(archive)
    assert all(f"{course.lesson.id}/{identity}" in archive.nodes for identity in changes)
    assert archive_state(archive, course.lesson.id)["document"] == document("A1")
    assert branch_sequence(archive, course.lesson.id)[-1] == changes[-1]
