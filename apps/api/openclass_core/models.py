from __future__ import annotations
from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator, model_validator
from openclass_core.source_refinement_models import SourceCatalogRefinementView

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def new_id(prefix: str) -> str:
    return f'{prefix}_{uuid4().hex[:12]}'
BlockType = Literal['heading', 'paragraph', 'formula', 'table', 'image', 'note', 'exercise', 'dialogue']
PatchOperationType = Literal['insert_block', 'delete_block', 'update_block_content', 'replace_range_in_block', 'move_block', 'update_block_style', 'attach_asset']
CourseEdgeType = Literal['recommended_next', 'prerequisite', 'deep_dive', 'alternate_path', 'derived_from']
PublicationVisibility = Literal['private', 'public']
PublicationReviewStatus = Literal['not_started', 'scanning', 'approved', 'blocked', 'error']
TeachingMode = Literal['definition', 'intuition', 'analogy', 'example', 'dialogue']
BoardAction = Literal['no_change', 'edit_board']
SelectionKind = Literal['chat', 'board', 'source']
BoardFocusSource = Literal['board', 'chat']
BoardFocusLocationStatus = Literal['missing', 'selected', 'resolved', 'ambiguous']
BoardSegmentKind = Literal['heading', 'paragraph', 'list', 'table', 'code', 'image', 'formula', 'other']
AgentActivityStage = Literal['turn_decision', 'resolve_target', 'build_context', 'execute_role', 'verify', 'persist_history', 'final']
AgentActivityStatus = Literal['pending', 'running', 'completed', 'cancelled', 'blocked', 'failed', 'skipped']
LessonMergeMode = Literal['manual', 'ai']
LessonMergeStatus = Literal['draft', 'ai_running', 'ready', 'stale', 'committed', 'abandoned', 'failed']
LessonMergeConflictKind = Literal['board', 'learning_requirement', 'board_task', 'source_reference']
LessonMergeResolutionKind = Literal['unresolved', 'target', 'source', 'both', 'custom', 'clear', 'ai']
LessonContributionStatus = Literal['open', 'merge_draft', 'merged', 'closed']
LessonContributionEventKind = Literal['opened', 'revision_submitted', 'commented', 'comment_edited', 'comment_deleted', 'merge_started', 'returned_for_changes', 'merged', 'closed', 'reopened']
InitialLearningWorkMode = Literal['knowledge_board', 'narrow_topic', 'practice_artifact', 'unknown']
LearningTeachingType = Literal['knowledge_point', 'skill_practice']
GuidedRequirementDiscoveryStrategy = Literal['entry_point_discovery', 'level_discovery', 'goal_discovery', 'mode_discovery', 'bottleneck_discovery']
GuidedRequirementSelectionTarget = Literal['learning_content', 'current_level', 'target_scenario', 'teaching_type', 'bottleneck']
InitialLearningGranularity = Literal['single_knowledge_point', 'source_chapter', 'source_range', 'broad_topic', 'practice_artifact', 'unclear']
ConversationRole = Literal['user', 'assistant']
RealtimeTranscriptRole = Literal['user', 'assistant', 'tool']
AIProvider = Annotated[str, StringConstraints(pattern='^[a-z][a-z0-9_-]{0,63}$')]
AIModelAccessMethod = Literal['chatgpt_subscription', 'personal_api', 'platform_credits']
AIModelCapability = Literal['text', 'realtime']
AIRealtimeTransport = Literal['openai_webrtc', 'gemini_live_websocket']
ResourceScanStrategy = Literal['outline_only', 'heading_section', 'page_window', 'fulltext_match']
ResourcePageRole = Literal['cover', 'copyright', 'toc', 'preface', 'body', 'appendix', 'back_matter', 'unknown']
ChatInteractionMode = Literal['ask', 'direct_edit']
FormulaInkAction = Literal['reference', 'replace']
TeachingAction = Literal['continue', 'restart']
BoardGenerationAction = Literal['start']
BoardTaskConfirmation = Literal['confirm', 'decline']
ChatChannel = Literal['text', 'realtime']
ChatInputKind = Literal['typed', 'voice']
TurnIntent = Literal['ordinary_chat', 'learning_need', 'unclear']
LearningFlow = Literal['teach', 'generate_only', 'none']
TurnExplicitAction = Literal['direct_edit', 'board_generation', 'teaching_continue', 'teaching_restart', 'formula_reference', 'formula_replace', 'board_task_confirm', 'board_task_decline']
TurnContinuationKind = Literal['none', 'learning_requirement', 'board_task', 'pending_write_offer', 'teaching_sequence', 'interaction_session']
TurnRelationToActive = Literal['none', 'continue', 'supplement', 'replace', 'new_task', 'unresolved']
TurnDecisionBoardAccess = Literal['forbidden', 'state_check_only']
TurnBoardAccess = Literal['forbidden', 'state_check_only', 'bounded_board_role']
TurnRequirementEffect = Literal['preserved', 'eligible', 'updated']
BoardWorkflow = Literal['generate_from_scratch', 'act_on_existing_board', 'unknown']
LearningRequirementFactCategory = Literal['learning', 'level', 'vocabulary', 'scenario', 'output', 'other']
LearningRequirementRunStatus = Literal['collecting', 'ready', 'frozen', 'consumed', 'archived']
LearningRequirementChangeKind = Literal['created', 'updated', 'completed', 'refinement_failed', 'source_reference_confirmed', 'source_reference_declined', 'frozen', 'forced_frozen', 'consumed', 'archived', 'generation_failed']
LearningSourceConfirmationStatus = Literal['none', 'confirmed', 'skipped', 'stale']
BoardTaskRunStatus = Literal['collecting', 'ready', 'awaiting_confirmation', 'consumed', 'not_executed', 'archived']
BoardTaskChangeKind = Literal['created', 'updated', 'ready', 'awaiting_confirmation', 'consumed', 'not_executed', 'archived', 'execution_failed']
BoardTaskRequestedAction = Literal['write', 'edit', 'explain', 'delete', 'interact', 'chat']
BoardTaskConfirmationStatus = Literal['none', 'awaiting', 'confirmed', 'declined']
BoardTaskRoute = Literal['write', 'edit', 'explain', 'delete', 'interact', 'chat', 'clarify_location', 'await_write_confirmation']
BoardTaskLocationStatus = Literal['missing', 'selected', 'resolved', 'ambiguous', 'content_absent']
BoardTaskLocationKind = Literal['target_range', 'insertion_anchor', 'unspecified']
BoardContentExtent = Literal['sentence', 'paragraph', 'section', 'article', 'whole_board']
BoardTopicRelation = Literal['current_document', 'independent', 'unresolved']
BoardDocumentDestination = Literal['current_lesson', 'new_lesson', 'unresolved']
BoardDocumentOperationStatus = Literal['none', 'succeeded', 'failed']
LearningRequirementOperationStatus = Literal['none', 'succeeded', 'failed']
BoardPatchContentFormat = Literal['markdown', 'plain_text']
DocumentMarginPreset = Literal['narrow', 'normal', 'wide']
DocumentOrientation = Literal['portrait', 'landscape']
DocumentPageSize = Literal['a4', 'letter', 'a3']
DocumentBackgroundStyle = Literal['plain', 'warm', 'grid']

class BlockStyle(BaseModel):
    font_family: str = 'sans'
    font_size: Literal['sm', 'md', 'lg', 'xl'] = 'md'
    alignment: Literal['left', 'center', 'right'] = 'left'
    emphasis: Literal['plain', 'accent', 'callout'] = 'plain'
    width: Literal['normal', 'wide', 'full'] = 'normal'

class BoardBlock(BaseModel):
    id: str = Field(default_factory=lambda: new_id('block'))
    type: BlockType
    title: str
    content: str
    style: BlockStyle = Field(default_factory=BlockStyle)
    metadata: dict[str, Any] = Field(default_factory=dict)

class DocumentPageSettings(BaseModel):
    margin_preset: DocumentMarginPreset = 'normal'
    orientation: DocumentOrientation = 'portrait'
    page_size: DocumentPageSize = 'a4'
    columns: Literal[1, 2] = 1
    page_border: bool = True
    background_style: DocumentBackgroundStyle = 'plain'
    watermark_text: str = ''
    line_numbers: bool = False
    show_page_number: bool = False
    header_text: str = ''
    footer_text: str = ''

def _plain_text_to_tiptap_doc(text: str) -> dict[str, Any]:
    paragraphs = [{'type': 'paragraph', 'content': [{'type': 'text', 'text': line}]} for line in text.splitlines() if line.strip()]
    return {'type': 'doc', 'content': paragraphs or [{'type': 'paragraph'}]}

def _legacy_blocks_to_rich_document(blocks: list[Any]) -> tuple[str, str]:
    html_parts: list[str] = []
    text_parts: list[str] = []
    for raw_block in blocks:
        block = raw_block.model_dump(mode='json') if hasattr(raw_block, 'model_dump') else raw_block
        if not isinstance(block, dict):
            continue
        title = str(block.get('title') or '').strip()
        content = str(block.get('content') or '').strip()
        block_type = block.get('type')
        if title:
            tag = 'h1' if block_type == 'heading' else 'h2'
            html_parts.append(f'<{tag}>{title}</{tag}>')
            text_parts.append(title)
        if content:
            paragraphs = [line.strip() for line in content.splitlines() if line.strip()]
            for paragraph in paragraphs:
                html_parts.append(f'<p>{paragraph}</p>')
                text_parts.append(paragraph)
    return ('\n'.join(html_parts), '\n'.join(text_parts))

class BoardDocument(BaseModel):
    id: str = Field(default_factory=lambda: new_id('doc'))
    title: str
    content_json: dict[str, Any] = Field(default_factory=lambda: {'type': 'doc', 'content': [{'type': 'paragraph'}]})
    content_html: str = ''
    content_text: str = ''
    page_settings: DocumentPageSettings = Field(default_factory=DocumentPageSettings)

    @model_validator(mode='before')
    @classmethod
    def upgrade_legacy_blocks(cls, value: Any) -> Any:
        if not isinstance(value, dict) or 'blocks' not in value:
            return value
        if value.get('content_html') or value.get('content_text') or value.get('content_json'):
            return value
        html, text = _legacy_blocks_to_rich_document(value.get('blocks') or [])
        return {'id': value.get('id'), 'title': value.get('title') or 'Untitled document', 'content_json': _plain_text_to_tiptap_doc(text), 'content_html': html, 'content_text': text}

class PatchOperation(BaseModel):
    op: PatchOperationType
    block_id: str | None = None
    after_block_id: str | None = None
    node_path: list[int] = Field(default_factory=list)
    title: str | None = None
    content: str | None = None
    content_format: BoardPatchContentFormat = 'markdown'
    block: BoardBlock | None = None
    search: str | None = None
    replacement: str | None = None
    expected_text: str | None = None
    expected_text_hash: str | None = None
    style: BlockStyle | None = None
    asset_url: str | None = None
    note: str | None = None

    @field_validator('node_path', mode='before')
    @classmethod
    def _coerce_nullable_node_path(cls, value: object) -> object:
        if value is None:
            return []
        return value

class BoardSegment(BaseModel):
    segment_id: str
    document_id: str
    kind: BoardSegmentKind
    heading_path: list[str] = Field(default_factory=list)
    order_index: int = 0
    text: str = ''
    html: str = ''
    text_hash: str = ''
    parent_id: str | None = None
    before_segment_id: str | None = None
    after_segment_id: str | None = None

class BoardChunk(BaseModel):
    chunk_id: str
    document_id: str
    source_segment_ids: list[str] = Field(default_factory=list)
    heading_path: list[str] = Field(default_factory=list)
    order_start: int = 0
    order_end: int = 0
    text: str = ''
    text_hash: str = ''

class BoardSegmentIndex(BaseModel):
    document_id: str
    document_title: str = ''
    segments: list[BoardSegment] = Field(default_factory=list)
    chunks: list[BoardChunk] = Field(default_factory=list)

class DocumentSegmentSearchResult(BaseModel):
    package_id: str
    package_title: str
    lesson_id: str
    lesson_title: str
    document_id: str
    document_title: str
    segment_id: str
    kind: BoardSegmentKind
    heading_path: list[str] = Field(default_factory=list)
    order_index: int = 0
    text: str = ''
    text_hash: str = ''

class DocumentSegmentSearchResponse(BaseModel):
    query: str = ''
    kind: BoardSegmentKind | None = None
    results: list[DocumentSegmentSearchResult] = Field(default_factory=list)

