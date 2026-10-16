"use client";

import { useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from "react";

import { useStudioApi } from "../../contexts/studio-api-context";
import { useCoursePrReview } from "../../contexts/course-pr-review-context";
import { coursePrNodeCommit } from "../../lib/course-pr-history-view";
import { useDocumentHistory } from "./use-document-history";
import {
  currentHeadCommitId,
  getLessonCommit,
  nextBranchName,
} from "../../components/course-studio/history-utils";
import type { AutoSaveReason } from "./use-board-draft";
import type { AppliedCoursePackage, CoursePackageApplyOptions } from "./use-course-workspace";
import type { BoardDocument, CommitRecord, CoursePackage, Lesson, RidocHistoryPage } from "../../types";

type UseLessonHistoryOptions = {
  repositoryId?: string | null;
  storageKind?: "legacy" | "ridoc";
  ridocRevision?: string | null;
  activeLesson: Lesson | null;
  flushAutoSave: (reason: AutoSaveReason) => Promise<boolean>;
  resetDraftToLesson: (lesson: Lesson | null) => void;
  setPreviewDocument: (document: BoardDocument) => void;
  applyCoursePackage: (nextPackage: CoursePackage, options?: CoursePackageApplyOptions) => AppliedCoursePackage;
  setError: Dispatch<SetStateAction<string | null>>;
  setBusyAction: Dispatch<SetStateAction<string | null>>;
};

export function useLessonHistory({
  repositoryId,
  storageKind = "legacy",
  ridocRevision = null,
  activeLesson,
  flushAutoSave,
  resetDraftToLesson,
  setPreviewDocument,
  applyCoursePackage,
  setError,
  setBusyAction,
}: UseLessonHistoryOptions) {
  const api = useStudioApi();
  const prComparison = useCoursePrReview()?.review.comparison;
  const [previewCommitId, setPreviewCommitId] = useState<string | null>(null);
  const [chatPreviewSelection, setChatPreviewSelection] = useState<{ scope: string; id: string } | null>(null);
  const [documentPreviewOnly, setDocumentPreviewOnly] = useState(false);
  const [newBranchName, setNewBranchName] = useState("");
  const [loadedRidocHistory, setLoadedRidocHistory] = useState<RidocHistoryPage | null>(null);
  const historyRequestSequenceRef = useRef(0);
  const previewRequestSequenceRef = useRef(0);
  const chatPreviewRequestSequenceRef = useRef(0);
  useEffect(() => { ++previewRequestSequenceRef.current; ++chatPreviewRequestSequenceRef.current; }, [activeLesson?.id, activeLesson?.history_graph.current_branch]);
  const ridocHistory: RidocHistoryPage | null =
    storageKind === "ridoc" &&
    activeLesson !== null &&
    Boolean(ridocRevision) &&
    loadedRidocHistory?.lesson_id === activeLesson.id &&
    loadedRidocHistory.ridoc_revision === ridocRevision
      ? loadedRidocHistory
      : null;
  const ridocHistorySyncing =
    storageKind === "ridoc" &&
    Boolean(ridocRevision) &&
    !ridocHistory;

  useEffect(() => {
    if (storageKind !== "ridoc" || !activeLesson?.id || !ridocRevision) {
      historyRequestSequenceRef.current += 1;
      return;
    }
    const requestSequence = ++historyRequestSequenceRef.current;
    const loadAllHistoryPages = async () => {
      let firstPage: RidocHistoryPage | null = null;
      let cursor: string | undefined;
      const seenCursors = new Set<string>();
      const seenNodeIds = new Set<string>();
      const nodes: RidocHistoryPage["nodes"] = [];
      const replayDocuments: Record<string, BoardDocument> = {};

      do {
        const page = await api.loadRidocHistory(activeLesson.id, {
          scope: "all",
          cursor,
          limit: 300,
          includeReplayDocuments: firstPage === null,
        }, repositoryId);
        if (requestSequence !== historyRequestSequenceRef.current) return;
        if (page.lesson_id !== activeLesson.id || page.ridoc_revision !== ridocRevision) return;
        if (firstPage && (page.current_branch !== firstPage.current_branch ||
          JSON.stringify(page.learning_node_ids) !== JSON.stringify(firstPage.learning_node_ids))) {
          throw new Error("RIDOC history learning order changed while loading");
        }
        firstPage ??= page;
        Object.assign(replayDocuments, page.replay_documents);
        page.nodes.forEach((node) => {
          if (!seenNodeIds.has(node.id)) {
            seenNodeIds.add(node.id);
            nodes.push(node);
          }
        });
        const nextCursor = page.next_cursor ?? undefined;
        if (!nextCursor) {
          cursor = undefined;
          break;
        }
        if (seenCursors.has(nextCursor)) {
          throw new Error("RIDOC history pagination returned a repeated cursor");
        }
        seenCursors.add(nextCursor);
        cursor = nextCursor;
      } while (cursor);

      if (firstPage && requestSequence === historyRequestSequenceRef.current) {
        const learningIds = firstPage.learning_node_ids ?? [];
        if (new Set(learningIds).size !== learningIds.length || learningIds.some((id) => !seenNodeIds.has(id))) {
          throw new Error("RIDOC history learning order contains unavailable events");
        }
        setLoadedRidocHistory({ ...firstPage, nodes, replay_documents: replayDocuments, next_cursor: null });
      }
    };
    void loadAllHistoryPages().catch((historyError) => {
      if (requestSequence !== historyRequestSequenceRef.current) return;
      setError(historyError instanceof Error ? historyError.message : "Failed to load RIDOC history");
    });
    return () => {
      if (historyRequestSequenceRef.current === requestSequence) {
        historyRequestSequenceRef.current += 1;
      }
    };
  }, [api, activeLesson?.id, repositoryId, ridocRevision, setError, storageKind]);

  const previewLesson = useMemo(() => {
    if (!activeLesson || !prComparison) return activeLesson;
    const commits = new Map(activeLesson.history_graph.commits.map((commit) => [commit.id, commit]));
    for (const node of prComparison.diff_nodes) {
      if (!commits.has(node.id)) { const commit = coursePrNodeCommit(node); if (commit) commits.set(node.id, commit); }
    }
    return { ...activeLesson, history_graph: { ...activeLesson.history_graph, commits: [...commits.values()] } };
  }, [activeLesson, prComparison]);
  const previewCommit = useMemo(
    () =>
      previewCommitId && previewLesson
        ? previewLesson.history_graph.commits.find((commit) => commit.id === previewCommitId) ?? null
        : null,
    [previewLesson, previewCommitId]
  );
  const activeHeadCommit = useMemo(
    () => (activeLesson ? getLessonCommit(activeLesson, currentHeadCommitId(activeLesson)) : null),
    [activeLesson]
  );
  const chatPreviewCommit = useMemo(() => chatPreviewSelection?.scope === `${activeLesson?.id}:${activeLesson?.history_graph.current_branch}`
    ? previewLesson?.history_graph.commits.find((commit) => commit.id === chatPreviewSelection.id) ?? null : null,
  [activeLesson?.id, activeLesson?.history_graph.current_branch, chatPreviewSelection, previewLesson]);
  const isPreviewMode = Boolean(previewCommit);
  const stepRidocDocumentHistory = useDocumentHistory({
    repositoryId, lesson: activeLesson, revision: ridocRevision, history: ridocHistory,
    flushAutoSave, applyCoursePackage, resetDraftToLesson, setError, setBusyAction,
    endPreview: () => setPreviewCommitId(null),
  });

  async function loadAuthoritativeHistoryContext(lessonId: string) {
    if (storageKind !== "ridoc") {
      if (!activeLesson || activeLesson.id !== lessonId) {
        throw new Error("The active lesson is no longer available");
      }
      return { lesson: activeLesson, revision: ridocRevision };
    }
    const currentPackage = await api.getCoursePackage(repositoryId);
    const currentLesson = currentPackage.lessons.find((lesson) => lesson.id === lessonId);
    if (
      !currentLesson ||
      currentPackage.storage_kind !== "ridoc" ||
      !currentPackage.ridoc_revision
    ) {
      throw new Error("The current RIDOC lesson is unavailable");
    }
    return { lesson: currentLesson, revision: currentPackage.ridoc_revision };
  }

  async function handleCreateBranch(fromCommitId = previewCommitId, branchNameOverride?: string) {
    if (!activeLesson) {
      return;
    }
    if (!fromCommitId && !(await flushAutoSave("branch"))) {
      return;
    }
    setBusyAction("branch");
    try {
      const authoritative = await loadAuthoritativeHistoryContext(activeLesson.id);
      if (
        fromCommitId &&
        !authoritative.lesson.history_graph.commits.some((commit) => commit.id === fromCommitId)
      ) {
        throw new Error("The selected branch point is no longer available");
      }
      const branchName = (branchNameOverride ?? newBranchName.trim()).trim();
      const finalBranchName = branchName || nextBranchName(authoritative.lesson);
      const nextPackage = await api.createBranch(
        authoritative.lesson.id,
        finalBranchName,
        fromCommitId,
        repositoryId,
        storageKind === "ridoc" ? authoritative.revision : null
      );
      applyCoursePackage(nextPackage, {
        activeLessonId: authoritative.lesson.id,
        rebuildMessageLessonIds: fromCommitId ? [authoritative.lesson.id] : undefined,
      });
      setPreviewCommitId(null);
      setNewBranchName("");
    } catch (branchError) {
      setError(branchError instanceof Error ? branchError.message : "Failed to create branch");
    } finally {
      setBusyAction(null);
    }
  }

  async function handlePreviewCommit(commit: CommitRecord, documentOnly = false) {
    const sequence = ++previewRequestSequenceRef.current;
    const chatSequence = documentOnly ? null : ++chatPreviewRequestSequenceRef.current;
    if (!(await flushAutoSave("preview"))) {
      return;
    }
    if (chatSequence !== null && chatSequence === chatPreviewRequestSequenceRef.current) {
      setChatPreviewSelection({ scope: `${activeLesson?.id}:${activeLesson?.history_graph.current_branch}`, id: commit.id });
    }
    if (sequence === previewRequestSequenceRef.current) {
      setDocumentPreviewOnly(documentOnly);
      setPreviewCommitId(commit.id);
      setPreviewDocument(commit.snapshot);
    }
  }

  function exitChatPreviewMode() {
    ++chatPreviewRequestSequenceRef.current;
    setChatPreviewSelection(null);
    if (!documentPreviewOnly && previewCommitId === chatPreviewCommit?.id) exitPreviewMode();
  }

  function exitPreviewMode() {
    ++previewRequestSequenceRef.current;
    if (!documentPreviewOnly) { ++chatPreviewRequestSequenceRef.current; setChatPreviewSelection(null); }
    if (!activeLesson || !previewCommitId) {
      return;
    }
    setPreviewCommitId(null);
    resetDraftToLesson(activeLesson);
  }

  async function handleCreateBranchFromCommit(commit: CommitRecord) {
    if (!activeLesson) {
      return;
    }
    if (!(await flushAutoSave("branch"))) {
      return;
    }
    setPreviewCommitId(commit.id);
    setPreviewDocument(commit.snapshot);
    await handleCreateBranch(commit.id, newBranchName.trim() || nextBranchName(activeLesson));
  }

  async function handleSwitchBranch(branchName: string) {
    if (!activeLesson) {
      return;
    }
    if (!(await flushAutoSave("switch-branch"))) {
      return;
    }
    setBusyAction("switch-branch");
    try {
      const authoritative = await loadAuthoritativeHistoryContext(activeLesson.id);
      if (!authoritative.lesson.history_graph.branches[branchName]) {
        throw new Error("The selected branch is no longer available");
      }
      const nextPackage = await api.switchBranch(
        authoritative.lesson.id,
        branchName,
        repositoryId,
        storageKind === "ridoc" ? authoritative.revision : null
      );
      applyCoursePackage(nextPackage, {
        activeLessonId: authoritative.lesson.id,
        rebuildMessageLessonIds: [authoritative.lesson.id],
      });
      setPreviewCommitId(null);
    } catch (branchError) {
      setError(branchError instanceof Error ? branchError.message : "Failed to switch branches");
    } finally {
      setBusyAction(null);
    }
  }

  async function handleSetChatCommitVisibility(commitId: string, visibility: "hidden" | "visible") {
    if (!activeLesson) {
      return;
    }
    setBusyAction("chat-visibility");
    try {
      const nextPackage = await api.setChatCommitVisibility(activeLesson.id, commitId, visibility, repositoryId);
      applyCoursePackage(nextPackage, {
        activeLessonId: activeLesson.id,
        rebuildMessageLessonIds: [activeLesson.id],
      });
    } catch (visibilityError) {
      setError(
        visibilityError instanceof Error ? visibilityError.message : "Failed to update chat visibility"
      );
    } finally {
      setBusyAction(null);
    }
  }

  async function handleStepDocumentHistory(direction: "undo" | "redo", targetCommitId: string) {
    if (storageKind === "ridoc") return stepRidocDocumentHistory(direction, targetCommitId);
    if (!activeLesson || !(await flushAutoSave("manual"))) {
      return;
    }
    setBusyAction(`document-history-${direction}`);
    try {
      const freshPackage = await api.getCoursePackage(repositoryId);
      const freshLesson = freshPackage.lessons.find((lesson) => lesson.id === activeLesson.id);
      if (!freshLesson) {
        throw new Error("The active lesson is no longer available");
      }
      const branchName = freshLesson.history_graph.current_branch;
      const branch = freshLesson.history_graph.branches[branchName];
      const nextPackage = await api.stepDocumentHistory(
        freshLesson.id,
        {
          direction,
          target_commit_id: targetCommitId,
          expected_branch_name: branchName,
          expected_head_commit_id: branch.head_commit_id,
          expected_document_cursor_commit_id:
            branch.document_cursor_commit_id || branch.head_commit_id,
        },
        repositoryId
      );
      const result = applyCoursePackage(nextPackage, {
        activeLessonId: freshLesson.id,
        rebuildMessageLessonIds: [],
      });
      resetDraftToLesson(result.activeLesson);
      setPreviewCommitId(null);
    } catch (stepError) {
      setError(stepError instanceof Error ? stepError.message : "Failed to move document history");
    } finally {
      setBusyAction(null);
    }
  }

  async function handleEnsureDocumentHistoryBranch(): Promise<Lesson | null> {
    if (!activeLesson) {
      return null;
    }
    // RIDOC creates the continuation branch atomically with the next successful outcome.
    if (storageKind === "ridoc") return activeLesson;
    const branch = activeLesson.history_graph.branches[activeLesson.history_graph.current_branch];
    if (!branch?.document_redo_commit_ids?.length) {
      return activeLesson;
    }
    try {
      const nextPackage = await api.ensureDocumentHistoryBranch(
        activeLesson.id,
        {
          expected_branch_name: branch.name,
          expected_head_commit_id: branch.head_commit_id,
          expected_document_cursor_commit_id:
            branch.document_cursor_commit_id || branch.head_commit_id,
        },
        repositoryId
      );
      const result = applyCoursePackage(nextPackage, {
        activeLessonId: activeLesson.id,
        rebuildMessageLessonIds: [],
      });
      resetDraftToLesson(result.activeLesson);
      return result.activeLesson;
    } catch (branchError) {
      setError(branchError instanceof Error ? branchError.message : "Failed to create a document branch");
      return null;
    }
  }

  return {
    previewLesson,
    previewCommitId,
    previewCommit,
    chatPreviewCommit,
    exitChatPreviewMode,
    activeHeadCommit,
    isPreviewMode,
    newBranchName,
    setPreviewCommitId,
    setNewBranchName,
    handleCreateBranch,
    handlePreviewCommit,
    exitPreviewMode,
    handleCreateBranchFromCommit,
    handleSwitchBranch,
    handleSetChatCommitVisibility,
    handleStepDocumentHistory,
    handleEnsureDocumentHistoryBranch,
    ridocHistory,
    ridocHistorySyncing,
  };
}
