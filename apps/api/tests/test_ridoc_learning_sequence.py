from __future__ import annotations

import pytest

from openclass_core.models import BoardDocument, CoursePackage
from openclass_core.services.lesson_factory import create_empty_lesson
from openclass_core.services.rich_document import rebuild_document_from_content_json
from openclass_core.services.ridoc_chat_context import authoritative_conversation
from openclass_core.services.ridoc_document_actions import DocumentActionBlocked, apply_document_action, build_document_action
from openclass_core.services.ridoc_history_edit import delete_message_node
from openclass_core.services.ridoc_learning_sequence import branch_sequence, build_merge_plan, replay_sequence, set_branch_sequence
from openclass_core.services.ridoc_v3 import RidocMergeEventNode, RidocMessageNode, RidocV3Error, new_ridoc_v3, read_ridoc_v3, validate_ridoc_v3, write_ridoc_v3
from openclass_core.services.ridoc_workspace_store import RidocWorkspaceStore


def document(a="A", b="B"):
    return rebuild_document_from_content_json(BoardDocument(id="board", title="Board"), {
        "type": "doc", "content": [
            {"type": "paragraph", "attrs": {"blockId": key}, "content": [{"type": "text", "text": value}]}
            for key, value in (("a", a), ("b", b))
        ],
    })


class Course:
    def __init__(self, tmp_path):
        self.store = RidocWorkspaceStore(tmp_path / "ridoc", tmp_path / "runtime.sqlite3")
        self.lesson = create_empty_lesson("Board")
        self.lesson.board_document = document()
        self.package = CoursePackage(title="Order", summary="", lessons=[self.lesson], active_lesson_id=self.lesson.id)
        self.record = self.store.create_from_package("owner", self.package)

    def archive(self):
        return self.store.load_archive("owner", package_id=self.package.id)

    def write(self, method, **kwargs):
        self.record = getattr(self.store, method)(
            owner_user_id="owner", package_id=self.package.id, lesson_id=self.lesson.id,
            expected_revision=self.record.revision, **kwargs,
        )
        return self.archive().package.lessons[self.lesson.id].branches[self.archive().package.lessons[self.lesson.id].current_branch].head_node_id

    def chat(self, label, board=None):
        return self.write("commit_turn", transaction_id=label, user_message=label, assistant_message="answer " + label, document=board)

    def branch(self, name, source=None):
        return self.write("create_branch", name=name, from_node_id=source)

    def switch(self, name):
        self.write("switch_branch", name=name)

    def merge(self, ordered=None, transaction="merge"):
        graph = self.archive().package.lessons[self.lesson.id]
        return self.write("merge_branches", transaction_id=transaction, source_branch_name="extra", target_branch_name="main",
                          expected_source_head_id=graph.branches["extra"].head_node_id,
                          expected_target_head_id=graph.branches["main"].head_node_id,
                          document=document("UNTRUSTED"), label="Merge", ordered_node_ids=ordered)


def forked_course(tmp_path, *, target_document=None):
    course = Course(tmp_path)
    main = [course.chat(str(index)) for index in range(1, 7)]
    if target_document is not None:
        course.write("commit_document", transaction_id="target-edit", document=target_document)
    course.branch("extra", main[2])
    source = [course.chat("3.1"), course.chat("3.2")]
    source_c = course.chat("3.3", document("A expanded"))
    source.append(course.archive().nodes[f"{course.lesson.id}/{source_c}"].parent_ids[0])
    course.switch("main")
    return course, main, source, source_c


def test_default_merge_inserts_nodes_and_preserves_facts_and_effective_board(tmp_path):
    course, main, source, source_c = forked_course(tmp_path)
    before = course.archive()
    old_facts = {key: node.model_dump() for key, node in before.nodes.items()}
    course.merge()
    archive = course.archive()
    expected = [f"c0_{course.lesson.id}", *main[:3], *source, source_c, *main[3:]]
    assert branch_sequence(archive, course.lesson.id) == expected
    assert old_facts == {key: node.model_dump() for key, node in archive.nodes.items()}
    assert archive.manifest["spec_version"] == "3.2"
    loaded = course.store.load_package("owner", package_id=course.package.id).lessons[0]
    assert "A expanded" in loaded.board_document.content_text
    assert loaded.history_graph.branches["main"].learning_order_merged
    assert loaded.history_graph.branches["main"].learning_node_ids == expected
    assert all(node.kind in {"M", "C"} for node in archive.nodes.values())
    with pytest.raises(RidocV3Error, match="already been merged"):
        build_merge_plan(archive, course.lesson.id, "extra")
    course.write("commit_document", transaction_id="after-merge", document=document("A expanded", "B changed"))
    assert "B changed" in course.store.load_package("owner", package_id=course.package.id).lessons[0].board_document.content_text
    course.chat("followup")
    validate_ridoc_v3(course.archive())


