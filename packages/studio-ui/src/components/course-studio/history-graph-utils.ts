import { compactText, documentsEqual, metadataBool, metadataText } from "./history-utils";
import type { CommitRecord, Lesson, SelectionRef } from "../../types";

export type HistoryNodeKind = "chat" | "document" | "restore" | "merge" | "system";

const CHAT_COMMIT_KINDS = new Set([
  "basic_chat",
  "learning_requirement_refinement",
  "board_task_requirement_refinement",
  "chat_flow",
  "board_section_teaching",
]);

const DOCUMENT_COMMIT_KINDS = new Set([
  "manual_document_save",
  "manual_document_edit",
  "auto_document_save",
  "board_document_generation",
  "board_document_edit",
  "import_docx",
]);

function commitKind(commit: CommitRecord) {
  return typeof commit.metadata?.kind === "string" ? commit.metadata.kind : "";
}

export function isChatCommitHidden(commit: CommitRecord): boolean {
  return metadataText(commit, "chat_visibility") === "hidden";
}

export function isHideableChatCommit(commit: CommitRecord): boolean {
  if (commit.operations.length) {
    return false;
  }
  if (metadataBool(commit, "document_changed")) {
    return false;
  }
  const kind = commitKind(commit);
  if (DOCUMENT_COMMIT_KINDS.has(kind) || kind === "restore_snapshot" || kind === "branch_merge") {
    return false;
  }
  return historyNodeKind(commit) === "chat";
}

export function historyNodeKind(commit: CommitRecord): HistoryNodeKind {
  const explicit = commit.metadata?.history_node_kind;
  if (explicit === "chat" || explicit === "document" || explicit === "restore" || explicit === "merge" || explicit === "system") {
    return explicit;
  }
  const kind = commitKind(commit);
  if (kind === "restore_snapshot") {
    return "restore";
  }
  if (kind === "branch_merge") {
    return "merge";
  }
  if (kind === "initial_document") {
    return "system";
  }
  if (metadataBool(commit, "document_changed") || commit.operations.length || DOCUMENT_COMMIT_KINDS.has(kind)) {
    return "document";
  }
  if (CHAT_COMMIT_KINDS.has(kind) || metadataText(commit, "user_message") || metadataText(commit, "assistant_message")) {
    return "chat";
  }
  return "system";
}

export function isDocumentVersionNode(commit: CommitRecord) {
  const kind = historyNodeKind(commit);
  return kind === "document" || kind === "restore" || kind === "merge" || !commit.parent_ids.length;
}

export function documentParentCommitId(lesson: Lesson, commit: CommitRecord): string | null {
  const explicit = metadataText(commit, "document_parent_commit_id");
  if (explicit && lesson.history_graph.commits.some((item) => item.id === explicit)) {
    return explicit;
  }
  const commits = new Map(lesson.history_graph.commits.map((item) => [item.id, item]));
  let cursor = commit.parent_ids[0] ? commits.get(commit.parent_ids[0]) : undefined;
  const seen = new Set<string>();
  while (cursor && !seen.has(cursor.id)) {
    seen.add(cursor.id);
    if (isDocumentVersionNode(cursor) && !documentsEqual(cursor.snapshot, commit.snapshot)) {
      return cursor.id;
    }
    cursor = cursor.parent_ids[0] ? commits.get(cursor.parent_ids[0]) : undefined;
  }
  return null;
}

export function historyNodeTitle(commit: CommitRecord) {
  const explicit = metadataText(commit, "history_node_title");
  if (explicit) {
    return explicit;
  }
  const userMessage = metadataText(commit, "user_message");
  if (historyNodeKind(commit) === "chat" && userMessage) {
    return compactText(userMessage, 64);
  }
  const editorSummary = metadataText(commit, "board_document_editor_summary");
  if (historyNodeKind(commit) === "document" && editorSummary) {
    return compactText(editorSummary, 64);
  }
  return commit.label;
}

export function historyNodeSummary(commit: CommitRecord) {
  const explicit = metadataText(commit, "history_node_summary");
  if (explicit) {
    return explicit;
  }
  const assistantMessage = metadataText(commit, "assistant_message");
  if (historyNodeKind(commit) === "chat" && assistantMessage) {
    return compactText(assistantMessage, 160);
  }
  const editorSummary = metadataText(commit, "board_document_editor_summary");
  if (historyNodeKind(commit) === "document" && editorSummary) {
    return compactText(editorSummary, 160);
  }
  return compactText(commit.message, 160);
}

