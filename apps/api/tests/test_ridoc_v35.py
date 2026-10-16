from __future__ import annotations
from copy import deepcopy
import zipfile
import pytest
from openclass_core.services.ridoc_v3 import RidocV3Error, _revision, canonical_json_bytes, read_ridoc_v3, validate_ridoc_v3, write_ridoc_v3
from openclass_core.services.ridoc_v34 import archive_state, remove_nodes, upgrade_archive
from openclass_core.services.ridoc_v35 import install_range_replay, prepare_range_archive
from test_ridoc_learning_sequence import Course, document

def test_prepare_is_explicit_and_old_archive_roundtrip_stays_pinned(tmp_path):
    course = Course(tmp_path)
    course.chat('question', document('changed'))
    archive = course.archive()
    old = deepcopy(archive)
    target = tmp_path / 'old.ridoc'
    before = write_ridoc_v3(archive, target)
    restored = read_ridoc_v3(target)
    assert write_ridoc_v3(restored, target).value == before.value
    assert restored.manifest['spec_version'] == '3.2'
    prepare_range_archive(restored)
    assert restored.manifest['spec_version'] == '3.5'
    assert old.nodes.keys() == restored.nodes.keys()
    for key, node in old.nodes.items():
        new = restored.nodes[key]
        assert node.model_dump(exclude={'input_state_ref', 'output_state_ref'}) == new.model_dump(exclude={'input_state_ref', 'output_state_ref'})
    new_revision = write_ridoc_v3(restored, target).value
    assert write_ridoc_v3(read_ridoc_v3(target), target).value == new_revision

def test_other_branch_keeps_last_deleted_c_document_and_can_continue(tmp_path):
    course = Course(tmp_path)
    removed = course.write('commit_document', transaction_id='first', document=document('A1'))
    course.branch('other')
    course.switch('main')
    archive = course.archive()
    prepare_range_archive(archive)
    graph = archive.package.lessons[course.lesson.id]
    original = archive_state(archive, course.lesson.id, 'other')['document_ref']
    c0 = graph.branches['main'].learning_node_ids[0]
    graph.branches['main'].current_state_ref = graph.branches['main'].document_occurrences[c0].after_state_ref
    remove_nodes(archive, {course.lesson.id: [removed]})
    assert archive_state(archive, course.lesson.id, 'main')['document'] == document()
    assert archive_state(archive, course.lesson.id, 'other')['document_ref'] == original
    assert archive.node_refs[f'{course.lesson.id}/{removed}'].status == 'deleted'
    validate_ridoc_v3(archive)
    course.record = course.store.mutate('owner', course.package.id, course.record.revision, lambda saved: saved.__dict__.update(deepcopy(archive.__dict__)))
    course.switch('other')
    course.chat('question after last C removal')
    assert archive_state(course.archive(), course.lesson.id)['document'] == document('A1')
    course.write('commit_document', transaction_id='continue', document=document('A2'))
    assert course.archive().manifest['spec_version'] == '3.5'
    assert archive_state(course.archive(), course.lesson.id)['document'] == document('A2')

@pytest.mark.parametrize('tamper', ['missing-current', 'wrong-current', 'hidden-proof', 'hidden-trace', 'downgrade'])
def test_v35_current_state_and_proof_objects_fail_closed(tmp_path, tamper):
    course = Course(tmp_path)
    archive = course.archive()
    prepare_range_archive(archive)
    branch = archive.package.lessons[course.lesson.id].branches['main']
    if tamper == 'missing-current':
        branch.current_state_ref = None
    elif tamper == 'wrong-current':
        branch.current_state_ref = 'f' * 64
    elif tamper == 'hidden-proof':
        archive.range_proofs['f' * 64] = {'unreferenced': True}
    elif tamper == 'hidden-trace':
        archive.range_traces['f' * 64] = {'unreferenced': True}
    else:
        archive.manifest['spec_version'] = '3.4'
    with pytest.raises(RidocV3Error):
        validate_ridoc_v3(archive)

def test_v34_upgrade_does_not_downgrade_range_archive(tmp_path):
    archive = Course(tmp_path).archive()
    prepare_range_archive(archive)
    before = deepcopy(archive)
    upgrade_archive(archive)
    assert archive == before

def test_message_only_merge_preserves_independent_state_after_final_c_removal(tmp_path):
    course = Course(tmp_path)
    change = course.write('commit_document', transaction_id='first', document=document('A1'))
    course.branch('extra')
    course.chat('source question')
    course.switch('main')

    def remove(archive):
        prepare_range_archive(archive)
        remove_nodes(archive, {course.lesson.id: [change]})
    course.record = course.store.mutate('owner', course.package.id, course.record.revision, remove)
    before = archive_state(course.archive(), course.lesson.id)['document']
    course.merge()
    assert before == document('A1')
    assert archive_state(course.archive(), course.lesson.id)['document'] == before

