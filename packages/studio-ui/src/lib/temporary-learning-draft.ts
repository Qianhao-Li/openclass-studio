import type { BoardDocument } from "../types";

type RecoveredDraft = { document: BoardDocument; revision: string; expiresAt: number; transactionId: string | null };
const key = (sessionId: string, lessonId: string) => `openclass.learning-draft:${sessionId}:${lessonId}`;

export function readTemporaryDraft(sessionId: string | null, lessonId: string): RecoveredDraft | null {
  if (!sessionId || typeof window === "undefined") return null;
  try {
    const value = JSON.parse(localStorage.getItem(key(sessionId, lessonId)) || "null") as RecoveredDraft | null;
    if (!value || value.expiresAt <= Date.now() || !value.document?.id || typeof value.revision !== "string") {
      localStorage.removeItem(key(sessionId, lessonId));
      return null;
    }
    return value;
  } catch { return null; }
}

export function writeTemporaryDraft(sessionId: string | null, lessonId: string, document: BoardDocument | null, revision: string | null, transactionId: string | null = null) {
  if (!sessionId || typeof window === "undefined") return;
  try {
    if (document && revision) localStorage.setItem(key(sessionId, lessonId), JSON.stringify({ document, revision, transactionId, expiresAt: Date.now() + 86_400_000 }));
    else localStorage.removeItem(key(sessionId, lessonId));
  } catch { /* Server autosave remains authoritative if browser storage is unavailable. */ }
}