export function historyNodeKindLabel(kind: HistoryNodeKind) {
  if (kind === "chat") {
    return "Chat";
  }
  if (kind === "document") {
    return "Document";
  }
  if (kind === "restore") {
    return "Restore";
  }
  if (kind === "merge") {
    return "Merge";
  }
  return "System";
}

function historyNodeContent(commit: CommitRecord) {
  const userMessage = metadataText(commit, "user_message");
  const assistantMessage = metadataText(commit, "assistant_message");
  if (historyNodeKind(commit) === "chat" && (userMessage || assistantMessage)) {
    return [
      userMessage ? `Learner: ${userMessage}` : "",
      assistantMessage ? `OpenClass：${assistantMessage}` : "",
    ].filter(Boolean).join("\n\n");
  }
  return commit.snapshot.content_text.trim() || historyNodeSummary(commit) || commit.message.trim();
}

function boundedText(value: string, maxLength: number) {
  const normalized = value.trim();
  return normalized.length <= maxLength ? normalized : `${normalized.slice(0, maxLength).trimEnd()}…`;
}

export function historyNodeSelection(lesson: Lesson, commit: CommitRecord): SelectionRef {
  const title = historyNodeTitle(commit);
  const excerpt = boundedText(
    [
      `History node: ${title}`,
      `Type: ${historyNodeKindLabel(historyNodeKind(commit))}`,
      `Branch: ${commit.branch_name}`,
      "",
      historyNodeContent(commit),
    ].join("\n"),
    12_000
  );
  return {
    kind: "chat",
    excerpt,
    lesson_id: lesson.id,
    document_id: commit.snapshot.id,
    segment_id: commit.id,
    heading_path: [lesson.title, title],
    before_text: `History node ${commit.id}`,
    after_text: `Created at ${commit.created_at}`,
  };
}

export function historyNodeCommunityShareHref(lesson: Lesson, commit: CommitRecord) {
  const params = new URLSearchParams({
    reference: "history_node",
    lesson_id: lesson.id,
    history_node: commit.id,
  });
  return `/community?${params.toString()}`;
}

/** Short git-style code shown in the graph table: `C0`, `M`, `C`, `R`, `Merge`. */
export function historyNodeTypeCode(commit: CommitRecord) {
  const kind = historyNodeKind(commit);
  if (kind === "merge") {
    return "Merge";
  }
  if (!commit.parent_ids.length) {
    return "C0";
  }
  if (kind === "chat") {
    return "M";
  }
  if (kind === "restore") {
    return "R";
  }
  if (kind === "document") {
    return "C";
  }
  return "S";
}

/** Who produced the node, derived from the commit kind rather than any subject-specific hint. */
export function historyNodeActor(commit: CommitRecord) {
  const kind = commitKind(commit);
  if (kind === "auto_document_save") {
    return "Auto";
  }
  if (kind === "manual_document_save" || kind === "manual_document_edit" || kind === "import_docx") {
    return "You";
  }
  if (kind === "restore_snapshot" || kind === "branch_merge") {
    return "You";
  }
  if (kind === "board_document_generation" || kind === "board_document_edit") {
    return "AI";
  }
  if (historyNodeKind(commit) === "document" && metadataText(commit, "assistant_message")) {
    return "AI";
  }
  if (metadataText(commit, "user_message")) {
    return "You";
  }
  return "System";
}

const RELATIVE_TIME_STEPS: Array<{ limit: number; unit: string; size: number }> = [
  { limit: 60_000, unit: "s", size: 1_000 },
  { limit: 3_600_000, unit: "m", size: 60_000 },
  { limit: 86_400_000, unit: "h", size: 3_600_000 },
  { limit: 604_800_000, unit: "d", size: 86_400_000 },
];

/** Compact age column: `now`, `12m`, `3h`, `5d`, then the calendar date. */
export function historyNodeRelativeTime(value: string, now = Date.now()) {
  const created = new Date(value).getTime();
  if (!Number.isFinite(created)) {
    return "";
  }
  const elapsed = Math.max(now - created, 0);
  if (elapsed < 45_000) {
    return "now";
  }
  const step = RELATIVE_TIME_STEPS.find((candidate) => elapsed < candidate.limit);
  if (!step) {
    return new Date(created).toLocaleDateString("en-US", { month: "2-digit", day: "2-digit" });
  }
  return `${Math.max(Math.floor(elapsed / step.size), 1)}${step.unit}`;
}
