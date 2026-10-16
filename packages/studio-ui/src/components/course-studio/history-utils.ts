import type { CourseChatMessageView } from "../chatbot";
import { normalizePageSettings } from "./page-settings";
import { sameSelection } from "./selection-utils";
import { lessonLearningCommits, lessonSourceCommits } from "./history-learning-order";
import {
  sourceCitationsFromMetadata,
  sourceSelectionsFromCommit,
} from "./chat-source-references";
import type {
  AgentActivityEvent,
  BoardDocument,
  ChatAttachmentRef,
  ChatInteractionMode,
  CommitRecord,
  GuidedRequirementDiscovery,
  LearningClarificationStatus,
  LearningRequirementKeyFact,
  Lesson,
  SelectionRef,
  SectionTeachingProgress,
  SourceRange,
} from "../../types";

export type ChatMessage = CourseChatMessageView;

export type LessonMessageMap = Record<string, ChatMessage[]>;
export type ChatWaitingQueueItem = {
  id: string;
  text: string;
  createdAt: string;
};

export type PendingHistoricalFork = {
  sourceMessageId: string;
  sourceNodeId: string;
  continuationNodeId: string;
  sourceLabel: string;
  originBranchName: string;
  originHeadNodeId: string;
  originRidocRevision: string;
  baseDocument: BoardDocument;
  startedAt: string;
};

export type LessonComposerState = {
  chatInput: string;
  composerMode: ChatInteractionMode;
  includeSelectionInPrompt: boolean;
  composerSelection: SelectionRef | null;
  composerSelections: SelectionRef[];
  sourceQuerySelections: SelectionRef[];
  sourceQueryAllReady: boolean;
  composerAttachments: ChatAttachmentRef[];
  /** Follow-ups queued while an agent turn is still running. */
  waitingQueue: ChatWaitingQueueItem[];
  /** Ephemeral historical checkout. It never enters RIDOC until the first valid M/C. */
  pendingHistoricalFork?: PendingHistoricalFork | null;
};
export type LessonComposerStateMap = Record<string, LessonComposerState>;

export const DEFAULT_LESSON_COMPOSER_STATE: LessonComposerState = {
  chatInput: "",
  composerMode: "ask",
  includeSelectionInPrompt: true,
  composerSelection: null,
  composerSelections: [],
  sourceQuerySelections: [],
  sourceQueryAllReady: false,
  composerAttachments: [],
  waitingQueue: [],
  pendingHistoricalFork: null,
};

export const MAX_COMPOSER_SELECTIONS = 8;

export function appendComposerSelection(current: SelectionRef[], next: SelectionRef): SelectionRef[] {
  if (next.kind !== "board") {
    return [next];
  }
  if (current.some((item) => sameSelection(item, next)) || current.length >= MAX_COMPOSER_SELECTIONS) {
    return current;
  }
  return [...current, next];
}

export const AUTO_SAVE_DELAY_MS = 1600;

export function createChatMessage(
  role: ChatMessage["role"],
  content: string,
  status: ChatMessage["status"] = "ready",
  id?: string,
  selection?: SelectionRef | null,
  teachingProgress?: SectionTeachingProgress | null,
  metadata?: Partial<
    Pick<
      ChatMessage,
      | "commitId"
      | "parentCommitIds"
      | "editableContent"
      | "interactionMode"
      | "editedFromCommitId"
      | "agentActivity"
      | "guidedRequirementDiscovery"
      | "followUpSuggestions"
      | "sourceSelections"
      | "sourceCitations"
    >
  >
): ChatMessage {
  return {
    id: id ?? crypto.randomUUID(),
    role,
    content,
    status,
    ...(selection ? { selection } : {}),
    ...(teachingProgress ? { teachingProgress } : {}),
    ...metadata,
  };
}

export function createLessonComposerState(): LessonComposerState {
  return {
    ...DEFAULT_LESSON_COMPOSER_STATE,
    composerSelections: [],
    sourceQuerySelections: [],
    composerAttachments: [],
    waitingQueue: [],
  };
}

export function createChatWaitingQueueItem(text: string): ChatWaitingQueueItem {
  return {
    id: crypto.randomUUID(),
    text: text.trim(),
    createdAt: new Date().toISOString(),
  };
}