class AgentActivityEvent(BaseModel):
    id: str = Field(default_factory=lambda: new_id('agentevt'))
    turn_id: str
    stage: AgentActivityStage
    label: str
    status: AgentActivityStatus = 'completed'
    role: str = 'system'
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now_iso)

class BoardFocusRef(BaseModel):
    source: BoardFocusSource = 'board'
    lesson_id: str | None = None
    document_id: str | None = None
    segment_id: str | None = None
    kind: BoardSegmentKind | None = None
    heading_path: list[str] = Field(default_factory=list)
    excerpt: str = ''
    before_text: str = ''
    after_text: str = ''
    text_hash: str | None = None
    excerpt_hash: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ''
    display_label: str = ''
    match_id: str | None = None
    source_segment_ids: list[str] = Field(default_factory=list)
    order_start: int | None = None
    order_end: int | None = None
    score_breakdown: dict[str, float] = Field(default_factory=dict)

class BoardExplanationDirective(BaseModel):
    status: Literal['approved', 'needs_clarification', 'blocked'] = 'approved'
    target_summary: str = ''
    target_reference: str = Field(default='', pattern='^(?:[a-f0-9]{64})?$')
    target_excerpt: str = ''
    board_feedback: str = ''
    teaching_instruction: str = ''
    constraints: list[str] = Field(default_factory=list)
    clarification_question: str = ''
    reason: str = ''

class LearningSourceReference(BaseModel):
    evidence_bundle_id: str
    source_ingestion_id: str
    source_title: str = ''
    source_chapter_id: str = ''
    chapter_number: str = ''
    chapter_title: str = ''
    scope_kind: str = 'section'
    scope_chapter_id: str = ''
    scope_chapter_number: str = ''
    scope_chapter_title: str = ''
    section_path: list[str] = Field(default_factory=list)
    source_locator: str = ''
    page_range: str = ''
    page_start: int | None = None
    page_end: int | None = None
    body_start_offset: int | None = None
    body_end_offset: int | None = None
    chunk_ids: list[str] = Field(default_factory=list)
    visual_ids: list[str] = Field(default_factory=list)
    source_structure_id: str = ''
    source_structure_updated_at: str = ''
    content_hash: str = ''

class LearningSourceGrounding(BaseModel):
    requested_by_user: bool = False
    confirmation_status: LearningSourceConfirmationStatus = 'none'
    confirmed_bundle_id: str = ''
    confirmed_at: str | None = None
    confirmed_references: list[LearningSourceReference] = Field(default_factory=list)
    frozen_evidence: list['RetrievalEvidence'] = Field(default_factory=list)
    frozen_visual_evidence: list['SourceVisualEvidence'] = Field(default_factory=list)
    reading_notes: list[dict[str, Any]] = Field(default_factory=list)

class LearningRequirementAuxiliaryFactor(BaseModel):
    label: str
    value: str
    evidence: str = ''

class LearningRequirementSheet(BaseModel):
    teaching_type: LearningTeachingType | None = None
    learning_content: str = ''
    current_level: str = ''
    target_scenario: str = ''
    auxiliary_factors: list[LearningRequirementAuxiliaryFactor] = Field(default_factory=list)
    theme: str
    learning_goal: str
    level: str
    known_background: str
    current_questions: list[str]
    learning_need_checklist: list[str] = Field(default_factory=list)
    target_depth: str
    output_preference: str
    boundary: str
    board_scope: list[str]
    success_criteria: str
    risk_notes: list[str] = Field(default_factory=list)
    board_workflow: BoardWorkflow = 'unknown'
    work_mode: InitialLearningWorkMode | None = None
    granularity: InitialLearningGranularity | None = None
    source_grounding: LearningSourceGrounding = Field(default_factory=LearningSourceGrounding)

class BoardTaskRequirementSheet(BaseModel):
    teach_after_mutation: bool = False
    task_id: str = Field(default_factory=lambda: new_id('boardtask'))
    board_workflow: BoardWorkflow = 'act_on_existing_board'
    location_kind: BoardTaskLocationKind = 'unspecified'
    target_hint: str = ''
    insertion_position: Literal['before', 'after', 'parent_start', 'parent_end'] | None = None
    target_location: BoardFocusRef | None = None
    location_status: BoardTaskLocationStatus = 'missing'
    requested_action: BoardTaskRequestedAction | None = None
    question_or_topic: str = ''
    source_query_scope: 'SourceQueryScope | None' = None
    source_scope_origin: Literal['none', 'current_request', 'continued_task'] = 'none'
    special_interaction_requirements: str | None = None
    content_extent: BoardContentExtent | None = None
    topic_relation: BoardTopicRelation = 'unresolved'
    document_destination: BoardDocumentDestination = 'unresolved'
    target_candidates: list[BoardFocusRef] = Field(default_factory=list, max_length=5)
    target_resolution_reason: str = ''
    base_commit_id: str = ''
    base_document_hash: str = ''
    mutation_plan: dict[str, Any] | None = None
    interaction_session: dict[str, Any] | None = None
    missing_items: list[str] = Field(default_factory=list)
    progress: int = Field(default=0, ge=0, le=100)
    confirmation_status: BoardTaskConfirmationStatus = 'none'
    clarification_question: str = ''
    failure_count: int = Field(default=0, ge=0)

class LearningRequirementChecklistItem(BaseModel):
    title: str
    is_clear: bool = False
    evidence: str = ''

class LearningRequirementKeyFact(BaseModel):
    label: str
    value: str
    evidence: str = ''
    category: LearningRequirementFactCategory | None = None

class LearningClarificationStatus(BaseModel):
    progress: int = Field(ge=0, le=100)
    label: str
    reason: str
    missing_items: list[str] = Field(default_factory=list)
    can_start: bool = False
    forced_start: bool = False
    summary: str = ''
    key_facts: list[LearningRequirementKeyFact] = Field(default_factory=list)
    checklist: list[LearningRequirementChecklistItem] = Field(default_factory=list)
    next_question: str = ''
    ready_for_board: bool = False
    teaching_type: LearningTeachingType | None = None
    work_mode: InitialLearningWorkMode | None = None
    granularity: InitialLearningGranularity | None = None

class GuidedRequirementEntryPoint(BaseModel):
    title: str
    description: str
    answer_value: str = ''
    why_it_matters: str = ''
    best_for: str = ''

class GuidedRequirementDiscovery(BaseModel):
    strategy: GuidedRequirementDiscoveryStrategy = 'entry_point_discovery'
    selection_target: GuidedRequirementSelectionTarget = 'learning_content'
    question_title: str = ''
    learning_map_summary: str = ''
    entry_point_options: list[GuidedRequirementEntryPoint] = Field(default_factory=list, max_length=6)
    recommended_entry_point: str = ''
    reason_for_recommendation: str = ''
    learner_profile_inference: str = ''

    def is_empty(self) -> bool:
        return not self.entry_point_options

class TeachingGuideMapping(BaseModel):
    block_id: str
    supports_goal: str
    teaching_mode: TeachingMode
    focus_points: list[str]
    optional_points: list[str] = Field(default_factory=list)
    difficult_points: list[str] = Field(default_factory=list)
    check_questions: list[str] = Field(default_factory=list)

class TeachingGuide(BaseModel):
    lesson_id: str
    summary: str
    structure_note: str
    pacing: str
    mappings: list[TeachingGuideMapping]
    strategy: str

class CommitRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id('commit'))
    label: str
    message: str
    branch_name: str
    created_at: str = Field(default_factory=now_iso)
    parent_ids: list[str] = Field(default_factory=list)
    operations: list[PatchOperation] = Field(default_factory=list)
    snapshot: BoardDocument
    runtime_snapshot: 'LessonRuntimeSnapshot | None' = Field(default=None, exclude=True)
    metadata: dict[str, Any] = Field(default_factory=dict)

class BranchRef(BaseModel):
    name: str
    head_commit_id: str
    base_commit_id: str
    document_cursor_commit_id: str = ''
    document_redo_commit_ids: list[str] = Field(default_factory=list)
    learning_node_ids: list[str] = Field(default_factory=list)
    replay_documents: dict[str, BoardDocument] = Field(default_factory=dict)
    learning_order_merged: bool = False
    node_refs: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = Field(default_factory=now_iso)

class PublicationReviewFinding(BaseModel):
    source_id: str
    source_title: str
    location: str = ''
    evidence_excerpt: str
    reason: str = ''

class PublicationReview(BaseModel):
    id: str = Field(default_factory=lambda: new_id('publicationreview'))
    status: PublicationReviewStatus = 'not_started'
    agent_backend: str = ''
    provider: str = ''
    model: str = ''
    source_fingerprint: str = ''
    scanned_source_count: int = 0
    scanned_unit_count: int = 0
    findings: list[PublicationReviewFinding] = Field(default_factory=list)
    message: str = ''
    started_at: str | None = None
    completed_at: str | None = None

class LessonHistoryGraph(BaseModel):
    branches: dict[str, BranchRef]
    commits: list[CommitRecord]
    current_branch: str = 'main'

class PublishedConversationTurn(BaseModel):
    role: ConversationRole
    content: str

class PublishedLessonVersion(BaseModel):
    revision_id: str = Field(default_factory=lambda: new_id('lessonversion'))
    lesson_id: str
    source_commit_id: str
    title: str
    slug: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    board_document: BoardDocument
    conversation: list[PublishedConversationTurn] = Field(default_factory=list)
    published_at: str = Field(default_factory=now_iso)

class Lesson(BaseModel):
    id: str = Field(default_factory=lambda: new_id('lesson'))
    title: str
    slug: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    visibility: PublicationVisibility = 'private'
    publication_review: PublicationReview = Field(default_factory=PublicationReview)
    published_version: PublishedLessonVersion | None = None
    board_document: BoardDocument
    board_teaching_guide: BoardTeachingGuide | None = None
    board_teaching_progress: BoardTeachingProgress | None = None
    learning_requirements: LearningRequirementSheet | None = None
    board_task_requirements: BoardTaskRequirementSheet | None = None
    teaching_guide: TeachingGuide
    history_graph: LessonHistoryGraph
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)

class LessonRuntimeSnapshot(BaseModel):
    learning_requirements: LearningRequirementSheet | None = None
    board_task_requirements: BoardTaskRequirementSheet | None = None
    board_teaching_guide: 'BoardTeachingGuide | None' = None
    board_teaching_progress: 'BoardTeachingProgress | None' = None

class LessonMergeConflict(BaseModel):
    id: str = Field(default_factory=lambda: new_id('mergeconflict'))
    kind: LessonMergeConflictKind
    path: str = ''
    title: str = ''
    base_value: Any = None
    target_value: Any = None
    source_value: Any = None
    resolution: LessonMergeResolutionKind = 'unresolved'
    custom_value: Any = None
    resolved: bool = False

class MergeRuntimeDraft(BaseModel):
    learning_requirements: LearningRequirementSheet | None = None
    board_task_requirements: BoardTaskRequirementSheet | None = None
    board_teaching_restart_heading_path: list[str] = Field(default_factory=list)
    invalidated_teaching_state: bool = False

class LessonMergeEventNode(BaseModel):
    id: str
    kind: Literal['M', 'C']
    title: str = ''
    summary: str = ''
    branch_name: str = ''
    initial: bool = False

class LessonMergeBlocker(BaseModel):
    node_id: str
    reason: str
    object_id: str | None = None

class LessonMergeEventPlan(BaseModel):
    ridoc_revision: str
    base_node_id: str
    target_node_ids: list[str]
    source_node_ids: list[str]
    default_node_ids: list[str]
    ordered_node_ids: list[str]
    nodes: list[LessonMergeEventNode]
    blockers: list[LessonMergeBlocker] = Field(default_factory=list)

class LessonMergeSession(BaseModel):
    id: str = Field(default_factory=lambda: new_id('mergesession'))
    owner_user_id: str = Field(exclude=True)
    lesson_id: str
    target_branch_name: str
    source_branch_name: str
    base_commit_id: str
    target_head_commit_id: str
    source_head_commit_id: str
    mode: LessonMergeMode = 'manual'
    status: LessonMergeStatus = 'draft'
    version: int = Field(default=1, ge=1)
    conflicts: list[LessonMergeConflict] = Field(default_factory=list)
    merge_blueprint: list[dict[str, Any]] = Field(default_factory=list, exclude=True)
    draft_document: BoardDocument
    event_plan: LessonMergeEventPlan | None = None
    draft_runtime: MergeRuntimeDraft = Field(default_factory=MergeRuntimeDraft)
    ai_model: AIModelSelection | None = None
    agent_activity: list[AgentActivityEvent] = Field(default_factory=list)
    audit: dict[str, Any] = Field(default_factory=dict)
    supersedes_session_id: str | None = None
    committed_commit_id: str | None = None
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)

