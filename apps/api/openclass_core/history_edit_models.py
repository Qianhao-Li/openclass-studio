"""History editor transport types; draft data is never a formal course revision."""
from __future__ import annotations
from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from openclass_core.models import BoardDocument, CoursePackageView, RidocHistoryPageView

class EditOperation(BaseModel):
    model_config = ConfigDict(extra='forbid')

class ReorderMessages(EditOperation):
    kind: Literal['reorder_messages']
    branch_name: str = Field(min_length=1)
    ordered_node_ids: list[str]

class StepDocument(EditOperation):
    kind: Literal['step_document']
    branch_name: str = Field(min_length=1)
    direction: Literal['undo', 'redo']
    target_node_id: str = Field(min_length=1)

class StepDocumentActions(EditOperation):
    kind: Literal['step_document_actions']
    branch_name: str = Field(min_length=1)
    direction: Literal['undo', 'redo']
    target_node_ids: list[str] = Field(min_length=1, max_length=1000)

    @model_validator(mode='after')
    def unique_targets(self):
        if any((not value.strip() for value in self.target_node_ids)) or len(set(self.target_node_ids)) != len(self.target_node_ids):
            raise ValueError('Action targets must be nonempty and unique')
        return self

class DeleteMessage(EditOperation):
    kind: Literal['delete_message']
    node_id: str = Field(min_length=1)

class RestoreMessage(EditOperation):
    kind: Literal['restore_message']
    node_id: str = Field(min_length=1)

class CreateBranch(EditOperation):
    kind: Literal['create_branch']
    name: str = Field(min_length=1)
    from_node_id: str | None = None

class CheckoutBranch(EditOperation):
    kind: Literal['checkout_branch']
    name: str = Field(min_length=1)

class MergeBranch(EditOperation):
    kind: Literal['merge_branch']
    source_branch_name: str = Field(min_length=1)
    target_branch_name: str = Field(min_length=1)
    ordered_node_ids: list[str] | None = None
HistoryEditOperation = Annotated[ReorderMessages | StepDocument | StepDocumentActions | DeleteMessage | RestoreMessage | CreateBranch | CheckoutBranch | MergeBranch, Field(discriminator='kind')]

class BeginHistoryEditRequest(BaseModel):
    expected_ridoc_revision: str = Field(min_length=64, max_length=64)
    expected_version: int | None = Field(default=None, ge=1)

class UpdateHistoryEditRequest(BaseModel):
    expected_draft_version: int = Field(ge=1)
    operation_id: str = Field(min_length=1, max_length=200, pattern='\\S')
    operation: HistoryEditOperation
    impact_token: str | None = Field(default=None, min_length=1, max_length=512)

class CommitHistoryEditRequest(BaseModel):
    expected_draft_version: int = Field(ge=1)
    mutation_id: str = Field(min_length=1, max_length=200, pattern='\\S')
    confirmation_token: str = Field(min_length=1, max_length=512)

class DiscardHistoryEditRequest(BaseModel):
    expected_draft_version: int = Field(ge=1)

class HistoryEditVersionRequest(BaseModel):
    expected_draft_version: int = Field(ge=1)

class HistoryRemovalImpactRequest(HistoryEditVersionRequest):
    node_id: str | None = Field(default=None, min_length=1)
    node_ids: list[str] | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode='after')
    def one_target_form(self):
        if (self.node_id is None) == (self.node_ids is None):
            raise ValueError('Provide either node_id or node_ids, but not both')
        if self.node_ids is not None and (any((not value.strip() for value in self.node_ids)) or len(set(self.node_ids)) != len(self.node_ids)):
            raise ValueError('Action targets must be nonempty and unique')
        return self

class PendingHistoryRemoval(BaseModel):
    lesson_id: str
    node_id: str
    kind: Literal['M', 'C']

class HistoryReferrer(BaseModel):
    lesson_id: str
    node_id: str | None = None
    kind: str
    branch_name: str | None = None
    relation: str
    title: str | None = None

class HistoryEditChangeSummary(BaseModel):
    lesson_id: str
    kind: str
    count: int = Field(ge=0)

class HistoryRemovalImpactView(BaseModel):
    impact_token: str
    lesson_id: str
    node_id: str
    kind: Literal['M', 'C']
    referrers: list[HistoryReferrer]
    requires_confirmation: bool
    targets: list[PendingHistoryRemoval] = Field(default_factory=list)

class HistoryEditReviewView(BaseModel):
    confirmation_token: str
    pending_removals: list[PendingHistoryRemoval]
    referrers: list[HistoryReferrer]
    changes: list[HistoryEditChangeSummary]
    affected_lesson_ids: list[str]
    dirty: bool
    stale: bool
    blocked_reason: str | None = None
    version: int
    base_ridoc_revision: str

class HistoryEditSessionView(BaseModel):
    id: str
    package_id: str
    lesson_id: str
    base_ridoc_revision: str
    base_repository_version: int | None = None
    version: int
    state: Literal['editing', 'committed', 'discarded']
    dirty: bool
    pending: bool
    editing_enabled: bool
    pending_removals: list[PendingHistoryRemoval]
    affected_lesson_ids: list[str]
    stale: bool
    blocked_reason: str | None = None
    operation_count: int
    deleted_node_ids: list[str]
    draft_course_package: CoursePackageView
    draft_history: RidocHistoryPageView

class CompactHistoryEditSessionView(HistoryEditSessionView):
    draft_course_package: dict[str, Any] | None = None
    draft_history: dict[str, Any] | None = None
    document_table: dict[str, BoardDocument] = Field(default_factory=dict)
    reuse_base_revision: str | None = None
    base_learning_node_ids: list[str] | None = None

class CommitHistoryEditResponse(BaseModel):
    session_id: str
    ridoc_revision: str
    changed: bool
    deleted_node_ids: list[str]
    removed_nodes: list[PendingHistoryRemoval]
    mutation_id: str
    course_package: CoursePackageView

class DiscardHistoryEditResponse(BaseModel):
    session_id: str
    state: Literal['discarded'] = 'discarded'