export function formatDate(value: string) {
  return new Date(value).toLocaleString("en-US", {
    hour12: false,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function metadataText(commit: CommitRecord, key: string): string | null {
  const value = commit.metadata?.[key];
  return typeof value === "string" && value.trim() ? value : null;
}

function metadataStringList(commit: CommitRecord, key: string): string[] {
  const value = commit.metadata?.[key];
  if (!Array.isArray(value)) {
    return [];
  }
  return value.flatMap((item) => typeof item === "string" && item.trim() ? [item.trim()] : []).slice(0, 4);
}

export function metadataBool(commit: CommitRecord, key: string): boolean {
  return commit.metadata?.[key] === true;
}

function metadataLearningClarificationForcedStart(commit: CommitRecord): boolean {
  const value = commit.metadata?.learning_clarification;
  return Boolean(value && typeof value === "object" && (value as { forced_start?: unknown }).forced_start === true);
}

const LEGACY_NON_AI_ASSISTANT_PATTERNS = [
  "give me a keyword",
  "Start from there",
  "Let's find a small entrance first",
  "There is no available on-site explanation content this time.",
  "No writing on the board",
  "Model not returned available",
  "Request has been sent, generating lesson documents may be required",
] as const;

const LEGACY_DISPLAYABLE_CHAT_COMMIT_KINDS = new Set([
  "chat_flow",
  "board_section_teaching",
  "board_document_generation",
  "board_document_edit",
  "basic_chat",
  "learning_requirement_refinement",
  "board_task_requirement_refinement",
]);

function commitContainsDisplayableChat(commit: CommitRecord): boolean {
  if (metadataText(commit, "chat_visibility") === "hidden") {
    return false;
  }
  const historyNodeKind = metadataText(commit, "history_node_kind");
  const requirementPhase = metadataText(commit, "requirement_phase");
  if (requirementPhase === "ready" || requirementPhase === "frozen") {
    return false;
  }
  if (historyNodeKind === "chat") {
    return true;
  }
  if (LEGACY_DISPLAYABLE_CHAT_COMMIT_KINDS.has(String(commit.metadata?.kind ?? ""))) {
    return true;
  }
  return !historyNodeKind && Boolean(
    metadataText(commit, "user_message") || metadataText(commit, "assistant_message")
  );
}

function isDisplayableAssistantContent(content: string | null): content is string {
  const text = content?.trim();
  if (!text) {
    return false;
  }
  return !LEGACY_NON_AI_ASSISTANT_PATTERNS.some((pattern) => text.includes(pattern));
}

function sourceRangeFromMetadata(value: unknown): SourceRange | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const raw = value as Record<string, unknown>;
  const allowedKinds = new Set<SourceRange["kind"]>([
    "pdf_pages",
    "epub_spine",
    "docx_paragraphs",
    "ppt_slides",
    "sheet_rows",
    "text_lines",
    "dom_anchor",
    "structured_path",
  ]);
  if (typeof raw.kind !== "string" || !allowedKinds.has(raw.kind as SourceRange["kind"])) {
    return null;
  }
  const scalar = (candidate: unknown) =>
    typeof candidate === "number" || typeof candidate === "string" ? candidate : null;
  return {
    kind: raw.kind as SourceRange["kind"],
    start: scalar(raw.start),
    end: scalar(raw.end),
    container: typeof raw.container === "string" ? raw.container : "",
    start_anchor: typeof raw.start_anchor === "string" ? raw.start_anchor : "",
    end_anchor: typeof raw.end_anchor === "string" ? raw.end_anchor : "",
    path: Array.isArray(raw.path) ? raw.path.filter((item): item is string => typeof item === "string") : [],
    display_label: typeof raw.display_label === "string" ? raw.display_label : "",
    end_inclusive: true,
    metadata: raw.metadata && typeof raw.metadata === "object"
      ? raw.metadata as Record<string, unknown>
      : {},
  };
}

function selectionFromMetadata(value: unknown): SelectionRef | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const raw = value as Record<string, unknown>;
  const kind = raw.kind === "chat" || raw.kind === "board" || raw.kind === "source" ? raw.kind : null;
  const excerpt = typeof raw.excerpt === "string" ? raw.excerpt.trim() : "";
  if (!kind || !excerpt) {
    return null;
  }
  return {
    kind,
    excerpt,
    lesson_id: typeof raw.lesson_id === "string" ? raw.lesson_id : null,
    block_id: typeof raw.block_id === "string" ? raw.block_id : null,
    document_id: typeof raw.document_id === "string" ? raw.document_id : null,
    segment_id: typeof raw.segment_id === "string" ? raw.segment_id : null,
    heading_path: Array.isArray(raw.heading_path) ? raw.heading_path.filter((item): item is string => typeof item === "string") : [],
    before_text: typeof raw.before_text === "string" ? raw.before_text : "",
    after_text: typeof raw.after_text === "string" ? raw.after_text : "",
    text_hash: typeof raw.text_hash === "string" ? raw.text_hash : null,
    source_ingestion_id: typeof raw.source_ingestion_id === "string" ? raw.source_ingestion_id : null,
    source_title: typeof raw.source_title === "string" ? raw.source_title : "",
    source_uri: typeof raw.source_uri === "string" ? raw.source_uri : null,
    source_chapter_id: typeof raw.source_chapter_id === "string" ? raw.source_chapter_id : null,
    source_chapter_number: typeof raw.source_chapter_number === "string" ? raw.source_chapter_number : "",
    source_chapter_title: typeof raw.source_chapter_title === "string" ? raw.source_chapter_title : "",
    source_page_range: typeof raw.source_page_range === "string" ? raw.source_page_range : "",
    source_locator: typeof raw.source_locator === "string" ? raw.source_locator : "",
    source_page_start: typeof raw.source_page_start === "number" ? raw.source_page_start : null,
    source_page_end: typeof raw.source_page_end === "number" ? raw.source_page_end : null,
    source_scope_kind:
      raw.source_scope_kind === "source" ||
      raw.source_scope_kind === "chapter" ||
      raw.source_scope_kind === "page_range"
        ? raw.source_scope_kind
        : undefined,
    source_range: sourceRangeFromMetadata(raw.source_range),
    catalog_version: typeof raw.catalog_version === "number" ? raw.catalog_version : null,
    source_content_hash: typeof raw.source_content_hash === "string" ? raw.source_content_hash : "",
  };
}

function teachingProgressFromMetadata(value: unknown): SectionTeachingProgress | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const raw = value as Record<string, unknown>;
  const sectionIndex = typeof raw.section_index === "number" ? raw.section_index : null;
  const sectionCount = typeof raw.section_count === "number" ? raw.section_count : null;
  if (sectionIndex === null || sectionCount === null || sectionCount <= 0) {
    return null;
  }
  return {
    section_index: sectionIndex,
    section_count: sectionCount,
    current_section_title: typeof raw.current_section_title === "string" ? raw.current_section_title : "",
    has_next_section: raw.has_next_section === true,
    waiting_for_continue: raw.waiting_for_continue === true,
    target_heading_path: Array.isArray(raw.target_heading_path)
      ? raw.target_heading_path.filter((item): item is string => typeof item === "string")
      : [],
    current_heading_path: Array.isArray(raw.current_heading_path)
      ? raw.current_heading_path.filter((item): item is string => typeof item === "string")
      : [],
  };
}

