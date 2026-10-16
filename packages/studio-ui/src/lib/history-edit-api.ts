import { request as rootRequest } from "./api";
import { decodeHistoryEditSession } from "./history-edit-transport";
import type { LessonMergeEventPlan } from "../types";
import type { HistoryEditCommit, HistoryEditImpact, HistoryEditOperation, HistoryEditReview, HistoryEditSessionTransport } from "../types/history-edit";

function path(lessonId: string, repositoryId?: string | null) {
  return `${repositoryId ? `/api/repositories/${encodeURIComponent(repositoryId)}/studio` : "/api"}/lessons/${encodeURIComponent(lessonId)}/history/edit-session`;
}
export function createHistoryEditApi(requestClient: typeof rootRequest = rootRequest) {
  const request = requestClient;
  const compactHeaders = { "X-OpenClass-History-View": "compact" };
  const session = (url: string, init: RequestInit) => request<HistoryEditSessionTransport>(url, { ...init, headers: compactHeaders }).then(decodeHistoryEditSession);
  return {
  active(lessonId: string, repositoryId?: string | null, compact = true) {
    return request<HistoryEditSessionTransport | null>(path(lessonId, repositoryId), { cache: "no-store", ...(compact ? { headers: compactHeaders } : {}) })
      .then((value) => value ? decodeHistoryEditSession(value) : null);
  },
  begin(lessonId: string, revision: string, repositoryId?: string | null, version?: number | null) {
    return session(path(lessonId, repositoryId), { method: "POST", body: JSON.stringify({ expected_ridoc_revision: revision, expected_version: version }) });
  },
  operate(lessonId: string, id: string, version: number, operationId: string, operation: HistoryEditOperation, repositoryId?: string | null, impactToken?: string) {
    return session(`${path(lessonId, repositoryId)}/${encodeURIComponent(id)}`, {
      method: "PATCH", body: JSON.stringify({ expected_draft_version: version, operation_id: operationId, operation, impact_token: impactToken }),
    });
  },
  commit(lessonId: string, id: string, version: number, mutationId: string, repositoryId?: string | null, confirmationToken?: string) {
    return request<HistoryEditCommit>(`${path(lessonId, repositoryId)}/${encodeURIComponent(id)}/commit`, {
      method: "POST", body: JSON.stringify({ expected_draft_version: version, mutation_id: mutationId, confirmation_token: confirmationToken }),
    });
  },
  discard(lessonId: string, id: string, version: number, repositoryId?: string | null) {
    return request<{ session_id: string; state: string }>(`${path(lessonId, repositoryId)}/${encodeURIComponent(id)}/discard`, { method: "POST", body: JSON.stringify({ expected_draft_version: version }) });
  },
  mergePlan(lessonId: string, id: string, source: string, repositoryId?: string | null) {
    return request<LessonMergeEventPlan>(`${path(lessonId, repositoryId)}/${encodeURIComponent(id)}/merge-plan?source_branch_name=${encodeURIComponent(source)}`, { cache: "no-store" });
  },
  impact(lessonId: string, id: string, version: number, target: string | string[], repositoryId?: string | null) {
    return request<HistoryEditImpact>(`${path(lessonId, repositoryId)}/${encodeURIComponent(id)}/impact`, {
      method: "POST", body: JSON.stringify({ expected_draft_version: version, ...(Array.isArray(target) ? { node_ids: target } : { node_id: target }) }),
    });
  },
  review(lessonId: string, id: string, version: number, repositoryId?: string | null) {
    return request<HistoryEditReview>(`${path(lessonId, repositoryId)}/${encodeURIComponent(id)}/review`, {
      method: "POST", body: JSON.stringify({ expected_draft_version: version }),
    });
  },
  pause(lessonId: string, id: string, version: number, repositoryId?: string | null) {
    return session(`${path(lessonId, repositoryId)}/${encodeURIComponent(id)}/pause`, {
      method: "POST", body: JSON.stringify({ expected_draft_version: version }),
    });
  },
};
}

export const historyEditApi = createHistoryEditApi();
