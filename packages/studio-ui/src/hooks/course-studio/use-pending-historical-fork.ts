"use client";

import { useCallback, useEffect, useRef, type Dispatch, type SetStateAction } from "react";

import type {
  ChatMessage,
  LessonComposerState,
  PendingHistoricalFork,
} from "../../components/course-studio/history-utils";
import { buildLessonMessagesFromHistory } from "../../components/course-studio/history-utils";
import type { AutoSaveReason } from "./use-board-draft";
import type {
  AppliedCoursePackage,
  CoursePackageApplyOptions,
} from "./use-course-workspace";
import { useStudioApi } from "../../contexts/studio-api-context";
import type { BoardDocument, CoursePackage, Lesson, RidocHistoryNode } from "../../types";

type UsePendingHistoricalForkOptions = {
  repositoryId?: string | null;
  storageKind?: "legacy" | "ridoc";
  activeLesson: Lesson | null;
  activeMessages: ChatMessage[];
  activeComposerState: LessonComposerState;
  flushAutoSave: (reason: AutoSaveReason) => Promise<boolean>;
  applyCoursePackage: (
    nextPackage: CoursePackage,
    options?: CoursePackageApplyOptions
  ) => AppliedCoursePackage;
  updateLessonComposerState: (
    lessonId: string,
    updater: (current: LessonComposerState) => LessonComposerState
  ) => void;
  enterHistoricalFork: (
    fork: PendingHistoricalFork,
    document: BoardDocument
  ) => void;
  prepareReturnToCurrentVersion: () => boolean;
  setError: Dispatch<SetStateAction<string | null>>;
  setBusyAction: Dispatch<SetStateAction<string | null>>;
};

async function loadCompleteHistory(api: ReturnType<typeof useStudioApi>, lessonId: string) {
  const nodes: RidocHistoryNode[] = [];
  let cursor: string | undefined;
  let revision = "";
  do {
    const page = await api.loadRidocHistory(lessonId, {
      scope: "all",
      cursor,
      limit: 300,
    });
    if (revision && revision !== page.ridoc_revision) {
      throw new Error("RIDOC history changed while preparing the branch");
    }
    revision = page.ridoc_revision;
    nodes.push(...page.nodes);
    cursor = page.next_cursor ?? undefined;
  } while (cursor);
  return { nodes, revision };
}

