from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from openclass_core.models import ChatRequest, RealtimeConnectRequest, RidocHistoryOrderRequest
from openclass_core.services import workspace_state
from openclass_core.services.ridoc_chat_context import authoritative_conversation, create_ridoc_chat_context
from openclass_core.services.ridoc_history_order import history_order_view, update_history_order
from openclass_core.services.ridoc_learning_sequence import branch_sequence, replay_sequence
from openclass_core.services.ridoc_realtime_context import assert_realtime_context, prepare_realtime_context
from openclass_core.services.ridoc_v3 import RidocMergeEventNode, RidocV3ConflictError, RidocV3Error, read_ridoc_v3, write_ridoc_v3
from openclass_core.services.ridoc_workspace_store import RidocWorkspaceStore
from test_ridoc_learning_sequence import Course, document, forked_course


def reorder(course, ordered, *, mutation="order", revision=None, branch="main", store=None):
    return update_history_order(
        store or course.store, owner_user_id="owner", package_id=course.package.id,
        lesson_id=course.lesson.id, request=RidocHistoryOrderRequest(
            branch_name=branch, ordered_node_ids=ordered, mutation_id=mutation,
            expected_ridoc_revision=revision or course.archive().revision.value,
        ),
    )


@pytest.mark.parametrize("merged", [False, True])
def test_order_preserves_facts_heads_other_branches_and_final_board(tmp_path, merged):
    course, main, _, _ = forked_course(tmp_path)
    if merged:
        course.merge()
    before = course.archive()
    original = branch_sequence(before, course.lesson.id)
    moved = [value for value in original if value != main[-1]]
    moved.insert(1, main[-1])
    result = reorder(course, moved)
    after = course.archive()
    graph = after.package.lessons[course.lesson.id]
    assert result["changed"] and result["ridoc_revision"] != before.revision.value
    assert branch_sequence(after, course.lesson.id) == moved
    assert after.nodes == before.nodes
    assert graph.node_order == before.package.lessons[course.lesson.id].node_order
    assert graph.merge_records == before.package.lessons[course.lesson.id].merge_records
    assert graph.branches["extra"] == before.package.lessons[course.lesson.id].branches["extra"]
    assert graph.branches["main"].head_node_id == main[-1]
    assert graph.branches["main"].learning_order_merged == merged
    assert replay_sequence(after, course.lesson.id, moved)["document"] == replay_sequence(before, course.lesson.id, original)["document"]
    assert after.manifest["spec_version"] == "3.2"
    exported = tmp_path / "reordered.ridoc"
    write_ridoc_v3(after, exported)
    assert branch_sequence(read_ridoc_v3(exported), course.lesson.id) == moved
    # Deleting only the rebuildable indexes cannot lose the reading order.
    with course.store._connect() as connection:
        connection.execute("DELETE FROM ridoc_lesson_index WHERE owner_user_id='owner'")
        connection.execute("DELETE FROM ridoc_package_index WHERE owner_user_id='owner'")
    course.store.rebuild_owner_index("owner")
    assert course.store.load_package("owner", package_id=course.package.id).lessons[0].history_graph.branches["main"].learning_node_ids == moved
    course.record = course.store._upsert_index("owner", course.store.package_path("owner", course.package.id), after)
    following = course.chat("after-order")
    assert branch_sequence(course.archive(), course.lesson.id) == [*moved, following]
    course.branch("followup", main[1])
    assert branch_sequence(course.archive(), course.lesson.id) == moved[:moved.index(main[1]) + 1]