def test_content_addressed_object_alias_paths_are_rejected(tmp_path):
    archive = Course(tmp_path).archive()
    prepare_range_archive(archive)
    path = tmp_path / 'alias.ridoc'
    write_ridoc_v3(archive, path)
    with zipfile.ZipFile(path) as handle:
        payloads = {name: handle.read(name) for name in handle.namelist()}
    payloads.pop('integrity/checksums.json')
    document_path = next((name for name in payloads if name.startswith('documents/')))
    payloads[document_path.replace('documents/', 'documents/alias/')] = payloads.pop(document_path)
    revision, checksums = _revision(payloads)
    payloads['integrity/checksums.json'] = canonical_json_bytes({'algorithm': 'sha256', 'revision': revision, 'files': checksums})
    with zipfile.ZipFile(path, 'w') as handle:
        for name, payload in payloads.items():
            handle.writestr(name, payload)
    with pytest.raises(RidocV3Error, match='path'):
        read_ridoc_v3(path)

def selective_archive(tmp_path):
    from openclass_core.services.ridoc_range_history import build_range_history
    course = Course(tmp_path)
    first = course.write('commit_document', transaction_id='first', document=document('A1', 'B'))
    second = course.write('commit_document', transaction_id='second', document=document('A1', 'B2'))
    archive = course.archive()
    before = deepcopy(archive.nodes)
    prepare_range_archive(archive)
    graph = archive.package.lessons[course.lesson.id]
    c0 = archive.nodes[f'{course.lesson.id}/{graph.node_order[0]}']
    from openclass_core.models import BoardDocument
    initial = BoardDocument.model_validate(archive.documents[c0.after_document_ref])
    actions = [{'node_id': identity, 'before': BoardDocument.model_validate(archive.documents[archive.nodes[f'{course.lesson.id}/{identity}'].before_document_ref]), 'after': BoardDocument.model_validate(archive.documents[archive.nodes[f'{course.lesson.id}/{identity}'].after_document_ref])} for identity in (first, second)]
    result = build_range_history(initial, actions, excluded_node_ids=[first])
    install_range_replay(archive, course.lesson.id, 'main', result)
    assert archive_state(archive, course.lesson.id)['document'] == document('A', 'B2')
    for key, original in before.items():
        assert original.model_dump(exclude={'input_state_ref', 'output_state_ref'}) == archive.nodes[key].model_dump(exclude={'input_state_ref', 'output_state_ref'})
    remove_nodes(archive, {course.lesson.id: [first]})
    return (course, archive, second)

def test_range_replay_roundtrip_preserves_original_c_and_new_actual_state(tmp_path):
    course, archive, second = selective_archive(tmp_path)
    path = tmp_path / 'range.ridoc'
    revision = write_ridoc_v3(archive, path)
    restored = read_ridoc_v3(path)
    assert write_ridoc_v3(restored, path).value == revision.value
    assert archive_state(restored, course.lesson.id)['document'] == document('A', 'B2')
    occurrence = restored.package.lessons[course.lesson.id].branches['main'].document_occurrences[second]
    assert occurrence.range_proof_ref and occurrence.input_trace_ref and occurrence.output_trace_ref
    node = restored.nodes[f'{course.lesson.id}/{second}']
    assert restored.document_states[occurrence.after_state_ref].document_ref != node.after_document_ref
    for trace in restored.range_traces.values():
        assert all(('value' not in token for block in trace['blocks'].values() for token in block['tokens']))
        assert all(('shell' not in block for block in trace['blocks'].values()))
    assert all(('compiled_action_hash' in proof['action'] and 'operations' not in proof['action'] for proof in restored.range_proofs.values()))

def test_second_range_replay_can_derive_from_verified_branch_occurrence(tmp_path):
    from openclass_core.services.ridoc_range_history import build_range_history
    from openclass_core.services.ridoc_v34_states import state_result
    course, archive, second = selective_archive(tmp_path)
    branch = archive.package.lessons[course.lesson.id].branches['main']
    source = branch.document_occurrences[second].model_copy(deep=True)
    initial = state_result(archive, source.before_state_ref)['document']
    after = state_result(archive, source.after_state_ref)['document']
    replay = build_range_history(initial, [{'node_id': second, 'before': initial, 'after': after}])
    install_range_replay(archive, course.lesson.id, 'main', replay)
    occurrence = archive.package.lessons[course.lesson.id].branches['main'].document_occurrences[second]
    proof = archive.range_proofs[occurrence.range_proof_ref]
    assert proof['source_occurrence']['range_proof_ref'] == source.range_proof_ref
    assert source.range_proof_ref in archive.range_proofs
    path = tmp_path / 'twice.ridoc'
    write_ridoc_v3(archive, path)
    assert archive_state(read_ridoc_v3(path), course.lesson.id)['document'] == document('A', 'B2')