class LessonMergeConflictView(BaseModel):
    id: str
    kind: LessonMergeConflictKind
    path: str = ''
    title: str = ''
    base_value: Any = None
    target_value: Any = None
    source_value: Any = None
    resolution: LessonMergeResolutionKind = 'unresolved'
    custom_value: Any = None
    resolved: bool = False

class LessonMergeSessionView(BaseModel):
    id: str
    lesson_id: str
    target_branch_name: str
    source_branch_name: str
    base_commit_id: str
    target_head_commit_id: str
    source_head_commit_id: str
    mode: LessonMergeMode
    status: LessonMergeStatus
    version: int
    conflicts: list[LessonMergeConflictView] = Field(default_factory=list)
    draft_document: BoardDocument
    event_plan: LessonMergeEventPlan | None = None
    draft_runtime: MergeRuntimeDraft = Field(default_factory=MergeRuntimeDraft)
    ai_model: AIModelSelection | None = None
    agent_activity: list[AgentActivityEvent] = Field(default_factory=list)
    audit: dict[str, Any] = Field(default_factory=dict)
    supersedes_session_id: str | None = None
    committed_commit_id: str | None = None
    created_at: str
    updated_at: str

class CreateLessonMergeSessionRequest(BaseModel):
    source_branch_name: str = Field(min_length=1, max_length=128)
    mode: LessonMergeMode = 'manual'
    text_model: AIModelSelection | None = None

class LessonMergeConflictResolution(BaseModel):
    conflict_id: str
    resolution: LessonMergeResolutionKind
    custom_value: Any = None

class UpdateLessonMergeSessionRequest(BaseModel):
    expected_version: int = Field(ge=1)
    draft_document: BoardDocument | None = None
    draft_runtime: MergeRuntimeDraft | None = None
    resolutions: list[LessonMergeConflictResolution] = Field(default_factory=list)
    ordered_node_ids: list[str] | None = Field(default=None, max_length=10000)

class SubmitLessonMergeSessionRequest(BaseModel):
    expected_version: int = Field(ge=1)
    expected_ridoc_revision: str | None = None

class AIProposalLessonMergeSessionRequest(BaseModel):
    expected_version: int = Field(ge=1)

class RecomputeLessonMergeSessionRequest(BaseModel):
    expected_version: int = Field(ge=1)
    mode: LessonMergeMode | None = None
    text_model: AIModelSelection | None = None

class LessonContributionActor(BaseModel):
    user_id: str
    display_name: str
    avatar_url: str | None = None

class LessonContributionRevision(BaseModel):
    id: str = Field(default_factory=lambda: new_id('contributionrevision'))
    contribution_id: str
    revision_number: int = Field(ge=1)
    source_commit_id: str
    contributor_commit_id: str
    base_document: BoardDocument
    proposed_document: BoardDocument
    created_at: str = Field(default_factory=now_iso)

class LessonContributionEvent(BaseModel):
    id: str = Field(default_factory=lambda: new_id('contributionevent'))
    contribution_id: str
    kind: LessonContributionEventKind
    actor: LessonContributionActor
    body: str = ''
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now_iso)

class LessonContribution(BaseModel):
    id: str = Field(default_factory=lambda: new_id('contribution'))
    source_lesson_id: str
    source_owner_user_id: str
    contributor_lesson_id: str
    contributor_user_id: str
    source_title: str
    title: str
    description: str = ''
    status: LessonContributionStatus = 'open'
    version: int = Field(default=1, ge=1)
    current_revision: int = Field(default=1, ge=1)
    current_revision_id: str
    source_author: LessonContributionActor
    contributor: LessonContributionActor
    merge_session_id: str | None = None
    merged_commit_id: str | None = None
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    closed_at: str | None = None

class LessonContributionRevisionView(BaseModel):
    id: str
    revision_number: int
    source_commit_id: str
    base_document: BoardDocument
    proposed_document: BoardDocument
    created_at: str

class LessonContributionViewerPermissions(BaseModel):
    can_comment: bool = False
    can_update: bool = False
    can_close: bool = False
    can_reopen: bool = False
    can_start_merge: bool = False
    can_return_for_changes: bool = False

class LessonContributionView(BaseModel):
    id: str
    source_lesson_id: str
    viewer_project_lesson_id: str | None = None
    source_title: str
    title: str
    description: str
    status: LessonContributionStatus
    version: int
    current_revision: int
    source_author: LessonContributionActor
    contributor: LessonContributionActor
    revision: LessonContributionRevisionView
    events: list[LessonContributionEvent] = Field(default_factory=list)
    viewer_permissions: LessonContributionViewerPermissions = Field(default_factory=LessonContributionViewerPermissions)
    source_is_public: bool
    merge_session_id: str | None = None
    merged_commit_id: str | None = None
    created_at: str
    updated_at: str
    closed_at: str | None = None

class CreateLessonContributionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default='', max_length=4000)

class UpdateLessonContributionRevisionRequest(BaseModel):
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4000)

class LessonContributionVersionRequest(BaseModel):
    expected_version: int = Field(ge=1)

class LessonContributionCommentRequest(BaseModel):
    expected_version: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=4000)

class CourseGraphEdge(BaseModel):
    id: str = Field(default_factory=lambda: new_id('edge'))
    source_lesson_id: str
    target_lesson_id: str
    relationship: CourseEdgeType

class ResourcePageSection(BaseModel):
    role: ResourcePageRole = 'unknown'
    page_idx_start: int | None = None
    page_idx_end: int | None = None
    page_no_start: int | None = None
    page_no_end: int | None = None
    title: str = ''
    confidence: float = 0.0
    evidence_excerpt: str = ''

class ResourcePageMapEntry(BaseModel):
    page_idx: int
    page_no: int
    role: ResourcePageRole = 'unknown'
    printed_page: int | None = None
    body_offset: int | None = None
    confidence: float = 0.0
    evidence_excerpt: str = ''

class ResourcePageStructure(BaseModel):
    page_count: int = 0
    body_start_page_idx: int | None = None
    body_start_page_no: int | None = None
    toc_page_indices: list[int] = Field(default_factory=list)
    sections: list[ResourcePageSection] = Field(default_factory=list)
    page_map: list[ResourcePageMapEntry] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    confidence: float = 0.0
ResourceSourceType = Literal['local_file', 'web_url', 'code_repository', 'audio_file', 'video_file', 'video_url', 'pasted_text', 'transcript']
SourceIngestionStatus = Literal['queued', 'fetching', 'parsing', 'indexing', 'ready', 'cancelled', 'interrupted', 'failed']
EvidenceBundleStatus = Literal['candidate', 'confirmed', 'consumed', 'archived']
EvidencePurpose = Literal['chat', 'board_generation', 'board_edit', 'board_explain', 'board_chat']
SourceStructureStatus = Literal['pending', 'building', 'ready', 'linear_only', 'failed']
SourceStructureQualityLevel = Literal['unassessed', 'fully_verified', 'partially_verified', 'unverified', 'search_only']
SourceTextReadiness = Literal['unknown', 'ready', 'sparse', 'very_sparse', 'empty']
SourceStructureStrategy = Literal['code_repository_v1', 'native_directory_v1', 'hybrid_directory_v1', 'ai_directory_v1', 'codex_directory_v1', 'codex_catalog', 'epub_navigation', 'epub_heading', 'pdf_outline', 'pdf_toc', 'pdf_merged_toc', 'pdf_layout_toc', 'docx_heading', 'markdown_heading', 'linear_text', 'open_notebook_search_only']
SourceChapterAnchorStatus = Literal['verified', 'unverified']
SourceChapterMappingStatus = Literal['verified', 'partial', 'unverified', 'unmapped']
SourceCatalogWorkState = Literal['working', 'paused', 'satisfied', 'partial']
SourceCatalogPhase = Literal['directory_discovery', 'page_calibration', 'range_mapping', 'validation', 'terminal']
SourceCatalogDirectoryStatus = Literal['incomplete', 'uncertain', 'complete']
SourceCatalogIndexStatus = Literal['pending', 'in_progress', 'complete', 'partial']
SourceRangeKind = Literal['file_lines', 'pdf_pages', 'epub_spine', 'docx_paragraphs', 'ppt_slides', 'sheet_rows', 'sheet_cells', 'text_lines', 'dom_anchor', 'structured_path', 'notebook_cells', 'image_frames', 'media_time', 'workbook_visual']
SourceCatalogRunStatus = Literal['queued', 'running', 'succeeded', 'failed']
SourceVisualIndexStatus = Literal['pending', 'ready', 'partial', 'failed', 'unsupported']
SourceVisualAnchorStatus = Literal['verified', 'unverified']
SourceVisualKind = Literal['image', 'chart', 'table', 'diagram', 'page_snapshot']
SourceScopeKind = Literal['source', 'chapter', 'page_range', 'repository_node']
SourceQueryScopeMode = Literal['chapter', 'page_range', 'source', 'sources', 'all_ready_sources']
PostGenerationAction = Literal['auto_explain', 'stop_after_generation']
AutoTeachingOperationStatus = Literal['none', 'succeeded', 'failed']

class SourceRange(BaseModel):
    """A format-native, inclusive range that can be validated before source reading."""
    kind: SourceRangeKind
    start: int | str | None = None
    end: int | str | None = None
    container: str = ''
    start_anchor: str = ''
    end_anchor: str = ''
    path: list[str] = Field(default_factory=list)
    display_label: str = ''
    end_inclusive: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_range(self) -> 'SourceRange':
        if not self.end_inclusive:
            raise ValueError('SourceRange.end must be inclusive.')
        if self.kind in {'pdf_pages', 'file_lines'}:
            if not isinstance(self.start, int) or isinstance(self.start, bool):
                raise ValueError('Page and file-line ranges require an integer start.')
            if not isinstance(self.end, int) or isinstance(self.end, bool):
                raise ValueError('Page and file-line ranges require an integer end.')
            if self.start < 1 or self.end < 1:
                raise ValueError('Page and file-line ranges use 1-based positions.')
            if self.end < self.start:
                raise ValueError('SourceRange.end must not precede SourceRange.start.')
            if self.kind == 'file_lines' and (not self.container.strip()):
                raise ValueError('File-line ranges require a repository path container.')
        elif isinstance(self.start, int) and isinstance(self.end, int) and (self.end < self.start):
            raise ValueError('SourceRange.end must not precede SourceRange.start.')
        return self

class SourceCatalogEvidence(BaseModel):
    method: str = ''
    source_locator: str = ''
    page_start: int | None = None
    page_end: int | None = None
    excerpt: str = ''
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)

class SourceStructureQuality(BaseModel):
    evaluator_version: int = 1
    level: SourceStructureQualityLevel = 'unassessed'
    text_readiness: SourceTextReadiness = 'unknown'
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    total_chapter_count: int = 0
    verified_chapter_count: int = 0
    unverified_chapter_count: int = 0
    demoted_chapter_count: int = 0
    verified_leaf_count: int = 0
    expected_leaf_count: int = 0
    verified_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    boundary_valid_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    body_coverage_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    independent_anchor_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    meaningful_characters_per_page: float = Field(default=0.0, ge=0.0)
    duplicate_locator_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    duplicate_range_count: int = 0
    overlap_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    non_monotonic_count: int = 0
    oversized_leaf_count: int = 0
    diagnostics: list[str] = Field(default_factory=list)

class SourceIngestionJob(BaseModel):
    id: str = Field(default_factory=lambda: new_id('ingest'))
    run_id: str = ''
    resource_id: str | None = None
    source_type: ResourceSourceType = 'local_file'
    source_uri: str | None = None
    adapter: str = ''
    status: SourceIngestionStatus = 'queued'
    progress: int = Field(default=0, ge=0, le=100)
    error: str = ''
    phase_history: list[str] = Field(default_factory=list)
    agent_activity: list[AgentActivityEvent] = Field(default_factory=list)
    processing_metrics: dict[str, Any] | None = None
    cancel_requested: bool = False
    heartbeat_at: str | None = None
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)

