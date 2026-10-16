"use client";

import { useEffect, useMemo, useState, type Dispatch, type SetStateAction } from "react";

import { buildLessonMessagesFromHistory, documentsEqual } from "../../components/course-studio/history-utils";
import { lessonLearningCommits, lessonPlaybackIdentity, lessonSourceCommits } from "../../components/course-studio/history-learning-order";
import type { AutoSaveReason } from "./use-board-draft";
import type { AppliedCoursePackage, CoursePackageApplyOptions } from "./use-course-workspace";
import { useStudioApi } from "../../contexts/studio-api-context";
import { downloadRidoc } from "../../lib/ridoc-file";
import type { BoardDocument, CommitRecord, CoursePackage, Lesson } from "../../types";

export type LessonPlaybackStepKind =
  | "user"
  | "source"
  | "activity"
  | "assistant"
  | "board"
  | "merge"
  | "branch"
  | "complete";

export type LessonPlaybackStep = {
  id: string;
  kind: LessonPlaybackStepKind;
  title: string;
  detail: string;
  commitId: string;
  checkpointCommitId: string;
  boardCommitId: string;
  messageCommitId: string;
  /** Effective board at this step, distinct from the event's original snapshot. */
  boardDocument?: BoardDocument;
};

type UseLessonPackageOptions = {
  repositoryId?: string | null;
  ridocRevision?: string | null;
  activeLesson: Lesson | null;
  mergeActive: boolean;
  flushAutoSave: (reason: AutoSaveReason) => Promise<boolean>;
  setPreviewCommitId: Dispatch<SetStateAction<string | null>>;
  setPreviewDocument: (document: BoardDocument) => void;
  resetPreview: () => void;
  createBranchFromCommit: (commit: CommitRecord) => void | Promise<void>;
  applyCoursePackage: (
    nextPackage: CoursePackage,
    options?: CoursePackageApplyOptions
  ) => AppliedCoursePackage;
  setError: Dispatch<SetStateAction<string | null>>;
  setBusyAction: Dispatch<SetStateAction<string | null>>;
};

function metadataText(commit: CommitRecord, key: string): string {
  const value = commit.metadata?.[key];
  return typeof value === "string" ? value.trim() : "";
}

function sourceDetail(commit: CommitRecord): string {
  const selection = commit.metadata?.selection;
  if (!selection || typeof selection !== "object") {
    return "Evidence that frozen course materials have been used";
  }
  const record = selection as Record<string, unknown>;
  const parts = [record.source_title, record.source_chapter_title, record.source_page_range]
    .filter((value): value is string => typeof value === "string" && Boolean(value.trim()))
    .map((value) => value.trim());
  return parts.join(" · ") || "Evidence that frozen course materials have been used";
}

function buildLearningPlaybackSteps(lesson: Lesson, commits: CommitRecord[]): LessonPlaybackStep[] {
  const branch = lesson.history_graph.branches[lesson.history_graph.current_branch];
  const steps: LessonPlaybackStep[] = [];
  let boardCommit: CommitRecord | null = null;
  let boardDocument: BoardDocument | undefined;
  for (const commit of commits) {
    const kind = metadataText(commit, "history_node_kind");
    if (kind === "document" || (kind === "merge" && commit.metadata?.document_changed === true)) {
      boardDocument = branch.replay_documents?.[commit.id]
        ?? (!branch.learning_node_ids?.length ? commit.snapshot : undefined);
      if (!boardDocument) return []; // A merged C must never replay its unrebased source snapshot.
      boardCommit = commit;
      steps.push({
        id: commit.id, kind: "board", title: commit.metadata?.ridoc_initial ? "Initial document" : "Board changes",
        detail: commit.label, commitId: commit.id, checkpointCommitId: commit.id,
        boardCommitId: commit.id, messageCommitId: commit.id, boardDocument,
      });
    } else if (kind === "chat") {
      const user = metadataText(commit, "user_message");
      const assistant = metadataText(commit, "assistant_message");
      if (!user || !assistant || !boardCommit || !boardDocument) return [];
      steps.push({
        id: commit.id, kind: "assistant", title: "Dialogue", detail: `${user}\n\n${assistant}`,
        commitId: commit.id, checkpointCommitId: commit.id, boardCommitId: boardCommit.id,
        messageCommitId: commit.id, boardDocument,
      });
    }
  }
  return steps;
}

