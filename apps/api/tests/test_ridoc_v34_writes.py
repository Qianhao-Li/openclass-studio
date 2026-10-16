from __future__ import annotations
import pytest
from openclass_core.services.lesson_factory import create_empty_lesson
from openclass_core.services.ridoc_history_edit import preview_message_deletion
from openclass_core.services.ridoc_learning_sequence import build_merge_plan
from openclass_core.services.ridoc_v3 import RidocV3Error, read_ridoc_v3, validate_ridoc_v3, write_ridoc_v3
from openclass_core.services.ridoc_v34 import archive_state
from openclass_core.services.ridoc_workspace_store import RidocWorkspaceStore
from test_ridoc_learning_sequence import Course, document
from test_ridoc_v34_states import native, chain

def test_new_merge_validates_actual_blocks_after_source_input_producer_deleted(tmp_path):
    course = Course(tmp_path)
    producer = course.write('commit_document', transaction_id='producer', document=document('A1'))
    course.branch('extra')
    retained = course.write('commit_document', transaction_id='local-change', document=document('A1', 'B2'))
    course.switch('main')
    native(course, [producer])
    plan = build_merge_plan(course.archive(), course.lesson.id, 'extra')
    assert not plan['blockers']
    assert plan['document'] == document('A', 'B2')
    course.merge()
    archive = course.archive()
    assert archive_state(archive, course.lesson.id)['document'] == document('A', 'B2')
    assert archive_state(archive, course.lesson.id, 'extra')['document'] == document('A1', 'B2')
    assert archive.nodes[f'{course.lesson.id}/{retained}'].document_action['input_document_version'] == producer
    validate_ridoc_v3(archive)

def test_new_merge_rejects_real_input_hash_version_conflict(tmp_path):
    course = Course(tmp_path)
    producer = course.write('commit_document', transaction_id='producer', document=document('A1'))
    course.branch('extra')
    course.chat('whole-document', document('A2'))
    course.switch('main')
    native(course, [producer])
    plan = build_merge_plan(course.archive(), course.lesson.id, 'extra')
    assert plan['blockers'] and plan['blockers'][0]['object_id'] == 'document'
    revision = course.record.revision
    with pytest.raises(RidocV3Error, match='cannot be merged'):
        course.merge()
    assert course.archive().revision.value == revision

def test_m_only_merge_preserves_target_c_with_deleted_input(tmp_path):
    course = Course(tmp_path)
    producer = course.write('commit_document', transaction_id='producer', document=document('A1'))
    target = course.write('commit_document', transaction_id='target', document=document('A2'))
    course.branch('extra')
    course.chat('additional-question')
    course.switch('main')
    native(course, [producer])
    before = course.archive()
    occurrence = before.package.lessons[course.lesson.id].branches['main'].document_occurrences[target]
    course.merge()
    assert course.archive().package.lessons[course.lesson.id].branches['main'].document_occurrences[target] == occurrence
    assert archive_state(course.archive(), course.lesson.id)['document'] == document('A2')

def test_manual_session_extension_updates_independent_states_without_orphans(tmp_path):
    course = Course(tmp_path)
    native(course)
    first = course.write('commit_document', transaction_id='manual', document=document('A1'))
    course.write('commit_document', transaction_id='manual', document=document('A2'))
    archive = course.archive()
    assert archive_state(archive, course.lesson.id)['document'] == document('A2')
    assert archive.package.lessons[course.lesson.id].node_order.count(first) == 1
    validate_ridoc_v3(archive)

def test_immediate_m_delete_on_v34_preserves_source_ids_and_states(tmp_path):
    course, changes = chain(tmp_path)
    archive = native(course, [changes[0]])
    message = archive.nodes[f'{course.lesson.id}/{changes[1]}'].parent_ids[0]
    preview = preview_message_deletion(archive, course.lesson.id, message)
    assert changes[1] in preview['retained_document_node_ids']
    result = course.store.delete_history_message(owner_user_id='owner', package_id=course.package.id, lesson_id=course.lesson.id, node_id=message, expected_revision=archive.revision.value, mutation_id='delete-m')
    after = course.archive()
    assert result['deleted_node_id'] == message
    assert after.nodes[f'{course.lesson.id}/{changes[1]}'].parent_ids == [message]
    assert after.node_refs[f'{course.lesson.id}/{message}'].status == 'deleted'
    assert archive_state(after, course.lesson.id)['document'] == document('A3')
    validate_ridoc_v3(after)

