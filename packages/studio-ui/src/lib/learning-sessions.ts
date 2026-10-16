import { request } from "./api";
import type { CoursePackage } from "../types";

export type LearningSource = { source_kind: "lesson" | "package"; source_id: string; repository_id?: string | null; revision?: string | null };
export type LearningSession = {
  id: string; source_kind: "lesson" | "package"; source_id: string;
  source_repository_id: string | null; source_revision: string;
  title: string; revision: string; has_changes: boolean; expires_at: number;
  status: "active"; course_package: CoursePackage;
};
export type OwnedLearningTarget = { kind: "repository" | "workspace"; id: string; source_kind?: "lesson" | "package" };
export type OpenLearningResult = { mode: "owned"; target: OwnedLearningTarget } | { mode: "temporary"; session: LearningSession };
export const learningSessions = {
  open: (source: LearningSource) => request<OpenLearningResult>("/api/learning-sessions", { method: "POST", body: JSON.stringify(source) }),
  get: (id: string) => request<LearningSession>(`/api/learning-sessions/${encodeURIComponent(id)}`),
  status: (id: string) => request<{ status: string; saved_repository_id: string | null; expires_at: number }>(`/api/learning-sessions/${encodeURIComponent(id)}/status`),
  list: (source: LearningSource) => request<Array<Pick<LearningSession, "id" | "title" | "expires_at" | "source_revision">>>(
    `/api/learning-sessions?${new URLSearchParams({ source_kind: source.source_kind, source_id: source.source_id })}`),
  touch: (id: string) => request<{ expires_at: number }>(`/api/learning-sessions/${encodeURIComponent(id)}/touch`, { method: "POST" }),
  save: (id: string, revision: string) => request<{ repository_id: string }>(`/api/learning-sessions/${encodeURIComponent(id)}/save`, { method: "POST", body: JSON.stringify({ expected_revision: revision }) }),
  discard: (id: string) => request<{ discarded: boolean }>(`/api/learning-sessions/${encodeURIComponent(id)}`, { method: "DELETE" }),
};
