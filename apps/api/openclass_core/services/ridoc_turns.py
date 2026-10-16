from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field, model_validator
from openclass_core.models import BoardDocument, Lesson
from openclass_core.services.ridoc_v3 import RidocV3Error
from openclass_core.services.ridoc_workspace_store import RidocWorkspaceRecord, RidocWorkspaceStore

class RidocTurnOutcome(BaseModel):
    transaction_id: str = Field(min_length=1)
    user_message: str = Field(min_length=1)
    assistant_message: str = Field(min_length=1)
    assistant_source: str = ''
    document: BoardDocument | None = None
    new_lesson: Lesson | None = None
    document_source: str = 'ai'
    document_label: str = 'Document update'
    evidence_ids: list[str] = Field(default_factory=list)
    resource_refs: list[str] = Field(default_factory=list)
    evidence_payloads: dict[str, dict] = Field(default_factory=dict)
    asset_payloads: dict[str, bytes] = Field(default_factory=dict)
    decision_metadata: dict[str, object] = Field(default_factory=dict)
    package_metadata: dict[str, object] = Field(default_factory=dict)
    lesson_metadata: dict[str, object] = Field(default_factory=dict)
    branch_action_kind: Literal['regenerate', 'fork'] | None = None
    branch_source_node_id: str = ''

    @model_validator(mode='after')
    def validate_complete_turn(self) -> 'RidocTurnOutcome':
        self.transaction_id = self.transaction_id.strip()
        self.user_message = self.user_message.strip()
        self.assistant_message = self.assistant_message.strip()
        if not self.transaction_id or not self.user_message or (not self.assistant_message):
            raise ValueError('RIDOC outcomes require a complete M')
        if self.branch_action_kind and (not self.branch_source_node_id.strip()):
            raise ValueError('RIDOC branched outcomes require a source M')
        if self.new_lesson is not None and self.branch_action_kind is not None:
            raise ValueError('A new lesson destination cannot also branch a historical turn')
        return self

def commit_ridoc_turn_outcome(store: RidocWorkspaceStore, *, owner_user_id: str, package_id: str, lesson_id: str, expected_revision: str, outcome: RidocTurnOutcome) -> RidocWorkspaceRecord:
    if not expected_revision.strip():
        raise RidocV3Error('RIDOC revision is required before committing a turn')
    return store.commit_turn(owner_user_id=owner_user_id, package_id=package_id, lesson_id=lesson_id, expected_revision=expected_revision, transaction_id=outcome.transaction_id, user_message=outcome.user_message, assistant_message=outcome.assistant_message, assistant_source=outcome.assistant_source, document=outcome.document, new_lesson=outcome.new_lesson, document_source=outcome.document_source, document_label=outcome.document_label, evidence_ids=outcome.evidence_ids, resource_refs=outcome.resource_refs, evidence_payloads=outcome.evidence_payloads, asset_payloads=outcome.asset_payloads, decision_metadata=outcome.decision_metadata, package_metadata=outcome.package_metadata, lesson_metadata=outcome.lesson_metadata, branch_action_kind=outcome.branch_action_kind, branch_source_node_id=outcome.branch_source_node_id)
