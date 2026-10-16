import type { BoardDocument, CoursePackage, LessonMergeEventPlan, RidocHistoryPage } from ".";

export interface HistoryEditSession {
  id: string;
  package_id: string;
  lesson_id: string;
  base_ridoc_revision: string;
  base_repository_version?: number | null;
  version: number;
  state: string;
  editing_enabled?: boolean;
  dirty: boolean;
  stale: boolean;
  blocked_reason: string | null;
  operation_count: number;
  deleted_node_ids: string[];
  pending?: boolean;
  pending_removals?: HistoryPendingRemoval[];
  affected_lesson_ids?: string[];
  draft_course_package: CoursePackage;
  draft_history: RidocHistoryPage;
}
export interface HistoryDocumentReference { $history_document_ref: string; }
type EncodedHistoryDocuments<T> = T extends BoardDocument ? HistoryDocumentReference
  : T extends Array<infer Item> ? Array<EncodedHistoryDocuments<Item>>
  : T extends object ? { [Key in keyof T]: EncodedHistoryDocuments<T[Key]> } : T;
export type HistoryEditCleanSession = Omit<HistoryEditSession, "draft_course_package" | "draft_history"> & {
  reuse_base_revision: string;
  base_learning_node_ids?: string[];
  document_table?: Record<string, never>;
  draft_course_package?: never;
  draft_history?: never;
};
export type HistoryEditCompactSession = Omit<HistoryEditSession, "draft_course_package" | "draft_history"> & {
  document_table: Record<string, BoardDocument>;
  draft_course_package: EncodedHistoryDocuments<CoursePackage>;
  draft_history: EncodedHistoryDocuments<RidocHistoryPage>;
};
export type HistoryEditSessionTransport = HistoryEditSession | HistoryEditCleanSession | HistoryEditCompactSession;
export type HistoryEditSessionResponse = HistoryEditSession | HistoryEditCleanSession;
export type HistoryEditOperation =
  | { kind: "reorder_messages"; branch_name: string; ordered_node_ids: string[] }
  | { kind: "step_document"; branch_name: string; direction: "undo" | "redo"; target_node_id: string }
  | { kind: "step_document_actions"; branch_name: string; direction: "undo" | "redo"; target_node_ids: string[] }
  | { kind: "delete_message"; node_id: string }
  | { kind: "restore_message"; node_id: string }
  | { kind: "create_branch"; name: string; from_node_id?: string }
  | { kind: "checkout_branch"; name: string }
  | { kind: "merge_branch"; source_branch_name: string; target_branch_name: string; ordered_node_ids?: string[] };
export interface HistoryEditCommit {
  session_id: string;
  ridoc_revision: string;
  changed: boolean;
  deleted_node_ids?: string[];
  removed_nodes?: HistoryPendingRemoval[];
  mutation_id: string;
  course_package: CoursePackage;
}
export type HistoryEditMergeDraft = { source: string; target: string; plan: LessonMergeEventPlan };

export interface HistoryPendingRemoval { lesson_id: string; node_id: string; kind: "M" | "C"; }
export interface HistoryNodeReferrer {
  lesson_id: string; node_id?: string | null; kind: string; branch_name?: string | null;
  relation: string; title?: string | null;
}
export interface HistoryEditImpact {
  impact_token: string; referrers: HistoryNodeReferrer[]; node_id: string; lesson_id: string;
  kind: "M" | "C"; requires_confirmation: boolean;
  targets?: HistoryPendingRemoval[];
}
export interface HistoryEditReview {
  confirmation_token: string; pending_removals: HistoryPendingRemoval[];
  referrers: HistoryNodeReferrer[]; affected_lesson_ids: string[]; dirty: boolean;
  stale?: boolean; blocked_reason?: string | null;
  version: number; base_ridoc_revision: string;
  changes?: Array<{ lesson_id: string; kind: string; count: number }>;
}
