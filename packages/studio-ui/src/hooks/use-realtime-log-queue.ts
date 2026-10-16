import { useCallback, useRef } from "react";

import { api } from "../lib/api";
import type { RealtimeEventLogPayload } from "../types";

type QueuedRealtimeLogEvent = {
  lessonId: string;
  payload: RealtimeEventLogPayload;
};

type UseRealtimeLogQueueOptions = {
  getClientSessionId: () => string | null;
  getLessonTitle: () => string | null;
};

type RealtimeLogEventIdentity = {
  clientEventId?: string;
  turnId?: string | null;
};

export function useRealtimeLogQueue({
  getClientSessionId,
  getLessonTitle,
}: UseRealtimeLogQueueOptions) {
  const queueRef = useRef<QueuedRealtimeLogEvent[]>([]);
  const flushInFlightRef = useRef(false);

  const flushRealtimeLogQueue = useCallback(async () => {
    if (flushInFlightRef.current) {
      return;
    }
    flushInFlightRef.current = true;
    try {
      while (queueRef.current.length > 0) {
        const nextEvent = queueRef.current[0];
        await api.logRealtimeEvent(nextEvent.lessonId, nextEvent.payload);
        queueRef.current.shift();
      }
    } catch {
      // Keep the queue for retry.
    } finally {
      flushInFlightRef.current = false;
    }
  }, []);

  const flushRealtimeLogQueueWithBeacon = useCallback(() => {
    if (flushInFlightRef.current || queueRef.current.length === 0) {
      return;
    }
    const pending = [...queueRef.current];
    const failed: QueuedRealtimeLogEvent[] = [];
    pending.forEach((event) => {
      const sent = api.logRealtimeEventBeacon(event.lessonId, event.payload);
      if (!sent) {
        failed.push(event);
      }
    });
    queueRef.current = failed;
  }, []);

  const enqueueRealtimeLogEvent = useCallback(
    (
      lessonId: string,
      role: RealtimeEventLogPayload["role"],
      transportEventType: string,
      transcript: string,
      identity: RealtimeLogEventIdentity = {}
    ) => {
      const normalized = transcript.trim();
      if (!normalized) {
        return;
      }
      queueRef.current.push({
        lessonId,
        payload: {
          client_event_id: identity.clientEventId ?? `realtime-event:${crypto.randomUUID()}`,
          client_session_id: getClientSessionId(),
          turn_id: identity.turnId ?? null,
          occurred_at: new Date().toISOString(),
          lesson_title: getLessonTitle(),
          role,
          transport_event_type: transportEventType,
          transcript: normalized,
        },
      });
      void flushRealtimeLogQueue();
    },
    [flushRealtimeLogQueue, getClientSessionId, getLessonTitle]
  );

  return {
    enqueueRealtimeLogEvent,
    flushRealtimeLogQueue,
    flushRealtimeLogQueueWithBeacon,
  };
}