def test_verified_v34_discontinuity_remains_readable_and_range_editable(tmp_path):
    from openclass_core.services.ridoc_range_history import build_range_history
    from openclass_core.services.ridoc_v34_states import state_result
    course = Course(tmp_path)
    first = course.write('commit_document', transaction_id='first', document=document('A1'))
    second = course.write('commit_document', transaction_id='second', document=document('A1', 'B2'))
    archive = course.archive()
    remove_nodes(archive, {course.lesson.id: [first]})
    prepare_range_archive(archive)
    branch = archive.package.lessons[course.lesson.id].branches['main']
    initial = state_result(archive, branch.document_occurrences[branch.learning_node_ids[0]].after_state_ref)['document']
    source = branch.document_occurrences[second]
    replay = build_range_history(initial, [{'node_id': second, 'before': state_result(archive, source.before_state_ref)['document'], 'after': state_result(archive, source.after_state_ref)['document']}])
    assert replay['occurrences'][0]['proof']['checkpoint']
    install_range_replay(archive, course.lesson.id, 'main', replay)
    assert archive_state(archive, course.lesson.id)['document'] == document('A1', 'B2')
    validate_ridoc_v3(archive)

def test_range_codec_rebinds_stable_blocks_after_inserted_paragraph_is_removed(tmp_path):
    from openclass_core.models import BoardDocument, CoursePackage
    from openclass_core.services.lesson_factory import create_empty_lesson
    from openclass_core.services.rich_document import rebuild_document_from_content_json
    from openclass_core.services.ridoc_range_history import build_range_history
    from openclass_core.services.ridoc_workspace_store import RidocWorkspaceStore

    def board(*values):
        return rebuild_document_from_content_json(BoardDocument(id='board', title='Board'), {'type': 'doc', 'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': value}]} for value in values]})
    initial, first, second = (board('alpha', 'beta'), board('new', 'alpha', 'beta'), board('new', 'alpha', 'BETA'))
    lesson = create_empty_lesson('Range blocks')
    lesson.board_document = initial
    package = CoursePackage(title='Ranges', summary='', lessons=[lesson], active_lesson_id=lesson.id)
    store = RidocWorkspaceStore(tmp_path / 'ridoc', tmp_path / 'runtime.sqlite3')
    record = store.create_from_package('owner', package)
    ids = []
    for index, value in enumerate((first, second)):
        record = store.commit_document(owner_user_id='owner', package_id=package.id, lesson_id=lesson.id, expected_revision=record.revision, transaction_id=f'edit-{index}', document=value)
        ids.append(store.load_archive('owner', package_id=package.id).package.lessons[lesson.id].branches['main'].head_node_id)
    archive = store.load_archive('owner', package_id=package.id)
    replay = build_range_history(initial, [{'node_id': ids[0], 'before': initial, 'after': first}, {'node_id': ids[1], 'before': first, 'after': second}], excluded_node_ids=[ids[0]])
    install_range_replay(archive, lesson.id, 'main', replay)
    remove_nodes(archive, {lesson.id: [ids[0]]})
    path = tmp_path / 'blocks.ridoc'
    write_ridoc_v3(archive, path)
    assert archive_state(read_ridoc_v3(path), lesson.id)['document'] == board('alpha', 'BETA')

def test_selective_removal_keeps_legacy_cursor_suffix_available_for_redo(tmp_path):
    from openclass_core.models import BoardDocument
    from openclass_core.services.ridoc_document_cursor import document_history_state
    from openclass_core.services.ridoc_range_history import build_range_history
    course = Course(tmp_path)
    ids = [course.write('commit_document', transaction_id=f'edit-{index}', document=value) for index, value in enumerate((document('A1'), document('A1', 'B2'), document('A3', 'B2')))]
    archive = course.archive()
    archive.manifest['spec_version'] = '3.3'
    archive.package.lessons[course.lesson.id].branches['main'].document_cursor_node_id = ids[1]
    prepare_range_archive(archive)
    actions = []
    for identity in ids:
        node = archive.nodes[f'{course.lesson.id}/{identity}']
        actions.append({'node_id': identity, 'before': BoardDocument.model_validate(archive.documents[node.before_document_ref]), 'after': BoardDocument.model_validate(archive.documents[node.after_document_ref])})
    replay = build_range_history(document(), actions, excluded_node_ids=[ids[0], ids[2]])
    replay['preserve_withdrawn_node_ids'] = [ids[2]]
    install_range_replay(archive, course.lesson.id, 'main', replay)
    remove_nodes(archive, {course.lesson.id: [ids[0]]})
    assert archive_state(archive, course.lesson.id)['document'] == document('A', 'B2')
    state = document_history_state(archive, course.lesson.id)
    assert state['redo_node_id'] == ids[2] and state['withdrawn_node_ids'] == [ids[2]]
    assert ids[2] in archive.package.lessons[course.lesson.id].branches['main'].document_occurrences
    validate_ridoc_v3(archive)

