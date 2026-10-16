from __future__ import annotations
import json
import zipfile
import pytest
from openclass_core.models import CoursePackage
from openclass_core.services.lesson_factory import create_empty_lesson
from openclass_core.services.rich_document import build_document
from openclass_core.services.ridoc_document_cursor import applied_branch_replay, document_history_state
from openclass_core.services.ridoc_learning_sequence import set_branch_sequence
from openclass_core.services.ridoc_v3 import RidocDocumentNode, RidocMessageNode, RidocV3Error, content_hash, new_ridoc_v3, read_ridoc_v3, store_document, validate_ridoc_v3, write_ridoc_v3
from openclass_core.services.ridoc_workspace_store import RidocWorkspaceStore, materialize_course_package
from openclass_core.services.ridoc_history import history_page

def imported_store(tmp_path, *, withdrawn=False):
    archive, lesson_id = archive_fixture()
    if withdrawn:
        archive.package.lessons[lesson_id].branches['main'].document_cursor_node_id = archive.package.lessons[lesson_id].node_order[0]
    store = RidocWorkspaceStore(tmp_path / 'packages', tmp_path / 'index.sqlite3')
    write_ridoc_v3(archive, tmp_path / 'source.ridoc')
    record = store.import_archive(owner_user_id='owner', archive=archive)
    return (store, archive, lesson_id, record)

@pytest.mark.parametrize('withdrawn', [False, True])
def test_native_turns_and_autosave_preserve_sequences(tmp_path, withdrawn):
    store, original, lesson_id, record = imported_store(tmp_path, withdrawn=withdrawn)
    before = store.load_package('owner', package_id=original.package.id)
    document = build_document(title='Changed', document_id=before.lessons[0].board_document.id, content_text='Saved new content')
    record = store.commit_turn(owner_user_id='owner', package_id=original.package.id, lesson_id=lesson_id, expected_revision=record.revision, transaction_id='successful', user_message='New question', assistant_message='New complete answer', document=document)
    saved = store.load_archive('owner', package_id=original.package.id)
    graph = saved.package.lessons[lesson_id]
    active = graph.branches[graph.current_branch]
    assert saved.manifest['spec_version'] == '3.3'
    assert len(saved.nodes) == len(original.nodes) + 2
    assert materialize_course_package(saved).lessons[0].board_document == document.model_copy(update={'title': before.lessons[0].title})
    assert [node.id for node in history_page(saved, lesson_id).nodes] == active.learning_node_ids
    assert [saved.nodes[f'{lesson_id}/{i}'].kind for i in active.learning_node_ids[-2:]] == ['M', 'C']
    if withdrawn:
        assert graph.current_branch != 'main'
        assert graph.branches['main'] == original.package.lessons[lesson_id].branches['main']
        assert 'c1' not in active.learning_node_ids
    else:
        assert active.learning_node_ids[:3] == original.package.lessons[lesson_id].branches['main'].learning_node_ids
    retry = store.commit_turn(owner_user_id='owner', package_id=original.package.id, lesson_id=lesson_id, expected_revision=original.revision.value, transaction_id='successful', user_message='New question', assistant_message='New complete answer', document=document)
    assert retry.revision == record.revision
    with pytest.raises(RidocV3Error):
        store.commit_document(owner_user_id='owner', package_id=original.package.id, lesson_id=lesson_id, expected_revision=original.revision.value, transaction_id='stale', document=document)
    new_document = document.model_copy(update={'title': 'Manual content'})
    record = store.commit_document(owner_user_id='owner', package_id=original.package.id, lesson_id=lesson_id, expected_revision=record.revision, transaction_id='manual', document=new_document)
    store.commit_document(owner_user_id='owner', package_id=original.package.id, lesson_id=lesson_id, expected_revision=record.revision, transaction_id='manual', document=new_document.model_copy(update={'title': 'Continued'}))
    assert store.load_package('owner', package_id=original.package.id).lessons[0].board_document.title == 'Continued'

