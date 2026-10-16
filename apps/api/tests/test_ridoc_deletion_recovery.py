from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from openclass_core.models import CoursePackage
from openclass_core.services import ridoc_v3
from openclass_core.services.lesson_factory import create_empty_lesson
from openclass_core.services.ridoc_deletion_store import RidocDeletedTurnError
from openclass_core.services.ridoc_v3 import RidocV3ConflictError, RidocV3Error, read_ridoc_v3
from openclass_core.services.ridoc_workspace_store import RidocWorkspaceStore


OWNER = "recovery-owner"


def _seed(tmp_path: Path):
    store = RidocWorkspaceStore(tmp_path / "packages", tmp_path / "runtime.sqlite3")
    lesson = create_empty_lesson("Recovery")
    package = CoursePackage(title="Recovery", summary="", lessons=[lesson])
    revision = store.create_from_package(OWNER, package).revision
    nodes = []
    for index in range(3):
        revision = store.commit_turn(
            owner_user_id=OWNER, package_id=package.id, lesson_id=lesson.id,
            expected_revision=revision, transaction_id=f"turn-{index}",
            user_message=f"private question {index}", assistant_message=f"private answer {index}",
        ).revision
        archive = store.load_archive(OWNER, package_id=package.id)
        nodes.append(archive.package.lessons[lesson.id].branches["main"].head_node_id)
    kwargs = dict(owner_user_id=OWNER, package_id=package.id, lesson_id=lesson.id,
                  node_id=nodes[1], expected_revision=revision, mutation_id="delete-middle")
    return store, kwargs, nodes


def _restart(store):
    return RidocWorkspaceStore(store.root, store.database_path)


def _assert_restored(store, kwargs, nodes, *, deleted):
    archive = store.load_archive(OWNER, package_id=kwargs["package_id"])
    node_order = archive.package.lessons[kwargs["lesson_id"]].node_order
    assert node_order[1:] == ([nodes[0], nodes[2]] if deleted else nodes)
    record = store.deletion_journal.get(OWNER, kwargs["package_id"], kwargs["mutation_id"])
    if deleted:
        assert record["state"] == "completed"
        assert archive.revision.value == record["new_revision"]
        assert store.context_epoch(owner_user_id=OWNER, package_id=kwargs["package_id"], lesson_id=kwargs["lesson_id"]) == 1
        with pytest.raises(RidocDeletedTurnError):
            store.assert_turn_not_deleted(owner_user_id=OWNER, package_id=kwargs["package_id"],
                                          lesson_id=kwargs["lesson_id"], transaction_id="turn-1")
    else:
        assert record is None
        assert archive.revision.value == kwargs["expected_revision"]
        store.assert_turn_not_deleted(owner_user_id=OWNER, package_id=kwargs["package_id"],
                                      lesson_id=kwargs["lesson_id"], transaction_id="turn-1")
    return archive


@pytest.mark.parametrize("phase", ["before_prepare", "prepared", "replaced", "indexed", "completed"])
def test_checkpoint_interruption_recovers_exact_old_or_new_package(tmp_path, monkeypatch, phase):
    store, kwargs, nodes = _seed(tmp_path)

    def fail_at(current):
        if current == phase:
            raise OSError(f"interrupted at {phase}")

    monkeypatch.setattr(store, "_deletion_checkpoint", fail_at)
    with pytest.raises(OSError, match="interrupted"):
        store.delete_history_message(**kwargs)
    restarted = _restart(store)
    deleted = phase in {"replaced", "indexed", "completed"}
    recovered = _assert_restored(restarted, kwargs, nodes, deleted=deleted)
    retry = restarted.delete_history_message(**kwargs)
    final = _assert_restored(restarted, kwargs, nodes, deleted=True)
    assert retry["ridoc_revision"] == final.revision.value
    if deleted:
        assert final.revision == recovered.revision
    # Read recovery and request retries must not repeat invalidation/version writes.
    restarted.load_archive(OWNER, package_id=kwargs["package_id"])
    assert restarted.context_epoch(owner_user_id=OWNER, package_id=kwargs["package_id"], lesson_id=kwargs["lesson_id"]) == 1


@pytest.mark.parametrize("fault", ["temporary_write", "file_fsync", "temporary_validation", "before_replace", "after_replace", "directory_fsync"])
def test_file_write_failure_recovers_without_partial_history(tmp_path, monkeypatch, fault):
    store, kwargs, nodes = _seed(tmp_path)
    with monkeypatch.context() as patch:
        if fault == "temporary_write":
            def fail_write(*args, **kwargs):
                raise OSError("injected temporary_write")
            patch.setattr(ridoc_v3.zipfile.ZipFile, "writestr", fail_write)
        elif fault in {"file_fsync", "directory_fsync"}:
            original = ridoc_v3.os.fsync
            count = 0

            def fail_sync(descriptor):
                nonlocal count
                count += 1
                if count == (1 if fault == "file_fsync" else 2):
                    raise OSError(f"injected {fault}")
                return original(descriptor)

            patch.setattr(ridoc_v3.os, "fsync", fail_sync)
        elif fault == "temporary_validation":
            original = ridoc_v3.read_ridoc_v3

            def fail_validation(path):
                if path.suffix == ".tmp":
                    raise RidocV3Error("injected temporary_validation")
                return original(path)

            patch.setattr(ridoc_v3, "read_ridoc_v3", fail_validation)
        else:
            original = ridoc_v3.os.replace

            def fail_replace(source, target):
                if fault == "after_replace":
                    original(source, target)
                raise OSError(f"injected {fault}")

            patch.setattr(ridoc_v3.os, "replace", fail_replace)
        with pytest.raises((OSError, RidocV3Error), match="injected"):
            store.delete_history_message(**kwargs)
    restarted = _restart(store)
    _assert_restored(restarted, kwargs, nodes, deleted=fault in {"after_replace", "directory_fsync"})
    restarted.delete_history_message(**kwargs)
    _assert_restored(restarted, kwargs, nodes, deleted=True)
    assert list(store.package_path(OWNER, kwargs["package_id"]).parent.glob("*.tmp")) == []