def test_range_proof_public_fork_remaps_source_states_and_keeps_replay(tmp_path):
    course, archive, _second = selective_archive(tmp_path)
    course.record = course.store.mutate('owner', course.package.id, course.record.revision, lambda saved: saved.__dict__.update(deepcopy(archive.__dict__)))
    course.store.publish_package(owner_user_id='owner', package_id=course.package.id, expected_revision=course.record.revision)
    fork = course.store.fork_public_package(owner_user_id='reader', public_package_id=course.package.id)
    copied = course.store.load_archive('reader', package_id=fork.package_id)
    lesson_id = copied.package.lesson_order[0]
    assert lesson_id != course.lesson.id
    assert archive_state(copied, lesson_id)['document'] == document('A', 'B2')
    assert all((proof['lesson_id'] == lesson_id for proof in copied.range_proofs.values()))
    validate_ridoc_v3(copied)

@pytest.mark.parametrize('tamper', ['missing-proof', 'proof-hash', 'trace-hash', 'source-state', 'input-trace'])
def test_range_occurrence_tampering_fails_closed(tmp_path, tamper):
    from openclass_core.services.ridoc_v3 import content_hash
    course, archive, second = selective_archive(tmp_path)
    occurrence = archive.package.lessons[course.lesson.id].branches['main'].document_occurrences[second]
    if tamper == 'missing-proof':
        del archive.range_proofs[occurrence.range_proof_ref]
    elif tamper == 'proof-hash':
        archive.range_proofs[occurrence.range_proof_ref]['node_id'] = 'forged'
    elif tamper == 'trace-hash':
        archive.range_traces[occurrence.input_trace_ref]['document_hash'] = 'f' * 64
    elif tamper == 'source-state':
        proof = deepcopy(archive.range_proofs[occurrence.range_proof_ref])
        proof['source_occurrence']['after_state_ref'] = occurrence.before_state_ref
        occurrence.range_proof_ref = content_hash(proof)
        archive.range_proofs[occurrence.range_proof_ref] = proof
    else:
        occurrence.input_trace_ref = occurrence.output_trace_ref
    with pytest.raises(RidocV3Error):
        validate_ridoc_v3(archive)

def test_rehashed_consistent_producer_tampering_cannot_break_input_trace_chain(tmp_path):
    from openclass_core.services.ridoc_range_history import replay_range_proof
    from openclass_core.services.ridoc_v3 import content_hash
    from openclass_core.services.ridoc_v34_references import prune_objects
    from openclass_core.services.ridoc_v34_states import state_result
    from openclass_core.services.ridoc_v35 import _action, _put_action, _put_trace, _trace
    course, archive, second = selective_archive(tmp_path)
    branch = archive.package.lessons[course.lesson.id].branches['main']
    occurrence = branch.document_occurrences[second]
    envelope = deepcopy(archive.range_proofs[occurrence.range_proof_ref])
    source = envelope['source_occurrence']
    before = state_result(archive, occurrence.before_state_ref)['document']
    source_before = state_result(archive, source['before_state_ref'])['document']
    source_after = state_result(archive, source['after_state_ref'])['document']
    core = _action(archive, envelope['action'], source_before, source_after)
    input_trace = _trace(archive, occurrence.input_trace_ref, before)
    for trace in (input_trace, core['source_trace']):
        for block in trace['blocks'].values():
            for token in [*block['tokens'], *block['gaps']]:
                if not token['producer']:
                    token['producer'] = second
    result, output_trace = replay_range_proof(core, source_before, source_after, before, input_trace)
    assert result == state_result(archive, occurrence.after_state_ref)['document']
    occurrence.input_trace_ref = _put_trace(archive, input_trace)
    occurrence.output_trace_ref = _put_trace(archive, output_trace)
    branch.current_trace_ref = occurrence.output_trace_ref
    envelope['action'] = _put_action(archive, core)
    occurrence.range_proof_ref = content_hash(envelope)
    archive.range_proofs[occurrence.range_proof_ref] = envelope
    prune_objects(archive)
    with pytest.raises(RidocV3Error, match='lineage does not connect'):
        validate_ridoc_v3(archive)
