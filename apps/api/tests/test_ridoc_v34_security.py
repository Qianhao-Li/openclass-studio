from __future__ import annotations

import pytest
from pathlib import Path

from openclass_core.services.ridoc_v3 import RidocV3Error, store_document, validate_ridoc_v3
from test_ridoc_learning_sequence import Course, document
from test_ridoc_v34_states import native


def test_source_action_input_version_must_match_its_independent_input_state(tmp_path):
    course = Course(tmp_path)
    message = course.chat("question")
    change = course.write("commit_document", transaction_id="manual", document=document("A1"))
    archive = native(course)
    node = archive.nodes[f"{course.lesson.id}/{change}"]
    assert node.document_action["scope"] == "blocks"
    node.document_action["input_document_version"] = message
    with pytest.raises(RidocV3Error, match="original.*version|input.*version"):
        validate_ridoc_v3(archive)


def test_unused_document_payloads_cannot_hide_in_v34_package(tmp_path):
    course = Course(tmp_path)
    archive = native(course)
    store_document(archive.documents, document("unreferenced hidden payload"))
    with pytest.raises(RidocV3Error, match="document.*reference|reference.*document"):
        validate_ridoc_v3(archive)


def test_republishing_does_not_report_success_for_corrupt_immutable_snapshot(tmp_path):
    course = Course(tmp_path)
    native(course)
    course.store.publish_package(owner_user_id="owner", package_id=course.package.id,
                                  expected_revision=course.record.revision)
    public = course.store.root / "published" / course.package.id / f"{course.record.revision}.ridoc"
    public.write_bytes(b"corrupted immutable snapshot")
    with pytest.raises(RidocV3Error):
        course.store.publish_package(owner_user_id="owner", package_id=course.package.id,
                                      expected_revision=course.record.revision)


@pytest.mark.parametrize("identifier", ["../escape", "/absolute/published", "..\\escape", ".", ".."])
def test_imported_package_identity_cannot_escape_published_storage(tmp_path, identifier):
    course = Course(tmp_path)
    archive = native(course)
    archive.package.id = identifier
    imported = course.store.import_archive("publisher", archive)
    course.store.publish_package(owner_user_id="publisher", package_id=identifier,
                                  expected_revision=imported.revision)
    with course.store._connect() as connection:
        path = connection.execute("SELECT ridoc_path FROM ridoc_public_index WHERE package_id=?", (identifier,)).fetchone()[0]
    assert Path(path).resolve().is_relative_to((course.store.root / "published").resolve())
    public, _ = course.store.load_public_archive(identifier)
    assert public.package.id == identifier


def test_public_read_rejects_an_index_path_outside_published_storage(tmp_path):
    course = Course(tmp_path)
    native(course)
    course.store.publish_package(owner_user_id="owner", package_id=course.package.id,
                                  expected_revision=course.record.revision)
    with course.store._connect() as connection:
        connection.execute("UPDATE ridoc_public_index SET ridoc_path=? WHERE package_id=?",
                           (str(course.store.package_path("owner", course.package.id)), course.package.id))
    with pytest.raises(RidocV3Error, match="outside"):
        course.store.load_public_archive(course.package.id)
