"use client";

import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";

import type { AppliedCoursePackage, CoursePackageApplyOptions } from "./use-course-workspace";
import type { AutoSaveReason } from "./use-board-draft";
import { ApiRequestError } from "../../lib/api";
import { useStudioApi } from "../../contexts/studio-api-context";
import { sameEventOrder } from "../../lib/merge-event-order";
import type {
  AIModelSelection,
  BoardDocument,
  CoursePackage,
  Lesson,
  LessonMergeResolution,
  LessonMergeSessionView,
} from "../../types";

const MERGE_AUTO_SAVE_DELAY_MS = 900;

type UseLessonMergeOptions = {
  disabled?: boolean;
  repositoryId?: string | null;
  storageKind?: "legacy" | "ridoc";
  ridocRevision?: string | null;
  activeLesson: Lesson | null;
  selectedTextModel: AIModelSelection;
  flushBoardAutoSave: (reason: AutoSaveReason) => Promise<boolean>;
  applyCoursePackage: (
    nextPackage: CoursePackage,
    options?: CoursePackageApplyOptions
  ) => AppliedCoursePackage;
  setError: Dispatch<SetStateAction<string | null>>;
  setBusyAction: Dispatch<SetStateAction<string | null>>;
};

export function useLessonMerge({
  disabled = false,
  repositoryId,
  storageKind,
  ridocRevision,
  activeLesson,
  selectedTextModel,
  flushBoardAutoSave,
  applyCoursePackage,
  setError,
  setBusyAction,
}: UseLessonMergeOptions) {
  const api = useStudioApi();
  const [session, setSession] = useState<LessonMergeSessionView | null>(null);
  const [draftDocument, setDraftDocument] = useState<BoardDocument | null>(null);
  const [isDraftDirty, setIsDraftDirty] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [isAIProposing, setIsAIProposing] = useState(false);
  const sessionRef = useRef<LessonMergeSessionView | null>(null);
  const draftDocumentRef = useRef<BoardDocument | null>(null);
  const dirtyRef = useRef(false);
  const draftRevisionRef = useRef(0);
  const saveTimerRef = useRef<number | null>(null);
  const saveInFlightRef = useRef<Promise<boolean> | null>(null);
  const flushDraftRef = useRef<() => Promise<boolean>>(async () => true);
  const aiAbortRef = useRef<AbortController | null>(null);

  const replaceSession = useCallback((next: LessonMergeSessionView | null) => {
    sessionRef.current = next;
    draftDocumentRef.current = next?.draft_document ?? null;
    dirtyRef.current = false;
    setSession(next);
    setDraftDocument(next?.draft_document ?? null);
    setIsDraftDirty(false);
  }, []);

  const clearSaveTimer = useCallback(() => {
    if (saveTimerRef.current !== null) {
      window.clearTimeout(saveTimerRef.current);
      saveTimerRef.current = null;
    }
  }, []);

  const flushDraft = useCallback(async (): Promise<boolean> => {
    clearSaveTimer();
    if (saveInFlightRef.current) {
      if (!(await saveInFlightRef.current)) return false;
      if (!dirtyRef.current) {
        return true;
      }
    }
    const currentSession = sessionRef.current;
    const document = draftDocumentRef.current;
    if (!currentSession || !document || !dirtyRef.current) {
      return true;
    }
    const savedRevision = draftRevisionRef.current;
    dirtyRef.current = false;
    let saved = false;
    const request = (async () => {
      try {
        const next = await api.updateMergeSession(currentSession.lesson_id, currentSession.id, {
          expected_version: currentSession.version,
          ...(currentSession.event_plan
            ? { ordered_node_ids: currentSession.event_plan.ordered_node_ids }
            : { draft_document: document }),
        }, repositoryId);
        if (sessionRef.current?.id !== currentSession.id) return false;
        if (draftRevisionRef.current === savedRevision) {
          replaceSession(next);
        } else {
          const localDocument = draftDocumentRef.current ?? next.draft_document;
          const localOrder = sessionRef.current.event_plan?.ordered_node_ids;
          const withLocalDraft = {
            ...next,
            draft_document: localDocument,
            ...(next.event_plan && localOrder
              ? { event_plan: { ...next.event_plan, ordered_node_ids: localOrder } }
              : {}),
          };
          sessionRef.current = withLocalDraft;
          setSession(withLocalDraft);
        }
        saved = true;
        return true;
      } catch (error) {
        if (sessionRef.current?.id !== currentSession.id) return false;
        dirtyRef.current = true;
        setIsDraftDirty(true);
        setError(error instanceof Error ? error.message : "Failed to save merge draft");
        if (error instanceof ApiRequestError && error.status === 409) {
          try {
            const refreshed = await api.getMergeSession(currentSession.lesson_id, currentSession.id, repositoryId);
            if (sessionRef.current?.id === currentSession.id) replaceSession(refreshed);
          } catch {
            const current = sessionRef.current;
            if (current?.id === currentSession.id) {
              const stale = { ...current, status: "stale" as const };
              sessionRef.current = stale;
              setSession(stale);
            }
          }
        }
        return false;
      }
    })();
    saveInFlightRef.current = request;
    try {
      return await request;
    } finally {
      saveInFlightRef.current = null;
      if (saved && dirtyRef.current) {
        saveTimerRef.current = window.setTimeout(() => {
          saveTimerRef.current = null;
          void flushDraftRef.current();
        }, MERGE_AUTO_SAVE_DELAY_MS);
      }
    }
  }, [api, clearSaveTimer, replaceSession, repositoryId, setError]);

  useEffect(() => {
    flushDraftRef.current = disabled ? async () => true : flushDraft;
  }, [disabled, flushDraft]);

  const handleDocumentChange = useCallback(
    (document: BoardDocument) => {
      if (!sessionRef.current || sessionRef.current.event_plan) {
        return;
      }
      draftRevisionRef.current += 1;
      draftDocumentRef.current = document;
      dirtyRef.current = true;
      setDraftDocument(document);
      setIsDraftDirty(true);
      clearSaveTimer();
      saveTimerRef.current = window.setTimeout(() => {
        saveTimerRef.current = null;
        void flushDraft();
      }, MERGE_AUTO_SAVE_DELAY_MS);
    },
    [clearSaveTimer, flushDraft]
  );

  const handleOrderChange = useCallback((nodeIds: string[]) => {
    const current = sessionRef.current;
    if (disabled || !current?.event_plan || current.status === "stale") return;
    if (sameEventOrder(current.event_plan.ordered_node_ids, nodeIds)) return;
    const next = { ...current, event_plan: { ...current.event_plan, ordered_node_ids: [...nodeIds] } };
    draftRevisionRef.current += 1;
    sessionRef.current = next;
    dirtyRef.current = true;
    setSession(next);
    setIsDraftDirty(true);
    clearSaveTimer();
    saveTimerRef.current = window.setTimeout(() => {
      saveTimerRef.current = null;
      void flushDraft();
    }, MERGE_AUTO_SAVE_DELAY_MS);
  }, [clearSaveTimer, disabled, flushDraft]);

  useEffect(() => {
    const lessonId = activeLesson?.id;
    let cancelled = false;
    clearSaveTimer();
    aiAbortRef.current?.abort();
    const timer = window.setTimeout(() => {
      if (cancelled) {
        return;
      }
      replaceSession(null);
      if (disabled || !lessonId) {
        return;
      }
      setIsLoading(true);
      void api
        .getActiveMergeSession(lessonId, repositoryId)
        .then((next) => {
          if (!cancelled) {
            replaceSession(next);
          }
        })
        .catch((error) => {
          if (!cancelled) {
            setError(error instanceof Error ? error.message : "Failed to read merge draft");
          }
        })
        .finally(() => {
          if (!cancelled) {
            setIsLoading(false);
          }
        });
    }, 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [api, activeLesson?.id, clearSaveTimer, disabled, replaceSession, repositoryId, setError]);

  const startMerge = useCallback(
    async (sourceBranchName: string) => {
      if (!activeLesson || sessionRef.current) {
        return;
      }
      if (!(await flushBoardAutoSave("branch"))) {
        return;
      }
      setBusyAction("merge-create");
      try {
        let mergeLesson = activeLesson;
        const pendingBranch = mergeLesson.history_graph.branches[mergeLesson.history_graph.current_branch];
        if (storageKind !== "ridoc" && pendingBranch?.document_redo_commit_ids?.length) {
          const branchedPackage = await api.ensureDocumentHistoryBranch(
            mergeLesson.id,
            {
              expected_branch_name: pendingBranch.name,
              expected_head_commit_id: pendingBranch.head_commit_id,
              expected_document_cursor_commit_id:
                pendingBranch.document_cursor_commit_id || pendingBranch.head_commit_id,
            },
            repositoryId
          );
          const branched = applyCoursePackage(branchedPackage, { activeLessonId: mergeLesson.id });
          if (!branched.activeLesson) {
            throw new Error("The branched lesson is unavailable");
          }
          mergeLesson = branched.activeLesson;
        }
        const next = await api.createMergeSession(
          mergeLesson.id,
          sourceBranchName,
          "manual",
          selectedTextModel,
          repositoryId
        );
        replaceSession(next);
        setError(null);
      } catch (error) {
        setError(error instanceof Error ? error.message : "Failed to create merge draft");
      } finally {
        setBusyAction(null);
      }
    },
    [api, activeLesson, applyCoursePackage, flushBoardAutoSave, replaceSession, repositoryId, selectedTextModel, setBusyAction, setError, storageKind]
  );

  const resolveConflict = useCallback(
    async (conflictId: string, resolution: LessonMergeResolution, customValue?: unknown) => {
      if (!(await flushDraft())) {
        return;
      }
      const current = sessionRef.current;
      if (!current || current.event_plan) {
        return;
      }
      setBusyAction("merge-resolve");
      try {
        const next = await api.updateMergeSession(current.lesson_id, current.id, {
          expected_version: current.version,
          resolutions: [{ conflict_id: conflictId, resolution, custom_value: customValue }],
        }, repositoryId);
        replaceSession(next);
        setError(null);
      } catch (error) {
        setError(error instanceof Error ? error.message : "Saving conflict resolution failed");
      } finally {
        setBusyAction(null);
      }
    },
    [api, flushDraft, replaceSession, repositoryId, setBusyAction, setError]
  );

  const proposeWithAI = useCallback(async () => {
    if (!(await flushDraft())) {
      return;
    }
    const current = sessionRef.current;
    if (!current || current.event_plan || isAIProposing) {
      return;
    }
    const controller = new AbortController();
    aiAbortRef.current = controller;
    setIsAIProposing(true);
    setBusyAction("merge-ai");
    try {
      const next = await api.streamMergeProposal(
        current.lesson_id,
        current.id,
        current.version,
        {
          onAgentActivity: (activity) => {
            setSession((value) =>
              value ? { ...value, agent_activity: [...value.agent_activity, activity] } : value
            );
          },
          onFinal: replaceSession,
        },
        { signal: controller.signal, repositoryId }
      );
      replaceSession(next);
      setError(null);
    } catch (error) {
      if (!controller.signal.aborted) {
        setError(error instanceof Error ? error.message : "AI merge failed");
        try {
          replaceSession(await api.getMergeSession(current.lesson_id, current.id, repositoryId));
        } catch {
          // Keep the last durable view if the refresh also fails.
        }
      }
    } finally {
      aiAbortRef.current = null;
      setIsAIProposing(false);
      setBusyAction(null);
    }
  }, [api, flushDraft, isAIProposing, replaceSession, repositoryId, setBusyAction, setError]);

  const cancelAI = useCallback(() => {
    aiAbortRef.current?.abort();
  }, []);

  const recompute = useCallback(async () => {
    clearSaveTimer();
    if (saveInFlightRef.current) await saveInFlightRef.current;
    const current = sessionRef.current;
    if (!current) {
      return;
    }
    setBusyAction("merge-recompute");
    try {
      const next = await api.recomputeMergeSession(
        current.lesson_id,
        current.id,
        current.version,
        selectedTextModel,
        repositoryId
      );
      replaceSession(next);
      setError(null);
    } catch (error) {
      setError(error instanceof Error ? error.message : "Recalculation of merge draft failed");
    } finally {
      setBusyAction(null);
    }
  }, [api, clearSaveTimer, replaceSession, repositoryId, selectedTextModel, setBusyAction, setError]);

  const abandon = useCallback(async () => {
    const current = sessionRef.current;
    if (!current) {
      return;
    }
    clearSaveTimer();
    aiAbortRef.current?.abort();
    if (saveInFlightRef.current) await saveInFlightRef.current;
    const durableVersion = sessionRef.current?.id === current.id ? sessionRef.current.version : current.version;
    setBusyAction("merge-abandon");
    try {
      const contributionId = current.audit.lesson_contribution_id;
      if (typeof contributionId === "string" && contributionId) {
        const contribution = await api.getLessonContribution(contributionId);
        await api.returnLessonContributionForChanges(contributionId, contribution.version);
      } else {
        await api.abandonMergeSession(current.lesson_id, current.id, durableVersion, repositoryId);
      }
      replaceSession(null);
      setError(null);
    } catch (error) {
      setError(error instanceof Error ? error.message : "Abandoning merge draft failed");
    } finally {
      setBusyAction(null);
    }
  }, [api, clearSaveTimer, replaceSession, repositoryId, setBusyAction, setError]);

  const submit = useCallback(async () => {
    if (!(await flushDraft())) {
      return;
    }
    const current = sessionRef.current;
    if (!current || current.status === "stale" || current.event_plan?.blockers.length || current.conflicts.some((conflict) => !conflict.resolved)) {
      return;
    }
    setBusyAction("merge-submit");
    try {
      const nextPackage = await api.submitMergeSession(
        current.lesson_id,
        current.id,
        current.version,
        repositoryId,
        storageKind === "ridoc" ? ridocRevision : null
      );
      applyCoursePackage(nextPackage, {
        activeLessonId: current.lesson_id,
        rebuildMessageLessonIds: [current.lesson_id],
      });
      replaceSession(null);
      setError(null);
      const contributionId = current.audit.lesson_contribution_id;
      if (typeof contributionId === "string" && contributionId) {
        window.location.assign(`/contributions/${encodeURIComponent(contributionId)}`);
      }
    } catch (error) {
      setError(error instanceof Error ? error.message : "Commit merge failed");
      try {
        replaceSession(await api.getMergeSession(current.lesson_id, current.id, repositoryId));
      } catch {
        // The original submit error remains the actionable message.
      }
    } finally {
      setBusyAction(null);
    }
  }, [api, applyCoursePackage, flushDraft, replaceSession, repositoryId, ridocRevision, setBusyAction, setError, storageKind]);

  return {
    session,
    draftDocument,
    isActive: Boolean(session),
    isDraftDirty,
    isLoading,
    isAIProposing,
    startMerge,
    handleDocumentChange,
    handleOrderChange,
    flushDraft,
    resolveConflict,
    proposeWithAI,
    cancelAI,
    recompute,
    abandon,
    submit,
  };
}