class LibraryChapter(BaseModel):
    id: str = Field(default_factory=lambda: new_id('chapter'))
    title: str
    level: int = 1
    page_range: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    summary: str
    keywords: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    parent_id: str | None = None
    parent_title: str | None = None
    path: list[str] = Field(default_factory=list)
    locator_hint: str | None = None
    order_index: int = 0
    scan_strategy: ResourceScanStrategy = 'outline_only'
    body_start_order: int | None = None
    body_end_order: int | None = None
    body_page_start: int | None = None
    body_page_end: int | None = None
    body_match_status: str = ''
    body_match_confidence: float = 0.0
    body_match_reason: str = ''

class ResourceSourceUnit(BaseModel):
    id: str = Field(default_factory=lambda: new_id('sourceunit'))
    content_type: str = 'text'
    text: str = ''
    page_idx: int | None = None
    page_no: int | None = None
    source_locator: str | None = None
    url: str | None = None
    heading_path: list[str] = Field(default_factory=list)
    paragraph_index: int | None = None
    timestamp_start: float | None = None
    timestamp_end: float | None = None
    asset_path: str | None = None
    bbox: list[float] = Field(default_factory=list)
    order_index: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

class ResourceLibraryItem(BaseModel):
    id: str = Field(default_factory=lambda: new_id('resource'))
    name: str
    mime_type: str
    resource_type: str
    size_bytes: int
    uploaded_at: str = Field(default_factory=now_iso)
    scope_lesson_id: str | None = None
    outline: list[LibraryChapter] = Field(default_factory=list)
    concept_index: dict[str, list[str]] = Field(default_factory=dict)
    extracted_text_available: bool = False
    text_content: str | None = None
    source_path: str | None = None
    source_type: ResourceSourceType = 'local_file'
    source_uri: str | None = None
    ingestion_status: SourceIngestionStatus = 'ready'
    ingestion_error: str = ''
    ingestion_progress: int = Field(default=100, ge=0, le=100)
    ingestion_adapter: str = ''
    ingestion_job: SourceIngestionJob | None = None
    parser_provider: str = 'native'
    parser_artifacts_path: str | None = None
    parser_message: str = ''
    parse_warnings: list[str] = Field(default_factory=list)
    source_units: list[ResourceSourceUnit] = Field(default_factory=list)
    page_structure: ResourcePageStructure | None = None

class SourceIngestionRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id('source'))
    owner_user_id: str = ''
    package_id: str
    title: str
    source_type: ResourceSourceType = 'local_file'
    source_uri: str | None = None
    file_name: str = ''
    mime_type: str = ''
    size_bytes: int = 0
    status: SourceIngestionStatus = 'queued'
    error: str = ''
    open_notebook_notebook_id: str = ''
    open_notebook_source_id: str = ''
    open_notebook_command_id: str = ''
    structure_status: SourceStructureStatus = 'pending'
    structure_strategy: SourceStructureStrategy | None = None
    structure_has_verified_toc: bool = False
    structure_quality: SourceStructureQuality = Field(default_factory=SourceStructureQuality)
    structure_error: str = ''
    structure_updated_at: str | None = None
    ingestion_job: SourceIngestionJob | None = None
    refinement: SourceCatalogRefinementView | None = None
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    metadata: dict[str, Any] = Field(default_factory=dict)
RepositoryTreeKind = Literal['project', 'learning']
RepositoryNodeKind = Literal['root', 'directory', 'file', 'module', 'flow', 'entrypoint', 'concept']
RepositoryConnectionStatus = Literal['connected', 'revoked', 'suspended', 'disconnected']

class RepositorySnapshot(BaseModel):
    id: str = Field(default_factory=lambda: new_id('reposnapshot'))
    owner_user_id: str
    package_id: str
    source_ingestion_id: str
    provider: str = 'github'
    repository_id: int | None = None
    owner: str
    name: str
    visibility: str = 'public'
    requested_ref: str = ''
    resolved_commit_sha: str
    scope_path: str = ''
    scope_kind: Literal['repository', 'directory', 'file'] = 'repository'
    default_branch: str = ''
    archive_path: str = Field(default='', exclude=True)
    archive_hash: str
    manifest_hash: str
    license_spdx: str = ''
    supersedes_source_id: str | None = None
    created_at: str = Field(default_factory=now_iso)
    metadata: dict[str, Any] = Field(default_factory=dict)

class RepositoryFileEntry(BaseModel):
    id: str = Field(default_factory=lambda: new_id('repofile'))
    source_ingestion_id: str
    path: str
    blob_sha: str = ''
    content_hash: str = ''
    size_bytes: int = 0
    line_count: int = 0
    language: str = ''
    text_status: Literal['ready', 'binary', 'oversized', 'unsupported', 'unreadable'] = 'ready'
    skip_reason: str = ''
    archive_entry: str = Field(default='', exclude=True)
    order_index: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

class RepositoryNodeEvidence(BaseModel):
    file_id: str
    path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    reason: str = ''
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode='after')
    def validate_lines(self) -> 'RepositoryNodeEvidence':
        if self.line_end < self.line_start:
            raise ValueError('Repository evidence end line must not precede its start line.')
        return self

class RepositoryMapNode(BaseModel):
    id: str = Field(default_factory=lambda: new_id('reponode'))
    source_ingestion_id: str
    tree_kind: RepositoryTreeKind
    node_kind: RepositoryNodeKind
    parent_id: str | None = None
    title: str
    path: str = ''
    description: str = ''
    level: int = Field(default=0, ge=0)
    order_index: int = 0
    selectable: bool = False
    coverage_status: Literal['complete', 'partial', 'unexamined'] = 'unexamined'
    evidence: list[RepositoryNodeEvidence] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

class RepositoryMapView(BaseModel):
    source: SourceIngestionRecord
    snapshot: RepositorySnapshot
    project_nodes: list[RepositoryMapNode] = Field(default_factory=list)
    learning_nodes: list[RepositoryMapNode] = Field(default_factory=list)
    analyzed_file_count: int = 0
    readable_file_count: int = 0
    total_file_count: int = 0
    coverage_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list)

class GitHubInstallationView(BaseModel):
    installation_id: int
    account_id: int | None = None
    account_login: str = ''
    account_type: str = ''
    repository_selection: str = 'selected'
    status: RepositoryConnectionStatus = 'connected'
    permissions: dict[str, str] = Field(default_factory=dict)
    repository_count: int = 0
    updated_at: str = Field(default_factory=now_iso)

class GitHubConnectionView(BaseModel):
    enabled: bool
    configured: bool
    connected: bool
    installations: list[GitHubInstallationView] = Field(default_factory=list)
    message: str = ''

class GitHubRepositoryView(BaseModel):
    id: int
    full_name: str
    owner: str
    name: str
    private: bool = False
    default_branch: str = ''
    html_url: str
    description: str = ''
    installation_id: int | None = None

class SourceUrlImportRequest(BaseModel):
    source_uri: str
    title: str = ''

class RetrievalEvidence(BaseModel):
    id: str = Field(default_factory=lambda: new_id('evidence'))
    source_ingestion_id: str = ''
    open_notebook_source_id: str = ''
    source_title: str = ''
    source_uri: str | None = None
    chapter_id: str = ''
    section_path: list[str] = Field(default_factory=list)
    page_range: str = ''
    chunk_ids: list[str] = Field(default_factory=list)
    excerpt: str = ''
    expanded_text: str = ''
    relevance_score: float = 0.0
    reason: str = ''
    token_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

class SourceCitation(BaseModel):
    evidence_id: str
    citation_number: int | None = Field(default=None, ge=1)
    evidence_ref: str = ''
    source_document_id: str = ''
    snapshot_url: str = ''
    source_id: str
    source_title: str = ''
    section_path: list[str] = Field(default_factory=list)
    page_start: int | None = None
    page_end: int | None = None
    excerpt: str = ''
    chunk_ids: list[str] = Field(default_factory=list)
    block_ids: list[str] = Field(default_factory=list)
    bbox: list[float] = Field(default_factory=list)
    source_content_hash: str = ''
    parser_run_id: str = ''

class SourceStructure(BaseModel):
    id: str = Field(default_factory=lambda: new_id('structure'))
    owner_user_id: str = ''
    package_id: str
    source_ingestion_id: str
    status: SourceStructureStatus = 'pending'
    strategy: SourceStructureStrategy = 'linear_text'
    has_verified_toc: bool = False
    quality: SourceStructureQuality = Field(default_factory=SourceStructureQuality)
    chapter_count: int = 0
    chunk_count: int = 0
    visual_count: int = 0
    visual_index_status: SourceVisualIndexStatus = 'pending'
    visual_index_version: int = 0
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    catalog_version: int = Field(default=0, ge=0)
    catalog_updated_at: str | None = None
    source_content_hash: str = ''
    catalog_schema_version: str = 'legacy'
    catalog_model: str = ''
    error: str = ''
    warnings: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    metadata: dict[str, Any] = Field(default_factory=dict)

class SourceChapter(BaseModel):
    id: str = Field(default_factory=lambda: new_id('sourcechapter'))
    owner_user_id: str = ''
    package_id: str
    source_ingestion_id: str
    parent_id: str | None = None
    number: str = ''
    normalized_number: str = ''
    title: str
    level: int = 1
    path: list[str] = Field(default_factory=list)
    order_index: int = 0
    source_locator: str = ''
    body_start_offset: int | None = None
    body_end_offset: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    anchor_status: SourceChapterAnchorStatus = 'unverified'
    range: SourceRange | None = None
    mapping_status: SourceChapterMappingStatus = 'unverified'
    source_content_hash: str = ''
    catalog_evidence: list[SourceCatalogEvidence] = Field(default_factory=list)
    catalog_version: int = Field(default=0, ge=0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    excerpt: str = ''
    metadata: dict[str, Any] = Field(default_factory=dict)

class SourceCatalogRun(BaseModel):
    id: str = Field(default_factory=lambda: new_id('catalogrun'))
    owner_user_id: str = ''
    package_id: str
    source_ingestion_id: str
    status: SourceCatalogRunStatus = 'queued'
    catalog_version: int = Field(default=0, ge=0)
    model: str = ''
    turn_count: int = Field(default=0, ge=0)
    page_count: int = Field(default=0, ge=0)
    inspected_page_count: int = Field(default=0, ge=0)
    ocr_page_count: int = Field(default=0, ge=0)
    chapter_count: int = Field(default=0, ge=0)
    verified_chapter_count: int = Field(default=0, ge=0)
    verification_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    duration_ms: int = Field(default=0, ge=0)
    stage_history: list[str] = Field(default_factory=list)
    error: str = ''
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    completed_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

class SourceChunk(BaseModel):
    id: str = Field(default_factory=lambda: new_id('sourcechunk'))
    owner_user_id: str = ''
    package_id: str
    source_ingestion_id: str
    chapter_id: str | None = None
    order_index: int = 0
    source_locator: str = ''
    text: str = ''
    start_offset: int = 0
    end_offset: int = 0
    page_start: int | None = None
    page_end: int | None = None
    token_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

class SourceVisualAsset(BaseModel):
    id: str = Field(default_factory=lambda: new_id('sourcevisual'))
    owner_user_id: str = ''
    package_id: str
    source_ingestion_id: str
    structure_id: str = ''
    structure_version: int = 0
    chapter_id: str | None = None
    kind: SourceVisualKind = 'image'
    source_locator: str = ''
    page_start: int | None = None
    page_end: int | None = None
    paragraph_index: int | None = None
    slide_no: int | None = None
    sheet_name: str = ''
    bbox: list[float] = Field(default_factory=list)
    before_chunk_id: str | None = None
    after_chunk_id: str | None = None
    caption: str = ''
    extracted_text: str = ''
    surrounding_text: str = ''
    anchor_status: SourceVisualAnchorStatus = 'unverified'
    mime_type: str = ''
    asset_path: str = Field(default='', exclude=True)
    storage_key: str = Field(default='', exclude=True)
    order_index: int = 0
    content_hash: str = ''
    position_hash: str = ''
    width: int | None = None
    height: int | None = None
    table_data: list[list[str]] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    created_at: str = Field(default_factory=now_iso)
    metadata: dict[str, Any] = Field(default_factory=dict)

class SourceVisualEvidence(BaseModel):
    visual_id: str
    package_id: str = ''
    source_ingestion_id: str
    source_chapter_id: str = ''
    kind: SourceVisualKind = 'image'
    source_locator: str = ''
    page_start: int | None = None
    page_end: int | None = None
    paragraph_index: int | None = None
    slide_no: int | None = None
    sheet_name: str = ''
    bbox: list[float] = Field(default_factory=list)
    before_chunk_id: str | None = None
    after_chunk_id: str | None = None
    caption: str = ''
    extracted_text: str = ''
    surrounding_text: str = ''
    anchor_status: SourceVisualAnchorStatus = 'unverified'
    mime_type: str = ''
    order_index: int = 0
    content_hash: str = ''
    position_hash: str = ''
    width: int | None = None
    height: int | None = None
    table_data: list[list[str]] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)

