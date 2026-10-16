from copy import deepcopy

import pytest

from openclass_core.services.ridoc_document_actions import DocumentActionBlocked
from openclass_core.services.ridoc_edit_operations import pending_edit_draft
from openclass_core.services.ridoc_range_history import build_range_history, replay_range_proof
from openclass_core.services.ridoc_v3 import read_ridoc_v3, write_ridoc_v3, validate_ridoc_v3
from openclass_core.services.ridoc_v34 import upgrade_archive
from openclass_core.services.ridoc_workspace_store import materialize_course_package
from test_ridoc_learning_sequence import Course, document
from test_ridoc_range_history import doc, records


def test_rehashed_source_ids_cannot_move_a_unique_edit_to_equal_text_elsewhere():
    before, after = doc("abc abc"), doc("xyz abc")
    result = build_range_history(before, records(before, after))
    occurrence = result["occurrences"][0]
    proof = deepcopy(occurrence["proof"])
    assert not proof["action"]["ambiguities"]
    key = proof["source_trace"]["block_order"][0]
    source = proof["source_trace"]["blocks"][key]
    # Both source and current still contain exactly the same text. The forged
    # IDs would previously send the change to the second 'abc', producing
    # 'abc xyz'. Rehashing the envelope must not authorize that relocation.
    for left, right in [(0, 4), (1, 5), (2, 6)]:
        source["tokens"][left], source["tokens"][right] = source["tokens"][right], source["tokens"][left]
    for left, right in [(1, 5), (2, 6)]:
        source["gaps"][left], source["gaps"][right] = source["gaps"][right], source["gaps"][left]
    original = deepcopy(occurrence["input_trace"])
    with pytest.raises(DocumentActionBlocked, match="unique alignment"):
        replay_range_proof(proof, before, after, before, occurrence["input_trace"])
    assert occurrence["input_trace"] == original


def test_selective_undo_of_merged_source_uses_verified_actual_occurrence(tmp_path):
    course = Course(tmp_path)
    c0 = f"c0_{course.lesson.id}"
    target = course.write("commit_document", transaction_id="target", document=document(b="B target"))
    course.branch("extra", c0)
    source = course.write("commit_document", transaction_id="source", document=document(a="A source"))
    course.switch("main")
    course.merge()
    archive = course.archive()
    upgrade_archive(archive)
    original_target = deepcopy(archive.nodes[f"{course.lesson.id}/{target}"])
    operation = {"operation_id": "remove-source", "operation": {
        "kind": "step_document_actions", "branch_name": "main", "direction": "undo", "target_node_ids": [source]}}
    _display, effective, removed, _affected, changed = pending_edit_draft(
        archive, course.lesson.id, [operation], session_id="merged-ranges", created_at="2026-09-11T20:00:00+08:00")
    assert changed and [item["node_id"] for item in removed] == [source]
    assert materialize_course_package(effective).lessons[0].board_document == document(b="B target")
    assert effective.nodes[f"{course.lesson.id}/{target}"].model_dump() == original_target.model_dump()
    occurrence = effective.package.lessons[course.lesson.id].branches["main"].document_occurrences[target]
    proof = effective.range_proofs[occurrence.range_proof_ref]
    saved_source = proof["source_occurrence"]
    source_after = effective.document_states[saved_source["after_state_ref"]]
    assert source_after.document_ref != original_target.after_document_ref
    validate_ridoc_v3(effective)
    path = tmp_path / "merged-selective.ridoc"
    write_ridoc_v3(effective, path)
    reopened = read_ridoc_v3(path)
    assert materialize_course_package(reopened).lessons[0].board_document == document(b="B target")
