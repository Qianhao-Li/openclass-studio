"""Frozen requirements for historical input retries, outside canonical lesson runtime."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from openclass_core.services.history import current_head_commit
from openclass_core.services.ridoc_chat_context import ridoc_completion_guard
from openclass_core.services.ridoc_v3 import RidocV3ConflictError
from openclass_core.services.ridoc_workflow_runtime import RUNTIME_FIELDS, WORKFLOW_METADATA_KEYS, restore_runtime, runtime_payload
from openclass_core.services.source_content_contract import content_digest
from openclass_core.services.teaching_policy import assert_board_unchanged
CHECKPOINT_KEY = 'historical_frozen_requirement'
_CURRENT: ContextVar['HistoricalRequirementCheckpoint | None'] = ContextVar('historical_requirement_checkpoint', default=None)

def current_historical_requirement_checkpoint():
    return _CURRENT.get()

class HistoricalRequirementCheckpoint:

    def __init__(self, *, store, context, request, document, parent_id):
        self.store, self.context = (store, context)
        self.document = document.model_copy(deep=True)
        self.parent_id = parent_id
        self.revision = request.expected_ridoc_revision
        self.identity = {'owner_user_id': context.owner_user_id, 'package_id': context.package_id, 'lesson_id': context.lesson_id, 'turn_id': context.transaction_id, 'session_id': context.session_id, 'input_event_id': context.input_event_id, 'epoch': context.epoch, 'revision': self.revision, 'parent_id': parent_id, 'history_action': request.history_branch_action.model_dump(mode='json'), 'document_hash': content_digest(self.document.model_dump(mode='json')), 'request_hash': content_digest(request.model_dump(mode='json', exclude={'conversation'}))}

    def _turn(self):
        return self.store.runtime_turn(owner_user_id=self.context.owner_user_id, package_id=self.store.deletion_scope_id(self.context.package_id), lesson_id=self.context.lesson_id, turn_id=self.context.transaction_id)

    def _matching_checkpoint(self):
        row = self._turn()
        checkpoint = (row or {}).get('payload', {}).get(CHECKPOINT_KEY)
        if isinstance(checkpoint, dict) and checkpoint.get('scope') == self.identity:
            return checkpoint
        return None

    def preserve(self, payload):
        result = deepcopy(payload)
        checkpoint = self._matching_checkpoint()
        if checkpoint is not None:
            result[CHECKPOINT_KEY] = deepcopy(checkpoint)
        return result

    @contextmanager
    def bind(self):
        self.context.assert_current()
        token = _CURRENT.set(self)
        try:
            yield self
        finally:
            _CURRENT.reset(token)

    def restore(self, lesson):
        if lesson.id != self.context.lesson_id:
            raise RidocV3ConflictError('The historical checkpoint belongs to another lesson')
        assert_board_unchanged(self.document, lesson.board_document)
        for name in RUNTIME_FIELDS:
            setattr(lesson, name, None)
        head = current_head_commit(lesson)
        for name in WORKFLOW_METADATA_KEYS:
            head.metadata.pop(name, None)
        head.runtime_snapshot = None
        checkpoint = self._matching_checkpoint()
        if checkpoint is None:
            return
        restored = restore_runtime(lesson, checkpoint['runtime'], self.revision)
        for name in RUNTIME_FIELDS:
            setattr(lesson, name, getattr(restored, name))
        restored_head = current_head_commit(restored)
        head.metadata = restored_head.metadata
        head.runtime_snapshot = restored_head.runtime_snapshot

    def save(self, lesson):
        with ridoc_completion_guard(self.context):
            if lesson.id != self.context.lesson_id:
                raise RidocV3ConflictError('The historical checkpoint belongs to another lesson')
            archive = self.store.load_archive(self.context.owner_user_id, package_id=self.context.package_id)
            if archive.revision.value != self.revision:
                raise RidocV3ConflictError('The course changed before its historical frozen checkpoint')
            assert_board_unchanged(self.document, lesson.board_document)
            branch = lesson.history_graph.branches[lesson.history_graph.current_branch]
            if branch.base_commit_id != self.parent_id:
                raise RidocV3ConflictError('The frozen requirement belongs to a different historical parent')
            historical_base = lesson.model_copy(deep=True)
            historical_base.history_graph.branches[historical_base.history_graph.current_branch].head_commit_id = self.parent_id
            runtime = runtime_payload(lesson, historical_base, self.revision)
            payload = deepcopy((self._turn() or {}).get('payload', {}))
            payload[CHECKPOINT_KEY] = {'scope': self.identity, 'runtime': runtime}
            self.store.stage_runtime_turn(owner_user_id=self.context.owner_user_id, package_id=self.context.package_id, lesson_id=self.context.lesson_id, turn_id=self.context.transaction_id, state='pending', payload=payload)