def test_manual_m_order_keeps_c_fixed_and_playback_changes_only_on_c(tmp_path):
    course, main, source, source_c = forked_course(tmp_path)
    plan = build_merge_plan(course.archive(), course.lesson.id, "extra")
    reordered = [identity for identity in plan["default_node_ids"] if identity not in source]
    index = reordered.index(main[2])
    reordered[index:index] = source
    course.merge(reordered)
    archive = course.archive()
    replay = replay_sequence(archive, course.lesson.id, reordered)
    assert not replay["blockers"]
    assert [identity for identity in reordered if archive.nodes[f"{course.lesson.id}/{identity}"].kind == "C"] == [f"c0_{course.lesson.id}", source_c]
    for index, identity in enumerate(reordered[1:], 1):
        if archive.nodes[f"{course.lesson.id}/{identity}"].kind == "M":
            assert replay["document_refs"][identity] == replay["document_refs"][reordered[index - 1]]
    assert archive.nodes[f"{course.lesson.id}/{main[-1]}"].document_ref != replay["document_refs"][main[-1]]
    assert [turn.content for turn in authoritative_conversation(archive, course.lesson.id) if turn.role == "user"] == ["3", "4", "5", "6"]


def test_stale_same_object_blocks_whole_merge_and_cannot_be_overridden(tmp_path):
    course, *_ = forked_course(tmp_path, target_document=document("A target"))
    path = course.store.package_path("owner", course.package.id)
    before = path.read_bytes()
    plan = build_merge_plan(course.archive(), course.lesson.id, "extra")
    assert plan["blockers"][0]["object_id"] == "a"
    assert "object version originally targeted by the change no longer exists" in plan["blockers"][0]["reason"]
    with pytest.raises(RidocV3Error, match="object version.*no longer exists"):
        course.merge()
    assert path.read_bytes() == before


def test_different_objects_apply_without_replacing_whole_document(tmp_path):
    course, *_ = forked_course(tmp_path, target_document=document(b="B target"))
    plan = build_merge_plan(course.archive(), course.lesson.id, "extra")
    assert plan["blockers"] == []
    assert "A expanded" in plan["document"].content_text
    assert "B target" in plan["document"].content_text
    course.merge()
    validate_ridoc_v3(course.archive())


def test_legacy_c_cannot_guess_independent_scope(tmp_path):
    course, *_ = forked_course(tmp_path, target_document=document(b="B target"))
    archive = course.archive()
    for node in archive.nodes.values():
        if node.kind == "C":
            node.document_action = {}
    plan = build_merge_plan(archive, course.lesson.id, "extra")
    assert "legacy change has no" in plan["blockers"][0]["reason"]


def test_c_order_cannot_be_dragged_and_partial_selection_cannot_drop_exchanges(tmp_path):
    course, *_ = forked_course(tmp_path, target_document=document(b="B target"))
    archive = course.archive()
    default = build_merge_plan(archive, course.lesson.id, "extra")["default_node_ids"]
    with pytest.raises(RidocV3Error, match="every node exactly once"):
        build_merge_plan(archive, course.lesson.id, "extra", default[:-1])
    c_indices = [index for index, identity in enumerate(default) if archive.nodes[f"{course.lesson.id}/{identity}"].kind == "C" and index]
    default[c_indices[0]], default[c_indices[1]] = default[c_indices[1]], default[c_indices[0]]
    with pytest.raises(RidocV3Error, match="Only the M"):
        build_merge_plan(archive, course.lesson.id, "extra", default)


def test_delete_source_branch_and_m_keeps_linked_c_and_reopen_order(tmp_path):
    course, main, source, source_c = forked_course(tmp_path)
    course.merge()
    course.write("delete_branch", name="extra")
    archive = course.archive()
    assert f"{course.lesson.id}/{source_c}" in archive.nodes
    deleted = source[-1]
    delete_message_node(archive, course.lesson.id, deleted)
    assert archive.nodes[f"{course.lesson.id}/{source_c}"].source_message_deleted
    assert deleted not in branch_sequence(archive, course.lesson.id)
    assert all(deleted not in node.learning_context_node_ids for node in archive.nodes.values())
    destination = tmp_path / "roundtrip.ridoc"
    write_ridoc_v3(archive, destination)
    restored = read_ridoc_v3(destination)
    assert branch_sequence(restored, course.lesson.id) == branch_sequence(archive, course.lesson.id)
    assert "A expanded" in replay_sequence(restored, course.lesson.id, branch_sequence(restored, course.lesson.id))["document"].content_text