export function buildLessonPlaybackSteps(lesson: Lesson): LessonPlaybackStep[] {
  const learningCommits = lessonLearningCommits(lesson);
  if (learningCommits !== null) return buildLearningPlaybackSteps(lesson, learningCommits);
  const sourceCommits = lessonSourceCommits(lesson);
  if (sourceCommits.length && sourceCommits.every((commit) => commit.metadata?.ridoc_native === true)) {
    return buildLearningPlaybackSteps(lesson, sourceCommits);
  }
  const commitsById = new Map(lesson.history_graph.commits.map((commit) => [commit.id, commit]));
  const steps: LessonPlaybackStep[] = [];

  sourceCommits.forEach((commit) => {
    const parent = commitsById.get(commit.parent_ids[0] ?? "") ?? null;
    const beforeCommit = parent ?? commit;
    const historyNodeKind = metadataText(commit, "history_node_kind");
    const boardChanged =
      !parent ||
      historyNodeKind === "document" ||
      historyNodeKind === "restore" ||
      historyNodeKind === "merge" ||
      commit.metadata?.document_changed === true ||
      (!historyNodeKind && !documentsEqual(parent.snapshot, commit.snapshot));
    const beforeCheckpointId = boardChanged ? beforeCommit.id : commit.id;
    const userMessage = metadataText(commit, "user_message");
    const assistantMessage = metadataText(commit, "assistant_message");
    const selection = commit.metadata?.selection;
    const hasSource =
      (selection && typeof selection === "object" && (selection as Record<string, unknown>).kind === "source") ||
      commit.metadata?.verified_source_reference_used === true;

    const addStep = (
      kind: LessonPlaybackStepKind,
      title: string,
      detail: string,
      checkpointCommitId: string,
      boardCommitId: string,
      messageCommitId: string,
      suffix: string
    ) => {
      steps.push({
        id: `${commit.id}:${suffix}:${steps.length}`,
        kind,
        title,
        detail,
        commitId: commit.id,
        checkpointCommitId,
        boardCommitId,
        messageCommitId,
      });
    };

    if (userMessage) {
      addStep("user", "User speaks", userMessage, beforeCommit.id, beforeCommit.id, beforeCommit.id, "user");
    }
    if (hasSource) {
      addStep(
        "source",
        "Data citation",
        sourceDetail(commit),
        beforeCommit.id,
        beforeCommit.id,
        beforeCommit.id,
        "source"
      );
    }
    const activity = Array.isArray(commit.metadata?.agent_activity) ? commit.metadata.agent_activity : [];
    activity.forEach((item, index) => {
      if (!item || typeof item !== "object") {
        return;
      }
      const record = item as Record<string, unknown>;
      const label = typeof record.label === "string" ? record.label.trim() : "";
      if (!label) {
        return;
      }
      addStep(
        "activity",
        "AI working process",
        label,
        beforeCommit.id,
        beforeCommit.id,
        beforeCommit.id,
        `activity-${index}`
      );
    });
    if (assistantMessage) {
      addStep(
        "assistant",
        "AI Reply",
        assistantMessage,
        beforeCheckpointId,
        beforeCommit.id,
        commit.id,
        "assistant"
      );
    }
    if (boardChanged) {
      addStep("board", "Board changes", commit.label, commit.id, commit.id, commit.id, "board");
    }
    if (commit.parent_ids.length > 1) {
      addStep("merge", "Branch merge", commit.message, commit.id, commit.id, commit.id, "merge");
    }
    Object.entries(lesson.history_graph.branches).forEach(([branchName, branch]) => {
      if (branch.base_commit_id === commit.id && branchName !== commit.branch_name) {
        addStep("branch", "Create a branch", branchName, commit.id, commit.id, commit.id, `branch-${branchName}`);
      }
    });
    addStep("complete", "round completed", commit.message, commit.id, commit.id, commit.id, "complete");
  });

  return steps;
}