def test_v34_lesson_add_delete_and_public_fork_preserve_state_scope(tmp_path):
    course, changes = chain(tmp_path)
    native(course, [changes[0]])
    extra = create_empty_lesson('Additional lesson')
    course.record = course.store.add_lesson(owner_user_id='owner', package_id=course.package.id, expected_revision=course.record.revision, lesson=extra)
    course.record = course.store.delete_lesson(owner_user_id='owner', package_id=course.package.id, lesson_id=extra.id, expected_revision=course.record.revision)
    validate_ridoc_v3(course.archive())
    course.store.publish_package(owner_user_id='owner', package_id=course.package.id, expected_revision=course.record.revision)
    fork = course.store.fork_public_package(owner_user_id='reader', public_package_id=course.package.id)
    archive = course.store.load_archive('reader', package_id=fork.package_id)
    lesson_id = archive.package.lesson_order[0]
    assert lesson_id != course.lesson.id
    assert archive_state(archive, lesson_id)['document'] == document('A3')
    assert archive.node_refs[f'{lesson_id}/{changes[0]}'].status == 'deleted'
    validate_ridoc_v3(archive)

def test_deleted_manual_identity_cannot_reappear_after_import_without_runtime_tombstones(tmp_path):
    course = Course(tmp_path)
    removed = course.write('commit_document', transaction_id='manual', document=document('A1'))
    native(course, [removed])
    with pytest.raises(RidocV3Error, match='cannot be recreated through an old transaction'):
        course.write('commit_document', transaction_id='manual', document=document('A2'))
    assert archive_state(course.archive(), course.lesson.id)['document'] == document()

def test_regeneration_after_parent_c_deleted_uses_surviving_board_and_is_atomic(tmp_path):
    course = Course(tmp_path)
    removed = course.write('commit_document', transaction_id='old-board', document=document('A1'))
    source = course.chat('regenerate this question')
    archive = native(course, [removed])
    original = archive.nodes[f'{course.lesson.id}/{source}'].model_copy(deep=True)
    branches = dict(archive.package.lessons[course.lesson.id].branches)
    with pytest.raises(RidocV3Error, match='complete M'):
        course.write('commit_turn', transaction_id='failed-regenerate', user_message=original.user_message, assistant_message='', branch_action_kind='regenerate', branch_source_node_id=source)
    assert course.archive().package.lessons[course.lesson.id].branches == branches
    assert course.archive().nodes == archive.nodes
    new_id = course.write('commit_turn', transaction_id='success-regenerate', user_message=original.user_message, assistant_message='a complete new answer', branch_action_kind='regenerate', branch_source_node_id=source)
    after = course.archive()
    assert after.nodes[f'{course.lesson.id}/{source}'] == original
    new = after.nodes[f'{course.lesson.id}/{new_id}']
    assert all((f'{course.lesson.id}/{identity}' in after.nodes for identity in new.parent_ids))
    assert new.document_ref != original.document_ref
    assert archive_state(after, course.lesson.id)['document'] == document()
    assert after.package.lessons[course.lesson.id].current_branch != 'main'

@pytest.mark.parametrize('kind', ['manual', 'exchange', 'exchange-and-document'])
def test_export_to_fresh_runtime_keeps_deleted_identity_fence_and_allows_new_actions(tmp_path, kind):
    course = Course(tmp_path)
    transaction = 'original-transaction'
    if kind == 'manual':
        course.write('commit_document', transaction_id=transaction, document=document('removed'))
    else:
        course.write('commit_turn', transaction_id=transaction, user_message='original question', assistant_message='original answer', document=document('removed') if kind == 'exchange-and-document' else None)
    removed = [node.id for node in course.archive().nodes.values() if node.transaction_id == transaction]
    archive = native(course, removed)
    exported = tmp_path / 'exported-v34.ridoc'
    write_ridoc_v3(archive, exported)
    fresh = RidocWorkspaceStore(tmp_path / 'fresh-root', tmp_path / 'fresh-runtime.sqlite3')
    imported = fresh.import_archive('different-owner', read_ridoc_v3(exported))
    kwargs = {'owner_user_id': 'different-owner', 'package_id': imported.package_id, 'lesson_id': course.lesson.id, 'expected_revision': imported.revision}
    path = fresh.package_path('different-owner', imported.package_id)
    before = path.read_bytes()
    with fresh._connect() as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in {'ridoc_deleted_turns', 'ridoc_deleted_actions'} & tables:
            assert connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    with pytest.raises(RidocV3Error, match='cannot be recreated through an old transaction'):
        if kind == 'manual':
            fresh.commit_document(**kwargs, transaction_id=transaction, document=document('stale resurrection'))
        else:
            fresh.commit_turn(**kwargs, transaction_id=transaction, user_message='original question', assistant_message='stale answer', document=document('stale resurrection'))
    assert path.read_bytes() == before
    loaded = fresh.load_archive('different-owner', package_id=imported.package_id)
    assert all((f'{course.lesson.id}/{identity}' not in loaded.nodes for identity in removed))
    assert all((loaded.node_refs[f'{course.lesson.id}/{identity}'].status == 'deleted' for identity in removed))
    saved = fresh.commit_turn(**kwargs, transaction_id='genuinely-new', user_message='new question', assistant_message='new answer', document=document('new board'))
    assert saved.revision != imported.revision
    assert archive_state(fresh.load_archive('different-owner', package_id=imported.package_id), course.lesson.id)['document'] == document('new board')
