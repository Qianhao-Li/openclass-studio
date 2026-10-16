import { createChatMessage, type ChatMessage } from "./history-utils";
import type { RealtimeToolStatusUpdate } from "../../hooks/course-studio/codex-live-task-ui";

export type RealtimeTranscriptUpdate = {
  lessonId: string;
  turnId: string;
  messageId: string;
  role: "user" | "assistant";
  text: string;
  final: boolean;
};

export function applyRealtimeTranscriptUpdate(
  current: ChatMessage[],
  update: RealtimeTranscriptUpdate
): ChatMessage[] {
  const existing = current.find((message) => message.id === update.messageId);
  const status = update.role === "assistant" && !update.final ? "pending" : "ready";
  if (existing) {
    return current.map((message) =>
      message.id === update.messageId
        ? {
            ...message,
            content: update.text,
            status,
            statusLabel: status === "pending" ? message.statusLabel ?? "Replying in real time" : undefined,
          }
        : message
    );
  }
  return [
    ...current,
    createChatMessage(update.role, update.text, status, update.messageId, null, null, {
      editableContent: update.role === "user" ? update.text : undefined,
      interactionMode: update.role === "user" ? "ask" : undefined,
    }),
  ];
}

export function applyRealtimeToolStatusUpdate(
  current: ChatMessage[],
  update: RealtimeToolStatusUpdate
): ChatMessage[] {
  const messageId = `realtime:${update.turnId}:tool-status`;
  const messageStatus = update.status === "error"
    ? "error"
    : ["completed", "cancelled"].includes(update.status)
      ? "ready"
      : "pending";
  const existing = current.find((message) => message.id === messageId);
  if (existing) {
    return current.map((message) =>
      message.id === messageId
        ? {
            ...message,
            status: messageStatus,
            statusLabel: update.label,
            agentActivity: update.activity
              ? [
                  ...(message.agentActivity ?? []).filter((event) => event.id !== update.activity?.id),
                  update.activity,
                ]
              : message.agentActivity,
          }
        : message
    );
  }
  return [
    ...current,
    {
      ...createChatMessage("assistant", "", messageStatus, messageId),
      statusLabel: update.label,
      agentActivity: update.activity ? [update.activity] : undefined,
    },
  ];
}
