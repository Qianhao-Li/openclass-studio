from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from openclass_core.services import ridoc_v3
from openclass_core.services.ridoc_v3 import RidocV3ConflictError, RidocV3Error, read_ridoc_v3
from openclass_core.services.ridoc_workspace_store import RidocWorkspaceStore
from test_ridoc_learning_sequence import forked_course


@pytest.mark.parametrize("fault", ["temporary_write", "file_fsync", "temporary_validation", "before_replace", "after_replace", "directory_fsync", "index"])
def test_merge_file_and_index_faults_recover_one_complete_sequence(tmp_path, monkeypatch, fault):
    course, _, _, _ = forked_course(tmp_path)
    before = course.archive()
    path = course.store.package_path("owner", course.package.id)
    original_bytes = path.read_bytes()
    with monkeypatch.context() as patch:
        if fault == "temporary_write":
            def fail_write(*args, **kwargs):
                raise OSError("injected temporary write")
            patch.setattr(ridoc_v3.zipfile.ZipFile, "writestr", fail_write)
        elif fault in {"file_fsync", "directory_fsync"}:
            original = ridoc_v3.os.fsync
            count = 0

            def fail_sync(descriptor):
                nonlocal count
                count += 1
                if count == (1 if fault == "file_fsync" else 2):
                    raise OSError("injected fsync")
                return original(descriptor)
            patch.setattr(ridoc_v3.os, "fsync", fail_sync)
        elif fault == "temporary_validation":
            original = ridoc_v3.read_ridoc_v3

            def fail_validation(candidate):
                if candidate.suffix == ".tmp":
                    raise RidocV3Error("injected validation")
                return original(candidate)
            patch.setattr(ridoc_v3, "read_ridoc_v3", fail_validation)
        elif fault == "index":
            def fail_index(*args, **kwargs):
                raise OSError("injected index")
            patch.setattr(course.store, "_upsert_index", fail_index)
        else:
            original = ridoc_v3.os.replace

            def fail_replace(source, target):
                if fault == "after_replace":
                    original(source, target)
                raise OSError("injected replace")
            patch.setattr(ridoc_v3.os, "replace", fail_replace)
        with pytest.raises((OSError, RidocV3Error), match="injected"):
            course.merge()
    completed = fault in {"after_replace", "directory_fsync", "index"}
    on_disk = read_ridoc_v3(path)
    assert set(on_disk.nodes) == set(before.nodes)
    assert len(on_disk.package.lessons[course.lesson.id].merge_records) == int(completed)
    if not completed:
        assert path.read_bytes() == original_bytes
    restarted = RidocWorkspaceStore(course.store.root, course.store.database_path)
    recovered = restarted.load_archive("owner", package_id=course.package.id)
    assert restarted.load_package("owner", package_id=course.package.id).ridoc_revision == recovered.revision.value
    graph = before.package.lessons[course.lesson.id]
    result = restarted.merge_branches(
        owner_user_id="owner", package_id=course.package.id, lesson_id=course.lesson.id,
        expected_revision=before.revision.value, transaction_id="merge", source_branch_name="extra", target_branch_name="main",
        expected_source_head_id=graph.branches["extra"].head_node_id,
        expected_target_head_id=graph.branches["main"].head_node_id,
        document=course.lesson.board_document, label="Recovered merge",
    )
    archive = restarted.load_archive("owner", package_id=course.package.id)
    assert len(archive.package.lessons[course.lesson.id].merge_records) == 1
    assert result.revision == archive.revision.value
    assert list(path.parent.glob("*.tmp")) == []
    with restarted._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ridoc_sequence_mutations").fetchone()[0] == 0


def test_two_clients_with_same_revision_only_one_merge_can_commit(tmp_path):
    course, _, _, _ = forked_course(tmp_path)
    before = course.archive()
    graph = before.package.lessons[course.lesson.id]

    def submit(index):
        store = RidocWorkspaceStore(course.store.root, course.store.database_path)
        try:
            store.merge_branches(
                owner_user_id="owner", package_id=course.package.id, lesson_id=course.lesson.id,
                expected_revision=before.revision.value, transaction_id=f"competing-{index}",
                source_branch_name="extra", target_branch_name="main",
                expected_source_head_id=graph.branches["extra"].head_node_id,
                expected_target_head_id=graph.branches["main"].head_node_id,
                document=course.lesson.board_document, label="Concurrent merge",
            )
            return "committed"
        except RidocV3ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(submit, [1, 2])) == ["committed", "conflict"]
    assert len(course.archive().package.lessons[course.lesson.id].merge_records) == 1
