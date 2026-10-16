import type { CoursePackage } from ".";
import type { ContentAction } from "./content-actions";

/** Targets are computed from the complete current branch by the RIDOC service. */
export interface DocumentHistoryState {
  current_document_node_id: string;
  undo_node_id: string | null;
  redo_node_id: string | null;
  withdrawn_node_ids: string[];
  blocked_reason: string | null;
  /** Complete branch actions, independent of History pagination or visible rows. */
  actions?: ContentAction[];
}

export interface DocumentHistoryStepPayload {
  direction: "undo" | "redo";
  target_node_id: string;
  expected_branch_name: string;
  expected_ridoc_revision: string;
  mutation_id: string;
  expected_version?: number | null;
}

export interface DocumentHistoryStepResult {
  course_package: CoursePackage;
  ridoc_revision: string;
  mutation_id: string;
  document_history: DocumentHistoryState;
}
