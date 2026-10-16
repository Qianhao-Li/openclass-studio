import { request as rootRequest } from "./api";
import type { RidocNodeDeletionPreview, RidocNodeDeletionResult } from "../types";

function nodePath(lessonId: string, nodeId: string, repositoryId?: string | null) {
  const prefix = repositoryId ? `/api/repositories/${encodeURIComponent(repositoryId)}/studio` : "/api";
  return `${prefix}/lessons/${encodeURIComponent(lessonId)}/history/nodes/${encodeURIComponent(nodeId)}`;
}

export function createHistoryDeletionApi(requestClient: typeof rootRequest = rootRequest) {
  const request = requestClient;
  return {
  preview(lessonId: string, nodeId: string, repositoryId?: string | null) {
    return request<RidocNodeDeletionPreview>(`${nodePath(lessonId, nodeId, repositoryId)}/deletion-preview`);
  },
  delete(lessonId: string, nodeId: string, payload: {
    expected_ridoc_revision: string;
    mutation_id: string;
    expected_version?: number;
  }, repositoryId?: string | null) {
    return request<RidocNodeDeletionResult>(`${nodePath(lessonId, nodeId, repositoryId)}/delete`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
};
}

export const historyDeletionApi = createHistoryDeletionApi();
