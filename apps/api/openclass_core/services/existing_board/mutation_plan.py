from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass, replace
from typing import Literal
from pydantic import BaseModel, Field, model_validator
from openclass_core.models import BoardDocument
from openclass_core.services.board_segment_index import build_board_segment_index
from openclass_core.services.rich_document import build_document, document_to_markdown
BoardMutationAction = Literal['edit', 'write', 'delete']
BoardContentExtent = Literal['sentence', 'paragraph', 'section', 'article', 'whole_board']
BoardMutationDestination = Literal['current_lesson', 'new_lesson', 'unresolved']
ConfirmationStatus = Literal['not_required', 'pending', 'confirmed', 'rejected']
MutationExecutionStatus = Literal['applied', 'rejected']

def mutation_text_hash(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()

def board_document_hash(document: BoardDocument) -> str:
    payload = document.model_dump(mode='json')
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()

class BoardMutationTargetRange(BaseModel):
    """A frozen half-open range in the plan's canonical Markdown snapshot."""
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    expected_excerpt: str = Field(min_length=1)
    expected_excerpt_hash: str = Field(min_length=1)
    segment_id: str | None = None

    @model_validator(mode='after')
    def validate_range(self) -> 'BoardMutationTargetRange':
        if self.end <= self.start:
            raise ValueError('target range end must be greater than start')
        return self

class BoardInsertionAnchor(BaseModel):
    """A frozen zero-width insertion point with bounded context on each side."""
    offset: int = Field(ge=0)
    left_context: str = ''
    right_context: str = ''
    left_context_hash: str = Field(min_length=1)
    right_context_hash: str = Field(min_length=1)
    segment_id: str | None = None

class BoardMutationOperation(BaseModel):
    operation_id: str = Field(min_length=1, max_length=128)
    action: BoardMutationAction
    target_range: BoardMutationTargetRange | None = None
    insertion_anchor: BoardInsertionAnchor | None = None
    content_markdown: str = ''

    @model_validator(mode='after')
    def validate_binding(self) -> 'BoardMutationOperation':
        if self.action == 'write':
            if self.insertion_anchor is None or self.target_range is not None:
                raise ValueError('write requires exactly one insertion anchor')
            if not self.content_markdown:
                raise ValueError('write content must not be empty')
            return self
        if self.target_range is None or self.insertion_anchor is not None:
            raise ValueError(f'{self.action} requires exactly one target range')
        if self.action == 'edit' and (not self.content_markdown):
            raise ValueError('edit content must not be empty')
        if self.action == 'delete' and self.content_markdown:
            raise ValueError('delete content must be empty')
        return self

class BoardMutationPlan(BaseModel):
    plan_id: str = Field(min_length=1, max_length=128)
    base_commit_id: str = Field(min_length=1)
    base_document_hash: str = Field(min_length=1)
    extent: BoardContentExtent
    destination: BoardMutationDestination
    requires_confirmation: bool = False
    confirmation_status: ConfirmationStatus = 'not_required'
    operations: list[BoardMutationOperation] = Field(min_length=1, max_length=64)

    @model_validator(mode='after')
    def validate_operation_ids(self) -> 'BoardMutationPlan':
        operation_ids = [operation.operation_id for operation in self.operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError('operation ids must be unique')
        return self

class MutationScopeAudit(BaseModel):
    operation_id: str
    action: BoardMutationAction
    start: int
    end: int
    replacement_hash: str

class BoardMutationAudit(BaseModel):
    plan_id: str
    status: MutationExecutionStatus
    reason: str
    base_commit_id: str
    current_commit_id: str
    document_hash_before: str
    document_hash_after: str
    extent: BoardContentExtent
    destination: BoardMutationDestination
    confirmation_status: ConfirmationStatus
    applied_operation_ids: list[str] = Field(default_factory=list)
    authorized_scopes: list[MutationScopeAudit] = Field(default_factory=list)
    document_changed: bool = False

class BoardMutationExecutionResult(BaseModel):
    status: MutationExecutionStatus
    document: BoardDocument
    audit: BoardMutationAudit

@dataclass(frozen=True)
class _Patch:
    operation_id: str
    action: BoardMutationAction
    start: int
    end: int
    replacement: str
    operation_index: int

def execute_board_mutation_plan(plan: BoardMutationPlan, document: BoardDocument, *, current_commit_id: str) -> BoardMutationExecutionResult:
    """Validate all capabilities first, then return one atomic document snapshot."""
    original = document.model_copy(deep=True)
    current_document_hash = board_document_hash(original)
    rejection = _validate_plan_envelope(plan, current_commit_id=current_commit_id, current_document_hash=current_document_hash)
    if rejection:
        return _rejected(plan, original, current_commit_id=current_commit_id, current_document_hash=current_document_hash, reason=rejection)
    markdown = document_to_markdown(original)
    patches, validation_error = _resolve_all_patches(plan, original, markdown)
    if validation_error:
        return _rejected(plan, original, current_commit_id=current_commit_id, current_document_hash=current_document_hash, reason=validation_error)
    assert patches is not None
    try:
        patches = [_canonical_patch(patch, original) for patch in patches]
        next_markdown = _apply_patches(markdown, patches)
        if next_markdown == markdown:
            return _rejected(plan, original, current_commit_id=current_commit_id, current_document_hash=current_document_hash, reason='no_document_change')
        from openclass_core.services.board_citations import validate_board_citation_pairs
        citation_error = validate_board_citation_pairs(next_markdown)
        if citation_error:
            return _rejected(plan, original, current_commit_id=current_commit_id, current_document_hash=current_document_hash, reason=citation_error)
        next_document = build_document(title=original.title, normalize_citations=False, content_text=next_markdown, document_id=original.id, page_settings=original.page_settings.model_copy(deep=True))
        from openclass_core.services.existing_board.preserved_visual_nodes import restore_untouched_visual_nodes
        next_document = restore_untouched_visual_nodes(original, next_document, patches)
    except Exception:
        return _rejected(plan, original, current_commit_id=current_commit_id, current_document_hash=current_document_hash, reason='document_rebuild_failed')
    if document_to_markdown(next_document) != next_markdown:
        return _rejected(plan, original, current_commit_id=current_commit_id, current_document_hash=current_document_hash, reason='rendered_diff_outside_authorized_scope')
    if not _candidate_matches_authorized_patches(before=markdown, candidate=document_to_markdown(next_document), patches=patches):
        return _rejected(plan, original, current_commit_id=current_commit_id, current_document_hash=current_document_hash, reason='diff_outside_authorized_scope')
    next_hash = board_document_hash(next_document)
    audit = BoardMutationAudit(plan_id=plan.plan_id, status='applied', reason='applied', base_commit_id=plan.base_commit_id, current_commit_id=current_commit_id, document_hash_before=current_document_hash, document_hash_after=next_hash, extent=plan.extent, destination=plan.destination, confirmation_status=plan.confirmation_status, applied_operation_ids=[operation.operation_id for operation in plan.operations], authorized_scopes=[MutationScopeAudit(operation_id=patch.operation_id, action=patch.action, start=patch.start, end=patch.end, replacement_hash=mutation_text_hash(patch.replacement)) for patch in sorted(patches, key=lambda item: item.operation_index)], document_changed=True)
    return BoardMutationExecutionResult(status='applied', document=next_document, audit=audit)

def _canonical_patch(patch: _Patch, document: BoardDocument) -> _Patch:
    content = patch.replacement
    if not content.strip():
        return patch
    fragment = build_document(title=document.title, content_text=content, document_id=document.id, page_settings=document.page_settings.model_copy(deep=True), normalize_citations=False)
    leading = content[:len(content) - len(content.lstrip())]
    trailing = content[len(content.rstrip()):]
    return replace(patch, replacement=leading + document_to_markdown(fragment) + trailing)

def _validate_plan_envelope(plan: BoardMutationPlan, *, current_commit_id: str, current_document_hash: str) -> str | None:
    if plan.base_commit_id != current_commit_id:
        return 'base_commit_mismatch'
    if plan.base_document_hash != current_document_hash:
        return 'base_document_hash_mismatch'
    if plan.destination == 'unresolved':
        return 'destination_unresolved'
    if plan.destination == 'new_lesson':
        return 'destination_requires_new_lesson_handler'
    dangerous = plan.extent == 'whole_board' or any((operation.action == 'delete' for operation in plan.operations))
    if plan.confirmation_status == 'rejected':
        return 'confirmation_rejected'
    if dangerous and (not plan.requires_confirmation):
        return 'confirmation_contract_missing'
    if (dangerous or plan.requires_confirmation) and plan.confirmation_status != 'confirmed':
        return 'confirmation_required'
    return None

def _resolve_all_patches(plan: BoardMutationPlan, document: BoardDocument, markdown: str) -> tuple[list[_Patch] | None, str | None]:
    known_segments = {segment.segment_id for segment in build_board_segment_index(document).segments}
    patches: list[_Patch] = []
    for operation_index, operation in enumerate(plan.operations):
        if operation.target_range is not None:
            target = operation.target_range
            error = _validate_target(target, markdown, known_segments)
            if error:
                return (None, error)
            patches.append(_Patch(operation_id=operation.operation_id, action=operation.action, start=target.start, end=target.end, replacement=operation.content_markdown, operation_index=operation_index))
            continue
        anchor = operation.insertion_anchor
        assert anchor is not None
        error = _validate_anchor(anchor, markdown, known_segments)
        if error:
            return (None, error)
        patches.append(_Patch(operation_id=operation.operation_id, action=operation.action, start=anchor.offset, end=anchor.offset, replacement=operation.content_markdown, operation_index=operation_index))
    error = _validate_patch_relationships(plan, markdown, patches)
    if error:
        return (None, error)
    return (patches, None)

def _validate_target(target: BoardMutationTargetRange, markdown: str, known_segments: set[str]) -> str | None:
    if target.end > len(markdown):
        return 'target_out_of_bounds'
    if mutation_text_hash(target.expected_excerpt) != target.expected_excerpt_hash:
        return 'target_excerpt_hash_mismatch'
    if markdown[target.start:target.end] != target.expected_excerpt:
        return 'target_excerpt_mismatch'
    if target.segment_id and target.segment_id not in known_segments:
        return 'target_segment_missing'
    return None

def _validate_anchor(anchor: BoardInsertionAnchor, markdown: str, known_segments: set[str]) -> str | None:
    if anchor.offset > len(markdown):
        return 'anchor_out_of_bounds'
    if mutation_text_hash(anchor.left_context) != anchor.left_context_hash or mutation_text_hash(anchor.right_context) != anchor.right_context_hash:
        return 'anchor_context_hash_mismatch'
    left_start = max(0, anchor.offset - len(anchor.left_context))
    actual_left = markdown[left_start:anchor.offset]
    actual_right = markdown[anchor.offset:anchor.offset + len(anchor.right_context)]
    if actual_left != anchor.left_context or actual_right != anchor.right_context:
        return 'anchor_context_mismatch'
    if anchor.segment_id and anchor.segment_id not in known_segments:
        return 'anchor_segment_missing'
    return None

def _validate_patch_relationships(plan: BoardMutationPlan, markdown: str, patches: list[_Patch]) -> str | None:
    ranges = sorted((patch for patch in patches if patch.end > patch.start), key=lambda patch: (patch.start, patch.end))
    for previous, current in zip(ranges, ranges[1:]):
        if current.start < previous.end:
            return 'overlapping_target_ranges'
    anchors = [patch for patch in patches if patch.end == patch.start]
    for anchor in anchors:
        if any((target.start < anchor.start < target.end for target in ranges)):
            return 'anchor_inside_target_range'
    if plan.extent == 'whole_board':
        if len(patches) != 1:
            return 'whole_board_requires_single_operation'
        patch = patches[0]
        if patch.action not in {'edit', 'delete'}:
            return 'whole_board_requires_target_range'
        if patch.start != 0 or patch.end != len(markdown):
            return 'whole_board_scope_mismatch'
    return None

def _apply_patches(markdown: str, patches: list[_Patch]) -> str:
    ordered = sorted(patches, key=lambda patch: (patch.start, 1 if patch.end > patch.start else 0, patch.operation_index), reverse=True)
    result = markdown
    for patch in ordered:
        result = f'{result[:patch.start]}{patch.replacement}{result[patch.end:]}'
    return result

def _candidate_matches_authorized_patches(*, before: str, candidate: str, patches: list[_Patch]) -> bool:
    return candidate == _apply_patches(before, patches)

def _rejected(plan: BoardMutationPlan, original: BoardDocument, *, current_commit_id: str, current_document_hash: str, reason: str) -> BoardMutationExecutionResult:
    audit = BoardMutationAudit(plan_id=plan.plan_id, status='rejected', reason=reason, base_commit_id=plan.base_commit_id, current_commit_id=current_commit_id, document_hash_before=current_document_hash, document_hash_after=current_document_hash, extent=plan.extent, destination=plan.destination, confirmation_status=plan.confirmation_status, document_changed=False)
    return BoardMutationExecutionResult(status='rejected', document=original.model_copy(deep=True), audit=audit)
