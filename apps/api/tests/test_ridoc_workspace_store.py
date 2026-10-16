from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from openclass_core.models import CoursePackage
from openclass_core.services.lesson_factory import create_empty_lesson
from openclass_core.services.rich_document import build_document
from openclass_core.services.ridoc_turns import RidocTurnOutcome, commit_ridoc_turn_outcome
from openclass_core.services.ridoc_v3 import RidocV3ConflictError, read_ridoc_v3
from openclass_core.services.ridoc_workspace_store import RidocWorkspaceStore


def _package(title: str = "RIDOC workspace") -> CoursePackage:
    first = create_empty_lesson(f"{title} first")
    second = create_empty_lesson(f"{title} second")
    return CoursePackage(
        title=title,
        summary="Native package",
        lessons=[first, second],
        active_lesson_id=first.id,
        open_lesson_ids=[first.id],
        workspace_tab_order=[first.id],
    )


def _store(tmp_path: Path) -> RidocWorkspaceStore:
    return RidocWorkspaceStore(tmp_path / "packages", tmp_path / "index.sqlite3")


def test_existing_early_index_schema_is_upgraded_in_place(tmp_path: Path) -> None:
    database = tmp_path / "index.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE ridoc_package_index (
                owner_user_id TEXT NOT NULL,
                package_id TEXT NOT NULL,
                ridoc_path TEXT NOT NULL,
                revision TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                lessons_json TEXT NOT NULL,
                PRIMARY KEY(owner_user_id, package_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE ridoc_workspace_meta (
                owner_user_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY(owner_user_id, key)
            )
            """
        )
        connection.execute(
            "INSERT INTO ridoc_workspace_meta VALUES ('owner', 'active_package_id', 'course_old')"
        )

    RidocWorkspaceStore(tmp_path / "packages", database)

    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(ridoc_package_index)")
        }
        workspace_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(ridoc_workspace_meta)")
        }
    assert {
        "package_kind",
        "visibility",
        "active_lesson_id",
        "open_lesson_ids_json",
        "workspace_tab_order_json",
        "created_at",
        "updated_at",
    } <= columns
    assert workspace_columns == {"owner_user_id", "active_package_id", "updated_at"}


def test_index_can_be_rebuilt_from_ridoc_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    package = _package()
    created = store.create_from_package("owner", package)

    assert created.index.active_lesson_id == package.lessons[0].id
    assert store.load_package("owner", package_id=package.id).id == package.id
    (tmp_path / "index.sqlite3").unlink()
    (tmp_path / "index.sqlite3-wal").unlink(missing_ok=True)
    (tmp_path / "index.sqlite3-shm").unlink(missing_ok=True)

    rebuilt = _store(tmp_path)
    assert rebuilt.list_packages("owner") == []
    views = rebuilt.rebuild_owner_index("owner")
    assert [view.id for view in views] == [package.id]
    assert rebuilt.load_package("owner", lesson_id=package.lessons[1].id).id == package.id


def test_complete_turn_commits_one_m_and_optional_c_in_one_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    package = _package()
    created = store.create_from_package("owner", package)
    lesson = package.lessons[0]
    document = build_document(
        title=lesson.title,
        document_id=lesson.board_document.id,
        content_text="# Changed\n\nCommitted with the answer.",
    )

    committed = commit_ridoc_turn_outcome(
        store,
        owner_user_id="owner",
        package_id=package.id,
        lesson_id=lesson.id,
        expected_revision=created.revision,
        outcome=RidocTurnOutcome(
            transaction_id="turn-1",
            user_message="Please explain and update it.",
            assistant_message="The explanation and update are complete.",
            document=document,
        ),
    )
    archive = store.load_archive("owner", package_id=package.id)
    order = archive.package.lessons[lesson.id].node_order

    assert committed.revision != created.revision
    assert [archive.nodes[f"{lesson.id}/{node_id}"].kind for node_id in order] == ["C", "M", "C"]
    assert archive.nodes[f"{lesson.id}/{order[-2]}"].transaction_id == "turn-1"
    assert archive.nodes[f"{lesson.id}/{order[-1]}"].transaction_id == "turn-1"


def test_regenerate_creates_sibling_m_and_preserves_original_branch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    package = _package()
    created = store.create_from_package("owner", package)
    lesson_id = package.lessons[0].id
    revision = created.revision
    message_ids: list[str] = []
    for index in range(1, 4):
        committed = store.commit_turn(
            owner_user_id="owner",
            package_id=package.id,
            lesson_id=lesson_id,
            expected_revision=revision,
            transaction_id=f"chain-{index}",
            user_message=f"Question {index}",
            assistant_message=f"Answer {index}",
        )
        revision = committed.revision
        archive = store.load_archive("owner", package_id=package.id)
        message_ids.append(
            next(
                node.id
                for node in archive.nodes.values()
                if node.transaction_id == f"chain-{index}"
            )
        )

    before = store.load_archive("owner", package_id=package.id)
    graph_before = before.package.lessons[lesson_id]
    main_head = graph_before.branches["main"].head_node_id
    source = before.nodes[f"{lesson_id}/{message_ids[1]}"]
    regenerated = store.commit_turn(
        owner_user_id="owner",
        package_id=package.id,
        lesson_id=lesson_id,
        expected_revision=revision,
        transaction_id="regenerated-turn",
        user_message="Question 2",
        assistant_message="A different answer 2",
        branch_action_kind="regenerate",
        branch_source_node_id=message_ids[1],
    )

    archive = store.load_archive("owner", package_id=package.id)
    graph = archive.package.lessons[lesson_id]
    node = next(
        item for item in archive.nodes.values() if item.transaction_id == "regenerated-turn"
    )
    assert regenerated.revision != revision
    assert graph.current_branch.startswith("refresh-")
    assert graph.branches["main"].head_node_id == main_head
    assert node.parent_ids == source.parent_ids
    assert node.branch_origin_kind == "regenerate"
    assert node.branch_origin_node_id == source.id
    assert graph.branches[graph.current_branch].head_node_id == node.id


def test_fork_uses_source_turn_document_and_creates_no_empty_branch_on_failure(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    package = _package()
    created = store.create_from_package("owner", package)
    lesson = package.lessons[0]
    source_document = build_document(
        title=lesson.title,
        document_id=lesson.board_document.id,
        content_text="# Source state",
    )
    source_commit = store.commit_turn(
        owner_user_id="owner",
        package_id=package.id,
        lesson_id=lesson.id,
        expected_revision=created.revision,
        transaction_id="source-turn",
        user_message="Source question",
        assistant_message="Source answer",
        document=source_document,
    )
    before_tail = store.load_archive("owner", package_id=package.id)
    source_m = next(
        node for node in before_tail.nodes.values() if node.kind == "M" and node.transaction_id == "source-turn"
    )
    linked_c = next(
        node for node in before_tail.nodes.values() if node.kind == "C" and node.transaction_id == "source-turn"
    )
    tail = store.commit_turn(
        owner_user_id="owner",
        package_id=package.id,
        lesson_id=lesson.id,
        expected_revision=source_commit.revision,
        transaction_id="tail-turn",
        user_message="Later question",
        assistant_message="Later answer",
    )
    before = store.load_archive("owner", package_id=package.id)
    original_branches = set(before.package.lessons[lesson.id].branches)
    with pytest.raises(RidocV3ConflictError):
        store.commit_turn(
            owner_user_id="owner",
            package_id=package.id,
            lesson_id=lesson.id,
            expected_revision=source_commit.revision,
            transaction_id="stale-fork",
            user_message="New branch question",
            assistant_message="New branch answer",
            branch_action_kind="fork",
            branch_source_node_id=source_m.id,
        )
    assert set(
        store.load_archive("owner", package_id=package.id).package.lessons[lesson.id].branches
    ) == original_branches

    forked = store.commit_turn(
        owner_user_id="owner",
        package_id=package.id,
        lesson_id=lesson.id,
        expected_revision=tail.revision,
        transaction_id="forked-turn",
        user_message="New branch question",
        assistant_message="New branch answer",
        branch_action_kind="fork",
        branch_source_node_id=source_m.id,
    )
    archive = store.load_archive("owner", package_id=package.id)
    graph = archive.package.lessons[lesson.id]
    node = next(item for item in archive.nodes.values() if item.transaction_id == "forked-turn")
    assert forked.revision != tail.revision
    assert graph.current_branch.startswith("fork-")
    assert graph.branches[graph.current_branch].base_node_id == linked_c.id
    assert node.parent_ids == [linked_c.id]
    assert node.branch_origin_kind == "fork"
    assert node.branch_origin_node_id == source_m.id


def test_failed_or_empty_outcome_cannot_create_formal_nodes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    package = _package()
    created = store.create_from_package("owner", package)
    lesson = package.lessons[0]

    with pytest.raises(ValueError):
        RidocTurnOutcome(
            transaction_id="turn-empty",
            user_message="Question",
            assistant_message="   ",
        )

    archive = store.load_archive("owner", package_id=package.id)
    assert archive.revision.value == created.revision
    assert archive.package.lessons[lesson.id].node_order == [f"c0_{lesson.id}"]


def test_duplicate_transaction_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    package = _package()
    created = store.create_from_package("owner", package)
    lesson_id = package.lessons[0].id
    first = store.commit_turn(
        owner_user_id="owner",
        package_id=package.id,
        lesson_id=lesson_id,
        expected_revision=created.revision,
        transaction_id="same-turn",
        user_message="Question",
        assistant_message="Answer",
    )
    duplicate = store.commit_turn(
        owner_user_id="owner",
        package_id=package.id,
        lesson_id=lesson_id,
        expected_revision=created.revision,
        transaction_id="same-turn",
        user_message="Question",
        assistant_message="Answer",
    )

    assert duplicate.revision == first.revision
    archive = store.load_archive("owner", package_id=package.id)
    assert len(archive.package.lessons[lesson_id].node_order) == 2


def test_stale_revision_is_rejected_without_partial_write(tmp_path: Path) -> None:
    store = _store(tmp_path)
    package = _package()
    created = store.create_from_package("owner", package)
    lesson_id = package.lessons[0].id
    committed = store.commit_turn(
        owner_user_id="owner",
        package_id=package.id,
        lesson_id=lesson_id,
        expected_revision=created.revision,
        transaction_id="first",
        user_message="Question",
        assistant_message="Answer",
    )

    with pytest.raises(RidocV3ConflictError):
        store.commit_turn(
            owner_user_id="owner",
            package_id=package.id,
            lesson_id=lesson_id,
            expected_revision=created.revision,
            transaction_id="stale",
            user_message="Stale question",
            assistant_message="Stale answer",
        )

    assert store.load_archive("owner", package_id=package.id).revision.value == committed.revision


def test_document_noop_and_ui_state_do_not_change_ridoc_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    package = _package()
    created = store.create_from_package("owner", package)
    lesson = package.lessons[0]
    noop = store.commit_document(
        owner_user_id="owner",
        package_id=package.id,
        lesson_id=lesson.id,
        expected_revision=created.revision,
        transaction_id="noop",
        document=lesson.board_document,
    )
    store.update_ui_state(
        "owner",
        package.id,
        active_lesson_id=package.lessons[1].id,
        open_lesson_ids=[package.lessons[1].id],
        workspace_tab_order=[package.lessons[1].id],
    )

    assert noop.revision == created.revision
    assert store.load_archive("owner", package_id=package.id).revision.value == created.revision
    loaded = store.load_package("owner", package_id=package.id)
    assert loaded.active_lesson_id == package.lessons[1].id


def test_repeated_autosaves_extend_one_manual_c_action(tmp_path: Path) -> None:
    store = _store(tmp_path)
    package = _package()
    created = store.create_from_package("owner", package)
    lesson = package.lessons[0]
    first_document = build_document(
        title=lesson.title,
        document_id=lesson.board_document.id,
        content_text="# Draft one",
    )
    first = store.commit_document(
        owner_user_id="owner",
        package_id=package.id,
        lesson_id=lesson.id,
        expected_revision=created.revision,
        transaction_id="editing-session",
        document=first_document,
    )
    second_document = build_document(
        title=lesson.title,
        document_id=lesson.board_document.id,
        content_text="# Draft two\n\nSame continuous edit session.",
    )
    second = store.commit_document(
        owner_user_id="owner",
        package_id=package.id,
        lesson_id=lesson.id,
        expected_revision=first.revision,
        transaction_id="editing-session",
        document=second_document,
    )

    archive = store.load_archive("owner", package_id=package.id)
    order = archive.package.lessons[lesson.id].node_order
    assert len(order) == 2
    assert second.revision != first.revision
    assert archive.documents[
        archive.nodes[f"{lesson.id}/{order[-1]}"].after_document_ref
    ]["content_text"] == second_document.content_text


def test_runtime_turns_and_audit_stay_outside_ridoc(tmp_path: Path) -> None:
    store = _store(tmp_path)
    package = _package()
    created = store.create_from_package("owner", package)
    lesson_id = package.lessons[0].id
    store.stage_runtime_turn(
        owner_user_id="owner",
        package_id=package.id,
        lesson_id=lesson_id,
        turn_id="pending-turn",
        state="pending",
        payload={"progress": 20},
    )
    store.audit(
        owner_user_id="owner",
        package_id=package.id,
        lesson_id=lesson_id,
        kind="model_retry",
        payload={"error": "temporary"},
    )

    assert store.runtime_turn(
        owner_user_id="owner",
        package_id=package.id,
        lesson_id=lesson_id,
        turn_id="pending-turn",
    )["state"] == "pending"
    archive = read_ridoc_v3(Path(created.path))
    assert archive.revision.value == created.revision
    assert "pending-turn" not in Path(created.path).read_bytes().decode("latin1")
    store.clear_runtime_turn(
        owner_user_id="owner",
        package_id=package.id,
        lesson_id=lesson_id,
        turn_id="pending-turn",
    )
    assert store.runtime_turn(
        owner_user_id="owner",
        package_id=package.id,
        lesson_id=lesson_id,
        turn_id="pending-turn",
    ) is None


def test_divergent_whole_document_actions_cannot_be_merged_by_supplied_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    package = _package()
    created = store.create_from_package("owner", package)
    lesson = package.lessons[0]
    branched = store.create_branch(
        owner_user_id="owner",
        package_id=package.id,
        lesson_id=lesson.id,
        expected_revision=created.revision,
        name="alternate",
    )
    source_document = build_document(
        title=lesson.title,
        document_id=lesson.board_document.id,
        content_text="# Alternate\n\nSource branch change.",
    )
    source_commit = store.commit_document(
        owner_user_id="owner",
        package_id=package.id,
        lesson_id=lesson.id,
        expected_revision=branched.revision,
        transaction_id="alternate-edit",
        document=source_document,
    )
    switched = store.switch_branch(
        owner_user_id="owner",
        package_id=package.id,
        lesson_id=lesson.id,
        expected_revision=source_commit.revision,
        name="main",
    )
    target_document = build_document(
        title=lesson.title,
        document_id=lesson.board_document.id,
        content_text="# Main\n\nTarget branch change.",
    )
    target_commit = store.commit_document(
        owner_user_id="owner",
        package_id=package.id,
        lesson_id=lesson.id,
        expected_revision=switched.revision,
        transaction_id="main-edit",
        document=target_document,
    )
    before = store.load_archive("owner", package_id=package.id)
    graph = before.package.lessons[lesson.id]
    source_head = graph.branches["alternate"].head_node_id
    target_head = graph.branches["main"].head_node_id
    merged_document = build_document(
        title=lesson.title,
        document_id=lesson.board_document.id,
        content_text="# Merged\n\nTarget and source changes resolved.",
    )

    with pytest.raises(RidocV3ConflictError, match="input version has changed"):
        store.merge_branches(
            owner_user_id="owner",
            package_id=package.id,
            lesson_id=lesson.id,
            expected_revision=target_commit.revision,
            transaction_id="merge-session-1",
            source_branch_name="alternate",
            target_branch_name="main",
            expected_source_head_id=source_head,
            expected_target_head_id=target_head,
            document=merged_document,
            label="Merge alternate to main",
        )

    archive = store.load_archive("owner", package_id=package.id)
    head = archive.package.lessons[lesson.id].branches["main"].head_node_id
    assert archive.revision.value == target_commit.revision
    assert head == target_head
    assert set(archive.nodes) == set(before.nodes)


def test_package_delete_removes_index_and_moves_ridoc_out_of_rebuild_scope(tmp_path: Path) -> None:
    from openclass_core.services.recently_deleted import (
        RestoreDeletedCourseRequest,
        list_recently_deleted,
        recycle_root,
        restore_recently_deleted,
    )

    store = _store(tmp_path)
    package = _package()
    created = store.create_from_package("owner", package)
    original_path = Path(created.path)

    store.delete_package(
        owner_user_id="owner",
        package_id=package.id,
        expected_revision=created.revision,
    )

    assert not original_path.exists()
    assert store.list_packages("owner") == []
    assert store.rebuild_owner_index("owner") == []
    deleted = list_recently_deleted(store, "owner").items
    assert len(deleted) == 1
    assert deleted[0].kind == "package"
    assert deleted[0].lesson_count == len(package.lessons)
    assert list_recently_deleted(store, "another-owner").items == []
    saved_paths = list(recycle_root(store).glob("*/*/course.ridoc"))
    assert len(saved_paths) == 1
    saved = read_ridoc_v3(saved_paths[0])
    assert saved.package.id == package.id
    assert saved.package.lesson_order == [lesson.id for lesson in package.lessons]
    assert saved.revision.value == deleted[0].revision

    restored = restore_recently_deleted(store, "owner", deleted[0].id, RestoreDeletedCourseRequest(
        expected_revision=deleted[0].revision, expected_target_revision=deleted[0].target_revision,
    ))
    assert restored.package_id == package.id
    assert original_path.exists()
    assert store.load_package("owner", package_id=package.id).id == package.id
    restored_archive = store.load_archive("owner", package_id=package.id)
    assert restored_archive.package == saved.package
    assert restored_archive.nodes == saved.nodes
    assert restored_archive.documents == saved.documents
    assert list_recently_deleted(store, "owner").items == []


def test_materialized_compatibility_view_contains_only_c0_m_and_c(tmp_path: Path) -> None:
    store = _store(tmp_path)
    package = _package()
    created = store.create_from_package("owner", package)
    lesson_id = package.lessons[0].id
    store.commit_turn(
        owner_user_id="owner",
        package_id=package.id,
        lesson_id=lesson_id,
        expected_revision=created.revision,
        transaction_id="turn",
        user_message="Question",
        assistant_message="Answer",
    )

    projected = store.load_package("owner", package_id=package.id)
    kinds = [commit.metadata["history_node_kind"] for commit in projected.lessons[0].history_graph.commits]
    assert kinds == ["document", "chat"]
    assert all(commit.runtime_snapshot is None for commit in projected.lessons[0].history_graph.commits)