class SourceStructureView(BaseModel):
    source: SourceIngestionRecord
    structure: SourceStructure | None = None
    chapters: list[SourceChapter] = Field(default_factory=list)
    chunks: list[SourceChunk] = Field(default_factory=list)
    visuals: list[SourceVisualAsset] = Field(default_factory=list)

class SourceCatalogSourceSummary(BaseModel):
    id: str
    title: str
    file_name: str = ''
    mime_type: str = ''
    size_bytes: int = 0
    status: SourceIngestionStatus = 'queued'
    structure_status: SourceStructureStatus = 'pending'

class SourceCatalogWorkPageRange(BaseModel):
    start: int = Field(ge=1)
    end: int = Field(ge=1)

class SourceCatalogWorkItem(BaseModel):
    id: str
    kind: Literal['directory_discovery', 'directory_page_attribution', 'pagination_calibration', 'range_mapping', 'conflict_resolution']
    node_keys: list[str] = Field(default_factory=list)
    page_ranges: list[SourceCatalogWorkPageRange] = Field(default_factory=list)
    reason: str

class SourceCatalogView(BaseModel):
    source: SourceCatalogSourceSummary
    structure_id: str | None = None
    status: SourceStructureStatus = 'pending'
    strategy: SourceStructureStrategy | None = None
    has_verified_toc: bool = False
    catalog_version: int = Field(default=0, ge=0)
    catalog_updated_at: str | None = None
    source_content_hash: str = ''
    catalog_schema_version: str = 'legacy'
    catalog_model: str = ''
    catalog_authority: str = ''
    model_call_count: int = Field(default=0, ge=0)
    task_contract: str = ''
    work_state: SourceCatalogWorkState = 'satisfied'
    phase: SourceCatalogPhase = 'terminal'
    directory_status: SourceCatalogDirectoryStatus = 'complete'
    index_status: SourceCatalogIndexStatus = 'complete'
    summary: str = ''
    next_plan: str = ''
    stop_reason: str = ''
    completion_reason: str = ''
    directory_gaps: list[str] = Field(default_factory=list)
    remaining_work: list[SourceCatalogWorkItem] = Field(default_factory=list)
    remaining_work_count: int = Field(default=0, ge=0)
    snapshot_reason: Literal['first_citable', 'top_level_subtree', 'batch', 'budget_increment', 'correction', 'pause', 'final'] = 'budget_increment'
    background_refine_active: bool = False
    pagination_regime_count: int = Field(default=0, ge=0)
    unresolved_node_count: int = Field(default=0, ge=0)
    locator_method: str = ''
    revision: int = Field(default=0, ge=0)
    can_refine: bool = False
    recent_tool_activity: list[dict[str, Any]] = Field(default_factory=list)
    chapter_count: int = Field(default=0, ge=0)
    verified_chapter_count: int = Field(default=0, ge=0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    quality: SourceStructureQuality = Field(default_factory=SourceStructureQuality)
    error: str = ''
    warnings: list[str] = Field(default_factory=list)
    chapters: list[SourceChapter] = Field(default_factory=list)

class SourceCatalogBatchView(BaseModel):
    package_id: str
    catalogs: list[SourceCatalogView] = Field(default_factory=list)

class EvidenceBundle(BaseModel):
    id: str = Field(default_factory=lambda: new_id('bundle'))
    owner_user_id: str = ''
    package_id: str
    lesson_id: str | None = None
    requirement_run_id: str | None = None
    board_task_run_id: str | None = None
    purpose: EvidencePurpose = 'chat'
    status: EvidenceBundleStatus = 'candidate'
    query: str = ''
    evidence_items: list[RetrievalEvidence] = Field(default_factory=list)
    visual_items: list[SourceVisualEvidence] = Field(default_factory=list)
    context_text: str = ''
    token_count: int = 0
    confirmed_by_user: bool = False
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    confirmed_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

class ResourceLibraryItemView(BaseModel):
    id: str
    name: str
    mime_type: str
    resource_type: str
    size_bytes: int
    uploaded_at: str
    scope_lesson_id: str | None = None
    outline: list[LibraryChapter] = Field(default_factory=list)
    concept_index: dict[str, list[str]] = Field(default_factory=dict)
    extracted_text_available: bool = False
    source_type: ResourceSourceType = 'local_file'
    source_uri: str | None = None
    ingestion_status: SourceIngestionStatus = 'ready'
    ingestion_error: str = ''
    ingestion_progress: int = Field(default=100, ge=0, le=100)
    ingestion_adapter: str = ''
    ingestion_job: SourceIngestionJob | None = None
    parser_provider: str = 'native'
    parser_artifacts_path: str | None = None
    parser_message: str = ''
    parse_warnings: list[str] = Field(default_factory=list)
    source_units: list[ResourceSourceUnit] = Field(default_factory=list)
    page_structure: ResourcePageStructure | None = None

class CoursePackage(BaseModel):
    id: str = Field(default_factory=lambda: new_id('course'))
    title: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    visibility: PublicationVisibility = 'private'
    publication_review: PublicationReview = Field(default_factory=PublicationReview)
    published_version: 'PublishedCoursePackageVersion | None' = None
    lessons: list[Lesson]
    course_graph: list[CourseGraphEdge] = Field(default_factory=list)
    resources: list[ResourceLibraryItem] = Field(default_factory=list)
    open_lesson_ids: list[str] = Field(default_factory=list)
    active_lesson_id: str | None = None
    workspace_tab_order: list[str] = Field(default_factory=list)
    storage_kind: Literal['legacy', 'ridoc'] = 'legacy'
    ridoc_revision: str | None = None
    package_kind: Literal['course', 'standalone'] = 'course'

class PublishedCoursePackageVersion(BaseModel):
    revision_id: str = Field(default_factory=lambda: new_id('packageversion'))
    title: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    lessons: list[PublishedLessonVersion] = Field(default_factory=list)
    course_graph: list[CourseGraphEdge] = Field(default_factory=list)
    published_at: str = Field(default_factory=now_iso)

class WorkspaceState(BaseModel):
    packages: list[CoursePackage] = Field(default_factory=list)
    active_package_id: str | None = None

    @model_validator(mode='after')
    def ensure_active_package(self) -> 'WorkspaceState':
        if self.active_package_id and any((package.id == self.active_package_id for package in self.packages)):
            return self
        self.active_package_id = self.packages[0].id if self.packages else None
        return self

class SelectionRef(BaseModel):
    kind: SelectionKind
    excerpt: str
    location_kind: BoardTaskLocationKind | None = None
    lesson_id: str | None = None
    source_commit_id: str | None = None
    block_id: str | None = None
    document_id: str | None = None
    segment_id: str | None = None
    heading_path: list[str] = Field(default_factory=list)
    before_text: str = ''
    after_text: str = ''
    text_hash: str | None = None
    source_ingestion_id: str | None = None
    source_title: str = ''
    source_uri: str | None = None
    source_chapter_id: str | None = None
    source_chapter_number: str = ''
    source_chapter_title: str = ''
    source_page_range: str = ''
    source_locator: str = ''
    source_page_start: int | None = None
    source_page_end: int | None = None
    source_range: SourceRange | None = None
    catalog_version: int | None = Field(default=None, ge=0)
    source_content_hash: str | None = None
    source_scope_kind: SourceScopeKind = 'chapter'
    source_repository_node_id: str | None = None
    source_repository_tree_kind: RepositoryTreeKind | None = None

class SourceQueryRef(BaseModel):
    source_range: SourceRange | None = None
    source_ingestion_id: str = Field(min_length=1, max_length=128)
    source_content_hash: str = Field(min_length=1, max_length=128)
    source_chapter_id: str | None = Field(default=None, max_length=128)
    catalog_version: int | None = Field(default=None, ge=0)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)

    @model_validator(mode='after')
    def validate_page_range(self) -> 'SourceQueryRef':
        if (self.page_start is None) != (self.page_end is None):
            raise ValueError('Source query page ranges require both start and end pages.')
        if self.page_start is not None and self.page_end is not None and (self.page_end < self.page_start):
            raise ValueError('Source query page_end must not precede page_start.')
        return self

class SourceQueryScope(BaseModel):
    mode: SourceQueryScopeMode
    refs: list[SourceQueryRef] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def validate_scope(self) -> 'SourceQueryScope':
        if self.mode == 'all_ready_sources':
            if self.refs:
                raise ValueError('all_ready_sources does not accept explicit refs.')
            return self
        if not self.refs:
            raise ValueError(f'{self.mode} requires at least one source ref.')
        if self.mode == 'source' and len(self.refs) != 1:
            raise ValueError(f'{self.mode} requires exactly one source ref.')
        if self.mode == 'chapter' and any((not ref.source_chapter_id for ref in self.refs)):
            raise ValueError('chapter scope requires source_chapter_id.')
        if self.mode == 'page_range' and any((ref.page_start is None for ref in self.refs)):
            raise ValueError('page_range scope requires page_start and page_end.')
        if self.mode in {'source', 'sources'} and any((ref.source_chapter_id or ref.page_start is not None for ref in self.refs)):
            raise ValueError(f'{self.mode} accepts whole-source refs only.')
        return self

class FormulaInkPayload(BaseModel):
    image_data_url: str
    source_latex: str | None = None
    action: FormulaInkAction

class ChatAttachmentRef(BaseModel):
    source_ingestion_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=512)
    mime_type: str = Field(default='application/octet-stream', max_length=255)
    size_bytes: int = Field(default=0, ge=0)
    kind: Literal['image', 'file'] = 'file'
    status: SourceIngestionStatus = 'queued'

class ConversationTurn(BaseModel):
    role: ConversationRole
    content: str

class AIReasoningEffortOption(BaseModel):
    reasoning_effort: str = Field(min_length=1)
    description: str = ''

class AIServiceTierOption(BaseModel):
    id: str = Field(min_length=1)
    name: str = ''
    description: str = ''

class AIAgentBackendOption(BaseModel):
    id: Literal['codex', 'pi']
    label: str
    description: str = ''
    enabled: bool = False

class AIModelSelection(BaseModel):
    agent_backend: Literal['codex', 'pi'] = 'pi'
    provider: AIProvider
    model: str
    access_method: AIModelAccessMethod | None = None
    connection_id: str | None = Field(default=None, min_length=1, max_length=128)
    reasoning_effort: str | None = None
    service_tier: str | None = None

    @model_validator(mode='after')
    def validate_access_method(self) -> 'AIModelSelection':
        if self.connection_id and (self.provider != 'openai_codex' or self.access_method == 'platform_credits'):
            raise ValueError('A Codex connection is only available for ChatGPT subscription models.')
        if self.access_method is None:
            return self
        supported_access_methods: dict[AIProvider, set[AIModelAccessMethod]] = {'openai_codex': {'chatgpt_subscription', 'platform_credits'}, 'openai': {'personal_api', 'platform_credits'}, 'deepseek': {'personal_api', 'platform_credits'}}
        allowed = supported_access_methods.get(self.provider)
        if allowed is not None and self.access_method not in allowed:
            raise ValueError(f'Access method {self.access_method!r} is not available for provider {self.provider!r}')
        return self

class AIModelOption(BaseModel):
    provider: AIProvider
    model: str
    access_method: AIModelAccessMethod
    connection_id: str | None = None
    label: str
    capability: AIModelCapability
    supports_image_inputs: bool = False
    enabled: bool = False
    configured: bool = False
    default: bool = False
    transport: AIRealtimeTransport | None = None
    default_reasoning_effort: str | None = None
    supported_reasoning_efforts: list[AIReasoningEffortOption] = Field(default_factory=list)
    default_service_tier: str | None = None
    service_tiers: list[AIServiceTierOption] = Field(default_factory=list)
    input_price_credits_per_million: int | None = Field(default=None, ge=0)

class AIModelCatalog(BaseModel):
    text: list[AIModelOption] = Field(default_factory=list)
    realtime: list[AIModelOption] = Field(default_factory=list)
    defaults: dict[AIModelCapability, AIModelSelection]
    agent_backends: dict[Literal['teaching', 'source'], list[AIAgentBackendOption]] = Field(default_factory=dict)