export function useLessonPackage({
  repositoryId,
  ridocRevision,
  activeLesson,
  mergeActive,
  flushAutoSave,
  setPreviewCommitId,
  setPreviewDocument,
  resetPreview,
  createBranchFromCommit,
  applyCoursePackage,
  setError,
  setBusyAction,
}: UseLessonPackageOptions) {
  const api = useStudioApi();
  const steps = useMemo(() => (activeLesson ? buildLessonPlaybackSteps(activeLesson) : []), [activeLesson]);
  const [stepIndex, setStepIndex] = useState(-1);
  const [isPlaying, setIsPlaying] = useState(false);
  const [sessionIdentity, setSessionIdentity] = useState("");
  const [speed, setSpeed] = useState(1);
  const [operation, setOperation] = useState<"export" | "import" | null>(null);
  const playbackIdentity = activeLesson ? `${ridocRevision ?? ""}:${lessonPlaybackIdentity(activeLesson)}` : "";
  const effectiveStepIndex = sessionIdentity === playbackIdentity ? stepIndex : -1;
  const currentStep = effectiveStepIndex >= 0 ? steps[effectiveStepIndex] ?? null : null;
  const currentCommit = currentStep
    ? activeLesson?.history_graph.commits.find((commit) => commit.id === currentStep.commitId) ?? null
    : null;
  const effectiveIsPlaying =
    sessionIdentity === playbackIdentity && isPlaying && effectiveStepIndex < steps.length - 1;
  const playbackMessages = useMemo(
    () => currentStep && activeLesson
      ? buildLessonMessagesFromHistory(activeLesson, currentStep.messageCommitId, "learning")
      : null,
    [activeLesson, currentStep]
  );
  const currentBoardDocument = currentStep?.boardDocument ?? activeLesson?.history_graph.commits.find(
    (commit) => commit.id === currentStep?.boardCommitId
  )?.snapshot;
  const currentPlaybackCommitId = currentStep?.commitId;

  useEffect(() => {
    if (currentPlaybackCommitId) setPreviewCommitId(currentPlaybackCommitId);
  }, [currentPlaybackCommitId, setPreviewCommitId]);

  useEffect(() => {
    if (currentBoardDocument) setPreviewDocument(currentBoardDocument);
  }, [currentBoardDocument, setPreviewDocument]);

  useEffect(() => {
    if (!effectiveIsPlaying || effectiveStepIndex < 0) {
      return;
    }
    const timer = window.setTimeout(() => setStepIndex((current) => current + 1), 1400 / speed);
    return () => window.clearTimeout(timer);
  }, [effectiveIsPlaying, effectiveStepIndex, speed]);

  async function startOrTogglePlayback() {
    if (mergeActive || !activeLesson || !steps.length) {
      return;
    }
    if (effectiveStepIndex >= 0) {
      if (effectiveStepIndex >= steps.length - 1) {
        setSessionIdentity(playbackIdentity);
        setStepIndex(0);
        setIsPlaying(true);
        return;
      }
      setIsPlaying((current) => !current);
      return;
    }
    if (!(await flushAutoSave("preview"))) {
      return;
    }
    setSessionIdentity(playbackIdentity);
    setStepIndex(0);
    setIsPlaying(true);
  }

  async function movePlayback(delta: number) {
    if (mergeActive || !activeLesson || !steps.length) {
      return;
    }
    if (effectiveStepIndex < 0 && !(await flushAutoSave("preview"))) {
      return;
    }
    setSessionIdentity(playbackIdentity);
    setIsPlaying(false);
    setStepIndex(Math.max(0, Math.min(steps.length - 1, (effectiveStepIndex < 0 ? 0 : effectiveStepIndex) + delta)));
  }

  function exitPlayback() {
    setIsPlaying(false);
    setStepIndex(-1);
    setSessionIdentity("");
    resetPreview();
  }

  async function forkFromCurrentStep() {
    if (mergeActive || !activeLesson || !currentStep) {
      return;
    }
    const checkpoint = activeLesson.history_graph.commits.find(
      (commit) => commit.id === currentStep.checkpointCommitId
    );
    if (!checkpoint) {
      setError("There are no history checkpoints available for the current playback step.");
      return;
    }
    setIsPlaying(false);
    await createBranchFromCommit(checkpoint);
  }

  async function exportRidoc() {
    if (mergeActive || !activeLesson) {
      setError("Course packages cannot be exported during the merge draft, please submit or abandon the merge first.");
      return;
    }
    if (!(await flushAutoSave("export"))) {
      return;
    }
    setOperation("export");
    setBusyAction("ridoc-export");
    try {
      const blob = await api.exportRidoc(activeLesson.id, repositoryId);
      downloadRidoc(blob, activeLesson);
    } catch (exportError) {
      setError(exportError instanceof Error ? exportError.message : "Course package export failed");
    } finally {
      setOperation(null);
      setBusyAction(null);
    }
  }

  async function importRidoc(file: File) {
    if (repositoryId) {
      setError("A repository project has a fixed course collection; import RIDOC from the repository list to create a new project.");
      return null;
    }
    if (mergeActive) {
      setError("Course packages cannot be imported during the merge draft, please submit or abandon the merge first.");
      return null;
    }
    if (!(await flushAutoSave("import"))) {
      return null;
    }
    setOperation("import");
    setBusyAction("ridoc-import");
    try {
      const nextPackage = await api.importRidoc(file);
      const lessonId = nextPackage.active_lesson_id ?? nextPackage.lessons.at(-1)?.id ?? null;
      applyCoursePackage(nextPackage, {
        activeLessonId: lessonId,
        // Only rebuild the newly imported lesson. Rebuilding every standalone lesson
        // from a large workspace can freeze the UI and make history look "missing".
        rebuildMessageLessonIds: lessonId ? [lessonId] : [],
      });
      return lessonId;
    } catch (importError) {
      setError(importError instanceof Error ? importError.message : "Course package import failed");
      return null;
    } finally {
      setOperation(null);
      setBusyAction(null);
    }
  }

  return {
    steps,
    currentStep,
    currentCommit,
    stepIndex: effectiveStepIndex,
    isPlaying: effectiveIsPlaying,
    isPlaybackActive: effectiveStepIndex >= 0,
    playbackMessageCommitId: currentStep?.messageCommitId ?? null,
    playbackMessages,
    speed,
    operation,
    setSpeed,
    startOrTogglePlayback,
    movePlayback,
    exitPlayback,
    forkFromCurrentStep,
    exportRidoc,
    importRidoc,
  };
}
