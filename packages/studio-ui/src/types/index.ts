import type { DocumentHistoryState } from "./document-history";

export type BoardAction = "no_change" | "edit_board";
export type CommunityPostType = "question" | "discussion" | "resource" | "study_note";
export type CommunityFeedSort = "recent" | "hot" | "unanswered";
export type CommunitySpaceSort = "active" | "new" | "popular";

export interface CommunityIntegration {
  provider: "answer";
  public_url?: string | null;
  entry_url: string;
  available: boolean;
  sso_enabled: boolean;
  setup_required: boolean;
}

export interface CommunitySpace {
  id: string;
  slug: string;
  name: string;
  description: string;
  creator_user_id: string;
  created_at: string;
  updated_at: string;
  post_count: number;
  follower_count: number;
}

export interface CommunityPost {
  id: string;
  community_id: string;
  community_slug: string;
  community_name: string;
  author_user_id: string;
  author_display_name: string;
  post_type: CommunityPostType;
  title: string;
  body: string;
  tags: string[];
  vote_score: number;
  comment_count: number;
  answer_count: number;
  accepted_answer_id?: string | null;
  viewer_vote: number;
  created_at: string;
  updated_at: string;
}

export interface CommunityComment {
  id: string;
  post_id: string;
  parent_comment_id?: string | null;
  author_user_id: string;
  author_display_name: string;
  body: string;
  created_at: string;
  updated_at: string;
}

export interface CommunityAnswer {
  id: string;
  post_id: string;
  author_user_id: string;
  author_display_name: string;
  body: string;
  vote_score: number;
  viewer_vote: number;
  is_accepted: boolean;
  author_reputation: number;
  created_at: string;
  updated_at: string;
}

export interface CommunityPostDetail {
  post: CommunityPost;
  answers: CommunityAnswer[];
  comments: CommunityComment[];
}

export interface CreateCommunitySpacePayload {
  name: string;
  slug?: string;
  description?: string;
}

export interface CreateCommunityPostPayload {
  community_slug: string;
  post_type: CommunityPostType;
  title: string;
  body: string;
  tags: string[];
}

export interface UpdateCommunityPostPayload {
  title: string;
  body: string;
  tags: string[];
}

export interface CommunityVoteResult {
  post_id: string;
  viewer_vote: number;
  vote_score: number;
}

export interface CommunityAnswerVoteResult {
  answer_id: string;
  viewer_vote: number;
  vote_score: number;
}

export interface CommunityAcceptedAnswerResult {
  post_id: string;
  accepted_answer_id?: string | null;
}

export interface CommunityFollowResult {
  community_id: string;
  following: boolean;
  follower_count: number;
}

export type ChatInteractionMode = "ask" | "direct_edit";
export type AIProvider = string;
export type AIModelAccessMethod =
  | "chatgpt_subscription"
  | "personal_api"
  | "platform_credits";
export type AIModelCapability = "text" | "realtime";
export type AIRealtimeTransport = "openai_webrtc" | "gemini_live_websocket";
export type DocumentMarginPreset = "narrow" | "normal" | "wide";
export type DocumentOrientation = "portrait" | "landscape";
export type DocumentPageSize = "a4" | "letter" | "a3";
export type DocumentBackgroundStyle = "plain" | "warm" | "grid";

export interface DocumentPageSettings {
  margin_preset: DocumentMarginPreset;
  orientation: DocumentOrientation;
  page_size: DocumentPageSize;
  columns: 1 | 2;
  page_border: boolean;
  background_style: DocumentBackgroundStyle;
  watermark_text: string;
  line_numbers: boolean;
  show_page_number: boolean;
  header_text: string;
  footer_text: string;
}

export interface BoardDocument {
  id: string;
  title: string;
  content_json: Record<string, unknown>;
  content_html: string;
  content_text: string;
  page_settings: DocumentPageSettings;
}

export type LearningSourceConfirmationStatus = "none" | "confirmed" | "skipped" | "stale";

export interface LearningSourceReference {
  evidence_bundle_id: string;
  source_ingestion_id: string;
  source_title: string;
  source_chapter_id: string;
  chapter_number: string;
  chapter_title: string;
  scope_kind: "chapter" | "page_range";
  scope_chapter_id: string;
  scope_chapter_number: string;
  scope_chapter_title: string;
  section_path: string[];
  source_locator: string;
  page_range: string;
  page_start?: number | null;
  page_end?: number | null;
  body_start_offset?: number | null;
  body_end_offset?: number | null;
  chunk_ids: string[];
  visual_ids: string[];
  source_structure_id: string;
  source_structure_updated_at: string;
  content_hash: string;
}

export interface LearningSourceGrounding {
  requested_by_user: boolean;
  confirmation_status: LearningSourceConfirmationStatus;
  confirmed_bundle_id: string;
  confirmed_at?: string | null;
  confirmed_references: LearningSourceReference[];
}

export interface LearningRequirementSheet {
  theme: string;
  learning_goal: string;
  level: string;
  known_background: string;
  current_questions: string[];
  learning_need_checklist: string[];
  target_depth: string;
  output_preference: string;
  boundary: string;
  board_scope: string[];
  success_criteria: string;
  risk_notes: string[];
  board_workflow?: BoardWorkflow | null;
  work_mode?: InitialLearningWorkMode | null;
  granularity?: InitialLearningGranularity | null;
  source_grounding?: LearningSourceGrounding;
}

export type BoardWorkflow = "generate_from_scratch" | "act_on_existing_board" | "unknown";
export type InitialLearningWorkMode = "knowledge_board" | "narrow_topic" | "practice_artifact" | "unknown";
export type InitialLearningGranularity = "single_knowledge_point" | "source_chapter" | "source_range" | "broad_topic" | "practice_artifact" | "unclear";
export type BoardTaskRunStatus = "collecting" | "ready" | "awaiting_confirmation" | "consumed" | "not_executed" | "archived";
export type BoardDocumentOperationStatus = "none" | "succeeded" | "failed";
export type BoardTaskRequestedAction = "write" | "edit" | "explain" | "delete" | "interact" | "chat";
export type BoardTaskConfirmationStatus = "none" | "awaiting" | "confirmed" | "declined";
export type BoardTaskLocationStatus = "missing" | "selected" | "resolved" | "ambiguous" | "content_absent";
export type BoardTaskLocationKind = "target_range" | "insertion_anchor" | "unspecified";
export type BoardContentExtent = "sentence" | "paragraph" | "section" | "article" | "whole_board";
export type BoardTopicRelation = "current_document" | "independent" | "unresolved";
export type BoardDocumentDestination = "current_lesson" | "new_lesson" | "unresolved";

