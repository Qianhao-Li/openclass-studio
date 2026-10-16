import { request as rootRequest } from "./api";
import type { DocumentHistoryStepPayload, DocumentHistoryStepResult } from "../types/document-history";

export function createDocumentHistoryApi(requestClient: typeof rootRequest = rootRequest) {
  const request = requestClient;
  return {
  step(lessonId: string, payload: DocumentHistoryStepPayload, repositoryId?: string | null) {
    const prefix = repositoryId ? `/api/repositories/${encodeURIComponent(repositoryId)}/studio` : "/api";
    return request<DocumentHistoryStepResult>(
      `${prefix}/lessons/${encodeURIComponent(lessonId)}/history/document/step`,
      { method: "POST", body: JSON.stringify(payload) }
    );
  },
};
}

export const documentHistoryApi = createDocumentHistoryApi();