class AIProviderCredentialInput(BaseModel):
    api_key: SecretStr
    provider_config: dict[str, str] = Field(default_factory=dict)

class AIProviderCredentialStatus(BaseModel):
    provider: AIProvider
    label: str
    configured: bool = False
    manageable: bool = False
    configuration_fields: list[dict[str, str]] = Field(default_factory=list)

class CodexAccountView(BaseModel):
    type: str | None = None
    email: str | None = None
    plan_type: str | None = None

class CodexProviderStatus(BaseModel):
    connection_id: str | None = None
    enabled: bool
    available: bool
    configured: bool
    account: CodexAccountView | None = None
    rate_limits: dict[str, Any] | None = None
    quota_status: Literal['not_requested', 'available', 'unavailable'] = 'not_requested'
    quota_error_code: str | None = None
    quota_checked_at: str | None = None
    message: str = ''

class CodexLoginStartResponse(BaseModel):
    connection_id: str | None = None
    login_id: str
    verification_url: str
    user_code: str
    expires_at: str | None = None

class CodexLoginStatusResponse(BaseModel):
    connection_id: str | None = None
    login_id: str
    status: Literal['pending', 'succeeded', 'failed', 'cancelled', 'expired']
    error: str | None = None
    account: CodexAccountView | None = None

class BoardDecision(BaseModel):
    action: BoardAction
    reason: str

class BoardTeachingSelectedItem(BaseModel):
    excerpt: str
    source_heading: str | None = None
    reason: str
    mapped_needs: list[str] = Field(default_factory=list)
    teaching_role: str = 'main_idea'
    order_index: int = 0

class BoardNeedMapping(BaseModel):
    need: str
    matched_excerpt: str
    source_heading: str | None = None
    rationale: str

class BoardSectionTeachingPlan(BaseModel):
    order_index: int = 0
    heading: str
    heading_level: int = Field(default=0, ge=0, le=12)
    heading_path: list[str] = Field(default_factory=list)
    parent_heading: str = ''
    heading_order_index: int = Field(default=0, ge=0)
    line_start: int = Field(default=0, ge=0)
    line_end: int = Field(default=0, ge=0)
    has_child_headings: bool = False
    board_excerpt: str = ''
    core_points: list[str] = Field(default_factory=list)
    teaching_steps: list[str] = Field(default_factory=list)
    teaching_method: str = ''
    example_or_analogy: str = ''
    common_pitfalls: list[str] = Field(default_factory=list)
    check_question: str = ''
    transition_to_next: str = ''

class BoardTeachingGuide(BaseModel):
    board_document_id: str = ''
    board_snapshot_hash: str = ''
    board_title: str = ''
    selected_items: list[BoardTeachingSelectedItem] = Field(default_factory=list)
    need_mappings: list[BoardNeedMapping] = Field(default_factory=list)
    teaching_flow: list[str] = Field(default_factory=list)
    generation_rationale: str = ''
    chatbot_brief: str = ''
    lecture_handout: str = ''
    section_plans: list[BoardSectionTeachingPlan] = Field(default_factory=list)
    target_heading: str = ''
    target_heading_path: list[str] = Field(default_factory=list)
    sequence_mode: str = 'heading_tree_preorder'

class BoardTeachingProgress(BaseModel):
    board_document_id: str = ''
    board_snapshot_hash: str = ''
    current_section_index: int = 0
    completed_section_indexes: list[int] = Field(default_factory=list)
    waiting_for_continue: bool = False
    target_heading_path: list[str] = Field(default_factory=list)
    current_heading_path: list[str] = Field(default_factory=list)

class SectionTeachingProgressView(BaseModel):
    section_index: int = 0
    section_count: int = 0
    current_section_title: str = ''
    has_next_section: bool = False
    waiting_for_continue: bool = False
    target_heading_path: list[str] = Field(default_factory=list)
    current_heading_path: list[str] = Field(default_factory=list)

class HistoryBranchAction(BaseModel):
    kind: Literal['regenerate', 'fork']
    source_node_id: str = Field(min_length=1, max_length=200)

class ExternalSourceReference(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    connection_id: str = Field(min_length=1, max_length=128)
    resource_id: str = Field(min_length=1, max_length=1024)
    kind: str = Field(min_length=1, max_length=64, pattern='^[a-z][a-z0-9_]*$')
    expected_generation: int = Field(ge=1)
    capability: str | None = Field(default=None, min_length=1, max_length=64)
    provider_version: str | None = Field(default=None, max_length=512)
    repository_id: str | int | None = None
    commit_sha: str | None = Field(default=None, pattern='^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$')
    path: str | None = Field(default=None, min_length=1, max_length=4096)
    drive_id: str | None = Field(default=None, min_length=1, max_length=512)
    item_id: str | None = Field(default=None, min_length=1, max_length=512)
    library_type: Literal['user', 'group'] | None = None
    library_id: str | int | None = None
    item_key: str | None = Field(default=None, min_length=1, max_length=64)
    page_id: str | None = Field(default=None, min_length=1, max_length=128)
    block_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode='after')
    def validate_external_identity(self) -> 'ExternalSourceReference':
        for value in (self.repository_id, self.library_id):
            if value is not None and (isinstance(value, bool) or not str(value).isdigit() or (not 0 < int(value) < 10 ** 24)):
                raise ValueError('External repository and library identities must be positive numeric identifiers.')
        if self.kind == 'repository_file' and (not all((self.repository_id, self.commit_sha, self.path))):
            raise ValueError('Repository references require repository identity, commit, and path.')
        if self.path and (self.path.startswith('/') or '\\' in self.path or '\x00' in self.path or any((part in {'', '.', '..'} for part in self.path.split('/')))):
            raise ValueError('External repository paths must be relative and contained.')
        return self

class ChatRequest(BaseModel):
    message: str
    session_id: str | None = Field(default=None, min_length=1, max_length=160)
    turn_id: str | None = Field(default=None, min_length=1, max_length=200)
    input_event_id: str | None = Field(default=None, min_length=1, max_length=200)
    expected_ridoc_revision: str | None = Field(default=None, min_length=64, max_length=64)
    history_branch_action: HistoryBranchAction | None = None
    channel: ChatChannel = 'text'
    input_kind: ChatInputKind = 'typed'
    provider_reference: str | None = Field(default=None, min_length=1, max_length=512)
    text_model: AIModelSelection | None = None
    selection: SelectionRef | None = None
    selections: list[SelectionRef] = Field(default_factory=list, max_length=8)
    formula_ink: FormulaInkPayload | None = None
    attachments: list[ChatAttachmentRef] = Field(default_factory=list, max_length=10)
    source_query_scope: SourceQueryScope | None = None
    external_source_refs: list[ExternalSourceReference] = Field(default_factory=list, max_length=8)

    @field_validator('external_source_refs', mode='before')
    @classmethod
    def require_sources_import(cls, value):
        if value:
            raise ValueError('Add external resources to Sources and wait until they are ready before using them in a question.')
        return value
    interaction_mode: ChatInteractionMode = 'ask'
    board_generation_action: BoardGenerationAction | None = None
    teaching_action: TeachingAction | None = None
    board_task_confirmation: BoardTaskConfirmation | None = None
    post_generation_action: PostGenerationAction = 'stop_after_generation'
    chat_edit_source_commit_id: str | None = None
    chat_edit_base_commit_id: str | None = None
    chat_edit_original_message: str | None = None
    conversation: list[ConversationTurn] = Field(default_factory=list)

class ChatCancellationRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=160)
    input_event_id: str = Field(min_length=1, max_length=200)

class TurnEnvelope(BaseModel):
    """Frozen transport contract for one user input event."""
    lesson_id: str = Field(min_length=1)
    session_id: str | None = None
    turn_id: str | None = None
    input_event_id: str | None = None
    channel: ChatChannel = 'text'
    input_kind: ChatInputKind = 'typed'
    provider_reference: str | None = None
    message: str
    conversation: list[ConversationTurn] = Field(default_factory=list)
    selected_model: AIModelSelection
    references: list[SelectionRef] = Field(default_factory=list, max_length=8)
    explicit_action: TurnExplicitAction | None = None
    interaction_mode: ChatInteractionMode = 'ask'
    board_generation_action: BoardGenerationAction | None = None
    teaching_action: TeachingAction | None = None
    board_task_confirmation: BoardTaskConfirmation | None = None
    has_selection: bool = False
    selection_kind: SelectionKind | None = None
    has_multiple_selections: bool = False
    has_formula_ink: bool = False
    has_attachments: bool = False
    has_source_query_scope: bool = False

class TurnDecision(BaseModel):
    intent: TurnIntent
    learning_flow: LearningFlow = 'none'
    board_write_opt_out: bool = False
    explicit_teaching_request: bool = False
    continuation: TurnContinuationKind = 'none'
    relation_to_active: TurnRelationToActive = 'none'
    board_access: TurnDecisionBoardAccess = 'forbidden'
    reason: str

class DecisionTrace(BaseModel):
    intent_signals: list[str] = Field(default_factory=list)
    matched_rules: list[str] = Field(default_factory=list)
    selected_action: TurnIntent
    rejected_actions: list[TurnIntent] = Field(default_factory=list)
    target_resolver: str = 'none'
    sequence_mode: str = 'single_turn'
    role_executed: str
    board_access: TurnBoardAccess
    requirement_effect: TurnRequirementEffect
    document_changed: bool = False
    reason: str

class LessonView(BaseModel):
    id: str
    title: str
    slug: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    visibility: PublicationVisibility = 'private'
    publication_review: PublicationReview = Field(default_factory=PublicationReview)
    published_version: PublishedLessonVersion | None = None
    board_document: BoardDocument
    learning_requirements: LearningRequirementSheet | None = None
    board_task_requirements: BoardTaskRequirementSheet | None = None
    history_graph: LessonHistoryGraph
    created_at: str
    updated_at: str

class CoursePackageView(BaseModel):
    id: str
    title: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    visibility: PublicationVisibility = 'private'
    publication_review: PublicationReview = Field(default_factory=PublicationReview)
    published_version: PublishedCoursePackageVersion | None = None
    is_standalone: bool = False
    lessons: list[LessonView]
    course_graph: list[CourseGraphEdge] = Field(default_factory=list)
    resources: list[ResourceLibraryItemView] = Field(default_factory=list)
    open_lesson_ids: list[str] = Field(default_factory=list)
    active_lesson_id: str | None = None
    workspace_tab_order: list[str] = Field(default_factory=list)
    storage_kind: Literal['legacy', 'ridoc'] = 'legacy'
    ridoc_revision: str | None = None
    package_kind: Literal['course', 'standalone'] = 'course'

class RidocHistoryNodeView(BaseModel):
    id: str
    continuation_node_id: str
    kind: Literal['M', 'C', 'E']
    initial: bool = False
    parent_ids: list[str] = Field(default_factory=list)
    branch_name: str
    created_at: str
    title: str
    summary: str = ''
    user_message: str = ''
    assistant_message: str = ''
    before_document_ref: str | None = None
    after_document_ref: str
    is_head: bool = False
    branch_origin_deleted: bool = False
    source_message_deleted: bool = False
    merge_target_node_id: str = ''
    merge_source_node_id: str = ''
    merge_source_branch: str = ''
    node_refs: list[dict[str, Any]] = Field(default_factory=list)
    pending_deletion: bool = False

class RidocMessageDeletionPreview(BaseModel):
    lesson_id: str
    node_id: str
    user_message: str
    assistant_message: str
    ridoc_revision: str
    affected_branches: list[str]
    retained_document_node_ids: list[str]
    blocked_reason: str | None = None
    expected_version: int | None = None

class RidocMessageDeletionRequest(BaseModel):
    expected_ridoc_revision: str = Field(min_length=64, max_length=64)
    mutation_id: str = Field(min_length=1, max_length=200, pattern='\\S')
    expected_version: int | None = Field(default=None, ge=1)

class RidocMessageDeletionResponse(BaseModel):
    course_package: CoursePackageView
    ridoc_revision: str
    deleted_node_id: str
    affected_branches: list[str]
    retained_document_node_ids: list[str]
    mutation_id: str

class RidocHistoryOrderView(BaseModel):
    lesson_id: str
    branch_name: str
    ordered_node_ids: list[str]
    ridoc_revision: str
    blocked_reason: str | None = None
    expected_version: int | None = None