const GUIDED_REQUIREMENT_DISCOVERY_STRATEGIES = new Set<GuidedRequirementDiscovery["strategy"]>([
  "entry_point_discovery",
  "level_discovery",
  "goal_discovery",
  "mode_discovery",
  "bottleneck_discovery",
]);

const GUIDED_REQUIREMENT_SELECTION_TARGETS = new Set<GuidedRequirementDiscovery["selection_target"]>([
  "learning_content",
  "current_level",
  "target_scenario",
  "teaching_type",
  "bottleneck",
]);

function guidedRequirementDiscoveryFromMetadata(value: unknown): GuidedRequirementDiscovery | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const raw = value as Record<string, unknown>;
  if (typeof raw.strategy !== "string" || !GUIDED_REQUIREMENT_DISCOVERY_STRATEGIES.has(raw.strategy as GuidedRequirementDiscovery["strategy"])) {
    return null;
  }
  const entryPointOptions = Array.isArray(raw.entry_point_options)
    ? raw.entry_point_options.flatMap((entry) => {
        if (!entry || typeof entry !== "object") {
          return [];
        }
        const candidate = entry as Record<string, unknown>;
        if (typeof candidate.title !== "string" || typeof candidate.description !== "string") {
          return [];
        }
        const title = candidate.title.trim();
        const description = candidate.description.trim();
        return title && description
          ? [{
              title,
              description,
              answer_value: typeof candidate.answer_value === "string" ? candidate.answer_value.trim() : "",
              why_it_matters: typeof candidate.why_it_matters === "string" ? candidate.why_it_matters.trim() : "",
              best_for: typeof candidate.best_for === "string" ? candidate.best_for.trim() : "",
            }]
          : [];
      })
    : [];
  if (!entryPointOptions.length) {
    return null;
  }
  return {
    strategy: raw.strategy as GuidedRequirementDiscovery["strategy"],
    selection_target:
      typeof raw.selection_target === "string" &&
      GUIDED_REQUIREMENT_SELECTION_TARGETS.has(raw.selection_target as GuidedRequirementDiscovery["selection_target"])
        ? (raw.selection_target as GuidedRequirementDiscovery["selection_target"])
        : "learning_content",
    question_title: typeof raw.question_title === "string" ? raw.question_title.trim() : "",
    learning_map_summary: typeof raw.learning_map_summary === "string" ? raw.learning_map_summary.trim() : "",
    entry_point_options: entryPointOptions,
    recommended_entry_point: typeof raw.recommended_entry_point === "string" ? raw.recommended_entry_point.trim() : "",
    reason_for_recommendation: typeof raw.reason_for_recommendation === "string" ? raw.reason_for_recommendation.trim() : "",
    learner_profile_inference: typeof raw.learner_profile_inference === "string" ? raw.learner_profile_inference.trim() : "",
  };
}

