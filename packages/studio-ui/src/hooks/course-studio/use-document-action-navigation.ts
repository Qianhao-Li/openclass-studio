"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { CommitRecord, Lesson, RidocHistoryPage } from "../../types";
import type { ContentActionSpan } from "../../types/content-actions";
import type { GeometryResult } from "../../components/course-studio/history-range-geometry";

export function useDocumentActionNavigation({ lesson, history, previewId, draftVersion, onPreview }: {
  lesson: Lesson; history?: RidocHistoryPage | null; previewId: string | null; draftVersion?: number;
  onPreview: (commit: CommitRecord, documentOnly?: boolean) => void | Promise<void>;
}) {
  const sequence = useRef(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [geometry, setGeometry] = useState<GeometryResult | null>(null);
  const actions = history?.document_history?.actions;
  const reference = actions?.[0]?.range_projection?.reference_document_ref ?? "";
  const key = `${lesson.id}:${history?.ridoc_revision}:${history?.current_branch}:${draftVersion}:${reference}`;
  const current = useRef({ lesson, history, onPreview });
  useEffect(() => { current.current = { lesson, history, onPreview }; }, [lesson, history, onPreview]);
  useEffect(() => {
    const status = (event: Event) => {
      const result = (event as CustomEvent<{ requestId: number; message: string }>).detail;
      if (result?.requestId === sequence.current) setError(result.message);
    };
    window.addEventListener("openclass:history-range-location-status", status);
    return () => { ++sequence.current; window.removeEventListener("openclass:history-range-location-status", status); };
  }, []);
  useEffect(() => { ++sequence.current; window.dispatchEvent(new Event("openclass:clear-history-range")); }, [key]);
  useEffect(() => {
    if (!previewId) ++sequence.current;
    window.dispatchEvent(new Event("openclass:clear-history-range"));
  }, [previewId]);
  useEffect(() => {
    if (!reference || !actions || !history || history.current_branch !== lesson.history_graph.current_branch) return;
    const receive = (event: Event) => {
      const result = (event as CustomEvent<GeometryResult>).detail;
      if (result?.key === key) setGeometry(result);
    };
    const measure = () => window.dispatchEvent(new CustomEvent("openclass:measure-history-ranges", {
      detail: { key, documentId: lesson.board_document.id, document: lesson.board_document, actions },
    }));
    window.addEventListener("openclass:history-range-geometry", receive);
    window.addEventListener("openclass:history-editor-ready", measure);
    measure();
    return () => { window.removeEventListener("openclass:history-range-geometry", receive); window.removeEventListener("openclass:history-editor-ready", measure); };
  }, [key, reference, actions, history, lesson.board_document, lesson.history_graph.current_branch]);

  const activate = useCallback(async (commit: CommitRecord, fragmentId?: string, documentOnly = false) => {
    const { lesson, history, onPreview } = current.current;
    const action = history?.document_history?.actions?.find((item) => item.id === commit.id);
    const projection = action?.range_projection;
    const request = ++sequence.current;
    window.dispatchEvent(new Event("openclass:clear-history-range"));
    setSelected(action ? commit.id : null); setError("");
    const fragments = projection?.fragments ?? [];
    const chosen = fragments.find((fragment) => fragment.id === fragmentId);
    const before = chosen?.kind === "delete" || fragments.length > 0 && fragments.every((fragment) => fragment.kind === "delete");
    const snapshot = before && projection?.before_document ? projection.before_document
      : action ? history?.replay_documents?.[commit.id] ?? commit.snapshot : commit.snapshot;
    await onPreview({ ...commit, snapshot }, documentOnly);
    // Preview state commits after the promise returns. The editor waits for the
    // exact typed snapshot; newer clicks invalidate this scheduled request.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (request !== sequence.current || current.current.lesson.id !== lesson.id) return;
      const ranges = (fragment: typeof fragments[number]) => {
        if (fragment.precision !== "exact") return [];
        const values = before ? fragment.before : fragment.after;
        const marker = before ? fragment.before_marker : fragment.after_marker;
        return values.length ? values : marker ? [marker] : [];
      };
      const anchors = fragments.flatMap(ranges);
      if (!anchors.length) { if (action) setError("The exact range for this action is not available. Its snapshot is open."); return; }
      window.dispatchEvent(new CustomEvent("openclass:locate-history-range", { detail: {
        requestId: request, documentId: snapshot.id, expectedText: snapshot.content_text, expectedJson: snapshot.content_json,
        anchors, focus: chosen ? ranges(chosen)[0] : anchors[0],
      } }));
    }));
  }, []);

  return { selectedId: selected, error: error || (geometry?.key === key ? geometry.error : ""), activate,
    measuring: Boolean(reference && geometry?.key !== key),
    measuredSpans: geometry?.key === key ? geometry.spans : {} as Record<string, ContentActionSpan[]> };
}

export type DocumentActionNavigation = ReturnType<typeof useDocumentActionNavigation>;