@pytest.mark.parametrize("failure_method", ["_upsert_index", "complete"])
def test_post_replace_index_or_journal_failure_is_completed_on_next_read(tmp_path, monkeypatch, failure_method):
    store, kwargs, nodes = _seed(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("injected index/journal failure")

    if failure_method == "complete":
        monkeypatch.setattr(store.deletion_journal, "complete", fail)
    else:
        monkeypatch.setattr(store, failure_method, fail)
    with pytest.raises(OSError, match="injected"):
        store.delete_history_message(**kwargs)
    restarted = _restart(store)
    _assert_restored(restarted, kwargs, nodes, deleted=True)


def test_two_store_instances_cannot_both_delete_from_one_revision(tmp_path):
    first, kwargs, nodes = _seed(tmp_path)
    second = _restart(first)
    barrier = Barrier(2)

    def run(store, node_id, mutation_id):
        barrier.wait(timeout=5)
        try:
            return store.delete_history_message(**{**kwargs, "node_id": node_id, "mutation_id": mutation_id})
        except RidocV3ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, first, nodes[0], "delete-first"), pool.submit(run, second, nodes[1], "delete-second")]
        results = [future.result(timeout=15) for future in futures]
    assert results.count("conflict") == 1
    winner = next(result for result in results if isinstance(result, dict))
    final = first.load_archive(OWNER, package_id=kwargs["package_id"])
    assert len(final.package.lessons[kwargs["lesson_id"]].node_order) == 3
    assert f"{kwargs['lesson_id']}/{winner['deleted_node_id']}" not in final.nodes


def test_index_rebuild_preserves_deleted_turn_and_input_identities(tmp_path):
    store, kwargs, nodes = _seed(tmp_path)
    store.bind_turn_input_identity(owner_user_id=OWNER, package_id=kwargs["package_id"],
                                   lesson_id=kwargs["lesson_id"], transaction_id="turn-1",
                                   session_id="session-before-delete", input_event_id="old-input")
    store.delete_history_message(**kwargs)
    with sqlite3.connect(store.database_path) as conn:
        conn.execute("DROP TABLE ridoc_lesson_index")
        conn.execute("DROP TABLE ridoc_package_index")
    restarted = _restart(store)
    assert restarted.list_packages(OWNER) == []
    rebuilt = restarted.rebuild_owner_index(OWNER)
    assert len(rebuilt) == 1
    _assert_restored(restarted, kwargs, nodes, deleted=True)
    with pytest.raises(RidocDeletedTurnError):
        restarted.assert_turn_not_deleted(owner_user_id=OWNER, package_id=kwargs["package_id"],
                                          lesson_id=kwargs["lesson_id"], transaction_id="new-attempt-id",
                                          session_id="session-before-delete", input_event_id="old-input")
    current = restarted.load_archive(OWNER, package_id=kwargs["package_id"]).revision.value
    with pytest.raises(RidocDeletedTurnError):
        restarted.commit_turn(owner_user_id=OWNER, package_id=kwargs["package_id"], lesson_id=kwargs["lesson_id"],
                              expected_revision=current, transaction_id="turn-1",
                              user_message="resubmitted deleted question", assistant_message="resubmitted answer")
    assert restarted.load_archive(OWNER, package_id=kwargs["package_id"]).revision.value == current


def test_journal_has_no_qa_and_invalidates_only_target_lesson_runtime(tmp_path):
    store, kwargs, nodes = _seed(tmp_path)
    with sqlite3.connect(store.database_path) as conn:
        for lesson_id in [kwargs["lesson_id"], "other-lesson"]:
            conn.execute("INSERT INTO ridoc_lesson_runtime VALUES (?,?,?,?,?)", (OWNER, kwargs["package_id"], lesson_id, "{}", "now"))
            conn.execute("INSERT INTO ridoc_runtime_turns VALUES (?,?,?,?,?,?,?,?)",
                         (OWNER, kwargs["package_id"], lesson_id, "pending", "pending", "{}", "", "now"))
    store.delete_history_message(**kwargs)
    with sqlite3.connect(store.database_path) as conn:
        row = conn.execute("SELECT result_json FROM ridoc_deletions").fetchone()
        assert "private question" not in row[0]
        assert "private answer" not in row[0]
        assert set(json.loads(row[0])) == {"deleted_node_id", "deleted_transaction_id", "affected_branches", "retained_document_node_ids"}
        assert conn.execute("SELECT lesson_id FROM ridoc_lesson_runtime").fetchall() == [("other-lesson",)]
        assert conn.execute("SELECT lesson_id FROM ridoc_runtime_turns").fetchall() == [("other-lesson",)]
    disk = read_ridoc_v3(store.package_path(OWNER, kwargs["package_id"]))
    assert all(node.kind in {"M", "C"} for node in disk.nodes.values())