const AGENT_ACTIVITY_STAGES = new Set<AgentActivityEvent["stage"]>([
  "turn_decision",
  "resolve_target",
  "build_context",
  "execute_role",
  "verify",
  "persist_history",
  "final",
]);

const AGENT_ACTIVITY_STATUSES = new Set<AgentActivityEvent["status"]>([
  "pending",
  "running",
  "completed",
  "blocked",
  "failed",
  "skipped",
]);

function agentActivityFromMetadata(value: unknown): AgentActivityEvent[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.flatMap((item) => {
    if (!item || typeof item !== "object") {
      return [];
    }
    const raw = item as Record<string, unknown>;
    if (
      typeof raw.id !== "string" ||
      typeof raw.turn_id !== "string" ||
      typeof raw.stage !== "string" ||
      !AGENT_ACTIVITY_STAGES.has(raw.stage as AgentActivityEvent["stage"]) ||
      typeof raw.label !== "string" ||
      typeof raw.status !== "string" ||
      !AGENT_ACTIVITY_STATUSES.has(raw.status as AgentActivityEvent["status"]) ||
      typeof raw.role !== "string" ||
      typeof raw.created_at !== "string"
    ) {
      return [];
    }
    return [
      {
        id: raw.id,
        turn_id: raw.turn_id,
        stage: raw.stage as AgentActivityEvent["stage"],
        label: raw.label,
        status: raw.status as AgentActivityEvent["status"],
        role: raw.role,
        metadata:
          raw.metadata && typeof raw.metadata === "object"
            ? (raw.metadata as Record<string, unknown>)
            : {},
        created_at: raw.created_at,
      },
    ];
  });
}

export function currentHeadCommitId(lesson: Lesson): string | null {
  const branch = lesson.history_graph.branches[lesson.history_graph.current_branch];
  return (
    branch?.head_commit_id ??
    lesson.history_graph.commits[lesson.history_graph.commits.length - 1]?.id ??
    null
  );
}

export function getLessonCommit(lesson: Lesson, commitId: string | null | undefined): CommitRecord | null {
  if (!commitId) {
    return null;
  }
  return lesson.history_graph.commits.find((commit) => commit.id === commitId) ?? null;
}

function conversationTargetCommitId(lesson: Lesson, commitId?: string | null): string | null {
  const requestedCommitId = commitId ?? currentHeadCommitId(lesson);
  const commit = getLessonCommit(lesson, requestedCommitId);
  if (!commit) {
    return requestedCommitId;
  }

  const restoredCommitId = metadataText(commit, "restored_commit_id");
  if (commit.metadata?.kind === "restore_snapshot" && restoredCommitId) {
    return restoredCommitId;
  }

  return commit.id;
}