def test_object_version_prevents_aba_and_action_is_atomic():
    before, after = document(), document("A next", "B next")
    action = build_document_action(before, after, versions={"a": "original", "b": "original"}, document_version="original", structure_version="original")
    with pytest.raises(DocumentActionBlocked, match="version.*no longer exists"):
        apply_document_action(before, before, after, action, node_id="new",
                              versions={"a": "original", "b": "changed-then-restored"}, document_version="restored",
                              structure_version="original", legacy_input_version="original")
    assert before == document()


def test_learning_context_tampering_cannot_bypass_parent_snapshot_gate(tmp_path):
    course, *_ = forked_course(tmp_path)
    course.merge()
    next_id = course.chat("next")
    archive = course.archive()
    node = archive.nodes[f"{course.lesson.id}/{next_id}"]
    node.learning_context_node_ids = [f"c0_{course.lesson.id}"]
    with pytest.raises(RidocV3Error, match="input document.*learning context"):
        validate_ridoc_v3(archive)


def test_legacy_e_can_continue_save_and_delete_but_not_use_strict_merge(tmp_path):
    course = Course(tmp_path)
    first, target = course.chat("1"), course.chat("2")
    course.branch("extra", first)
    source = course.chat("extra")
    course.switch("main")

    def legacy(archive):
        graph = archive.package.lessons[course.lesson.id]
        for branch in graph.branches.values():
            branch.learning_node_ids = []
            branch.replay_document_refs = {}
        for node in archive.nodes.values():
            node.learning_context_node_ids = []
        event = RidocMergeEventNode(
            id="legacy-merge", lesson_id=course.lesson.id, branch_name="main",
            parent_ids=[target, source], transaction_id="old-merge", created_at="2026-09-01T00:00:00Z",
            document_ref=archive.nodes[f"{course.lesson.id}/{target}"].document_ref,
            merge_source_branch="extra",
        )
        graph.node_order.append(event.id)
        archive.nodes[f"{course.lesson.id}/{event.id}"] = event
        graph.branches["main"].head_node_id = event.id
        archive.manifest["spec_version"] = "3.1"
    course.record = course.store.mutate("owner", course.package.id, course.record.revision, legacy)
    continued = course.chat("continue legacy")
    change = course.write("commit_document", transaction_id="legacy-edit", document=document("New A"))
    course.write("commit_document", transaction_id="legacy-edit", document=document("Newest A"))
    course.chat("continue after save")
    archive = course.archive()
    assert archive.manifest["spec_version"] == "3.1"
    assert archive.nodes[f"{course.lesson.id}/{change}"].document_action == {}
    assert "Newest A" in course.store.load_package("owner", package_id=course.package.id).lessons[0].board_document.content_text
    with pytest.raises(RidocV3Error, match="legacy E"):
        build_merge_plan(archive, course.lesson.id, "extra")
    delete_message_node(archive, course.lesson.id, continued)
    assert archive.nodes[f"{course.lesson.id}/{change}"].before_document_ref
    validate_ridoc_v3(archive)


def test_fork_from_merged_branch_inherits_order_and_append_context(tmp_path):
    course, main, source, source_c = forked_course(tmp_path)
    course.merge()
    course.branch("continued")
    new_m = course.chat("new branch")
    archive = course.archive()
    assert branch_sequence(archive, course.lesson.id)[-1] == new_m
    assert all(identity in branch_sequence(archive, course.lesson.id) for identity in [*source, source_c])
    assert archive.package.lessons[course.lesson.id].branches["continued"].learning_order_merged
    package = course.store.load_package("owner", package_id=course.package.id)
    assert "A expanded" in package.lessons[0].board_document.content_text
    assert package.lessons[0].history_graph.branches["continued"].learning_order_merged


def test_failed_atomic_write_keeps_old_package_and_successful_retry_is_idempotent(tmp_path, monkeypatch):
    import openclass_core.services.ridoc_sequence_mutations as module
    course, *_ = forked_course(tmp_path)
    path = course.store.package_path("owner", course.package.id)
    before = path.read_bytes()
    original = module.write_ridoc_v3
    def fail(*args, **kwargs):
        raise OSError("injected write failure")
    monkeypatch.setattr(module, "write_ridoc_v3", fail)
    with pytest.raises(OSError, match="injected"):
        course.merge()
    assert path.read_bytes() == before
    monkeypatch.setattr(module, "write_ridoc_v3", original)
    course.merge()
    merged_bytes = path.read_bytes()
    course.merge()
    assert path.read_bytes() == merged_bytes
    assert len(course.archive().package.lessons[course.lesson.id].merge_records) == 1


