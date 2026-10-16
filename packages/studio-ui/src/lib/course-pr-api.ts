import { request } from "./api";
import type { CoursePr, CoursePrDecision, CoursePrMergeResult, CoursePrReview, CoursePrSource, CoursePrTarget } from "../types/course-pr";

const prPath = (id: string) => `/api/course-prs/${encodeURIComponent(id)}`;
const reviewPath = (id: string) => `/api/course-pr-reviews/${encodeURIComponent(id)}`;
const json = (value: unknown, method = "POST"): RequestInit => ({ method, body: JSON.stringify(value) });
const expected = (review: CoursePrReview) => ({ expected_draft_version: review.draft_version, expected_review_revision: review.revision });

export const coursePrApi = {
  resolve: (source: CoursePrSource) => request<CoursePrTarget>("/api/course-prs/resolve", json(source)),
  create: (source: CoursePrSource & { title: string; description: string }) => request<CoursePr>("/api/course-prs", json(source)),
  list: (repositoryId: string) => request<CoursePr[]>(`/api/projects/${encodeURIComponent(repositoryId)}/pull-requests`, { cache: "no-store" }),
  get: (id: string) => request<CoursePr>(prPath(id), { cache: "no-store" }),
  update: (id: string, source: CoursePrSource & { title: string; description: string; expected_pr_revision: number }) => request<CoursePr>(`${prPath(id)}/revisions`, json(source)),
  close: (pr: CoursePr) => request<CoursePr>(`${prPath(pr.id)}/close`, json({ expected_pr_revision: pr.revision })),
  startReview: (id: string, revision: number) => request<CoursePrReview>(`${prPath(id)}/reviews`, json({ expected_pr_revision: revision })),
  review: (id: string) => request<CoursePrReview>(reviewPath(id), { cache: "no-store" }),
  decide: (review: CoursePrReview, decisions: Record<string, CoursePrDecision>, orderedNodeIds?: string[]) => request<CoursePrReview>(`${reviewPath(review.id)}/decisions`, json({ ...expected(review), decisions, ...(orderedNodeIds ? { ordered_node_ids: orderedNodeIds } : {}) }, "PUT")),
  save: (review: CoursePrReview) => request<CoursePrReview>(`${reviewPath(review.id)}/save`, json(expected(review))),
  rebase: (review: CoursePrReview) => request<CoursePrReview>(`${reviewPath(review.id)}/rebase`, json(expected(review))),
  discard: (review: CoursePrReview) => request<{ status: string }>(`${reviewPath(review.id)}/discard`, json(expected(review))),
  merge: (review: CoursePrReview) => request<CoursePrMergeResult>(`${reviewPath(review.id)}/merge`, json({ ...expected(review), expected_target_revision: review.target_revision, expected_pr_revision: review.pr_revision })),
};