function chatUserContentFromCommit(commit: CommitRecord): string | null {
  const userMessage = metadataText(commit, "user_message");
  if (!userMessage) {
    return null;
  }

  if (metadataText(commit, "board_generation_action") === "start") {
    return "Start generating board content";
  }

  return metadataText(commit, "interaction_mode") === "direct_edit" ? `Direct lesson edit: ${userMessage}` : userMessage;
}

function chatInteractionModeFromCommit(commit: CommitRecord): ChatInteractionMode {
  return metadataText(commit, "interaction_mode") === "direct_edit" ? "direct_edit" : "ask";
}

function publishedConversationMessagesFromCommit(commit: CommitRecord): ChatMessage[] {
  const value = commit.metadata?.published_conversation;
  if (!Array.isArray(value)) {
    return [];
  }
  return value.flatMap((item, index) => {
    if (!item || typeof item !== "object") {
      return [];
    }
    const turn = item as Record<string, unknown>;
    const role = turn.role;
    const content = typeof turn.content === "string" ? turn.content.trim() : "";
    if ((role !== "user" && role !== "assistant") || !content) {
      return [];
    }
    return [
      createChatMessage(
        role,
        content,
        "ready",
        `${commit.id}:published-conversation:${index}`
      ),
    ];
  });
}

export function buildLessonMessagesFromHistory(
  lesson: Lesson,
  commitId?: string | null,
  order: "learning" | "source" = commitId ? "source" : "learning"
): ChatMessage[] {
  const targetCommitId = conversationTargetCommitId(lesson, commitId);
  const commits = (order === "learning" ? lessonLearningCommits(lesson, commitId) : null)
    ?? lessonSourceCommits(lesson, targetCommitId);
  const messages: ChatMessage[] = [];

  commits.forEach((commit) => {
    messages.push(...publishedConversationMessagesFromCommit(commit));
    if (!commitContainsDisplayableChat(commit)) {
      return;
    }

    const userContent = chatUserContentFromCommit(commit);
    const nativeMessage = metadataBool(commit, "ridoc_native");
    const assistantMessage = metadataText(commit, "assistant_message");
    if (nativeMessage && (!userContent || !assistantMessage)) return;
    if (userContent) {
      const persistedSelection = selectionFromMetadata(commit.metadata?.selection);
      messages.push(
        createChatMessage(
          "user",
          userContent,
          "ready",
          `${commit.id}:user`,
          persistedSelection,
          null,
          {
            commitId: commit.id,
            parentCommitIds: commit.parent_ids,
            editableContent: metadataText(commit, "user_message") ?? userContent,
            interactionMode: chatInteractionModeFromCommit(commit),
            editedFromCommitId: metadataText(commit, "chat_edit_source_commit_id"),
            sourceSelections: persistedSelection?.kind === "source" ? [] : sourceSelectionsFromCommit(commit),
          }
        )
      );
    }

    const assistantMessageSource = metadataText(commit, "assistant_message_source");
    const legacyChatbotGeneratedDuringHandoff =
      metadataLearningClarificationForcedStart(commit) && assistantMessageSource === "ai";
    if (
      !legacyChatbotGeneratedDuringHandoff &&
      (nativeMessage ? Boolean(assistantMessage) : isDisplayableAssistantContent(assistantMessage)) && assistantMessage
    ) {
      messages.push(
        createChatMessage(
          "assistant",
          assistantMessage,
          "ready",
          `${commit.id}:assistant`,
          null,
          teachingProgressFromMetadata(commit.metadata?.teaching_progress),
          {
            agentActivity: agentActivityFromMetadata(commit.metadata?.agent_activity),
            guidedRequirementDiscovery: guidedRequirementDiscoveryFromMetadata(
              commit.metadata?.guided_requirement_discovery
            ),
            followUpSuggestions: metadataStringList(commit, "follow_up_suggestions"),
            sourceCitations: sourceCitationsFromMetadata(commit.metadata?.source_citations),
            commitId: commit.id,
            parentCommitIds: commit.parent_ids,
          }
        )
      );
    }
  });

  return messages;
}