def test_ai_action_has_conservative_read_scope_and_malformed_patch_fails_closed(tmp_path):
    course, *_ = forked_course(tmp_path)
    archive = course.archive()
    ai_changes = [node for node in archive.nodes.values() if node.kind == "C" and not node.initial]
    assert ai_changes[0].document_action["scope"] == "document"
    ai_changes[0].document_action["inputs"] = []
    with pytest.raises(RidocV3Error, match="fields that cannot be verified"):
        validate_ridoc_v3(archive)


def test_source_reading_reorder_does_not_change_fork_anchor_or_next_merge_anchor(tmp_path):
    course, main, source, source_c = forked_course(tmp_path)
    # Fixture: this source was previously arranged so a shared early M is
    # read last. Its original causal/append location remains unchanged.
    def arrange_source(archive):
        order = branch_sequence(archive, course.lesson.id, "extra")
        order.remove(main[0])
        order.append(main[0])
        set_branch_sequence(archive, course.lesson.id, "extra", order)
    course.record = course.store.mutate("owner", course.package.id, course.record.revision, arrange_source)
    plan = build_merge_plan(course.archive(), course.lesson.id, "extra")
    assert plan["source_node_ids"][-1] == main[0]
    assert plan["base_node_id"] == main[2]
    assert plan["default_node_ids"] == [f"c0_{course.lesson.id}", *main[:3], *source, source_c, *main[3:]]
    course.merge()
    first_merge = branch_sequence(course.archive(), course.lesson.id)

    course.switch("extra")
    added = course.chat("3.4")
    course.switch("main")
    plan = build_merge_plan(course.archive(), course.lesson.id, "extra")
    assert plan["source_node_ids"][-2] == main[0]
    assert plan["base_node_id"] == source_c
    expected = list(first_merge)
    expected.insert(expected.index(source_c) + 1, added)
    assert plan["default_node_ids"] == expected
    course.merge(transaction="merge-second")
    assert branch_sequence(course.archive(), course.lesson.id) == expected
    assert len(expected) == len(set(expected))


def test_hundred_m_large_board_serializes_once_per_replay_and_validation(monkeypatch):
    import openclass_core.services.ridoc_v3 as format_module
    lesson = create_empty_lesson("Large board")
    lesson.board_document = document("A" * 200_000, "B" * 200_000)
    archive = new_ridoc_v3(CoursePackage(title="Large", summary="", lessons=[lesson]))
    graph = archive.package.lessons[lesson.id]
    initial = archive.nodes[f"{lesson.id}/{graph.node_order[0]}"]
    digest = initial.after_document_ref
    for index in range(100):
        node = RidocMessageNode(
            id=f"message-{index}", lesson_id=lesson.id, branch_name="main",
            parent_ids=[graph.node_order[-1]], transaction_id=f"turn-{index}",
            created_at="2026-09-01T00:00:00Z", user_message=str(index), assistant_message="Answer",
            document_ref=digest, learning_context_node_ids=list(graph.node_order),
        )
        archive.nodes[f"{lesson.id}/{node.id}"] = node
        graph.node_order.append(node.id)
    branch = graph.branches["main"]
    branch.head_node_id = graph.node_order[-1]
    branch.learning_node_ids = list(graph.node_order)
    branch.replay_document_refs = dict.fromkeys(graph.node_order, digest)
    archive.manifest["spec_version"] = "3.2"

    counts = {"serializations": 0, "integrity_hashes": 0}
    original_dump, original_hash = BoardDocument.model_dump, format_module.content_hash
    def counted_dump(self, *args, **kwargs):
        counts["serializations"] += 1
        return original_dump(self, *args, **kwargs)
    def counted_hash(value):
        if isinstance(value, dict) and "content_json" in value:
            counts["integrity_hashes"] += 1
        return original_hash(value)
    monkeypatch.setattr(BoardDocument, "model_dump", counted_dump)
    monkeypatch.setattr(format_module, "content_hash", counted_hash)

    replay = replay_sequence(archive, lesson.id, branch.learning_node_ids)
    assert not replay["blockers"]
    assert counts["serializations"] == 1
    counts["serializations"] = 0
    validate_ridoc_v3(archive)
    # All 100 input contexts and the branch share the same C0 prefix, and
    # every reference still receives its normal identity/parent checks.
    assert counts == {"serializations": 1, "integrity_hashes": 1}
    validate_ridoc_v3(archive)
    assert counts == {"serializations": 2, "integrity_hashes": 2}
    archive.documents[digest]["title"] = "Tampered after validation"
    with pytest.raises(RidocV3Error, match="missing or invalid document"):
        validate_ridoc_v3(archive)