def test_one_message_can_cross_its_c_without_changing_the_c_or_original_qa(tmp_path):
    course = Course(tmp_path)
    c = course.chat("change", document("Expanded"))
    archive = course.archive()
    m = archive.nodes[f"{course.lesson.id}/{c}"].parent_ids[0]
    c0 = archive.package.lessons[course.lesson.id].node_order[0]
    assert history_order_view(archive, course.lesson.id)["blocked_reason"] is None
    reorder(course, [c0, c, m])
    after = course.archive()
    assert after.nodes == archive.nodes
    refs = after.package.lessons[course.lesson.id].branches["main"].replay_document_refs
    assert refs[m] == refs[c] != archive.nodes[f"{course.lesson.id}/{m}"].document_ref
    course.store.delete_history_message(
        owner_user_id="owner", package_id=course.package.id, lesson_id=course.lesson.id,
        node_id=m, expected_revision=after.revision.value, mutation_id="delete-original-question",
    )
    deleted = course.archive()
    assert branch_sequence(deleted, course.lesson.id) == [c0, c]
    assert deleted.nodes[f"{course.lesson.id}/{c}"].source_message_deleted
    assert deleted.nodes[f"{course.lesson.id}/{c}"].after_document_ref == archive.nodes[f"{course.lesson.id}/{c}"].after_document_ref


def test_legacy_merge_is_readable_but_order_is_explicitly_blocked(tmp_path):
    course = Course(tmp_path)
    first, target = course.chat("first"), course.chat("target")
    course.branch("extra", first)
    source = course.chat("source")
    course.switch("main")
    def legacy(archive):
        graph = archive.package.lessons[course.lesson.id]
        for branch in graph.branches.values():
            branch.learning_node_ids, branch.replay_document_refs = [], {}
        for node in archive.nodes.values():
            node.learning_context_node_ids = []
        event = RidocMergeEventNode(id="old-merge", lesson_id=course.lesson.id, branch_name="main",
                                    parent_ids=[target, source], transaction_id="legacy-merge", created_at="2026-09-01T00:00:00Z",
                                    document_ref=archive.nodes[f"{course.lesson.id}/{target}"].document_ref,
                                    merge_source_branch="extra")
        archive.nodes[f"{course.lesson.id}/{event.id}"] = event
        graph.node_order.append(event.id)
        graph.branches["main"].head_node_id = event.id
        archive.manifest["spec_version"] = "3.1"
    course.record = course.store.mutate("owner", course.package.id, course.record.revision, legacy)
    before = course.archive()
    view = history_order_view(before, course.lesson.id)
    assert "legacy E" in view["blocked_reason"]
    with pytest.raises(RidocV3Error, match="legacy E"):
        reorder(course, view["ordered_node_ids"])
    assert course.archive().revision == before.revision


@pytest.mark.parametrize("invalid", ["missing", "duplicate", "foreign", "c_order", "before_c0"])
def test_invalid_order_never_writes_course(tmp_path, invalid):
    course = Course(tmp_path)
    course.chat("one")
    course.write("commit_document", transaction_id="edit1", document=document("A1"))
    course.chat("two")
    course.write("commit_document", transaction_id="edit2", document=document("A2"))
    archive = course.archive()
    ordered = branch_sequence(archive, course.lesson.id)
    if invalid == "missing":
        ordered.pop()
    elif invalid == "duplicate":
        ordered.append(ordered[-1])
    elif invalid == "foreign":
        ordered[-1] = "not-in-current-branch"
    elif invalid == "c_order":
        ordered[2], ordered[4] = ordered[4], ordered[2]
    else:
        ordered[0], ordered[1] = ordered[1], ordered[0]
    path = course.store.package_path("owner", course.package.id)
    before = path.read_bytes()
    with pytest.raises(RidocV3Error):
        reorder(course, ordered)
    assert path.read_bytes() == before
    assert course.store.context_epoch(owner_user_id="owner", package_id=course.package.id, lesson_id=course.lesson.id) == 0