export function learningClarityFromCommit(commit: CommitRecord | null): LearningClarificationStatus | null {
  const value = commit?.metadata?.learning_clarification_after ?? commit?.metadata?.learning_clarification;
  if (!value || typeof value !== "object") {
    return null;
  }

  const record = value as Partial<LearningClarificationStatus>;
  if (typeof record.progress !== "number" || typeof record.label !== "string" || typeof record.reason !== "string") {
    return null;
  }

  return {
    progress: Math.max(0, Math.min(100, record.progress)),
    label: record.label,
    reason: record.reason,
    missing_items: Array.isArray(record.missing_items)
      ? record.missing_items.filter((item): item is string => typeof item === "string")
      : [],
    can_start: record.can_start === true,
    forced_start: record.forced_start === true,
    summary: typeof record.summary === "string" ? record.summary : "",
    key_facts: Array.isArray(record.key_facts)
      ? record.key_facts
          .flatMap((item) => {
            if (!item || typeof item !== "object") {
              return [];
            }
            const raw = item as unknown as Record<string, unknown>;
            if (typeof raw.label !== "string" || typeof raw.value !== "string") {
              return [];
            }
            return [
              {
                label: raw.label,
                value: raw.value,
                evidence: typeof raw.evidence === "string" ? raw.evidence : "",
                category: typeof raw.category === "string" ? (raw.category as LearningRequirementKeyFact["category"]) : null,
              },
            ];
          })
          .slice(0, 5)
      : [],
    checklist: Array.isArray(record.checklist)
      ? record.checklist
          .flatMap((item) => {
            if (!item || typeof item !== "object") {
              return [];
            }
            const raw = item as unknown as Record<string, unknown>;
            if (typeof raw.title !== "string") {
              return [];
            }
            return [
              {
                title: raw.title,
                is_clear: raw.is_clear === true,
                evidence: typeof raw.evidence === "string" ? raw.evidence : "",
              },
            ];
          })
      : [],
    next_question: typeof record.next_question === "string" ? record.next_question : "",
    ready_for_board: record.ready_for_board === true,
    work_mode:
      record.work_mode === "knowledge_board" ||
      record.work_mode === "narrow_topic" ||
      record.work_mode === "practice_artifact" ||
      record.work_mode === "unknown"
        ? record.work_mode
        : null,
    granularity:
      record.granularity === "single_knowledge_point" ||
      record.granularity === "source_chapter" ||
      record.granularity === "broad_topic" ||
      record.granularity === "practice_artifact" ||
      record.granularity === "unclear"
        ? record.granularity
        : null,
  };
}

export function compactText(value: string, limit = 120) {
  const compact = value.replace(/\s+/g, " ").trim();
  if (compact.length <= limit) {
    return compact;
  }
  return `${compact.slice(0, limit - 1)}…`;
}

export function nextBranchName(lesson: Lesson) {
  let index = Object.keys(lesson.history_graph.branches).length + 1;
  let name = `branch-${index}`;
  while (lesson.history_graph.branches[name]) {
    index += 1;
    name = `branch-${index}`;
  }
  return name;
}

export function nextEditBranchName(lesson: Lesson) {
  let index = 1;
  let name = `edit-${index}`;
  while (lesson.history_graph.branches[name]) {
    index += 1;
    name = `edit-${index}`;
  }
  return name;
}

export function documentsEqual(left: BoardDocument | null | undefined, right: BoardDocument | null | undefined) {
  if (!left || !right) {
    return false;
  }
  return (
    left.title === right.title &&
    JSON.stringify(left.content_json) === JSON.stringify(right.content_json) &&
    left.content_html === right.content_html &&
    left.content_text === right.content_text &&
    JSON.stringify(normalizePageSettings(left.page_settings)) ===
      JSON.stringify(normalizePageSettings(right.page_settings))
  );
}

function htmlToPlainText(value: string) {
  return value
    .replace(/<\/(h[1-6]|p|li|blockquote|tr)>/gi, "\n")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ")
    .trim();
}

export function isBoardDocumentEmpty(document: BoardDocument | null | undefined) {
  if (!document) {
    return true;
  }
  return !document.content_text.trim() && !htmlToPlainText(document.content_html).trim();
}
