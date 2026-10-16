"use client";

import { useMemo, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { ApiRequestError } from "../../lib/api";
import { useStudioApi } from "../../contexts/studio-api-context";
import { createHistoryDeletionApi } from "../../lib/history-deletion-api";
import type { AutoSaveReason, AutoSaveStatus } from "./use-board-draft";
import type { AppliedCoursePackage, CoursePackageApplyOptions } from "./use-course-workspace";
import type { CoursePackage, RidocNodeDeletionPreview } from "../../types";

type Options = {
  repositoryId?: string | null;
  coursePackage: CoursePackage | null;
  lessonId: string | null;
  readOnly: boolean;
  isChatBusy: boolean;
  voiceActive: boolean;
  autoSaveStatus: AutoSaveStatus;
  busyAction: string | null;
  mergeActive: boolean;
  playbackActive: boolean;
  flushAutoSave: (reason: AutoSaveReason) => Promise<boolean>;
  applyCoursePackage: (next: CoursePackage, options?: CoursePackageApplyOptions) => AppliedCoursePackage;
  onDeleted: (lessonId: string) => void;
  setBusyAction: Dispatch<SetStateAction<string | null>>;
  setError: Dispatch<SetStateAction<string | null>>;
};

export function useHistoryNodeDeletion(options: Options) {
  const api = useStudioApi();
  const historyDeletionApi = useMemo(() => createHistoryDeletionApi(api.request), [api]);
  const contextKey = JSON.stringify([options.repositoryId ?? null, options.lessonId]);
  const [preview, setPreview] = useState<RidocNodeDeletionPreview | null>(null);
  const [previewContext, setPreviewContext] = useState<string | null>(null);
  const [phaseState, setPhaseState] = useState<{ context: string; value: "idle" | "preview" | "deleting" }>({ context: contextKey, value: "idle" });
  const phase = phaseState.context === contextKey ? phaseState.value : "idle";
  function setPhase(value: "idle" | "preview" | "deleting") { setPhaseState({ context: contextKey, value }); }
  const [message, setMessage] = useState<string | null>(null);
  const mutationId = useRef<string | null>(null);
  const sequence = useRef(0);
  const latest = useRef(options);
  useEffect(() => { latest.current = options; });
  useEffect(() => {
    // A late response from another lesson/repository must not replace this view.
    sequence.current += 1;
    return () => { sequence.current += 1; };
  }, [options.lessonId, options.repositoryId]);

  const enabled = !options.readOnly && options.coursePackage?.storage_kind === "ridoc";
  const disabledReason = options.isChatBusy ? "Wait for the reply to finish or stop generation first."
    : options.voiceActive || options.busyAction === "voice-connect" ? "End the voice session first."
      : options.autoSaveStatus === "saving" ? "The board is saving. Please wait."
        : options.mergeActive ? "Finish or discard the current merge first."
          : options.playbackActive ? "Exit history playback first."
            : phase !== "idle" ? "Deleting the conversation. Please wait."
              : options.busyAction ? "The current operation is still running. Please wait."
                : null;
  const visiblePreview = previewContext === contextKey && preview?.lesson_id === options.lessonId ? preview : null;

  function cancel() {
    if (phase !== "idle") return;
    sequence.current += 1;
    setPreview(null);
    setMessage(null);
    mutationId.current = null;
  }

  async function open(nodeId: string) {
    if (!enabled || !options.lessonId || disabledReason) return;
    const requestSequence = ++sequence.current;
    const lessonId = options.lessonId;
    setPhase("preview");
    setMessage(null);
    options.setError(null);
    try {
      if (!(await options.flushAutoSave("preview"))) return;
      if (requestSequence !== sequence.current) return;
      const result = await historyDeletionApi.preview(lessonId, nodeId, options.repositoryId);
      if (requestSequence !== sequence.current) return;
      mutationId.current = crypto.randomUUID();
      setPreviewContext(contextKey);
      setPreview(result);
    } catch (error) {
      if (requestSequence === sequence.current) {
        options.setError(error instanceof Error ? error.message : "Could not load the deletion preview.");
      }
    } finally {
      if (requestSequence === sequence.current) setPhase("idle");
    }
  }

  async function refreshPreview(current: RidocNodeDeletionPreview, requestSequence: number) {
    const currentPackage = await api.getCoursePackage(options.repositoryId);
    if (requestSequence !== sequence.current) return;
    options.applyCoursePackage(currentPackage, {
      activeLessonId: current.lesson_id,
      rebuildMessageLessonIds: [current.lesson_id],
    });
    options.onDeleted(current.lesson_id);
    try {
      const updated = await historyDeletionApi.preview(current.lesson_id, current.node_id, options.repositoryId);
      if (requestSequence !== sequence.current) return;
      mutationId.current = crypto.randomUUID();
      setPreview(updated);
      setMessage("The course has changed. The deletion preview has refreshed. Review it and confirm again.");
    } catch (error) {
      if (requestSequence !== sequence.current) return;
      if (error instanceof ApiRequestError && error.status === 404) {
        setPreview(null);
        options.setError("This conversation turn no longer exists. The course has refreshed.");
        return;
      }
      throw error;
    }
  }

  async function confirm() {
    if (!visiblePreview || !enabled || disabledReason || visiblePreview.blocked_reason) return;
    const current = visiblePreview;
    const requestSequence = ++sequence.current;
    setPhase("deleting");
    setMessage(null);
    try {
      if (!(await options.flushAutoSave("preview"))) return;
      if (requestSequence !== sequence.current) return;
      const live = latest.current;
      if (live.isChatBusy || live.voiceActive || live.autoSaveStatus === "saving") {
        setMessage("Wait for the current reply, voice session, or save to finish before confirming deletion.");
        return;
      }
      options.setBusyAction("delete-history-node");
      mutationId.current ??= crypto.randomUUID();
      const result = await historyDeletionApi.delete(current.lesson_id, current.node_id, {
        expected_ridoc_revision: current.ridoc_revision,
        mutation_id: mutationId.current,
        ...(current.expected_version === undefined ? {} : { expected_version: current.expected_version }),
      }, options.repositoryId);
      if (requestSequence !== sequence.current) return;
      options.applyCoursePackage(result.course_package, {
        activeLessonId: current.lesson_id,
        rebuildMessageLessonIds: [current.lesson_id],
        deletedHistoryNodeIds: [result.deleted_node_id],
      });
      options.onDeleted(current.lesson_id);
      setPreview(null);
      mutationId.current = null;
    } catch (error) {
      if (requestSequence !== sequence.current) return;
      if (error instanceof ApiRequestError && error.status === 409) {
        try { await refreshPreview(current, requestSequence); }
        catch (refreshError) {
          setMessage(refreshError instanceof Error ? refreshError.message : "Could not refresh the course. Please try again.");
        }
      } else {
        // Retain the mutation identity when a response is lost, so retries cannot delete twice.
        setMessage(error instanceof Error ? error.message : "Deletion failed. Please try again.");
      }
    } finally {
      options.setBusyAction((current) => current === "delete-history-node" ? null : current);
      if (requestSequence === sequence.current) setPhase("idle");
    }
  }

  return { enabled, disabledReason, preview: visiblePreview, message, phase, open, cancel, confirm };
}