export interface BoardTaskRequirementSheet {
  teach_after_mutation?: boolean;
  task_id: string;
  board_workflow?: BoardWorkflow | null;
  location_kind?: BoardTaskLocationKind;
  target_hint: string;
  target_location?: BoardFocusRef | null;
  location_status: BoardTaskLocationStatus;
  requested_action?: BoardTaskRequestedAction | null;
  question_or_topic: string;
  special_interaction_requirements?: string | null;
  content_extent?: BoardContentExtent | null;
  topic_relation: BoardTopicRelation;
  document_destination: BoardDocumentDestination;
  target_candidates: BoardFocusRef[];
  target_resolution_reason: string;
  base_commit_id: string;
  base_document_hash: string;
  mutation_plan?: Record<string, unknown> | null;
  interaction_session?: Record<string, unknown> | null;
  missing_items: string[];
  progress: number;
  confirmation_status: BoardTaskConfirmationStatus;
  clarification_question: string;
  failure_count: number;
}

export interface LearningRequirementChecklistItem {
  title: string;
  is_clear: boolean;
  evidence: string;
}

export interface LearningRequirementKeyFact {
  label: string;
  value: string;
  evidence: string;
  category?: "learning" | "level" | "vocabulary" | "scenario" | "output" | "other" | null;
}

export interface LearningClarificationStatus {
  progress: number;
  label: string;
  reason: string;
  missing_items: string[];
  can_start: boolean;
  forced_start: boolean;
  summary: string;
  key_facts: LearningRequirementKeyFact[];
  checklist: LearningRequirementChecklistItem[];
  next_question: string;
  ready_for_board: boolean;
  work_mode?: InitialLearningWorkMode | null;
  granularity?: InitialLearningGranularity | null;
}

export type LearningRequirementRunStatus = "collecting" | "ready" | "frozen" | "consumed" | "archived";

export interface TeachingGuideMapping {
  block_id: string;
  supports_goal: string;
  teaching_mode: "definition" | "intuition" | "analogy" | "example" | "dialogue";
  focus_points: string[];
  optional_points: string[];
  difficult_points: string[];
  check_questions: string[];
}

export interface TeachingGuide {
  lesson_id: string;
  summary: string;
  structure_note: string;
  pacing: string;
  mappings: TeachingGuideMapping[];
  strategy: string;
}

export interface CommitRecord {
  id: string;
  label: string;
  message: string;
  branch_name: string;
  created_at: string;
  parent_ids: string[];
  operations: Array<Record<string, unknown>>;
  snapshot: BoardDocument;
  metadata?: Record<string, unknown> & {
    history_node_kind?: "chat" | "document" | "restore" | "merge" | "system";
    history_node_title?: string;
    history_node_summary?: string;
  };
}

export type LessonMergeMode = "manual" | "ai";
export type LessonMergeStatus =
  | "draft"
  | "ai_running"
  | "ready"
  | "stale"
  | "committed"
  | "abandoned"
  | "failed";
export type LessonMergeResolution =
  | "unresolved"
  | "target"
  | "source"
  | "both"
  | "custom"
  | "clear"
  | "ai";

export interface LessonMergeConflictView {
  id: string;
  kind: "board" | "learning_requirement" | "board_task" | "source_reference";
  path: string;
  title: string;
  base_value: unknown;
  target_value: unknown;
  source_value: unknown;
  resolution: LessonMergeResolution;
  custom_value: unknown;
  resolved: boolean;
}

export interface MergeRuntimeDraftView {
  learning_requirements?: LearningRequirementSheet | null;
  board_task_requirements?: BoardTaskRequirementSheet | null;
  board_teaching_restart_heading_path: string[];
  invalidated_teaching_state: boolean;
}

export interface LessonMergeEventNode {
  id: string;
  kind: "M" | "C";
  title: string;
  summary: string;
  branch_name: string;
  initial?: boolean;
}

export interface LessonMergeEventPlan {
  ridoc_revision: string;
  base_node_id: string;
  target_node_ids: string[];
  source_node_ids: string[];
  default_node_ids: string[];
  ordered_node_ids: string[];
  nodes: LessonMergeEventNode[];
  blockers: Array<{ node_id: string; reason: string; object_id?: string | null }>;
}

export interface LessonMergeSessionView {
  id: string;
  lesson_id: string;
  target_branch_name: string;
  source_branch_name: string;
  base_commit_id: string;
  target_head_commit_id: string;
  source_head_commit_id: string;
  mode: LessonMergeMode;
  status: LessonMergeStatus;
  version: number;
  conflicts: LessonMergeConflictView[];
  draft_document: BoardDocument;
  draft_runtime: MergeRuntimeDraftView;
  event_plan?: LessonMergeEventPlan | null;
  ai_model?: AIModelSelection | null;
  agent_activity: AgentActivityEvent[];
  audit: Record<string, unknown>;
  supersedes_session_id?: string | null;
  committed_commit_id?: string | null;
  created_at: string;
  updated_at: string;
}

export type LessonContributionStatus = "open" | "merge_draft" | "merged" | "closed";
export type LessonContributionEventKind =
  | "opened"
  | "revision_submitted"
  | "commented"
  | "comment_edited"
  | "comment_deleted"
  | "merge_started"
  | "returned_for_changes"
  | "merged"
  | "closed"
  | "reopened";

export interface LessonContributionActor {
  user_id: string;
  display_name: string;
  avatar_url?: string | null;
}

export interface LessonContributionRevisionView {
  id: string;
  revision_number: number;
  source_commit_id: string;
  base_document: BoardDocument;
  proposed_document: BoardDocument;
  created_at: string;
}

