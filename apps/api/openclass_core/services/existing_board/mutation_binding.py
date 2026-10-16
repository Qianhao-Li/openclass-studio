from __future__ import annotations
import hashlib
import re
from collections.abc import Sequence
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from openclass_core.models import BoardDocument, BoardFocusRef, BoardSegment
from openclass_core.services.board_segment_index import build_board_segment_index, segment_text_hash
from openclass_core.services.existing_board.focus_resolver import focus_range_text_hash
from openclass_core.services.existing_board.mutation_plan import BoardInsertionAnchor, BoardMutationAudit, BoardMutationExecutionResult, BoardMutationOperation, BoardMutationPlan, BoardMutationTargetRange, board_document_hash, execute_board_mutation_plan, mutation_text_hash
from openclass_core.services.existing_board.mutation_planner import BoardMutationPlanDraft, MutationPlannerOperationDraft
from openclass_core.services.rich_document import document_to_markdown
BindingExecutionStatus = Literal['applied', 'rejected']
WriteAnchorPosition = Literal['before', 'after', 'parent_start', 'parent_end']
ANCHOR_CONTEXT_CHARS = 64

class ConfirmedWriteAnchor(BaseModel):
    """An operation-specific capability; planner output cannot create one."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    confirmed: bool
    operation_id: str = Field(min_length=1, max_length=128)
    lesson_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    segment_id: str = Field(min_length=1)
    text_hash: str = Field(min_length=1)
    position: WriteAnchorPosition
    parent_heading_path: list[str] = Field(default_factory=list)

class MutationBindingExecutionResult(BaseModel):
    status: BindingExecutionStatus
    reason: str
    document: BoardDocument
    document_changed: bool = False
    atomic_operation_count: int = 0
    bound_plan: BoardMutationPlan | None = None
    execution_audit: BoardMutationAudit | None = None

class _SegmentMarkdownSpan(BaseModel):
    segment_id: str
    text_start: int
    text_end: int
    block_start: int
    block_end: int

def bind_and_execute_board_mutation(*, draft: BoardMutationPlanDraft, document: BoardDocument, current_commit_id: str, resolved_focus: BoardFocusRef | None=None, confirmed_write_anchors: Sequence[ConfirmedWriteAnchor]=()) -> MutationBindingExecutionResult:
    """Materialize one bounded executable plan and invoke the atomic executor once."""
    original = document.model_copy(deep=True)
    current_hash = board_document_hash(original)
    envelope_error = _validate_envelope(draft, current_commit_id=current_commit_id, current_document_hash=current_hash)
    if envelope_error:
        return _rejected(original, reason=envelope_error)
    markdown = document_to_markdown(original)
    index = build_board_segment_index(original)
    segment_by_id = {segment.segment_id: segment for segment in index.segments}
    anchor_by_operation, anchor_error = _confirmed_anchor_map(draft, confirmed_write_anchors)
    if anchor_error:
        return _rejected(original, reason=anchor_error)
    operations: list[BoardMutationOperation] = []
    for operation in draft.operations:
        bound_operation, binding_error = _bind_operation(operation, draft=draft, document=original, markdown=markdown, segments=index.segments, segment_by_id=segment_by_id, resolved_focus=resolved_focus, confirmed_anchor=anchor_by_operation.get(operation.operation_id))
        if binding_error:
            return _rejected(original, reason=binding_error)
        assert bound_operation is not None
        operations.append(bound_operation)
    bound_plan = BoardMutationPlan(plan_id=draft.plan_id, base_commit_id=draft.base_commit_id, base_document_hash=draft.base_document_hash, extent=draft.extent, destination=draft.destination, requires_confirmation=draft.requires_confirmation, confirmation_status=draft.confirmation_status, operations=operations)
    execution = execute_board_mutation_plan(bound_plan, original, current_commit_id=current_commit_id)
    return _from_execution(bound_plan, execution)

def _validate_envelope(draft: BoardMutationPlanDraft, *, current_commit_id: str, current_document_hash: str) -> str | None:
    if draft.base_commit_id != current_commit_id:
        return 'base_commit_mismatch'
    if draft.base_document_hash != current_document_hash:
        return 'base_document_hash_mismatch'
    if draft.destination == 'unresolved':
        return 'destination_unresolved'
    if draft.destination == 'new_lesson':
        return 'destination_requires_new_lesson_handler'
    dangerous = draft.extent == 'whole_board' or any((operation.action == 'delete' for operation in draft.operations))
    if draft.confirmation_status == 'rejected':
        return 'confirmation_rejected'
    if dangerous and (not draft.requires_confirmation):
        return 'confirmation_contract_missing'
    if (dangerous or draft.requires_confirmation) and draft.confirmation_status != 'confirmed':
        return 'confirmation_required'
    if not draft.execution_allowed:
        return 'draft_execution_not_allowed'
    return None

def _confirmed_anchor_map(draft: BoardMutationPlanDraft, anchors: Sequence[ConfirmedWriteAnchor]) -> tuple[dict[str, ConfirmedWriteAnchor], str | None]:
    write_ids = {operation.operation_id for operation in draft.operations if operation.action == 'write'}
    mapped: dict[str, ConfirmedWriteAnchor] = {}
    for anchor in anchors:
        if not anchor.confirmed:
            return ({}, 'write_anchor_not_confirmed')
        if anchor.operation_id not in write_ids:
            return ({}, 'unexpected_write_anchor')
        if anchor.operation_id in mapped:
            return ({}, 'duplicate_write_anchor')
        mapped[anchor.operation_id] = anchor.model_copy(deep=True)
    if set(mapped) != write_ids:
        return ({}, 'write_anchor_not_confirmed')
    return (mapped, None)

def _bind_operation(operation: MutationPlannerOperationDraft, *, draft: BoardMutationPlanDraft, document: BoardDocument, markdown: str, segments: list[BoardSegment], segment_by_id: dict[str, BoardSegment], resolved_focus: BoardFocusRef | None, confirmed_anchor: ConfirmedWriteAnchor | None) -> tuple[BoardMutationOperation | None, str | None]:
    binding = operation.binding
    if binding.document_id != document.id:
        return (None, 'binding_document_mismatch')
    segment = segment_by_id.get(binding.segment_id)
    if segment is None:
        return (None, 'binding_segment_missing')
    range_binding = resolved_focus is not None and binding.text_hash == resolved_focus.text_hash and _focus_declares_range(resolved_focus, segment)
    if binding.text_hash != segment.text_hash and (not range_binding):
        return (None, 'binding_segment_hash_mismatch')
    if resolved_focus is not None:
        focus_error = _validate_focus(binding, segment, resolved_focus, markdown=markdown, segments=segments)
        if focus_error:
            return (None, focus_error)
    span, span_error = _resolve_segment_span(markdown, segments, segment)
    if span_error:
        return (None, span_error)
    assert span is not None
    if operation.action in {'edit', 'delete'}:
        return _bind_target_operation(operation, draft=draft, markdown=markdown, segments=segments, segment=segment, span=span, resolved_focus=resolved_focus)
    return _bind_write_operation(operation, markdown=markdown, segments=segments, segment=segment, span=span, confirmed_anchor=confirmed_anchor)

def _bind_target_operation(operation: MutationPlannerOperationDraft, *, draft: BoardMutationPlanDraft, markdown: str, segments: list[BoardSegment], segment: BoardSegment, span: _SegmentMarkdownSpan, resolved_focus: BoardFocusRef | None) -> tuple[BoardMutationOperation | None, str | None]:
    binding = operation.binding
    if binding.kind != 'target_range' or binding.position != 'replace':
        return (None, 'target_binding_required')
    assert resolved_focus is not None
    excerpt = resolved_focus.excerpt.strip()
    frozen_segments, range_error = _frozen_focus_segments(resolved_focus, segments=segments, anchor_segment=segment)
    if range_error:
        return (None, range_error)
    assert frozen_segments is not None
    resolved_range, range_error = _resolve_focus_markdown_range(markdown, segments=segments, frozen_segments=frozen_segments, excerpt=excerpt)
    if range_error:
        return (None, range_error)
    assert resolved_range is not None
    start, end = resolved_range
    if draft.extent == 'whole_board':
        if len(draft.operations) != 1 or excerpt != markdown or start != 0 or (end != len(markdown)):
            return (None, 'whole_board_scope_not_authorized')
        start, end = (0, len(markdown))
        excerpt = markdown
    elif operation.action == 'delete':
        start, end = _expand_delete_span_over_separator(markdown, start, end)
        excerpt = markdown[start:end]
    target = BoardMutationTargetRange(start=start, end=end, expected_excerpt=excerpt, expected_excerpt_hash=mutation_text_hash(excerpt), segment_id=segment.segment_id)
    return (BoardMutationOperation(operation_id=operation.operation_id, action=operation.action, target_range=target, content_markdown=operation.content_markdown), None)

def _expand_delete_span_over_separator(markdown: str, start: int, end: int) -> tuple[int, int]:
    if markdown[end:].startswith('\n\n'):
        return (start, end + 2)
    if markdown[:start].endswith('\n\n'):
        return (start - 2, end)
    return (start, end)

def _bind_write_operation(operation: MutationPlannerOperationDraft, *, markdown: str, segments: list[BoardSegment], segment: BoardSegment, span: _SegmentMarkdownSpan, confirmed_anchor: ConfirmedWriteAnchor | None) -> tuple[BoardMutationOperation | None, str | None]:
    binding = operation.binding
    if binding.kind != 'insertion_anchor':
        return (None, 'write_binding_required')
    if confirmed_anchor is None or not confirmed_anchor.confirmed:
        return (None, 'write_anchor_not_confirmed')
    if _anchor_identity(confirmed_anchor) != _binding_identity(operation):
        return (None, 'write_anchor_identity_mismatch')
    if list(confirmed_anchor.parent_heading_path) != list(binding.parent_heading_path):
        return (None, 'write_anchor_parent_path_mismatch')
    offset, offset_error = _write_anchor_offset(binding.position, markdown=markdown, segments=segments, segment=segment, span=span)
    if offset_error:
        return (None, offset_error)
    assert offset is not None
    left_context = markdown[max(0, offset - ANCHOR_CONTEXT_CHARS):offset]
    right_context = markdown[offset:offset + ANCHOR_CONTEXT_CHARS]
    anchor = BoardInsertionAnchor(offset=offset, left_context=left_context, right_context=right_context, left_context_hash=mutation_text_hash(left_context), right_context_hash=mutation_text_hash(right_context), segment_id=segment.segment_id)
    return (BoardMutationOperation(operation_id=operation.operation_id, action='write', insertion_anchor=anchor, content_markdown=_as_markdown_block_insertion(operation.content_markdown, position=binding.position)), None)

def _validate_focus(binding, segment: BoardSegment, focus: BoardFocusRef | None, *, markdown: str, segments: list[BoardSegment]) -> str | None:
    if focus is None:
        return 'resolved_focus_required'
    if focus.source != 'board':
        return 'focus_source_mismatch'
    if (focus.lesson_id, focus.document_id, focus.segment_id, focus.text_hash) != (binding.lesson_id, binding.document_id, binding.segment_id, binding.text_hash):
        return 'focus_identity_mismatch'
    if focus.text_hash != segment.text_hash and (not _focus_declares_range(focus, segment)):
        return 'focus_segment_hash_mismatch'
    excerpt = focus.excerpt.strip()
    valid_excerpt_hashes = {segment_text_hash(excerpt), hashlib.sha256(excerpt.encode('utf-8')).hexdigest()}
    if focus.excerpt_hash and focus.excerpt_hash not in valid_excerpt_hashes:
        return 'focus_excerpt_hash_mismatch'
    if binding.excerpt_hash not in valid_excerpt_hashes:
        return 'focus_excerpt_hash_mismatch'
    if focus.excerpt_hash and binding.excerpt_hash != focus.excerpt_hash:
        return 'focus_excerpt_hash_mismatch'
    if list(binding.parent_heading_path) != list(focus.heading_path):
        return 'focus_parent_path_mismatch'
    frozen_segments, range_error = _frozen_focus_segments(focus, segments=segments, anchor_segment=segment)
    if range_error:
        return range_error
    assert frozen_segments is not None
    expected_text_hash = focus_range_text_hash(frozen_segments, excerpt) if _focus_declares_range(focus, segment) else segment.text_hash
    if focus.text_hash != expected_text_hash or binding.text_hash != expected_text_hash:
        return 'focus_range_hash_mismatch'
    _resolved_range, range_error = _resolve_focus_markdown_range(markdown, segments=segments, frozen_segments=frozen_segments, excerpt=excerpt)
    if range_error:
        return range_error
    return None

def _focus_declares_range(focus: BoardFocusRef, anchor: BoardSegment) -> bool:
    source_ids = list(focus.source_segment_ids)
    return len(source_ids) > 1 or (source_ids and source_ids != [anchor.segment_id]) or focus.order_start not in {None, anchor.order_index} or (focus.order_end not in {None, anchor.order_index}) or (focus.text_hash != anchor.text_hash)

def _frozen_focus_segments(focus: BoardFocusRef, *, segments: list[BoardSegment], anchor_segment: BoardSegment) -> tuple[list[BoardSegment] | None, str | None]:
    source_ids = list(focus.source_segment_ids or [anchor_segment.segment_id])
    order_start = anchor_segment.order_index if focus.order_start is None else focus.order_start
    order_end = anchor_segment.order_index if focus.order_end is None else focus.order_end
    if order_start < 0 or order_end < order_start or order_end >= len(segments) or (len(source_ids) != order_end - order_start + 1):
        return (None, 'focus_source_segment_order_mismatch')
    frozen_segments = segments[order_start:order_end + 1]
    if [candidate.segment_id for candidate in frozen_segments] != source_ids:
        return (None, 'focus_source_segment_order_mismatch')
    if not frozen_segments or frozen_segments[0].segment_id != anchor_segment.segment_id:
        return (None, 'focus_source_segment_order_mismatch')
    return (frozen_segments, None)

def _resolve_focus_markdown_range(markdown: str, *, segments: list[BoardSegment], frozen_segments: list[BoardSegment], excerpt: str) -> tuple[tuple[int, int] | None, str | None]:
    if not excerpt:
        return (None, 'focus_excerpt_not_in_segment')
    first_span, first_error = _resolve_segment_span(markdown, segments, frozen_segments[0])
    if first_error:
        return (None, first_error)
    assert first_span is not None
    next_order = frozen_segments[-1].order_index + 1
    if next_order < len(segments):
        next_span, next_error = _resolve_segment_span(markdown, segments, segments[next_order])
        if next_error:
            return (None, next_error)
        assert next_span is not None
        authorized_end = next_span.block_start
    else:
        authorized_end = len(markdown)
    candidates = []
    for start in _all_occurrences(markdown, excerpt):
        end = start + len(excerpt)
        if first_span.block_start <= start <= first_span.block_end and end <= authorized_end:
            candidates.append((start, end))
    if len(candidates) != 1:
        return (None, 'focus_excerpt_markdown_mismatch')
    return (candidates[0], None)

def _resolve_segment_span(markdown: str, segments: list[BoardSegment], target: BoardSegment) -> tuple[_SegmentMarkdownSpan | None, str | None]:
    peers = [segment for segment in segments if segment.kind == target.kind and segment.text == target.text]
    candidates = [start for start in _all_occurrences(markdown, target.text) if _matches_markdown_block(markdown, start, target)]
    if len(candidates) != len(peers):
        return (None, 'segment_markdown_span_not_unique')
    target_ordinal = next((index for index, segment in enumerate(peers) if segment.segment_id == target.segment_id), None)
    if target_ordinal is None or target_ordinal >= len(candidates):
        return (None, 'segment_markdown_span_not_unique')
    text_start = candidates[target_ordinal]
    text_end = text_start + len(target.text)
    block_start = markdown.rfind('\n', 0, text_start) + 1
    next_newline = markdown.find('\n', text_end)
    block_end = len(markdown) if next_newline < 0 else next_newline
    return (_SegmentMarkdownSpan(segment_id=target.segment_id, text_start=text_start, text_end=text_end, block_start=block_start, block_end=block_end), None)

def _matches_markdown_block(markdown: str, start: int, segment: BoardSegment) -> bool:
    line_start = markdown.rfind('\n', 0, start) + 1
    line_end = markdown.find('\n', start + len(segment.text))
    if line_end < 0:
        line_end = len(markdown)
    line = markdown[line_start:line_end]
    if segment.kind == 'heading':
        return bool(re.fullmatch('#{1,6}\\s+' + re.escape(segment.text), line))
    if segment.kind == 'list':
        return bool(re.fullmatch('(?:[-+*]|\\d+\\.)\\s+' + re.escape(segment.text), line))
    if segment.kind == 'paragraph':
        return line == segment.text
    return False

def _write_anchor_offset(position: str, *, markdown: str, segments: list[BoardSegment], segment: BoardSegment, span: _SegmentMarkdownSpan) -> tuple[int | None, str | None]:
    if position == 'before':
        return (span.block_start, None)
    if position == 'after':
        return (span.block_end, None)
    if segment.kind != 'heading':
        return (None, 'parent_anchor_requires_heading')
    if position == 'parent_start':
        return (span.block_end, None)
    if position != 'parent_end':
        return (None, 'write_anchor_position_invalid')
    target_index = next((index for index, candidate in enumerate(segments) if candidate.segment_id == segment.segment_id))
    parent_path = list(segment.heading_path)
    for candidate in segments[target_index + 1:]:
        if candidate.kind != 'heading':
            continue
        if list(candidate.heading_path[:len(parent_path)]) == parent_path:
            continue
        candidate_span, error = _resolve_segment_span(markdown, segments, candidate)
        if error:
            return (None, error)
        assert candidate_span is not None
        return (candidate_span.block_start, None)
    return (len(markdown), None)

def _as_markdown_block_insertion(content: str, *, position: str) -> str:
    bounded = content.strip('\n')
    if position in {'after', 'parent_start'}:
        return f'\n\n{bounded}'
    return f'{bounded}\n\n'

def _all_occurrences(value: str, needle: str) -> list[int]:
    if not needle:
        return []
    starts: list[int] = []
    offset = 0
    while True:
        found = value.find(needle, offset)
        if found < 0:
            return starts
        starts.append(found)
        offset = found + len(needle)

def _anchor_identity(anchor: ConfirmedWriteAnchor) -> tuple[str, ...]:
    return (anchor.operation_id, anchor.lesson_id, anchor.document_id, anchor.segment_id, anchor.text_hash, anchor.position)

def _binding_identity(operation: MutationPlannerOperationDraft) -> tuple[str, ...]:
    binding = operation.binding
    return (operation.operation_id, binding.lesson_id, binding.document_id, binding.segment_id, binding.text_hash, binding.position)

def _from_execution(plan: BoardMutationPlan, execution: BoardMutationExecutionResult) -> MutationBindingExecutionResult:
    return MutationBindingExecutionResult(status=execution.status, reason=execution.audit.reason, document=execution.document.model_copy(deep=True), document_changed=execution.audit.document_changed, atomic_operation_count=len(plan.operations) if execution.status == 'applied' else 0, bound_plan=plan.model_copy(deep=True), execution_audit=execution.audit.model_copy(deep=True))

def _rejected(document: BoardDocument, *, reason: str) -> MutationBindingExecutionResult:
    return MutationBindingExecutionResult(status='rejected', reason=reason, document=document.model_copy(deep=True), document_changed=False)
