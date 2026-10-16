"""Revision-aware persistence of explicit history edits."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from openclass_core.services.ridoc_deletion_store import RidocDeletionJournal
from openclass_core.services.ridoc_lock import ridoc_file_lock
from openclass_core.services.ridoc_v3 import RidocArchiveV3, RidocV3ConflictError, RidocV3Error, write_ridoc_v3

class RidocHistoryMutationMixin:
    """Shared by workspace and native Repository stores; no legacy writes."""

    def deletion_scope_id(self, package_id: str) -> str:
        return package_id

    @property
    def deletion_journal(self) -> RidocDeletionJournal:
        return self._deletion_journal

    def _deletion_checkpoint(self, phase: str) -> None:
        """Fault injection seam; production never changes behavior here."""

    def _validate_deletion_version(self) -> None:
        """Repository adapter checks its additional optimistic version here."""

    def _recover_deletions(self, owner_user_id: str, path: Path, archive: RidocArchiveV3) -> None:
        from openclass_core.services.ridoc_sequence_mutations import recover_sequence_mutations
        package_id = archive.package.id
        scope = self.deletion_scope_id(package_id)
        actual = archive.revision.value if archive.revision else ''
        for record in self.deletion_journal.pending(owner_user_id, scope):
            if actual == record['old_revision']:
                self.deletion_journal.abort(owner_user_id, scope, record['mutation_id'])
            elif actual == record['new_revision']:
                self._upsert_index(owner_user_id, path, archive)
                self.deletion_journal.complete(record, runtime_package_id=scope)
            else:
                raise RidocV3Error('An interrupted history edit has an unexpected RIDOC revision')
        recover_sequence_mutations(self, owner_user_id, path, archive)
        from openclass_core.services.ridoc_history_order_store import recover_history_orders
        recover_history_orders(self, owner_user_id, path, archive)
        from openclass_core.services.ridoc_document_history_store import recover_document_history_steps
        recover_document_history_steps(self, owner_user_id, path, archive)
        from openclass_core.services.ridoc_edit_session_store import recover_edit_sessions
        recover_edit_sessions(self, owner_user_id, path, archive)

    def preview_message_deletion(self, *, owner_user_id: str, lesson_id: str, node_id: str) -> dict[str, Any]:
        from openclass_core.services.ridoc_history_edit import preview_message_deletion
        archive = self.load_archive(owner_user_id, lesson_id=lesson_id)
        if f'{lesson_id}/{node_id}' not in archive.nodes:
            raise KeyError(node_id)
        return preview_message_deletion(archive, lesson_id, node_id)

    def delete_history_message(self, *, owner_user_id: str, package_id: str, lesson_id: str, node_id: str, expected_revision: str, mutation_id: str) -> dict[str, Any]:
        from openclass_core.services.ridoc_history_edit import delete_message_node
        from openclass_core.services.ridoc_v3 import ridoc_revision_for_archive
        if not mutation_id.strip() or not expected_revision.strip():
            raise RidocV3Error('Deletion requires a mutation identity and expected RIDOC revision')
        path = self.package_path(owner_user_id, package_id)
        scope = self.deletion_scope_id(package_id)
        with self._lock, ridoc_file_lock(path):
            archive = self.load_archive(owner_user_id, package_id=package_id)
            from openclass_core.services.ridoc_edit_session_store import assert_no_pending_history
            assert_no_pending_history(self, owner_user_id, package_id)
            actual = archive.revision.value if archive.revision else ''
            previous_operation = self.deletion_journal.get(owner_user_id, scope, mutation_id)
            if previous_operation:
                if (previous_operation['lesson_id'], previous_operation['node_id'], previous_operation['old_revision']) != (lesson_id, node_id, expected_revision):
                    raise RidocV3ConflictError('Deletion identity was reused with different parameters')
                result = json.loads(previous_operation['result_json'])
                return {**result, 'ridoc_revision': actual, 'mutation_id': mutation_id}
            self._validate_deletion_version()
            if actual != expected_revision:
                raise RidocV3ConflictError('RIDOC revision changed; review the deletion again')
            previous = self._existing_index(owner_user_id, package_id)
            edited = archive
            result = delete_message_node(edited, lesson_id, node_id)
            result = {key: result[key] for key in ('deleted_node_id', 'deleted_transaction_id', 'affected_branches', 'retained_document_node_ids')}
            new_revision = ridoc_revision_for_archive(edited)
            self._deletion_checkpoint('before_prepare')
            self.deletion_journal.prepare(owner_user_id, scope, mutation_id, lesson_id, node_id, result['deleted_transaction_id'], actual, new_revision, result)
            self._deletion_checkpoint('prepared')
            edited.revision = write_ridoc_v3(edited, path, expected_revision=actual)
            self._deletion_checkpoint('replaced')
            self._upsert_index(owner_user_id, path, edited, previous=previous)
            self._deletion_checkpoint('indexed')
            record = self.deletion_journal.get(owner_user_id, scope, mutation_id)
            assert record is not None
            self.deletion_journal.complete(record, runtime_package_id=scope)
            self._deletion_checkpoint('completed')
            return {**result, 'ridoc_revision': edited.revision.value, 'mutation_id': mutation_id}

    def assert_turn_not_deleted(self, *, owner_user_id: str, package_id: str, lesson_id: str, transaction_id: str, session_id: str='', input_event_id: str='') -> None:
        with self._lock, ridoc_file_lock(self.package_path(owner_user_id, package_id)):
            self.load_archive(owner_user_id, package_id=package_id)
            self.deletion_journal.assert_not_deleted(owner_user_id, self.deletion_scope_id(package_id), lesson_id, transaction_id, session_id, input_event_id)

    def bind_turn_input_identity(self, *, owner_user_id: str, package_id: str, lesson_id: str, transaction_id: str, session_id: str, input_event_id: str) -> None:
        with self._lock, ridoc_file_lock(self.package_path(owner_user_id, package_id)):
            self.load_archive(owner_user_id, package_id=package_id)
            self.deletion_journal.bind_input(owner_user_id, self.deletion_scope_id(package_id), lesson_id, transaction_id, session_id, input_event_id)

    def assert_action_not_deleted(self, *, owner_user_id: str, package_id: str, lesson_id: str, transaction_id: str, action_id: str='') -> None:
        with self._lock, ridoc_file_lock(self.package_path(owner_user_id, package_id)):
            self.load_archive(owner_user_id, package_id=package_id)
            self.deletion_journal.assert_action_not_deleted(owner_user_id, self.deletion_scope_id(package_id), lesson_id, transaction_id, action_id)

    def context_epoch(self, *, owner_user_id: str, package_id: str, lesson_id: str) -> int:
        archive = self.load_archive(owner_user_id, package_id=package_id)
        graph = archive.package.lessons.get(lesson_id)
        if graph is None:
            raise RidocV3Error('RIDOC lesson is unavailable')
        from openclass_core.services.ridoc_edit_session_store import completed_batch_merges
        scope = self.deletion_scope_id(package_id)
        batch_merges = completed_batch_merges(self, owner_user_id, scope, lesson_id)
        return self.deletion_journal.epoch(owner_user_id, scope, lesson_id) + sum((record['transaction_id'] not in batch_merges for record in graph.merge_records))