def archive_fixture(version='3.3'):
    lesson = create_empty_lesson('Compatibility')
    archive = new_ridoc_v3(CoursePackage(title='Compatibility', summary='', lessons=[lesson]))
    graph = archive.package.lessons[lesson.id]
    c0 = archive.nodes[f'{lesson.id}/{graph.node_order[0]}']
    document = build_document(title=lesson.board_document.title, document_id=lesson.board_document.id, content_text='A complete document change.')
    c1 = RidocDocumentNode(id='c1', lesson_id=lesson.id, branch_name='main', parent_ids=[c0.id], created_at=lesson.created_at, transaction_id='edit', action_id='edit', before_document_ref=c0.after_document_ref, after_document_ref=store_document(archive.documents, document))
    m1 = RidocMessageNode(id='m1', lesson_id=lesson.id, branch_name='main', parent_ids=[c1.id], created_at=lesson.created_at, transaction_id='turn', user_message='Question', assistant_message='Complete answer', document_ref=c1.after_document_ref)
    archive.nodes.update({f'{lesson.id}/c1': c1, f'{lesson.id}/m1': m1})
    graph.node_order.extend(['c1', 'm1'])
    graph.branches['main'].head_node_id = 'm1'
    archive.manifest['spec_version'] = version
    if version in {'3.2', '3.3'}:
        set_branch_sequence(archive, lesson.id, 'main', [c0.id, 'm1', 'c1'])
    return (archive, lesson.id)

@pytest.mark.parametrize('version', ['3.0', '3.1', '3.2', '3.3'])
def test_versions_roundtrip_without_revision_or_metadata_loss(tmp_path, version):
    archive, lesson_id = archive_fixture(version)
    if version != '3.0':
        archive.nodes[f'{lesson_id}/c1'].source_message_deleted = True
        archive.nodes[f'{lesson_id}/m1'].branch_origin_deleted = True
    original = tmp_path / 'original.ridoc'
    revision = write_ridoc_v3(archive, original)
    before = original.read_bytes()
    restored = read_ridoc_v3(original)
    assert write_ridoc_v3(restored, tmp_path / 'copy.ridoc') == revision
    assert restored.package == archive.package
    assert restored.nodes == archive.nodes
    assert original.read_bytes() == before
    assert restored.manifest['spec_version'] == version
    if version == '3.0':
        with zipfile.ZipFile(original) as z:
            branch = json.loads(z.read('package.json'))['lessons'][lesson_id]['branches']['main']
            assert 'document_cursor_node_id' not in branch
            assert 'learning_node_ids' not in branch

def test_cursor_retains_all_history_and_uses_effective_document(tmp_path):
    archive, lesson_id = archive_fixture()
    graph = archive.package.lessons[lesson_id]
    c0_id = graph.node_order[0]
    graph.branches['main'].document_cursor_node_id = c0_id
    path = tmp_path / 'course.ridoc'
    write_ridoc_v3(archive, path)
    restored = read_ridoc_v3(path)
    assert document_history_state(restored, lesson_id)['withdrawn_node_ids'] == ['c1']
    assert applied_branch_replay(restored, lesson_id)['document_ref'] == archive.nodes[f'{lesson_id}/{c0_id}'].after_document_ref
    assert restored.package.lessons[lesson_id].branches['main'].head_node_id == 'm1'
    assert len(restored.nodes) == 3

@pytest.mark.parametrize('damage', ['cache', 'missing_cursor', 'message_cursor', 'future_version', 'old_version', 'context'])
def test_invalid_extensions_fail_closed(damage):
    archive, lesson_id = archive_fixture()
    branch = archive.package.lessons[lesson_id].branches['main']
    if damage == 'cache':
        branch.replay_document_refs['c1'] = '0' * 64
    elif damage == 'missing_cursor':
        branch.document_cursor_node_id = 'missing'
    elif damage == 'message_cursor':
        branch.document_cursor_node_id = 'm1'
    elif damage == 'future_version':
        archive.manifest['spec_version'] = '3.4'
    elif damage == 'old_version':
        archive.manifest['spec_version'] = '3.0'
    else:
        archive.nodes[f'{lesson_id}/m1'].learning_context_node_ids = ['m1']
    with pytest.raises(RidocV3Error):
        validate_ridoc_v3(archive)