def test_noop_is_idempotent_without_file_or_epoch_change(tmp_path):
    course = Course(tmp_path)
    course.chat("one")
    course.chat("two")
    archive = course.archive()
    original = branch_sequence(archive, course.lesson.id)
    path = course.store.package_path("owner", course.package.id)
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    first = reorder(course, original)
    assert first == reorder(course, original)
    assert first == {"ridoc_revision": archive.revision.value, "changed": False, "mutation_id": "order"}
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
    assert course.store.context_epoch(owner_user_id="owner", package_id=course.package.id, lesson_id=course.lesson.id) == 0
    with pytest.raises(RidocV3ConflictError, match="identity"):
        reorder(course, [original[0], *original[:0:-1]])


def test_completed_retry_cannot_revive_deleted_message_or_repeat_epoch(tmp_path):
    course = Course(tmp_path)
    ids = [course.chat(str(i)) for i in range(3)]
    revision = course.archive().revision.value
    order = [f"c0_{course.lesson.id}", *reversed(ids)]
    first = reorder(course, order, revision=revision)
    assert reorder(course, order, revision=revision) == first
    deleted = course.store.delete_history_message(
        owner_user_id="owner", package_id=course.package.id, lesson_id=course.lesson.id,
        node_id=ids[1], expected_revision=first["ridoc_revision"], mutation_id="delete-after-order",
    )
    retry = reorder(course, order, revision=revision)
    assert retry["ridoc_revision"] == deleted["ridoc_revision"]
    assert ids[1] not in branch_sequence(course.archive(), course.lesson.id)
    assert course.store.context_epoch(owner_user_id="owner", package_id=course.package.id, lesson_id=course.lesson.id) == 2


def test_order_invalidates_chat_voice_and_requirements_without_deletion_tombstones(tmp_path, monkeypatch):
    course = Course(tmp_path)
    ids = [course.chat(str(i)) for i in range(3)]
    monkeypatch.setenv("OPENCLASS_COURSE_STORE_MODE", "ridoc_required")
    monkeypatch.setattr(workspace_state, "RIDOC_WORKSPACE_STORE", course.store)
    context = create_ridoc_chat_context(
        course.store, owner_user_id="owner", package_id=course.package.id, lesson_id=course.lesson.id,
        request=ChatRequest(message="next", turn_id="pending", session_id="chat", input_event_id="event"),
    )
    voice = RealtimeConnectRequest(offer_sdp="sdp", client_session_id="voice")
    prepare_realtime_context(course.lesson.id, voice, user_id="owner")
    course.store.save_lesson_runtime(owner_user_id="owner", package_id=course.package.id, lesson=course.lesson)
    reorder(course, [f"c0_{course.lesson.id}", *reversed(ids)])
    with pytest.raises(RidocV3ConflictError, match="history changed"):
        context.assert_current()
    with pytest.raises(RidocV3ConflictError, match="reconnect"):
        assert_realtime_context(course.lesson.id, user_id="owner", session_id="voice")
    fresh = prepare_realtime_context(course.lesson.id, voice.model_copy(update={"client_session_id": "new-voice"}), user_id="owner")
    assert fresh.latest_assistant_message == "answer 0"
    assert [turn.content for turn in authoritative_conversation(course.archive(), course.lesson.id) if turn.role == "user"] == ["2", "1", "0"]
    with course.store._connect() as connection:
        assert connection.execute("SELECT count(*) FROM ridoc_deleted_turns").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM ridoc_lesson_runtime").fetchone()[0] == 0


def test_two_clients_same_revision_only_one_order_succeeds(tmp_path):
    course = Course(tmp_path)
    ids = [course.chat(str(i)) for i in range(3)]
    revision = course.archive().revision.value
    c0 = f"c0_{course.lesson.id}"
    def write(index):
        store = RidocWorkspaceStore(course.store.root, course.store.database_path)
        try:
            reorder(course, [c0, *reversed(ids)] if index else [c0, ids[1], ids[0], ids[2]],
                    mutation=f"competing-{index}", revision=revision, store=store)
            return "saved"
        except RidocV3ConflictError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(write, [0, 1])) == ["conflict", "saved"]
    assert course.store.context_epoch(owner_user_id="owner", package_id=course.package.id, lesson_id=course.lesson.id) == 1