class RidocHistoryOrderRequest(BaseModel):
    branch_name: str = Field(min_length=1)
    ordered_node_ids: list[str]
    expected_ridoc_revision: str = Field(min_length=64, max_length=64)
    mutation_id: str = Field(min_length=1, max_length=200, pattern='\\S')
    expected_version: int | None = Field(default=None, ge=1)

class RidocHistoryOrderResponse(BaseModel):
    course_package: CoursePackageView
    ridoc_revision: str
    changed: bool
    mutation_id: str

class RidocDocumentHistoryState(BaseModel):
    current_document_node_id: str = ''
    undo_node_id: str | None = None
    redo_node_id: str | None = None
    withdrawn_node_ids: list[str] = Field(default_factory=list)
    blocked_reason: str | None = None
    actions: list[dict[str, Any]] = Field(default_factory=list)

class RidocDocumentHistoryStepRequest(BaseModel):
    direction: Literal['undo', 'redo']
    target_node_id: str = Field(min_length=1)
    expected_branch_name: str = Field(min_length=1)
    expected_ridoc_revision: str = Field(min_length=64, max_length=64)
    mutation_id: str = Field(min_length=1, max_length=200, pattern='\\S')
    expected_version: int | None = Field(default=None, ge=1)

class RidocDocumentHistoryStepResponse(BaseModel):
    course_package: CoursePackageView
    ridoc_revision: str
    mutation_id: str
    document_history: RidocDocumentHistoryState

class RidocHistoryBranchView(BaseModel):
    name: str
    head_node_id: str
    base_node_id: str
    archived: bool = False
    node_refs: list[dict[str, Any]] = Field(default_factory=list)

class RidocHistoryPageView(BaseModel):
    lesson_id: str
    ridoc_revision: str
    current_branch: str
    branches: list[RidocHistoryBranchView]
    nodes: list[RidocHistoryNodeView]
    next_cursor: str | None = None
    learning_node_ids: list[str] = Field(default_factory=list)
    replay_documents: dict[str, BoardDocument] = Field(default_factory=dict)
    learning_order_merged: bool = False
    document_history: RidocDocumentHistoryState = Field(default_factory=RidocDocumentHistoryState)
    expected_version: int | None = None

class RidocHistoryDocumentView(BaseModel):
    lesson_id: str
    node_id: str
    ridoc_revision: str
    document: BoardDocument

class PublicLessonView(BaseModel):
    id: str
    title: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    board_document: BoardDocument
    updated_at: str
    published_revision_id: str
    source_commit_id: str
    published_at: str

class PublicCoursePackageView(BaseModel):
    id: str
    title: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    lessons: list[PublicLessonView] = Field(default_factory=list)
    published_revision_id: str
    published_at: str

class PublicCourseSearchResult(BaseModel):
    id: str
    kind: Literal['lesson', 'package']
    owner_display_name: str
    owner_user_id: str | None = None
    owner_avatar_url: str | None = None
    title: str
    summary: str
    content_excerpt: str = ''
    tags: list[str] = Field(default_factory=list)
    lesson_count: int
    updated_at: str | None = None
    visibility: Literal['private', 'public'] = 'public'
    is_owned: bool = False
    is_starred: bool = False
    star_count: int = 0
    match_field: Literal['title', 'tag', 'summary', 'heading', 'body'] | None = None
    match_excerpt: str = ''
    match_path: list[str] = Field(default_factory=list)
    relevance_score: int = 0
    repository_id: str | None = None
    canonical_path: str | None = None
    fork_count: int = 0
    public_revision_id: str | None = None

class PublicCourseStarState(BaseModel):
    id: str
    kind: Literal['lesson', 'package']
    is_starred: bool

class CourseSearchResponse(BaseModel):
    owned_courses: list[PublicCourseSearchResult] = Field(default_factory=list)
    public_courses: list[PublicCourseSearchResult] = Field(default_factory=list)

class WorkspaceStateView(BaseModel):
    packages: list[CoursePackageView] = Field(default_factory=list)
    active_package_id: str | None = None

class PersonalRepositoryLessonItem(BaseModel):
    kind: Literal['lesson'] = 'lesson'
    lesson: LessonView
    is_synced: bool
    published_at: str
    source_commit_id: str

class PersonalRepositoryPackageItem(BaseModel):
    kind: Literal['package'] = 'package'
    package: CoursePackageView
    is_synced: bool
    published_at: str

class PersonalRepositoriesView(BaseModel):
    items: list[PersonalRepositoryLessonItem | PersonalRepositoryPackageItem] = Field(default_factory=list)

class LessonWorkspaceDelta(BaseModel):
    operation: Literal['create', 'close', 'delete']
    workspace_revision: int
    package_id: str
    active_package_id: str | None = None
    active_lesson_id: str | None = None
    open_lesson_ids: list[str] = Field(default_factory=list)
    workspace_tab_order: list[str] = Field(default_factory=list)
    created_lesson: LessonView | None = None
    deleted_lesson_id: str | None = None
    graph_edge: CourseGraphEdge | None = None
    storage_kind: Literal['legacy', 'ridoc'] = 'legacy'
    ridoc_revision: str | None = None

class DocumentSaveDelta(BaseModel):
    lesson_id: str
    package_id: str
    workspace_revision: int
    document: BoardDocument
    latest_commit: CommitRecord
    current_branch: str
    branch_head_commit_id: str
    branch_base_commit_id: str = ''
    updated_at: str
    changed: bool
    storage_kind: Literal['legacy', 'ridoc'] = 'legacy'
    ridoc_revision: str | None = None

class AuthIdentityView(BaseModel):
    id: str | None = None
    enabled: bool = True
    provider: str
    provider_label: str
    email: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    created_at: str
    last_login_at: str | None = None

class UserView(BaseModel):
    id: str
    email: str
    phone: str | None = None
    role: Literal['user', 'admin', 'guest']
    display_name: str | None = None
    handle: str | None = None
    avatar_url: str | None = None
    created_at: str
    last_login_at: str | None = None
    email_verified_at: str | None = None
    auth_identities: list[AuthIdentityView] = Field(default_factory=list)

class RepositoryProjectRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id('repository'))
    owner_user_id: str
    project_kind: Literal['lesson', 'package']
    title: str
    slug: str
    tags: list[str] = Field(default_factory=list)
    summary: str = ''
    visibility: PublicationVisibility = 'private'
    working_package: CoursePackage
    public_package: CoursePackage | None = None
    version: int = 1
    public_revision_id: str | None = None
    source_repository_id: str | None = None
    source_revision_id: str | None = None
    source_workspace_kind: Literal['lesson', 'package'] | None = None
    source_workspace_id: str | None = None
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)

class RepositoryProjectView(BaseModel):
    id: str
    owner_user_id: str
    owner_display_name: str
    owner_handle: str
    project_kind: Literal['lesson', 'package']
    title: str
    slug: str
    tags: list[str] = Field(default_factory=list)
    summary: str = ''
    visibility: PublicationVisibility
    canonical_path: str
    version: int
    public_revision_id: str | None = None
    source_repository_id: str | None = None
    source_revision_id: str | None = None
    star_count: int = 0
    fork_count: int = 0
    viewer_has_starred: bool = False
    upstream_has_updates: bool = False
    created_at: str
    updated_at: str

class RepositoryProjectDetail(RepositoryProjectView):
    course_package: CoursePackageView
    access_mode: Literal['owner', 'viewer'] = 'viewer'
    read_only: bool = True

class ProjectForkRequest(BaseModel):
    destination: Literal['repository', 'workspace']
    revision: str | None = None

class ProjectForkResult(BaseModel):
    fork_id: str
    destination: Literal['repository', 'workspace']
    repository: RepositoryProjectView | None = None
    workspace: WorkspaceStateView | None = None
    active_lesson_id: str | None = None

class ProjectUpstreamView(BaseModel):
    source_repository_id: str | None = None
    source_revision_id: str | None = None
    current_source_revision_id: str | None = None
    has_updates: bool = False

class ProjectCompareView(BaseModel):
    source_revision_id: str
    current_source_revision_id: str
    changed_lessons: list[str] = Field(default_factory=list)
    added_lessons: list[str] = Field(default_factory=list)
    removed_lessons: list[str] = Field(default_factory=list)
CommunityPostType = Literal['question', 'discussion', 'resource', 'study_note']
CommunityFeedSort = Literal['recent', 'hot', 'unanswered']
CommunityProvider = Literal['answer']

class CommunityIntegrationView(BaseModel):
    provider: CommunityProvider
    public_url: str | None = None
    entry_url: str
    available: bool
    sso_enabled: bool
    setup_required: bool

class CommunitySpaceCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    slug: str = Field(default='', max_length=80)
    description: str = Field(default='', max_length=500)

    @field_validator('name', 'slug', 'description')
    @classmethod
    def strip_space_fields(cls, value: str) -> str:
        return value.strip()

class CommunitySpaceView(BaseModel):
    id: str
    slug: str
    name: str
    description: str
    creator_user_id: str
    created_at: str
    updated_at: str
    post_count: int = 0
    follower_count: int = 0

class CommunityPostCreate(BaseModel):
    community_slug: str = Field(min_length=1, max_length=80)
    post_type: CommunityPostType = 'discussion'
    title: str = Field(min_length=4, max_length=180)
    body: str = Field(min_length=1, max_length=40000)
    tags: list[str] = Field(default_factory=list, max_length=8)

    @field_validator('community_slug', 'title', 'body')
    @classmethod
    def strip_post_fields(cls, value: str) -> str:
        return value.strip()

    @field_validator('tags')
    @classmethod
    def clean_tags(cls, tags: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw_tag in tags:
            tag = raw_tag.strip().lstrip('#').strip()
            if not tag:
                continue
            if len(tag) > 40:
                raise ValueError('Tags cannot exceed 40 characters.')
            key = tag.casefold()
            if key not in seen:
                result.append(tag)
                seen.add(key)
        return result

class CommunityPostUpdate(BaseModel):
    title: str = Field(min_length=4, max_length=180)
    body: str = Field(min_length=1, max_length=40000)
    tags: list[str] = Field(default_factory=list, max_length=8)
    _strip_fields = field_validator('title', 'body')(CommunityPostCreate.strip_post_fields.__func__)
    _clean_tags = field_validator('tags')(CommunityPostCreate.clean_tags.__func__)

class CommunityPostView(BaseModel):
    id: str
    community_id: str
    community_slug: str
    community_name: str
    author_user_id: str
    author_display_name: str
    post_type: CommunityPostType
    title: str
    body: str
    tags: list[str] = Field(default_factory=list)
    vote_score: int = 0
    comment_count: int = 0
    answer_count: int = 0
    accepted_answer_id: str | None = None
    viewer_vote: int = 0
    created_at: str
    updated_at: str

class CommunityCommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=10000)
    parent_comment_id: str | None = None

    @field_validator('body')
    @classmethod
    def strip_comment_body(cls, value: str) -> str:
        return value.strip()

class CommunityContentUpdate(BaseModel):
    body: str = Field(min_length=1, max_length=40000)

    @field_validator('body')
    @classmethod
    def strip_body(cls, value: str) -> str:
        return value.strip()

class CommunityCommentView(BaseModel):
    id: str
    post_id: str
    parent_comment_id: str | None = None
    author_user_id: str
    author_display_name: str
    body: str
    created_at: str
    updated_at: str

class CommunityAnswerCreate(BaseModel):
    body: str = Field(min_length=1, max_length=40000)

    @field_validator('body')
    @classmethod
    def strip_answer_body(cls, value: str) -> str:
        return value.strip()

class CommunityAnswerView(BaseModel):
    id: str
    post_id: str
    author_user_id: str
    author_display_name: str
    body: str
    vote_score: int = 0
    viewer_vote: int = 0
    is_accepted: bool = False
    author_reputation: int = 0
    created_at: str
    updated_at: str

class CommunityPostDetail(BaseModel):
    post: CommunityPostView
    answers: list[CommunityAnswerView] = Field(default_factory=list)
    comments: list[CommunityCommentView] = Field(default_factory=list)

class CommunityVoteRequest(BaseModel):
    value: Literal[-1, 0, 1]

class CommunityVoteView(BaseModel):
    post_id: str
    viewer_vote: int
    vote_score: int

class CommunityAnswerVoteView(BaseModel):
    answer_id: str
    viewer_vote: int
    vote_score: int

class CommunityAcceptedAnswerRequest(BaseModel):
    answer_id: str | None = None