export function usePendingHistoricalFork({
  repositoryId,
  storageKind = "legacy",
  activeLesson,
  activeMessages,
  activeComposerState,
  flushAutoSave,
  applyCoursePackage,
  updateLessonComposerState,
  enterHistoricalFork,
  prepareReturnToCurrentVersion,
  setError,
  setBusyAction,
}: UsePendingHistoricalForkOptions) {
  const api = useStudioApi();
  const pendingFork = activeComposerState.pendingHistoricalFork ?? null;
  const activeLessonIdRef = useRef(activeLesson?.id ?? null);
  const operationSequenceRef = useRef(0);

  useEffect(() => {
    activeLessonIdRef.current = activeLesson?.id ?? null;
    operationSequenceRef.current += 1;
  }, [activeLesson?.id]);

  const clearPendingFork = useCallback(
    (lessonId: string) => {
      updateLessonComposerState(lessonId, (current) => ({
        ...current,
        pendingHistoricalFork: null,
      }));
    },
    [updateLessonComposerState]
  );

  const preparePendingFork = useCallback(
    async (
      requestedNodeId: string,
      preferredSourceMessage?: ChatMessage,
      expectedContinuationNodeId?: string
    ) => {
      if (
        repositoryId ||
        storageKind !== "ridoc" ||
        !activeLesson ||
        !requestedNodeId
      ) {
        return;
      }
      if (pendingFork) {
        setError("Return to the current version before choosing another branch point.");
        return;
      }
      const lessonId = activeLesson.id;
      const operationSequence = ++operationSequenceRef.current;
      const operationIsCurrent = () =>
        operationSequenceRef.current === operationSequence && activeLessonIdRef.current === lessonId;
      setBusyAction("prepare-history-fork");
      setError(null);
      try {
        if (!(await flushAutoSave("branch"))) {
          return;
        }
        if (!operationIsCurrent()) return;
        const currentPackage = await api.getCoursePackage(repositoryId);
        if (!operationIsCurrent()) return;
        const currentLesson = currentPackage.lessons.find((lesson) => lesson.id === lessonId);
        if (!currentLesson || currentPackage.storage_kind !== "ridoc" || !currentPackage.ridoc_revision) {
          throw new Error("The current RIDOC lesson is unavailable");
        }
        applyCoursePackage(currentPackage, {
          activeLessonId: lessonId,
          rebuildMessageLessonIds: [lessonId],
        });
        const history = await loadCompleteHistory(api, lessonId);
        if (!operationIsCurrent()) return;
        if (history.revision !== currentPackage.ridoc_revision) {
          throw new Error("RIDOC history changed while preparing the branch");
        }
        const requestedNode = history.nodes.find((node) => node.id === requestedNodeId);
        const sourceNode = requestedNode?.kind === "M"
          ? requestedNode
          : history.nodes.find(
              (node) => node.kind === "M" && node.continuation_node_id === requestedNode?.id
            );
        if (!requestedNode || !sourceNode) {
          throw new Error(
            "This history node cannot start an atomic event branch yet. Choose a complete AI response or its linked document change."
          );
        }
        const sourceConversation = buildLessonMessagesFromHistory(currentLesson, sourceNode.id);
        const sourceMessage =
          preferredSourceMessage?.role === "assistant" && preferredSourceMessage.commitId === sourceNode.id
            ? preferredSourceMessage
            : [...sourceConversation]
                .reverse()
                .find((message) => message.role === "assistant" && message.commitId === sourceNode.id);
        if (!sourceMessage) {
          throw new Error("This response no longer has a complete conversation context");
        }
        const continuationNodeId = sourceNode.continuation_node_id || sourceNode.id;
        if (expectedContinuationNodeId && continuationNodeId !== expectedContinuationNodeId) {
          throw new Error("RIDOC history changed while resolving the branch continuation");
        }
        const snapshot = await api.loadRidocHistoryDocument(lessonId, continuationNodeId);
        if (!operationIsCurrent()) return;
        if (snapshot.ridoc_revision !== currentPackage.ridoc_revision) {
          throw new Error("RIDOC history changed while loading the branch snapshot");
        }
        const originBranch = currentLesson.history_graph.branches[currentLesson.history_graph.current_branch];
        if (!originBranch) {
          throw new Error("The current branch is unavailable");
        }
        const nextFork: PendingHistoricalFork = {
          sourceMessageId: sourceMessage.id,
          sourceNodeId: sourceNode.id,
          continuationNodeId,
          sourceLabel: sourceNode.title || sourceMessage.content,
          originBranchName: currentLesson.history_graph.current_branch,
          originHeadNodeId: originBranch.head_commit_id,
          originRidocRevision: currentPackage.ridoc_revision,
          baseDocument: snapshot.document,
          startedAt: new Date().toISOString(),
        };
        updateLessonComposerState(lessonId, (current) => ({
          ...current,
          pendingHistoricalFork: nextFork,
        }));
        enterHistoricalFork(nextFork, snapshot.document);
      } catch (error) {
        setError(error instanceof Error ? error.message : "Unable to prepare the historical branch");
      } finally {
        setBusyAction(null);
      }
    },
    [
      api,
      activeLesson,
      applyCoursePackage,
      enterHistoricalFork,
      flushAutoSave,
      pendingFork,
      repositoryId,
      setBusyAction,
      setError,
      storageKind,
      updateLessonComposerState,
    ]
  );

  const startPendingFork = useCallback(
    async (sourceMessage: ChatMessage) => {
      if (sourceMessage.role !== "assistant" || !sourceMessage.commitId) {
        return;
      }
      await preparePendingFork(sourceMessage.commitId, sourceMessage);
    },
    [preparePendingFork]
  );

  const startPendingForkFromNode = useCallback(
    async (nodeId: string, continuationNodeId: string) => {
      await preparePendingFork(nodeId, undefined, continuationNodeId);
    },
    [preparePendingFork]
  );

  const returnToCurrentVersion = useCallback(async () => {
    if (!activeLesson || !pendingFork) {
      return;
    }
    const lessonId = activeLesson.id;
    if (!prepareReturnToCurrentVersion()) {
      setError("The branch edit is still being saved. Wait for it to finish before returning.");
      return;
    }
    const operationSequence = ++operationSequenceRef.current;
    setBusyAction("return-current-version");
    setError(null);
    try {
      const currentPackage = await api.getCoursePackage(repositoryId);
      if (
        operationSequenceRef.current !== operationSequence ||
        activeLessonIdRef.current !== lessonId
      ) {
        return;
      }
      const currentLesson = currentPackage.lessons.find((lesson) => lesson.id === lessonId);
      if (!currentLesson) {
        throw new Error("The current lesson is unavailable");
      }
      applyCoursePackage(currentPackage, {
        activeLessonId: lessonId,
        rebuildMessageLessonIds: [lessonId],
      });
      clearPendingFork(lessonId);
    } catch (error) {
      setError(error instanceof Error ? error.message : "Unable to return to the current version");
    } finally {
      setBusyAction(null);
    }
  }, [
    api,
    activeLesson,
    applyCoursePackage,
    clearPendingFork,
    pendingFork,
    prepareReturnToCurrentVersion,
    repositoryId,
    setBusyAction,
    setError,
  ]);

  const sourceMessage = pendingFork && activeLesson
    ? buildLessonMessagesFromHistory(activeLesson, pendingFork.sourceNodeId).find(
        (message) =>
          message.id === pendingFork.sourceMessageId ||
          (message.role === "assistant" && message.commitId === pendingFork.sourceNodeId)
      ) ?? activeMessages.find((message) => message.id === pendingFork.sourceMessageId) ?? null
    : null;

  return {
    pendingFork,
    sourceMessage,
    clearPendingFork,
    startPendingFork,
    startPendingForkFromNode,
    returnToCurrentVersion,
  };
}
