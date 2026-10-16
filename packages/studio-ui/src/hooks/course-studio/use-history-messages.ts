import { useMemo } from "react";
import type { Lesson } from "../../types";
import { buildLessonMessagesFromHistory, type ChatMessage } from "../../components/course-studio/history-utils";

/** A document preview cannot replace the message array or reset its scroll. */
export function useHistoryMessages(playback: ChatMessage[] | null, lesson: Lesson | null,
  previewLesson: Lesson | null, nodeId: string | null, messages: ChatMessage[]) {
  return useMemo(() => playback ?? (lesson && nodeId
    ? buildLessonMessagesFromHistory(previewLesson ?? lesson, nodeId) : messages),
  [playback, lesson, previewLesson, nodeId, messages]);
}