class CommunityAcceptedAnswerView(BaseModel):
    post_id: str
    accepted_answer_id: str | None = None

class CommunityFollowView(BaseModel):
    community_id: str
    following: bool
    follower_count: int

class AuthRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    identifier: str | None = None
    email: str | None = None
    guest_token: str | None = None
    turnstile_token: str | None = Field(default=None, max_length=2048)
    password: str = Field(min_length=8, max_length=256)

    def account_identifier(self) -> str:
        return self.identifier or self.email or ''

class AuthSessionResponse(BaseModel):
    token: str | None = None
    user: UserView

class AuthMessageResponse(BaseModel):
    message: str

class HumanVerificationRequest(BaseModel):
    turnstile_token: str | None = Field(default=None, max_length=2048)

class PasswordResetRequest(BaseModel):
    challenge_id: str
    code: str = Field(min_length=6, max_length=6, pattern='^\\d{6}$')
    password: str = Field(min_length=8, max_length=256)
    password_confirmation: str = Field(min_length=8, max_length=256)
    turnstile_token: str | None = Field(default=None, max_length=2048)

    @model_validator(mode='after')
    def passwords_must_match(self) -> PasswordResetRequest:
        if self.password != self.password_confirmation:
            raise ValueError('The passwords do not match.')
        return self

class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=8, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)
    new_password_confirmation: str | None = Field(default=None, min_length=8, max_length=256)

    @model_validator(mode='after')
    def passwords_must_match(self) -> PasswordChangeRequest:
        if self.new_password_confirmation is not None and self.new_password != self.new_password_confirmation:
            raise ValueError('The new passwords do not match.')
        return self

class AccountDeleteRequest(BaseModel):
    password: str | None = Field(default=None, max_length=256)
    confirmation: Literal['DELETE']

class AccountDataExport(BaseModel):
    exported_at: str
    user: dict[str, Any]
    data: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)

class EmailCodeRequest(BaseModel):
    email: str
    turnstile_token: str | None = Field(default=None, max_length=2048)

class EmailCodeRequestResponse(BaseModel):
    challenge_id: str
    expires_in_seconds: int
    message: str

class EmailCodeVerifyRequest(BaseModel):
    challenge_id: str
    code: str = Field(min_length=6, max_length=6, pattern='^\\d{6}$')
    guest_token: str | None = None
    turnstile_token: str | None = Field(default=None, max_length=2048)

class EmailRegistrationRequest(BaseModel):
    email: str
    username: str = Field(min_length=2, max_length=50)
    password: str = Field(min_length=8, max_length=256)
    password_confirmation: str = Field(min_length=8, max_length=256)
    challenge_id: str
    code: str = Field(min_length=6, max_length=6, pattern='^\\d{6}$')
    guest_token: str | None = None
    turnstile_token: str | None = Field(default=None, max_length=2048)

    @field_validator('username')
    @classmethod
    def normalize_username(cls, value: str) -> str:
        username = value.strip()
        if len(username) < 2:
            raise ValueError('The username must contain at least 2 characters.')
        return username

    @model_validator(mode='after')
    def passwords_must_match(self) -> EmailRegistrationRequest:
        if self.password != self.password_confirmation:
            raise ValueError('The passwords do not match.')
        return self

class AuthProviderView(BaseModel):
    id: str
    label: str
    description: str
    configured: bool
    kind: Literal['password', 'oauth', 'device'] = 'oauth'

class AdminStats(BaseModel):
    users: int
    admins: int
    packages: int
    lessons: int
    resources: int

class AdminOverview(BaseModel):
    stats: AdminStats
    users: list[UserView] = Field(default_factory=list)

class ChatResponse(BaseModel):
    chatbot_message: str
    turn_decision: TurnDecision | None = None
    decision_trace: DecisionTrace | None = None
    source_citations: list[SourceCitation] = Field(default_factory=list)
    follow_up_suggestions: list[str] = Field(default_factory=list, max_length=4)
    agent_activity: list[AgentActivityEvent] = Field(default_factory=list)
    learning_requirement_sheet: LearningRequirementSheet
    active_requirement_sheet: LearningRequirementSheet | None = None
    learning_clarification: LearningClarificationStatus
    requirement_run_id: str | None = None
    requirement_version_id: str | None = None
    requirement_phase: LearningRequirementRunStatus | None = None
    learning_requirement_operation_status: LearningRequirementOperationStatus = 'none'
    learning_requirement_operation_failure_reason: str | None = None
    board_task_sheet: BoardTaskRequirementSheet | None = None
    active_board_task_sheet: BoardTaskRequirementSheet | None = None
    board_task_run_id: str | None = None
    board_task_version_id: str | None = None
    board_task_phase: BoardTaskRunStatus | None = None
    board_task_questions: list[str] = Field(default_factory=list)
    board_decision: BoardDecision
    needs_clarification: bool = False
    clarification_questions: list[str] = Field(default_factory=list)
    guided_requirement_discovery: GuidedRequirementDiscovery | None = None
    requirement_cleared: bool = False
    board_document_operation_status: BoardDocumentOperationStatus = 'none'
    board_document_operation_failure_reason: str | None = None
    teaching_progress: SectionTeachingProgressView | None = None
    auto_teaching_operation_status: AutoTeachingOperationStatus = 'none'
    auto_teaching_operation_failure_reason: str | None = None
    course_package: CoursePackageView
    ridoc_revision: str | None = None

class RequirementUpdateStreamPayload(BaseModel):
    learning_requirement_sheet: LearningRequirementSheet
    active_requirement_sheet: LearningRequirementSheet | None = None
    learning_clarification: LearningClarificationStatus
    requirement_run_id: str | None = None
    requirement_version_id: str | None = None
    requirement_phase: LearningRequirementRunStatus | None = None
    clarification_questions: list[str] = Field(default_factory=list)
    guided_requirement_discovery: GuidedRequirementDiscovery | None = None

class BoardTaskUpdateStreamPayload(BaseModel):
    board_task_sheet: BoardTaskRequirementSheet
    active_board_task_sheet: BoardTaskRequirementSheet | None = None
    board_task_run_id: str | None = None
    board_task_version_id: str | None = None
    board_task_phase: BoardTaskRunStatus | None = None
    board_task_questions: list[str] = Field(default_factory=list)

class CreatePackageRequest(BaseModel):
    title: str
    summary: str = ''

class UpdatePackageRequest(BaseModel):
    title: str | None = None
    summary: str | None = None
    visibility: PublicationVisibility | None = None
    expected_ridoc_revision: str | None = Field(default=None, min_length=64, max_length=64)

class UpdateLessonRequest(BaseModel):
    title: str
    expected_ridoc_revision: str | None = Field(default=None, min_length=64, max_length=64)

class RepositoryTagsUpdateRequest(BaseModel):
    tags: list[str] = Field(default_factory=list)

class RepositoryTagSuggestionsRequest(BaseModel):
    text_model: AIModelSelection

class RepositoryTagSuggestionsResponse(BaseModel):
    tags: list[str] = Field(default_factory=list)
    selected_model: AIModelSelection
    source_revision_id: str

class UpdateVisibilityRequest(BaseModel):
    visibility: PublicationVisibility
    expected_ridoc_revision: str | None = Field(default=None, min_length=64, max_length=64)

class MoveLessonRequest(BaseModel):
    target_package_id: str

class BatchLessonActionRequest(BaseModel):
    action: Literal['move', 'delete']
    lesson_ids: list[str] = Field(default_factory=list)
    target_package_id: str | None = None
    expected_ridoc_revisions: dict[str, str] = Field(default_factory=dict)

class GenerateLessonRequest(BaseModel):
    topic: str | None = None
    timezone: str | None = None
    branch_from_lesson_id: str | None = None
    target_package_id: str | None = None
    start_blank: bool = False
    expected_ridoc_revision: str | None = Field(default=None, min_length=64, max_length=64)

class ManualCommitRequest(BaseModel):
    document: BoardDocument | None = None
    label: str = 'Manual document edit'
    message: str = 'Saved rich document changes from the editor'

class DocumentSaveRequest(BaseModel):
    document: BoardDocument
    label: str = 'Manual document edit'
    message: str = 'Saved rich document changes from the editor'
    metadata: dict[str, Any] = Field(default_factory=dict)
    base_commit_id: str | None = None
    expected_ridoc_revision: str | None = Field(default=None, min_length=64, max_length=64)
    transaction_id: str | None = Field(default=None, min_length=1, max_length=200)
    history_branch_action: HistoryBranchAction | None = None

class DocumentAIEditRequest(BaseModel):
    instruction: str
    selection_text: str | None = None
    replace_whole: bool = False
    conversation: list[ConversationTurn] = Field(default_factory=list)

class CreateBranchRequest(BaseModel):
    name: str
    from_commit_id: str | None = None
    expected_ridoc_revision: str | None = Field(default=None, min_length=64, max_length=64)

class SwitchBranchRequest(BaseModel):
    name: str
    expected_ridoc_revision: str | None = Field(default=None, min_length=64, max_length=64)

class RenameBranchRequest(BaseModel):
    old_name: str
    new_name: str
    expected_ridoc_revision: str = Field(min_length=64, max_length=64)

class DeleteBranchRequest(BaseModel):
    name: str
    expected_ridoc_revision: str = Field(min_length=64, max_length=64)

class RestoreCommitRequest(BaseModel):
    commit_id: str
    label: str = 'Restore snapshot'

class DocumentHistoryStepRequest(BaseModel):
    direction: Literal['undo', 'redo']
    target_commit_id: str
    expected_branch_name: str
    expected_head_commit_id: str
    expected_document_cursor_commit_id: str

class EnsureDocumentHistoryBranchRequest(BaseModel):
    expected_branch_name: str
    expected_head_commit_id: str
    expected_document_cursor_commit_id: str

class ChatCommitVisibilityRequest(BaseModel):
    visibility: Literal['hidden', 'visible']

class ReorderTabsRequest(BaseModel):
    ordered_lesson_ids: list[str]
    active_lesson_id: str | None = None
    expected_ridoc_revision: str | None = Field(default=None, min_length=64, max_length=64)

class RealtimeConnectRequest(BaseModel):
    offer_sdp: str
    latest_assistant_message: str | None = None
    client_session_id: str | None = None
    realtime_model: AIModelSelection | None = None
    selection: SelectionRef | None = None
    voice: str | None = Field(default=None, min_length=1, max_length=64)
    interaction_mode: Literal['conversation', 'announcement'] = 'conversation'

class RealtimeConnectResponse(BaseModel):
    answer_sdp: str
    provider: str = 'openai'
    model: str
    voice: str
    call_id: str | None = None
    tools_enabled: bool = False
    client_delegation_enabled: bool = False
    delegation_websocket_url: str | None = None
    client_session_id: str | None = None

class GoogleRealtimeSessionRequest(BaseModel):
    latest_assistant_message: str | None = None
    client_session_id: str | None = None
    realtime_model: AIModelSelection | None = None

class GoogleRealtimeSessionResponse(BaseModel):
    websocket_url: str
    setup: dict[str, object]
    provider: str = 'google'
    model: str
    voice: str

class RealtimeTranscriptLogRequest(BaseModel):
    client_event_id: str | None = Field(default=None, min_length=1, max_length=200)
    client_session_id: str | None = None
    turn_id: str | None = Field(default=None, max_length=200)
    occurred_at: datetime | None = None
    lesson_title: str | None = None
    role: RealtimeTranscriptRole
    transport_event_type: str
    transcript: str
    tool_name: str | None = None
    tool_call_id: str | None = None
    tool_status: str | None = None
RealtimeToolName = Literal['read_board_context', 'run_chatbot_workflow']

class RealtimeToolCallRequest(BaseModel):
    client_session_id: str = Field(min_length=1, max_length=160)
    turn_id: str | None = Field(default=None, max_length=200)
    input_event_id: str | None = Field(default=None, min_length=1, max_length=200)
    input_kind: ChatInputKind
    provider_reference: str | None = Field(default=None, min_length=1, max_length=512)
    call_id: str = Field(min_length=1, max_length=160)
    name: RealtimeToolName
    arguments: dict[str, Any] = Field(default_factory=dict)
    selection: SelectionRef | None = None

class RealtimeToolCallResponse(BaseModel):
    status: Literal['ok', 'error']
    model_output: dict[str, Any] = Field(default_factory=dict)
    resolved_focus: BoardFocusRef | None = None
    course_package: CoursePackageView | None = None
