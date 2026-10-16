import { isHideableChatCommit } from "./history-graph-utils";
import { getLessonCommit, type ChatMessage } from "./history-utils";
import type { Lesson } from "../../types";

export function chatHistoryDeletionAction({ message, lesson, readOnly, legacyBlocked, onDelete, onHide }: {
  message: ChatMessage;
  lesson: Lesson;
  readOnly: boolean;
  legacyBlocked: boolean;
  onDelete?: (nodeId: string) => void | Promise<void>;
  onHide: (nodeId: string) => void | Promise<void>;
}): (() => void) | undefined {
  const id = message.commitId;
  if (readOnly || message.role !== "user" || !id) return undefined;
  if (onDelete) return message.status === "ready" ? () => { void onDelete(id); } : undefined;
  if (legacyBlocked) return undefined;
  const commit = getLessonCommit(lesson, id);
  return commit && isHideableChatCommit(commit) ? () => { void onHide(id); } : undefined;
}
