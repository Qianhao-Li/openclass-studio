from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from openclass_core.models import CoursePackage
from openclass_core.services.lesson_factory import create_empty_lesson
from openclass_core.services.rich_document import build_document
from openclass_core.services.ridoc_history_edit import delete_message_node, preview_message_deletion
from openclass_core.services.ridoc_v3 import (
    RidocBranch,
    RidocDocumentNode,
    RidocMergeEventNode,
    RidocMessageNode,
    RidocV3Error,
    content_hash,
    merge_parent_roles,
    new_ridoc_v3,
    read_ridoc_v3,
    ridoc_revision_for_archive,
    store_document,
    validate_ridoc_v3,
    write_ridoc_v3,
)


def _archive():
    lessons = [create_empty_lesson("Lesson"), create_empty_lesson("Other lesson")]
    return new_ridoc_v3(CoursePackage(title="Course", summary="", lessons=lessons)), lessons[0].id


def _node(archive, lesson_id, node_id):
    return archive.nodes[f"{lesson_id}/{node_id}"]


def _doc(node):
    return node.after_document_ref if isinstance(node, RidocDocumentNode) else node.document_ref


def _append(archive, lesson_id, node_id, *, branch="main", parent=None, change=False, transaction=None):
    graph = archive.package.lessons[lesson_id]
    parent = parent or graph.branches[branch].head_node_id
    ref = _doc(_node(archive, lesson_id, parent))
    common = dict(
        id=node_id, lesson_id=lesson_id, branch_name=branch, parent_ids=[parent],
        created_at="2026-09-05T00:00:00+00:00", transaction_id=transaction or f"turn:{node_id}",
    )
    if change:
        node = RidocDocumentNode(
            **common, before_document_ref=ref, action_id=f"action:{node_id}", source="ai",
            after_document_ref=store_document(archive.documents, build_document(
                title="Board", content_text=f"# Content\n\n{node_id}.", document_id="board",
            )),
        )
    else:
        node = RidocMessageNode(**common, document_ref=ref, user_message=f"Question {node_id}", assistant_message=f"Answer {node_id}")
    archive.nodes[f"{lesson_id}/{node_id}"] = node
    graph.node_order.append(node_id)
    graph.branches[branch].head_node_id = node_id
    return node


def _branch(archive, lesson_id, name, node_id):
    archive.package.lessons[lesson_id].branches[name] = RidocBranch(name=name, head_node_id=node_id, base_node_id=node_id)


@pytest.mark.parametrize("deleted", ["hello", "weather", "name"])
def test_delete_one_complete_exchange_and_round_trip(tmp_path: Path, deleted: str):
    archive, lesson_id = _archive()
    for node_id in ["hello", "weather", "name"]:
        _append(archive, lesson_id, node_id)
    original_nodes = deepcopy(archive.nodes)
    path = tmp_path / "course.ridoc"
    before_revision = write_ridoc_v3(archive, path)
    preview = preview_message_deletion(archive, lesson_id, deleted)
    assert preview["ridoc_revision"] == before_revision.value
    assert preview["affected_branches"] == ["main"]
    assert archive.manifest["spec_version"] == "3.0"
    result = delete_message_node(archive, lesson_id, deleted)
    assert result["deleted_transaction_id"] == f"turn:{deleted}"
    assert f"{lesson_id}/{deleted}" not in archive.nodes
    assert archive.manifest["spec_version"] == "3.1"
    remaining = [node_id for node_id in ["hello", "weather", "name"] if node_id != deleted]
    graph = archive.package.lessons[lesson_id]
    assert graph.node_order[1:] == remaining
    assert graph.branches["main"].head_node_id == remaining[-1]
    for key, node in archive.nodes.items():
        if isinstance(node, RidocMessageNode):
            assert node.user_message == original_nodes[key].user_message
            assert node.assistant_message == original_nodes[key].assistant_message
    revision = write_ridoc_v3(archive, path, expected_revision=before_revision.value)
    assert revision.value == ridoc_revision_for_archive(archive)
    restored = read_ridoc_v3(path)
    assert restored.nodes == archive.nodes
    assert restored.package == archive.package


def test_delete_only_message_retains_c0_and_all_branches():
    archive, lesson_id = _archive()
    _append(archive, lesson_id, "only")
    _branch(archive, lesson_id, "saved", "only")
    graph = archive.package.lessons[lesson_id]
    graph.current_branch = "saved"
    delete_message_node(archive, lesson_id, "only")
    graph = archive.package.lessons[lesson_id]
    assert len(graph.node_order) == 1
    assert graph.current_branch == "saved"
    assert list(graph.branches) == ["main", "saved"]
    assert all(branch.head_node_id == branch.base_node_id == graph.node_order[0] for branch in graph.branches.values())


def test_shared_ancestor_and_origin_references_are_repaired_without_changing_content():
    archive, lesson_id = _archive()
    original = _append(archive, lesson_id, "shared")
    _branch(archive, lesson_id, "fork", original.id)
    _append(archive, lesson_id, "main-next")
    fork = _append(archive, lesson_id, "fork-next", branch="fork")
    fork.branch_origin_kind = "fork"
    fork.branch_origin_node_id = original.id
    fork.decision_metadata = {"history_branch_source_node_id": original.id, "learner_note": original.id}
    preview = preview_message_deletion(archive, lesson_id, original.id)
    assert preview["affected_branches"] == ["main", "fork"]
    delete_message_node(archive, lesson_id, original.id)
    fork = _node(archive, lesson_id, fork.id)
    assert fork.branch_origin_deleted is True
    assert fork.branch_origin_node_id == ""
    assert fork.decision_metadata == {"learner_note": original.id}
    assert fork.parent_ids == original.parent_ids
    assert _node(archive, lesson_id, "main-next").parent_ids == original.parent_ids