export interface LessonContributionEvent {
  id: string;
  contribution_id: string;
  kind: LessonContributionEventKind;
  actor: LessonContributionActor;
  body: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface LessonContributionViewerPermissions {
  can_comment: boolean;
  can_update: boolean;
  can_close: boolean;
  can_reopen: boolean;
  can_start_merge: boolean;
  can_return_for_changes: boolean;
}

export interface LessonContributionView {
  id: string;
  source_lesson_id: string;
  viewer_project_lesson_id?: string | null;
  source_title: string;
  title: string;
  description: string;
  status: LessonContributionStatus;
  version: number;
  current_revision: number;
  source_author: LessonContributionActor;
  contributor: LessonContributionActor;
  revision: LessonContributionRevisionView;
  events: LessonContributionEvent[];
  viewer_permissions: LessonContributionViewerPermissions;
  source_is_public: boolean;
  merge_session_id?: string | null;
  merged_commit_id?: string | null;
  created_at: string;
  updated_at: string;
  closed_at?: string | null;
}

export interface BranchRef {
  node_refs?: RidocNodeReference[];
  name: string;
  head_commit_id: string;
  base_commit_id: string;
  document_cursor_commit_id: string;
  document_redo_commit_ids: string[];
  created_at: string;
  learning_node_ids?: string[];
  replay_documents?: Record<string, BoardDocument>;
  learning_order_merged?: boolean;
}

export interface LessonHistoryGraph {
  branches: Record<string, BranchRef>;
  commits: CommitRecord[];
  current_branch: string;
}

export type PublicationReviewStatus = "not_started" | "scanning" | "approved" | "blocked" | "error";

export interface PublicationReviewFinding {
  source_id: string;
  source_title: string;
  location: string;
  evidence_excerpt: string;
  reason: string;
}

export interface PublicationReview {
  id: string;
  status: PublicationReviewStatus;
  source_fingerprint: string;
  scanned_source_count: number;
  scanned_unit_count: number;
  findings: PublicationReviewFinding[];
  message: string;
  started_at?: string | null;
  completed_at?: string | null;
}

export interface PublishedLessonVersion {
  revision_id: string;
  lesson_id: string;
  source_commit_id: string;
  title: string;
  slug: string;
  summary: string;
  tags: string[];
  board_document: BoardDocument;
  conversation: Array<{
    role: "user" | "assistant";
    content: string;
  }>;
  published_at: string;
}

export interface Lesson {
  id: string;
  title: string;
  slug: string;
  summary: string;
  tags: string[];
  visibility: "private" | "public";
  publication_review: PublicationReview;
  published_version?: PublishedLessonVersion | null;
  board_document: BoardDocument;
  learning_requirements?: LearningRequirementSheet | null;
  board_task_requirements?: BoardTaskRequirementSheet | null;
  history_graph: LessonHistoryGraph;
  created_at: string;
  updated_at: string;
}

export interface CourseGraphEdge {
  id: string;
  source_lesson_id: string;
  target_lesson_id: string;
  relationship:
    | "recommended_next"
    | "prerequisite"
    | "deep_dive"
    | "alternate_path"
    | "derived_from";
}

export interface LibraryChapter {
  id: string;
  title: string;
  level: number;
  page_range?: string | null;
  page_start?: number | null;
  page_end?: number | null;
  summary: string;
  keywords: string[];
  prerequisites: string[];
  parent_id?: string | null;
  parent_title?: string | null;
  path: string[];
  locator_hint?: string | null;
  order_index: number;
  scan_strategy: "outline_only" | "heading_section" | "page_window" | "fulltext_match";
}

export interface ResourceSourceUnit {
  id: string;
  content_type: string;
  text: string;
  page_idx?: number | null;
  page_no?: number | null;
  source_locator?: string | null;
  url?: string | null;
  heading_path: string[];
  paragraph_index?: number | null;
  timestamp_start?: number | null;
  timestamp_end?: number | null;
  asset_path?: string | null;
  bbox: number[];
  order_index: number;
  metadata: Record<string, unknown>;
}

export type ResourceSourceType =
  | "code_repository"
  | "local_file"
  | "web_url"
  | "audio_file"
  | "video_file"
  | "video_url"
  | "pasted_text"
  | "transcript";

export type SourceIngestionStatus =
  | "queued"
  | "fetching"
  | "parsing"
  | "indexing"
  | "ready"
  | "cancelled"
  | "interrupted"
  | "failed";
export type EvidenceBundleStatus = "candidate" | "confirmed" | "consumed" | "archived";
export type EvidencePurpose = "chat" | "board_generation" | "board_edit" | "board_explain" | "board_chat";
export type SourceStructureStatus = "pending" | "building" | "ready" | "linear_only" | "failed";
export type SourceChapterMappingStatus = "verified" | "partial" | "unverified" | "unmapped";
export type SourceStructureQualityLevel =
  | "unassessed"
  | "fully_verified"
  | "partially_verified"
  | "unverified"
  | "search_only";
export type SourceTextReadiness = "unknown" | "ready" | "sparse" | "very_sparse" | "empty";
export type SourceStructureStrategy =
  | "code_repository_v1"
  | "native_directory_v1"
  | "hybrid_directory_v1"
  | "ai_directory_v1"
  | "codex_directory_v1"
  | "codex_catalog"
  | "epub_navigation"
  | "epub_heading"
  | "pdf_outline"
  | "pdf_toc"
  | "pdf_merged_toc"
  | "pdf_layout_toc"
  | "docx_heading"
  | "markdown_heading"
  | "linear_text"
  | "open_notebook_search_only";

export interface SourceStructureQuality {
  evaluator_version: number;
  level: SourceStructureQualityLevel;
  text_readiness: SourceTextReadiness;
  confidence: number;
  total_chapter_count: number;
  verified_chapter_count: number;
  unverified_chapter_count: number;
  demoted_chapter_count: number;
  verified_leaf_count: number;
  expected_leaf_count: number;
  verified_ratio: number;
  boundary_valid_ratio: number;
  body_coverage_ratio: number;
  independent_anchor_ratio: number;
  meaningful_characters_per_page: number;
  duplicate_locator_ratio: number;
  duplicate_range_count: number;
  overlap_ratio: number;
  non_monotonic_count: number;
  oversized_leaf_count: number;
  diagnostics: string[];
}

export interface SourceIngestionJob {
  id: string;
  run_id: string;
  resource_id?: string | null;
  source_type: ResourceSourceType;
  source_uri?: string | null;
  adapter: string;
  status: SourceIngestionStatus;
  progress: number;
  error: string;
  phase_history: string[];
  agent_activity: AgentActivityEvent[];
  cancel_requested: boolean;
  heartbeat_at?: string | null;
  created_at: string;
  updated_at: string;
  processing_metrics?: SourceProcessingMetrics | null;
}

export interface SourceProcessingMetrics {
  started_at?: string;
  finished_at?: string | null;
  status?: string;
  total_ms?: number;
  original_store_ms?: number | null;
  stage_timings_ms?: Record<string, number>;
  cache_hits?: Record<string, number>;
  model_calls?: number;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cached_input_tokens?: number | null;
}

export interface SourceIngestionRecord {
  id: string;
  owner_user_id: string;
  package_id: string;
  title: string;
  source_type: ResourceSourceType;
  source_uri?: string | null;
  file_name: string;
  mime_type: string;
  size_bytes: number;
  status: SourceIngestionStatus;
  error: string;
  structure_status: SourceStructureStatus;
  structure_strategy?: SourceStructureStrategy | null;
  structure_has_verified_toc: boolean;
  structure_quality?: SourceStructureQuality | null;
  structure_error: string;
  structure_updated_at?: string | null;
  ingestion_job?: SourceIngestionJob | null;
  refinement?: import("./source-refinement").SourceCatalogRefinementView | null;
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
}

export interface BulkDeleteSourcesResponse {
  removed: SourceIngestionRecord[];
  failed: Array<{ id: string; detail: string }>;
}

export interface RetrievalEvidence {
  id: string;
  source_ingestion_id: string;
  source_title: string;
  source_uri?: string | null;
  chapter_id: string;
  section_path: string[];
  page_range: string;
  chunk_ids: string[];
  excerpt: string;
  expanded_text: string;
  relevance_score: number;
  reason: string;
  token_count: number;
  metadata: Record<string, unknown>;
}

export type SourceQueryScopeMode =
  | "chapter"
  | "page_range"
  | "source"
  | "sources"
  | "all_ready_sources";

export interface SourceQueryRef {
  source_ingestion_id: string;
  source_content_hash: string;
  source_chapter_id?: string | null;
  catalog_version?: number | null;
  page_start?: number | null;
  page_end?: number | null;
}

export interface SourceQueryScope {
  mode: SourceQueryScopeMode;
  refs: SourceQueryRef[];
}

export interface SourceCitation {
  evidence_id: string;
  citation_number?: number | null;
  evidence_ref?: string;
  source_document_id?: string;
  snapshot_url?: string;
  source_id: string;
  source_title: string;
  section_path: string[];
  page_start?: number | null;
  page_end?: number | null;
  excerpt: string;
  chunk_ids: string[];
  block_ids?: string[];
  bbox: number[];
  source_content_hash: string;
  parser_run_id: string;
}

export interface SourceStructure {
  id: string;
  owner_user_id: string;
  package_id: string;
  source_ingestion_id: string;
  status: SourceStructureStatus;
  strategy: SourceStructureStrategy;
  has_verified_toc: boolean;
  quality?: SourceStructureQuality | null;
  chapter_count: number;
  chunk_count: number;
  confidence: number;
  error: string;
  warnings: string[];
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
}

export interface SourceChapter {
  id: string;
  owner_user_id: string;
  package_id: string;
  source_ingestion_id: string;
  parent_id?: string | null;
  number: string;
  normalized_number: string;
  title: string;
  level: number;
  path: string[];
  order_index: number;
  source_locator: string;
  body_start_offset?: number | null;
  body_end_offset?: number | null;
  page_start?: number | null;
  page_end?: number | null;
  anchor_status: "verified" | "unverified";
  range?: SourceRange | null;
  mapping_status?: SourceChapterMappingStatus;
  source_content_hash?: string;
  catalog_evidence?: SourceCatalogEvidence[];
  catalog_version?: number;
  confidence: number;
  excerpt: string;
  metadata: Record<string, unknown>;
}

export interface SourceCatalogEvidence {
  method: string;
  source_locator: string;
  page_start?: number | null;
  page_end?: number | null;
  excerpt: string;
  confidence: number;
  metadata: Record<string, unknown>;
}

export type SourceRangeKind =
  | "file_lines"
  | "pdf_pages"
  | "epub_spine"
  | "docx_paragraphs"
  | "ppt_slides"
  | "sheet_rows"
  | "text_lines"
  | "dom_anchor"
  | "structured_path";

export interface SourceRange {
  kind: SourceRangeKind;
  start: number | string | null;
  end: number | string | null;
  container: string;
  start_anchor: string;
  end_anchor: string;
  path: string[];
  display_label: string;
  end_inclusive: true;
  metadata: Record<string, unknown>;
}

export interface SourceCatalogSourceSummary {
  id: string;
  title: string;
  file_name: string;
  mime_type: string;
  size_bytes: number;
  status: SourceIngestionStatus;
  structure_status: SourceStructureStatus;
}

export interface SourceCatalogWorkItem {
  id: string;
  kind:
    | "directory_discovery"
    | "directory_page_attribution"
    | "pagination_calibration"
    | "range_mapping"
    | "conflict_resolution";
  node_keys: string[];
  page_ranges: Array<{ start: number; end: number }>;
  reason: string;
}

export interface SourceCatalogView {
  source: SourceCatalogSourceSummary;
  structure_id: string | null;
  status: SourceStructureStatus;
  strategy: SourceStructureStrategy | null;
  has_verified_toc: boolean;
  catalog_version: number;
  catalog_updated_at: string | null;
  source_content_hash: string;
  catalog_schema_version: string;
  catalog_model: string;
  catalog_authority?: string;
  model_call_count?: number;
  task_contract: string;
  work_state: "working" | "paused" | "satisfied" | "partial";
  phase: "directory_discovery" | "page_calibration" | "range_mapping" | "validation" | "terminal";
  directory_status: "incomplete" | "uncertain" | "complete";
  index_status: "pending" | "in_progress" | "complete" | "partial";
  summary: string;
  next_plan: string;
  stop_reason: string;
  completion_reason: string;
  directory_gaps: string[];
  remaining_work: SourceCatalogWorkItem[];
  remaining_work_count: number;
  snapshot_reason:
    | "first_citable"
    | "top_level_subtree"
    | "batch"
    | "budget_increment"
    | "correction"
    | "pause"
    | "final";
  background_refine_active: boolean;
  pagination_regime_count: number;
  unresolved_node_count: number;
  locator_method: string;
  revision: number;
  can_refine: boolean;
  recent_tool_activity: Array<Record<string, unknown>>;
  chapter_count: number;
  verified_chapter_count: number;
  confidence: number;
  quality: SourceStructureQuality;
  error: string;
  warnings: string[];
  chapters: SourceChapter[];
}

export interface SourceCatalogBatchView {
  package_id: string;
  catalogs: SourceCatalogView[];
}

export interface SourceChunk {
  id: string;
  owner_user_id: string;
  package_id: string;
  source_ingestion_id: string;
  chapter_id?: string | null;
  order_index: number;
  source_locator: string;
  text: string;
  start_offset: number;
  end_offset: number;
  page_start?: number | null;
  page_end?: number | null;
  token_count: number;
  metadata: Record<string, unknown>;
}

export type SourceVisualKind = "image" | "chart" | "table" | "diagram" | "page_snapshot";
export type SourceVisualAnchorStatus = "verified" | "unverified";

export interface SourceVisualAsset {
  id: string;
  owner_user_id: string;
  package_id: string;
  source_ingestion_id: string;
  structure_id: string;
  structure_version: number;
  chapter_id?: string | null;
  kind: SourceVisualKind;
  source_locator: string;
  page_start?: number | null;
  page_end?: number | null;
  paragraph_index?: number | null;
  slide_no?: number | null;
  sheet_name: string;
  bbox: number[];
  before_chunk_id?: string | null;
  after_chunk_id?: string | null;
  caption: string;
  extracted_text: string;
  surrounding_text: string;
  anchor_status: SourceVisualAnchorStatus;
  mime_type: string;
  order_index: number;
  content_hash: string;
  position_hash: string;
  width?: number | null;
  height?: number | null;
  table_data: string[][];
  confidence: number;
  created_at: string;
  metadata: Record<string, unknown>;
}

export interface SourceVisualEvidence {
  visual_id: string;
  source_ingestion_id: string;
  source_chapter_id: string;
  kind: SourceVisualKind;
  source_locator: string;
  page_start?: number | null;
  page_end?: number | null;
  paragraph_index?: number | null;
  slide_no?: number | null;
  sheet_name: string;
  bbox: number[];
  before_chunk_id?: string | null;
  after_chunk_id?: string | null;
  caption: string;
  extracted_text: string;
  surrounding_text: string;
  anchor_status: SourceVisualAnchorStatus;
  mime_type: string;
  content_hash: string;
  position_hash: string;
  width?: number | null;
  height?: number | null;
  table_data: string[][];
  confidence: number;
  metadata: Record<string, unknown>;
}

export interface SourceStructureView {
  source: SourceIngestionRecord;
  structure?: SourceStructure | null;
  chapters: SourceChapter[];
  chunks: SourceChunk[];
  visuals: SourceVisualAsset[];
}

export interface SourceContentView {
  source: SourceIngestionRecord;
  content: string;
}

export interface EvidenceBundle {
  id: string;
  owner_user_id: string;
  package_id: string;
  lesson_id?: string | null;
  requirement_run_id?: string | null;
  board_task_run_id?: string | null;
  purpose: EvidencePurpose;
  status: EvidenceBundleStatus;
  query: string;
  evidence_items: RetrievalEvidence[];
  visual_items: SourceVisualEvidence[];
  context_text: string;
  token_count: number;
  confirmed_by_user: boolean;
  created_at: string;
  updated_at: string;
  confirmed_at?: string | null;
  metadata: Record<string, unknown>;
}

export type ResourcePageRole =
  | "cover"
  | "copyright"
  | "toc"
  | "preface"
  | "body"
  | "appendix"
  | "back_matter"
  | "unknown";

export interface ResourcePageSection {
  role: ResourcePageRole;
  page_idx_start?: number | null;
  page_idx_end?: number | null;
  page_no_start?: number | null;
  page_no_end?: number | null;
  title: string;
  confidence: number;
  evidence_excerpt: string;
}

export interface ResourcePageMapEntry {
  page_idx: number;
  page_no: number;
  role: ResourcePageRole;
  printed_page?: number | null;
  body_offset?: number | null;
  confidence: number;
  evidence_excerpt: string;
}

export interface ResourcePageStructure {
  page_count: number;
  body_start_page_idx?: number | null;
  body_start_page_no?: number | null;
  toc_page_indices: number[];
  sections: ResourcePageSection[];
  page_map: ResourcePageMapEntry[];
  diagnostics: string[];
  confidence: number;
}

export interface ResourceLibraryItem {
  id: string;
  name: string;
  mime_type: string;
  resource_type: string;
  size_bytes: number;
  uploaded_at: string;
  scope_lesson_id?: string | null;
  outline: LibraryChapter[];
  concept_index: Record<string, string[]>;
  extracted_text_available: boolean;
  source_type: ResourceSourceType;
  source_uri?: string | null;
  ingestion_status: SourceIngestionStatus;
  ingestion_error: string;
  ingestion_progress: number;
  ingestion_adapter: string;
  ingestion_job?: SourceIngestionJob | null;
  parser_provider: string;
  parser_artifacts_path?: string | null;
  parser_message: string;
  parse_warnings: string[];
  source_units: ResourceSourceUnit[];
  page_structure?: ResourcePageStructure | null;
}

export interface CoursePackage {
  id: string;
  title: string;
  summary: string;
  tags: string[];
  visibility: "private" | "public";
  publication_review: PublicationReview;
  published_version?: {
    revision_id: string;
    title: string;
    summary: string;
    tags: string[];
    lessons: PublishedLessonVersion[];
    course_graph: CourseGraphEdge[];
    published_at: string;
  } | null;
  is_standalone: boolean;
  lessons: Lesson[];
  course_graph: CourseGraphEdge[];
  resources: ResourceLibraryItem[];
  open_lesson_ids: string[];
  active_lesson_id?: string | null;
  workspace_tab_order: string[];
  storage_kind?: "legacy" | "ridoc";
  ridoc_revision?: string | null;
  package_kind?: "course" | "standalone";
}

export interface WorkspaceState {
  packages: CoursePackage[];
  active_package_id?: string | null;
}

export interface RepositoryTagSuggestionsResponse {
  tags: string[];
  selected_model: AIModelSelection;
  source_revision_id: string;
}

export interface RepositoryProjectView {
  id: string;
  owner_user_id: string;
  owner_display_name: string;
  owner_handle: string;
  project_kind: "lesson" | "package";
  title: string;
  slug: string;
  tags: string[];
  summary: string;
  visibility: "private" | "public";
  canonical_path: string;
  version: number;
  public_revision_id?: string | null;
  source_repository_id?: string | null;
  source_revision_id?: string | null;
  star_count: number;
  fork_count: number;
  viewer_has_starred: boolean;
  upstream_has_updates: boolean;
  created_at: string;
  updated_at: string;
}

export interface RepositoryProjectDetail extends RepositoryProjectView {
  course_package: CoursePackage;
  access_mode: "owner" | "viewer";
  read_only: boolean;
}

export interface ProjectForkResult {
  fork_id: string;
  destination: "repository" | "workspace";
  repository?: RepositoryProjectView | null;
  workspace?: WorkspaceState | null;
  active_lesson_id?: string | null;
}

export interface ProjectUpstreamView {
  source_repository_id?: string | null;
  source_revision_id?: string | null;
  current_source_revision_id?: string | null;
  has_updates: boolean;
}

export interface ProjectCompareView {
  source_revision_id: string;
  current_source_revision_id: string;
  changed_lessons: string[];
  added_lessons: string[];
  removed_lessons: string[];
}

export type PersonalRepositoryItem =
  | {
      kind: "lesson";
      lesson: Lesson;
      is_synced: boolean;
      published_at: string;
      source_commit_id: string;
    }
  | {
      kind: "package";
      package: CoursePackage;
      is_synced: boolean;
      published_at: string;
    };

export interface PersonalRepositoriesView {
  items: PersonalRepositoryItem[];
}

export interface LessonWorkspaceDelta {
  operation: "create" | "close" | "delete";
  workspace_revision: number;
  package_id: string;
  active_package_id?: string | null;
  active_lesson_id?: string | null;
  open_lesson_ids: string[];
  workspace_tab_order: string[];
  created_lesson?: Lesson | null;
  deleted_lesson_id?: string | null;
  graph_edge?: CourseGraphEdge | null;
  storage_kind?: "legacy" | "ridoc";
  ridoc_revision?: string | null;
}

export interface DocumentSaveDelta {
  lesson_id: string;
  package_id: string;
  workspace_revision: number;
  document: BoardDocument;
  latest_commit: CommitRecord;
  current_branch: string;
  branch_head_commit_id: string;
  branch_base_commit_id?: string;
  updated_at: string;
  changed: boolean;
  storage_kind?: "legacy" | "ridoc";
  ridoc_revision?: string | null;
}

export interface BatchLessonActionRequest {
  action: "move" | "delete";
  lesson_ids: string[];
  target_package_id?: string | null;
  expected_ridoc_revisions?: Record<string, string>;
}

export interface UserView {
  id: string;
  email: string;
  phone?: string | null;
  role: "user" | "admin" | "guest";
  display_name?: string | null;
  handle?: string | null;
  avatar_url?: string | null;
  created_at: string;
  last_login_at?: string | null;
  email_verified_at?: string | null;
  auth_identities: AuthIdentityView[];
}

export interface AuthSessionResponse {
  token?: string | null;
  user: UserView;
}

export interface EmailCodeRequestResponse {
  challenge_id: string;
  expires_in_seconds: number;
  message: string;
}

export interface EmailRegistrationRequest {
  email: string;
  username: string;
  password: string;
  password_confirmation: string;
  challenge_id: string;
  code: string;
  turnstile_token?: string | null;
}

export interface AccountActionResponse {
  message: string;
}

export interface PasswordResetRequest {
  challenge_id: string;
  code: string;
  password: string;
  password_confirmation: string;
  turnstile_token?: string | null;
}

export interface AuthIdentityView {
  id?: string;
  enabled?: boolean;
  provider: string;
  provider_label: string;
  email?: string | null;
  display_name?: string | null;
  avatar_url?: string | null;
  created_at: string;
  last_login_at?: string | null;
}

export interface AuthProviderView {
  id: string;
  label: string;
  description: string;
  configured: boolean;
  kind: "password" | "oauth" | "device";
}

export interface AdminOverview {
  stats: {
    users: number;
    admins: number;
    packages: number;
    lessons: number;
    resources: number;
  };
  users: UserView[];
}

export interface AIModelSelection {
  agent_backend?: "codex" | "pi";
  provider: AIProvider;
  model: string;
  access_method?: AIModelAccessMethod | null;
  connection_id?: string | null;
  reasoning_effort?: string | null;
  service_tier?: string | null;
}

export interface AIAgentBackendOption {
  id: "codex" | "pi";
  label: string;
  description: string;
  enabled: boolean;
}

export interface AIReasoningEffortOption {
  reasoning_effort: string;
  description: string;
}

export interface AIServiceTierOption {
  id: string;
  name: string;
  description: string;
}

export interface AIModelOption {
  provider: AIProvider;
  model: string;
  access_method: AIModelAccessMethod;
  connection_id?: string | null;
  label: string;
  capability: AIModelCapability;
  enabled: boolean;
  configured: boolean;
  default: boolean;
  transport?: AIRealtimeTransport | null;
  default_reasoning_effort?: string | null;
  supported_reasoning_efforts?: AIReasoningEffortOption[];
  default_service_tier?: string | null;
  service_tiers?: AIServiceTierOption[];
  input_price_credits_per_million?: number | null;
}

export interface AIProviderCredentialStatus {
  provider: AIProvider;
  label: string;
  configured: boolean;
  manageable: boolean;
  configuration_fields?: { name: string; label: string; placeholder?: string }[];
}

export interface AIModelCatalog {
  text: AIModelOption[];
  realtime: AIModelOption[];
  defaults: {
    text: AIModelSelection;
    realtime: AIModelSelection;
  };
  agent_backends?: {
    teaching: AIAgentBackendOption[];
    source: AIAgentBackendOption[];
  };
}

export interface CodexAccountView {
  type?: string | null;
  email?: string | null;
  plan_type?: string | null;
}

export interface CodexProviderStatus {
  connection_id?: string | null;
  enabled: boolean;
  available: boolean;
  configured: boolean;
  account?: CodexAccountView | null;
  rate_limits?: Record<string, unknown> | null;
  quota_status?: "not_requested" | "available" | "unavailable";
  quota_error_code?: string | null;
  quota_checked_at?: string | null;
  message: string;
}

export interface CodexLoginStartResponse {
  connection_id?: string | null;
  login_id: string;
  verification_url: string;
  user_code: string;
  expires_at?: string | null;
}

export interface CodexLoginStatusResponse {
  connection_id?: string | null;
  login_id: string;
  status: "pending" | "succeeded" | "failed" | "cancelled" | "expired";
  error?: string | null;
  account?: CodexAccountView | null;
}

export interface SelectionRef {
  kind: "chat" | "board" | "source";
  excerpt: string;
  location_kind?: BoardTaskLocationKind | null;
  lesson_id?: string | null;
  source_commit_id?: string | null;
  block_id?: string | null;
  document_id?: string | null;
  segment_id?: string | null;
  heading_path?: string[];
  before_text?: string;
  after_text?: string;
  text_hash?: string | null;
  source_ingestion_id?: string | null;
  source_title?: string;
  source_uri?: string | null;
  source_chapter_id?: string | null;
  source_chapter_number?: string;
  source_chapter_title?: string;
  source_page_range?: string;
  source_locator?: string;
  source_page_start?: number | null;
  source_page_end?: number | null;
  source_scope_kind?: "source" | "chapter" | "page_range" | "repository_node";
  source_repository_node_id?: string | null;
  source_repository_tree_kind?: RepositoryTreeKind | null;
  source_range?: SourceRange | null;
  catalog_version?: number | null;
  source_content_hash?: string;
}

export type RepositoryTreeKind = "project" | "learning";
export type RepositoryNodeKind =
  | "root"
  | "directory"
  | "file"
  | "module"
  | "flow"
  | "entrypoint"
  | "concept";

export interface RepositoryNodeEvidence {
  file_id: string;
  path: string;
  line_start: number;
  line_end: number;
  reason: string;
  confidence: number;
}

export interface RepositoryMapNode {
  id: string;
  source_ingestion_id: string;
  tree_kind: RepositoryTreeKind;
  node_kind: RepositoryNodeKind;
  parent_id?: string | null;
  title: string;
  path: string;
  description: string;
  level: number;
  order_index: number;
  selectable: boolean;
  coverage_status: "complete" | "partial" | "unexamined";
  evidence: RepositoryNodeEvidence[];
  metadata: Record<string, unknown>;
}

export interface RepositorySnapshot {
  id: string;
  source_ingestion_id: string;
  provider: string;
  repository_id?: number | null;
  owner: string;
  name: string;
  visibility: string;
  requested_ref: string;
  resolved_commit_sha: string;
  scope_path: string;
  scope_kind: "repository" | "directory" | "file";
  default_branch: string;
  archive_hash: string;
  manifest_hash: string;
  license_spdx: string;
  supersedes_source_id?: string | null;
  created_at: string;
  metadata: Record<string, unknown>;
}

export interface RepositoryMapView {
  source: SourceIngestionRecord;
  snapshot: RepositorySnapshot;
  project_nodes: RepositoryMapNode[];
  learning_nodes: RepositoryMapNode[];
  analyzed_file_count: number;
  readable_file_count: number;
  total_file_count: number;
  coverage_ratio: number;
  warnings: string[];
}

export interface GitHubInstallationView {
  installation_id: number;
  account_id?: number | null;
  account_login: string;
  account_type: string;
  repository_selection: string;
  status: "connected" | "revoked" | "suspended" | "disconnected";
  permissions: Record<string, string>;
  repository_count: number;
  updated_at: string;
}

export interface GitHubConnectionView {
  enabled: boolean;
  configured: boolean;
  connected: boolean;
  installations: GitHubInstallationView[];
  message: string;
}

export interface GitHubRepositoryView {
  id: number;
  full_name: string;
  owner: string;
  name: string;
  private: boolean;
  default_branch: string;
  html_url: string;
  description: string;
  installation_id?: number | null;
}

export type FormulaInkAction = "reference" | "replace";

export interface FormulaInkPayload {
  image_data_url: string;
  source_latex?: string | null;
  action: FormulaInkAction;
}

export interface ChatAttachmentRef {
  source_ingestion_id: string;
  name: string;
  mime_type: string;
  size_bytes: number;
  kind: "image" | "file";
  status: SourceIngestionStatus;
}

export interface BoardFocusRef {
  source: "board" | "chat";
  lesson_id?: string | null;
  document_id?: string | null;
  segment_id?: string | null;
  kind?: "heading" | "paragraph" | "list" | "table" | "code" | "image" | "formula" | "other" | null;
  heading_path: string[];
  excerpt: string;
  before_text: string;
  after_text: string;
  text_hash?: string | null;
  excerpt_hash?: string | null;
  confidence: number;
  reason: string;
  display_label?: string;
  match_id?: string | null;
  source_segment_ids?: string[];
  order_start?: number | null;
  order_end?: number | null;
  score_breakdown?: Record<string, number>;
}

export interface ConversationTurn {
  role: "user" | "assistant";
  content: string;
}

export interface BoardDecision {
  action: BoardAction;
  reason: string;
}

export interface ChatRequestPayload {
  message: string;
  session_id?: string;
  turn_id?: string;
  input_event_id?: string;
  expected_ridoc_revision?: string | null;
  history_branch_action?: {
    kind: "regenerate" | "fork";
    source_node_id: string;
  } | null;
  channel?: "text" | "realtime";
  input_kind?: "typed" | "voice";
  provider_reference?: string | null;
  text_model?: AIModelSelection | null;
  selection?: SelectionRef | null;
  selections?: SelectionRef[];
  formula_ink?: FormulaInkPayload | null;
  attachments?: ChatAttachmentRef[];
  source_query_scope?: SourceQueryScope | null;
  interaction_mode?: ChatInteractionMode;
  board_generation_action?: "start" | null;
  teaching_action?: "continue" | "restart" | null;
  board_task_confirmation?: "confirm" | "decline" | null;
  post_generation_action?: "auto_explain" | "stop_after_generation";
  chat_edit_source_commit_id?: string | null;
  chat_edit_base_commit_id?: string | null;
  chat_edit_original_message?: string | null;
  conversation?: ConversationTurn[];
}

export interface SectionTeachingProgress {
  section_index: number;
  section_count: number;
  current_section_title: string;
  has_next_section: boolean;
  waiting_for_continue: boolean;
  target_heading_path?: string[];
  current_heading_path?: string[];
}

export type AgentActivityStage =
  | "turn_decision"
  | "resolve_target"
  | "build_context"
  | "execute_role"
  | "verify"
  | "persist_history"
  | "final";

export type AgentActivityStatus =
  | "pending"
  | "running"
  | "completed"
  | "cancelled"
  | "blocked"
  | "failed"
  | "skipped";

export interface AgentActivityEvent {
  id: string;
  turn_id: string;
  stage: AgentActivityStage;
  label: string;
  status: AgentActivityStatus;
  role: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export type GuidedRequirementDiscoveryStrategy =
  | "entry_point_discovery"
  | "level_discovery"
  | "goal_discovery"
  | "mode_discovery"
  | "bottleneck_discovery";

export interface GuidedRequirementEntryPoint {
  title: string;
  description: string;
  answer_value: string;
  why_it_matters: string;
  best_for: string;
}

export type GuidedRequirementSelectionTarget =
  | "learning_content"
  | "current_level"
  | "target_scenario"
  | "teaching_type"
  | "bottleneck";

export interface GuidedRequirementDiscovery {
  strategy: GuidedRequirementDiscoveryStrategy;
  selection_target: GuidedRequirementSelectionTarget;
  question_title: string;
  learning_map_summary: string;
  entry_point_options: GuidedRequirementEntryPoint[];
  recommended_entry_point: string;
  reason_for_recommendation: string;
  learner_profile_inference: string;
}

export interface ChatResponse {
  chatbot_message: string;
  turn_decision?: TurnDecision | null;
  decision_trace?: DecisionTrace | null;
  source_citations?: SourceCitation[];
  follow_up_suggestions?: string[];
  agent_activity?: AgentActivityEvent[];
  learning_requirement_sheet: LearningRequirementSheet;
  active_requirement_sheet?: LearningRequirementSheet | null;
  learning_clarification: LearningClarificationStatus;
  requirement_run_id?: string | null;
  requirement_version_id?: string | null;
  requirement_phase?: LearningRequirementRunStatus | null;
  learning_requirement_operation_status?: "none" | "succeeded" | "failed";
  learning_requirement_operation_failure_reason?: string | null;
  board_task_sheet?: BoardTaskRequirementSheet | null;
  active_board_task_sheet?: BoardTaskRequirementSheet | null;
  board_task_run_id?: string | null;
  board_task_version_id?: string | null;
  board_task_phase?: BoardTaskRunStatus | null;
  board_task_questions?: string[];
  board_decision: BoardDecision;
  needs_clarification: boolean;
  clarification_questions: string[];
  guided_requirement_discovery?: GuidedRequirementDiscovery | null;
  requirement_cleared?: boolean;
  board_document_operation_status?: BoardDocumentOperationStatus;
  board_document_operation_failure_reason?: string | null;
  teaching_progress?: SectionTeachingProgress | null;
  auto_teaching_operation_status?: "none" | "succeeded" | "failed";
  auto_teaching_operation_failure_reason?: string | null;
  course_package: CoursePackage;
  ridoc_revision?: string | null;
}

export type TurnIntent = "ordinary_chat" | "learning_need" | "unclear";
export type LearningFlow = "teach" | "generate_only" | "none";
export type TurnContinuation =
  | "none"
  | "learning_requirement"
  | "board_task"
  | "pending_write_offer"
  | "teaching_sequence"
  | "interaction_session";
export type TurnRelationToActive =
  | "none"
  | "continue"
  | "supplement"
  | "replace"
  | "new_task"
  | "unresolved";

export interface TurnDecision {
  intent: TurnIntent;
  learning_flow: LearningFlow;
  board_write_opt_out?: boolean;
  explicit_teaching_request?: boolean;
  continuation: TurnContinuation;
  relation_to_active: TurnRelationToActive;
  board_access: "forbidden" | "state_check_only";
  reason: string;
}

export interface DecisionTrace {
  intent_signals: string[];
  matched_rules: string[];
  selected_action: TurnIntent;
  rejected_actions: TurnIntent[];
  target_resolver: string;
  sequence_mode: string;
  role_executed: string;
  board_access: "forbidden" | "state_check_only" | "bounded_board_role";
  requirement_effect: "preserved" | "eligible" | "updated";
  document_changed: boolean;
  reason: string;
}

export interface RequirementUpdateStreamPayload {
  learning_requirement_sheet: LearningRequirementSheet;
  active_requirement_sheet?: LearningRequirementSheet | null;
  learning_clarification: LearningClarificationStatus;
  requirement_run_id?: string | null;
  requirement_version_id?: string | null;
  requirement_phase?: LearningRequirementRunStatus | null;
  clarification_questions: string[];
}

export interface BoardTaskUpdateStreamPayload {
  board_task_sheet: BoardTaskRequirementSheet;
  active_board_task_sheet?: BoardTaskRequirementSheet | null;
  board_task_run_id?: string | null;
  board_task_version_id?: string | null;
  board_task_phase?: BoardTaskRunStatus | null;
  board_task_questions: string[];
}

export interface RealtimeConnectPayload {
  offer_sdp: string;
  latest_assistant_message?: string | null;
  client_session_id?: string | null;
  realtime_model?: AIModelSelection | null;
  selection?: SelectionRef | null;
}

export interface RealtimeConnectResponse {
  answer_sdp: string;
  provider: AIProvider;
  model: string;
  voice: string;
  call_id?: string | null;
  tools_enabled?: boolean;
  client_delegation_enabled?: boolean;
  delegation_websocket_url?: string | null;
  client_session_id?: string | null;
}

export interface GoogleRealtimeSessionPayload {
  latest_assistant_message?: string | null;
  client_session_id?: string | null;
  realtime_model?: AIModelSelection | null;
}

export interface GoogleRealtimeSessionResponse {
  websocket_url: string;
  setup: Record<string, unknown>;
  provider: "google";
  model: string;
  voice: string;
}

export interface RealtimeEventLogPayload {
  client_event_id: string;
  client_session_id?: string | null;
  turn_id?: string | null;
  occurred_at: string;
  lesson_title?: string | null;
  role: "user" | "assistant" | "tool";
  transport_event_type: string;
  transcript: string;
  tool_name?: string | null;
  tool_call_id?: string | null;
  tool_status?: string | null;
}

export type RealtimeToolName = "read_board_context" | "run_chatbot_workflow";

export interface RealtimeToolCallPayload {
  client_session_id: string;
  turn_id?: string | null;
  input_event_id?: string | null;
  input_kind: "typed" | "voice";
  provider_reference?: string | null;
  call_id: string;
  name: RealtimeToolName;
  arguments: Record<string, unknown>;
  selection?: SelectionRef | null;
}

export interface RealtimeToolCallResponse {
  status: "ok" | "error";
  model_output: Record<string, unknown>;
  resolved_focus?: BoardFocusRef | null;
  course_package?: CoursePackage | null;
}

export interface DocumentSavePayload {
  document: BoardDocument;
  label?: string;
  message?: string;
  metadata?: Record<string, unknown>;
  base_commit_id?: string | null;
  expected_ridoc_revision?: string | null;
  transaction_id?: string | null;
  history_branch_action?: {
    kind: "fork";
    source_node_id: string;
  } | null;
}

export interface RidocHistoryNode {
  id: string;
  kind: "M" | "C" | "E";
  initial: boolean;
  parent_ids: string[];
  branch_name: string;
  created_at: string;
  title: string;
  summary: string;
  user_message: string;
  assistant_message: string;
  before_document_ref?: string | null;
  after_document_ref: string;
  is_head: boolean;
  continuation_node_id: string;
  branch_origin_deleted?: boolean;
  source_message_deleted?: boolean;
  merge_target_node_id?: string;
  merge_source_node_id?: string;
  merge_source_branch?: string;
  pending_deletion?: boolean;
  node_refs?: RidocNodeReference[];
}

export interface RidocNodeReference {
  lesson_id: string; node_id: string; kind: "M" | "C" | "E";
  status: "live" | "deleted"; role: string;
}

export interface RidocNodeDeletionPreview {
  lesson_id: string;
  node_id: string;
  user_message: string;
  assistant_message: string;
  ridoc_revision: string;
  affected_branches: string[];
  retained_document_node_ids: string[];
  blocked_reason: string | null;
  expected_version?: number;
}

export interface RidocNodeDeletionResult {
  course_package: CoursePackage;
  ridoc_revision: string;
  deleted_node_id: string;
  affected_branches: string[];
  retained_document_node_ids: string[];
  mutation_id: string;
}

export interface RidocHistoryPage {
  lesson_id: string;
  ridoc_revision: string;
  current_branch: string;
  document_history?: DocumentHistoryState;
  expected_version?: number | null;
  learning_node_ids?: string[];
  replay_documents?: Record<string, BoardDocument>;
  learning_order_merged?: boolean;
  branches: Array<{
    name: string;
    head_node_id: string;
    base_node_id: string;
    archived: boolean;
    learning_node_ids?: string[];
  }>;
  nodes: RidocHistoryNode[];
  next_cursor?: string | null;
}

export interface DocumentAIEditPayload {
  instruction: string;
  selection_text?: string | null;
  replace_whole?: boolean;
  conversation?: ConversationTurn[];
}
