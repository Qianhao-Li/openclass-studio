import type { CoursePackage, RepositoryProjectView } from ".";

export type CoursePrDecision = "accept" | "reject" | "undecided";
export type CoursePrSource = {
  target_address: string;
  source_repository_id?: string;
  source_package_id?: string;
  source_lesson_id: string;
  source_branch: string;
  expected_source_revision: string;
  target_branch: string;
};
export type CoursePrTarget = {
  target_repository: RepositoryProjectView;
  source_lesson_id: string;
  target_lesson_id: string;
  source_repository_id?: string | null;
  source_package_id?: string | null;
  source_branch?: string;
  target_branch?: string;
  base_revision: string;
  target_revision: string;
};
export type CoursePr = {
  id: string;
  title: string;
  description: string;
  status: string;
  revision: number;
  target_repository_id: string;
  source_repository_id?: string | null;
  source_package_id?: string | null;
  source_branch?: string;
  target_branch?: string;
  source_lesson_id: string;
  target_lesson_id: string;
  created_at: string;
  updated_at: string;
  contributor_user_id: string;
  contributor_display_name?: string;
  target_owner_user_id: string;
  viewer_permissions: { can_review: boolean; can_update: boolean; can_close: boolean };
};
export type CoursePrAction = {
  id: string;
  kind: "add" | "remove" | "reorder";
  node_ids: string[];
  dependencies: string[];
  title: string;
  author: "contributor" | "reviewer";
};
export type CoursePrDiffNode = {
  id: string;
  kind: string;
  title: string;
  status: "added" | "removed" | "unchanged" | "target_only" | "reviewer_added";
  parent_ids: string[];
  branch_name?: string;
  created_at?: string;
  summary?: string;
  document?: unknown;
  node?: Record<string, unknown>;
};
export type CoursePrComparison = {
  action_groups: CoursePrAction[];
  diff_nodes: CoursePrDiffNode[];
  target_node_ids?: string[];
  source_node_ids?: string[];
  ordered_node_ids?: string[];
  order_conflicts?: unknown[];
  order_confirmed?: boolean;
  blockers?: Array<{ reason: string }>;
  merged_document?: unknown;
  document_diff: { before: unknown; after: unknown; target?: unknown; before_document?: unknown; after_document?: unknown; target_document?: unknown };
};
export type CoursePrReview = {
  id: string;
  pr_id: string;
  pr_revision: number;
  draft_version: number;
  revision: string;
  target_revision: string;
  target_lesson_id: string;
  course_package: CoursePackage;
  comparison: CoursePrComparison;
  decisions: Record<string, CoursePrDecision>;
  status: string;
  stale?: boolean;
};
export type CoursePrMergeResult = {
  course_package?: CoursePackage;
  repository_id?: string;
  status?: string;
};