def test_retains_same_turn_c_with_original_documents_and_identity():
    archive, lesson_id = _archive()
    message = _append(archive, lesson_id, "m")
    change = _append(archive, lesson_id, "c", change=True, transaction=message.transaction_id)
    _append(archive, lesson_id, "later")
    original_change = change.model_dump()
    original_documents = deepcopy(archive.documents)
    result = delete_message_node(archive, lesson_id, "m")
    assert result["retained_document_node_ids"] == ["c"]
    actual = _node(archive, lesson_id, "c").model_dump()
    assert actual.pop("source_message_deleted") is True
    original_change.pop("source_message_deleted")
    actual.pop("parent_ids")
    original_change.pop("parent_ids")
    assert actual == original_change
    assert archive.documents == original_documents
    assert _node(archive, lesson_id, "later").document_ref == change.after_document_ref


@pytest.mark.parametrize("changes_document", [False, True])
def test_merge_roles_can_collapse_then_survive_read_and_playback(tmp_path: Path, changes_document: bool):
    archive, lesson_id = _archive()
    root = archive.package.lessons[lesson_id].node_order[0]
    left = _append(archive, lesson_id, "left")
    _branch(archive, lesson_id, "side", root)
    right = _append(archive, lesson_id, "right", branch="side")
    common = dict(
        id="merge", lesson_id=lesson_id, branch_name="main", parent_ids=[left.id, right.id],
        created_at="2026-09-05T01:00:00+00:00", transaction_id="merge-tx", merge_source_branch="side",
        decision_metadata={"merge_base_commit_id": root, "merge_source_head_commit_id": right.id},
    )
    if changes_document:
        merged_ref = store_document(archive.documents, build_document(title="Merged", content_text="Merged content", document_id="merged"))
        merge = RidocDocumentNode(**common, source="merge", before_document_ref=left.document_ref, after_document_ref=merged_ref, action_id="merge-action")
    else:
        merge = RidocMergeEventNode(**common, document_ref=left.document_ref)
    archive.nodes[f"{lesson_id}/merge"] = merge
    graph = archive.package.lessons[lesson_id]
    graph.node_order.append("merge")
    graph.branches["main"].head_node_id = "merge"
    delete_message_node(archive, lesson_id, "left")
    delete_message_node(archive, lesson_id, "right")
    merge = _node(archive, lesson_id, "merge")
    assert merge.parent_ids == [root]
    assert merge_parent_roles(merge) == (root, root)
    assert merge.decision_metadata["merge_source_head_commit_id"] == root
    path = tmp_path / "merge.ridoc"
    write_ridoc_v3(archive, path)
    restored = read_ridoc_v3(path)
    assert merge_parent_roles(_node(restored, lesson_id, "merge")) == (root, root)
    from openclass_core.services.ridoc_history import history_page, history_document
    page = history_page(restored, lesson_id, scope="all")
    assert page.nodes[-1].id == "merge"
    assert history_document(restored, lesson_id, "merge").document.model_dump(mode="json") == archive.documents[_doc(merge)]


def test_pruning_respects_other_lessons_and_retained_document_nodes():
    archive, lesson_id = _archive()
    message = _append(archive, lesson_id, "m")
    change = _append(archive, lesson_id, "c", change=True, transaction=message.transaction_id)
    other_lesson = archive.package.lesson_order[1]
    other = _append(archive, other_lesson, "other")
    assets = {name: content_hash(name.encode()) for name in ["exclusive", "shared", "board"]}
    archive.assets = {digest: name.encode() for name, digest in assets.items()}
    archive.evidence = {name: {"title": name} for name in assets}
    message.resource_refs = list(assets.values())
    message.evidence_ids = list(assets)
    other.resource_refs = [assets["shared"]]
    other.evidence_ids = ["shared"]
    change.resource_refs = [assets["board"]]
    change.evidence_ids = ["board"]
    unused = store_document(archive.documents, build_document(title="Unused", content_text="Unused", document_id="unused"))
    delete_message_node(archive, lesson_id, "m")
    assert set(archive.assets) == {assets["shared"], assets["board"]}
    assert set(archive.evidence) == {"shared", "board"}
    assert unused not in archive.documents


@pytest.mark.parametrize("invalid", ["multiparent", "document", "empty", "origin"])
def test_invalid_input_fails_before_any_mutation(invalid: str):
    archive, lesson_id = _archive()
    message = _append(archive, lesson_id, "m")
    if invalid == "multiparent":
        message.parent_ids *= 2
    elif invalid == "document":
        message.document_ref = store_document(archive.documents, build_document(title="Wrong", content_text="Wrong", document_id="wrong"))
    elif invalid == "empty":
        message.assistant_message = " "
    else:
        message.branch_origin_node_id = "missing"
    original = deepcopy(archive)
    with pytest.raises(RidocV3Error):
        delete_message_node(archive, lesson_id, "m")
    assert archive == original


def test_non_message_and_missing_nodes_are_not_deletable():
    archive, lesson_id = _archive()
    for node_id in [archive.package.lessons[lesson_id].node_order[0], "missing"]:
        original = deepcopy(archive)
        with pytest.raises(RidocV3Error):
            delete_message_node(archive, lesson_id, node_id)
        assert archive == original


def test_history_edit_fields_are_rejected_in_30():
    archive, lesson_id = _archive()
    _append(archive, lesson_id, "m")
    _append(archive, lesson_id, "c", change=True, transaction="turn:m")
    delete_message_node(archive, lesson_id, "m")
    archive.manifest["spec_version"] = "3.0"
    with pytest.raises(RidocV3Error, match="3.1"):
        validate_ridoc_v3(archive)
