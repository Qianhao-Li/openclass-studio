"use client";

import { useMemo, useEffect, useRef, type Dispatch, type SetStateAction } from "react";
import { ApiRequestError } from "../../lib/api";
import { useStudioApi } from "../../contexts/studio-api-context";
import { createDocumentHistoryApi } from "../../lib/document-history-api";
import { canStepDocumentAction, documentActionBlockedReason } from "../../lib/document-action-permissions";
import type { CoursePackage, Lesson, RidocHistoryPage } from "../../types";
import type { AppliedCoursePackage, CoursePackageApplyOptions } from "./use-course-workspace";
import type { AutoSaveReason } from "./use-board-draft";

type Options = {
  repositoryId?: string | null;
  lesson: Lesson | null;
  revision: string | null;
  history: RidocHistoryPage | null;
  flushAutoSave: (reason: AutoSaveReason) => Promise<boolean>;
  applyCoursePackage: (course: CoursePackage, options?: CoursePackageApplyOptions) => AppliedCoursePackage;
  resetDraftToLesson: (lesson: Lesson | null) => void;
  endPreview: () => void;
  setError: Dispatch<SetStateAction<string | null>>;
  setBusyAction: Dispatch<SetStateAction<string | null>>;
};

/** A click is bound to the C the learner saw, including across an autosave. */
export function useDocumentHistory(options: Options) {
  const api = useStudioApi();
  const documentHistoryApi = useMemo(() => createDocumentHistoryApi(api.request), [api]);
  const scope = JSON.stringify([options.repositoryId ?? null, options.lesson?.id]);
  const latest = useRef({ scope, revision: options.revision, branch: options.lesson?.history_graph.current_branch });
  const pending = useRef(false);
  const sequence = useRef(0);
  const retry = useRef<{ key: string; id: string } | null>(null);
  useEffect(() => {
    latest.current = { scope, revision: options.revision, branch: options.lesson?.history_graph.current_branch };
  }, [scope, options.revision, options.lesson]);
  useEffect(() => {
    sequence.current += 1;
    return () => { sequence.current += 1; };
  }, [scope]);

  return async function step(direction: "undo" | "redo", targetNodeId: string) {
    const history = options.history;
    const lesson = options.lesson;
    if (pending.current || !lesson || !history || !options.revision) return;
    const state = history.document_history;
    if (!canStepDocumentAction(state, targetNodeId, direction)) {
      options.setError(documentActionBlockedReason(state, targetNodeId) ?? "Board history has changed. Select an available action again.");
      return;
    }
    const requestSequence = ++sequence.current;
    const current = () => sequence.current === requestSequence && latest.current.scope === scope;
    const revision = history.ridoc_revision;
    const branch = history.current_branch;
    const key = JSON.stringify([scope, revision, branch, direction, targetNodeId]);
    if (retry.current?.key !== key) retry.current = { key, id: crypto.randomUUID() };
    const mutationId = retry.current.id;
    const busyAction = `document-history-${direction}`;
    pending.current = true;
    options.setBusyAction(busyAction);
    options.setError(null);
    const apply = (course: CoursePackage) => {
      const applied = options.applyCoursePackage(course, { activeLessonId: lesson.id, rebuildMessageLessonIds: [] });
      options.resetDraftToLesson(applied.activeLesson);
      options.endPreview();
    };
    try {
      if (!(await options.flushAutoSave("preview")) || !current()) return;
      // Autosave may change both the branch and the latest C. Never substitute a new target.
      const [course, fresh] = await Promise.all([
        api.getCoursePackage(options.repositoryId),
        api.loadRidocHistory(lesson.id, { scope: "all", limit: 1, includeReplayDocuments: false }, options.repositoryId),
      ]);
      if (!current()) return;
      if (course.ridoc_revision !== fresh.ridoc_revision) {
        throw new Error("The course is changing. Wait for saving to finish and try again.");
      }
      const visible = latest.current.revision;
      if (visible !== revision && visible !== fresh.ridoc_revision) return;
      if (fresh.ridoc_revision !== revision || fresh.current_branch !== branch || !canStepDocumentAction(fresh.document_history, targetNodeId, direction)) {
        apply(course);
        options.setError("The board or history was updated. Available actions have refreshed. Select undo or redo again.");
        return;
      }
      if (latest.current.branch !== branch) return;
      if (fresh.document_history?.blocked_reason) throw new Error(fresh.document_history.blocked_reason);
      const result = await documentHistoryApi.step(lesson.id, {
        direction, target_node_id: targetNodeId, expected_branch_name: branch,
        expected_ridoc_revision: revision, mutation_id: mutationId, expected_version: fresh.expected_version,
      }, options.repositoryId);
      if (!current() || latest.current.branch !== branch) return;
      if (latest.current.revision !== revision && latest.current.revision !== result.ridoc_revision) return;
      apply(result.course_package);
      retry.current = null;
      return { fromRevision: revision, result };
    } catch (error) {
      if (!current()) return;
      if (error instanceof ApiRequestError && error.status === 409) {
        const fresh = await api.getCoursePackage(options.repositoryId).catch(() => null);
        if (!current()) return;
        if (fresh && latest.current.revision === revision && latest.current.branch === branch) apply(fresh);
        options.setError("The course was updated, so this operation stopped. Review the board and try again.");
      } else options.setError(error instanceof Error ? error.message : "The board history operation failed. Please try again.");
    } finally {
      pending.current = false;
      options.setBusyAction((value) => value === busyAction ? null : value);
    }
  };
}
