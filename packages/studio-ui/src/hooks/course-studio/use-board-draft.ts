"use client";

import { useCallback, useEffect, useEffectEvent, useMemo, useRef, useState, type Dispatch, type SetStateAction } from "react";

import { useStudioApi, useStudioContext } from "../../contexts/studio-api-context";
import { readTemporaryDraft, writeTemporaryDraft } from "../../lib/temporary-learning-draft";
import {
  AUTO_SAVE_DELAY_MS,
  currentHeadCommitId,
  documentsEqual,
  type PendingHistoricalFork,
} from "../../components/course-studio/history-utils";
import type { AppliedCoursePackage, CoursePackageApplyOptions } from "./use-course-workspace";
import type { BoardDocument, CoursePackage, DocumentSaveDelta, Lesson } from "../../types";

export type AutoSaveStatus = "idle" | "pending" | "saving" | "saved" | "error";
export type AutoSaveReason =
  | "debounce"
  | "queued"
  | "manual"
  | "return-home"
  | "select-lesson"
  | "open-lesson"
  | "close-lesson"
  | "create-lesson"
  | "chat"
  | "branch"
  | "preview"
  | "switch-branch"
  | "restore"
  | "import"
  | "export"
  | "voice"
  | "pagehide";

type AutoSavedPackageResult = {
  coursePackage: CoursePackage;
  savedLesson: Lesson | null;
  ignored?: boolean;
};

type UseBoardDraftOptions = {
  repositoryId?: string | null;
  storageKind?: "legacy" | "ridoc";
  ridocRevision?: string | null;
  activeLesson: Lesson | null;
  setError: Dispatch<SetStateAction<string | null>>;
  setBusyAction: Dispatch<SetStateAction<string | null>>;
  applyCoursePackage: (nextPackage: CoursePackage, options?: CoursePackageApplyOptions) => AppliedCoursePackage;
  applyAutoSavedCoursePackage: (
    delta: DocumentSaveDelta,
    lessonId: string,
    currentActiveLessonId: string | null,
    requestDeletionGeneration?: number
  ) => AutoSavedPackageResult;
  getHistoryDeletionGeneration: () => number;
  onPackageApplied?: () => void;
  onHistoricalForkCommitted?: (lessonId: string) => void;
};

function buildDocumentSavePayload(
  document: BoardDocument,
  reason: AutoSaveReason,
  baseCommitId: string | null,
  allowStructureRemoval: boolean,
  storageKind: "legacy" | "ridoc",
  ridocRevision: string | null | undefined,
  transactionId: string | null,
  pendingHistoricalFork: PendingHistoricalFork | null
) {
  const ridocFields = storageKind === "ridoc"
    ? {
        expected_ridoc_revision: ridocRevision ?? null,
        transaction_id: transactionId,
        history_branch_action: pendingHistoricalFork
          ? { kind: "fork" as const, source_node_id: pendingHistoricalFork.sourceNodeId }
          : null,
      }
    : {};
  if (reason === "manual") {
    return {
      document,
      label: "Manual document edit",
      message: "Saved Word-like rich document changes from the editor",
      base_commit_id: baseCommitId,
      ...ridocFields,
      metadata: {
        kind: "manual_document_save",
        ...(allowStructureRemoval ? { structure_removal_intent: true } : {}),
      },
    };
  }
  return {
    document,
    label: "Document edit",
    message: "Saved a semantic document change from the editor",
    base_commit_id: baseCommitId,
    ...ridocFields,
    metadata: {
      kind: "auto_document_save",
      autosave: true,
      autosave_reason: reason,
      source: "word_board_editor",
      ...(allowStructureRemoval ? { structure_removal_intent: true } : {}),
    },
  };
}

function plainTextFromHtml(value: string) {
  return value
    .replace(/<\/(h[1-6]|p|li|blockquote|tr)>/gi, "\n")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function visibleDocumentText(document: BoardDocument) {
  return plainTextFromHtml(document.content_html) || document.content_text.replace(/\s+/g, " ").trim();
}

function looksLikeSameRenderedDocument(left: BoardDocument, right: BoardDocument) {
  return left.id === right.id && visibleDocumentText(left) === visibleDocumentText(right);
}

function richStructureCounts(document: BoardDocument) {
  const counts = {
    heading: 0,
    bold: 0,
    italic: 0,
    bulletList: 0,
    orderedList: 0,
    listItem: 0,
    table: 0,
    resourceVisualBlock: 0,
    blockquote: 0,
    paragraph: 0,
  };

  function walk(value: unknown) {
    if (Array.isArray(value)) {
      value.forEach(walk);
      return;
    }
    if (!value || typeof value !== "object") {
      return;
    }
    const node = value as { type?: unknown; marks?: unknown };
    if (typeof node.type === "string" && node.type in counts) {
      counts[node.type as keyof typeof counts] += 1;
    }
    if (Array.isArray(node.marks)) {
      for (const mark of node.marks) {
        if (!mark || typeof mark !== "object") {
          continue;
        }
        const markType = (mark as { type?: unknown }).type;
        if (typeof markType === "string" && markType in counts) {
          counts[markType as keyof typeof counts] += 1;
        }
      }
    }
    Object.values(value).forEach(walk);
  }

  walk(document.content_json);
  return counts;
}

function richStructureScore(counts: ReturnType<typeof richStructureCounts>) {
  return (
    counts.heading * 3 +
    counts.table * 4 +
    counts.bulletList * 2 +
    counts.orderedList * 2 +
    counts.listItem +
    counts.blockquote * 2 +
    counts.bold +
    counts.italic
  );
}

function isStructuredDocument(document: BoardDocument) {
  return richStructureScore(richStructureCounts(document)) >= 8;
}

function wouldFlattenRenderedDocument(
  currentDocument: BoardDocument,
  nextDocument: BoardDocument,
  allowStructureRemoval = false
) {
  const currentCounts = richStructureCounts(currentDocument);
  const nextCounts = richStructureCounts(nextDocument);
  const currentScore = richStructureScore(currentCounts);
  const headingHierarchyLost = currentCounts.heading >= 2 && nextCounts.heading === 0;
  const tableStructureLost = !allowStructureRemoval && currentCounts.table > 0 && nextCounts.table === 0;
  const resourceVisualLost =
    !allowStructureRemoval && currentCounts.resourceVisualBlock > nextCounts.resourceVisualBlock;
  if (resourceVisualLost) {
    return true;
  }
  if (currentScore < 8 && !headingHierarchyLost && !tableStructureLost) {
    return false;
  }
  if (currentCounts.heading + currentCounts.table <= 0) {
    return false;
  }
  if (headingHierarchyLost || tableStructureLost) {
    return true;
  }
  if (nextCounts.heading || nextCounts.table) {
    return false;
  }
  const nextScore = richStructureScore(nextCounts);
  return (
    nextScore <= Math.max(2, Math.floor(currentScore * 0.5)) &&
    nextCounts.paragraph >= Math.max(8, Math.floor(currentCounts.paragraph / 2))
  );
}

export function useBoardDraft({
  repositoryId,
  storageKind = "legacy",
  ridocRevision = null,
  activeLesson,
  setError,
  setBusyAction,
  applyCoursePackage,
  applyAutoSavedCoursePackage,
  getHistoryDeletionGeneration,
  onPackageApplied,
  onHistoricalForkCommitted,
}: UseBoardDraftOptions) {
  const api = useStudioApi();
  const studioContext = useStudioContext();
  const learningSessionId = studioContext.kind === "temporary" ? studioContext.sessionId : studioContext.kind === "review" ? `review:${studioContext.sessionId}` : null;
  const recoveredLessons = useRef(new Set<string>());
  const autoSaveTimerRef = useRef<number | null>(null);
  const autoSaveInFlightRef = useRef<Promise<boolean> | null>(null);
  const autoSaveQueuedRef = useRef(false);
  const scheduleAutoSaveRef = useRef<(reason?: AutoSaveReason) => void>(() => undefined);
  const flushAutoSaveRef = useRef<(reason: AutoSaveReason) => Promise<boolean>>(async () => true);
  const latestRidocRevisionRef = useRef<string | null>(ridocRevision ?? null);
  const documentDraftVersionRef = useRef(0);
  const activeLessonRef = useRef<Lesson | null>(null);
  const draftDocumentRef = useRef<BoardDocument | null>(null);
  const isDocumentDirtyRef = useRef(false);
  const isPreviewingRef = useRef(false);
  const ignoredStreamingPreviewRef = useRef<BoardDocument | null>(null);
  const lastStructuredDocumentRef = useRef<BoardDocument | null>(null);
  const structureRemovalIntentRef = useRef(false);
  const documentTransactionIdRef = useRef<string | null>(null);
  const lastDocumentEditAtRef = useRef(0);
  const pendingHistoricalForkRef = useRef<PendingHistoricalFork | null>(null);

  const [draftDocument, setDraftDocument] = useState<BoardDocument | null>(null);
  const [isDocumentDirty, setIsDocumentDirty] = useState(false);
  const [isPreviewing, setIsPreviewing] = useState(false);
  const [autoSaveStatus, setAutoSaveStatus] = useState<AutoSaveStatus>("idle");

  const displayedDocument = useMemo(
    () => draftDocument ?? activeLesson?.board_document ?? null,
    [activeLesson?.board_document, draftDocument]
  );

  const clearAutoSaveTimer = useCallback(() => {
    if (autoSaveTimerRef.current === null) {
      return;
    }
    window.clearTimeout(autoSaveTimerRef.current);
    autoSaveTimerRef.current = null;
  }, []);

  const baselineDocumentForLesson = useCallback(
    (lesson: Lesson) => pendingHistoricalForkRef.current?.baseDocument ?? lesson.board_document,
    []
  );

  const resetToLesson = useCallback(
    (lesson: Lesson | null) => {
      let nextDocument = lesson?.board_document ?? null;
      let recovered = false;
      let recoveredTransaction: string | null = null;
      if (lesson && !recoveredLessons.current.has(lesson.id)) {
        recoveredLessons.current.add(lesson.id);
        const cached = readTemporaryDraft(learningSessionId, lesson.id);
        if (cached && cached.document.id === nextDocument?.id && !documentsEqual(cached.document, nextDocument)) {
          if (cached.revision === latestRidocRevisionRef.current) {
            nextDocument = cached.document; recovered = true; recoveredTransaction = cached.transactionId;
          } else setError("A recovered draft belongs to an older course revision. It was retained locally and has not overwritten the current version.");
        }
      }
      clearAutoSaveTimer();
      autoSaveQueuedRef.current = false;
      documentDraftVersionRef.current += 1;
      activeLessonRef.current = lesson;
      draftDocumentRef.current = nextDocument;
      lastStructuredDocumentRef.current = nextDocument && isStructuredDocument(nextDocument) ? nextDocument : null;
      structureRemovalIntentRef.current = false;
      documentTransactionIdRef.current = recoveredTransaction;
      lastDocumentEditAtRef.current = 0;
      pendingHistoricalForkRef.current = null;
      isDocumentDirtyRef.current = recovered;
      isPreviewingRef.current = false;
      if (!nextDocument || ignoredStreamingPreviewRef.current?.id !== nextDocument.id) {
        ignoredStreamingPreviewRef.current = null;
      }
      setDraftDocument(nextDocument);
      setIsDocumentDirty(recovered);
      setIsPreviewing(false);
      setAutoSaveStatus(recovered ? "pending" : "idle");
    },
    [clearAutoSaveTimer, learningSessionId, setError]
  );

  const enterHistoricalFork = useCallback(
    (fork: PendingHistoricalFork, document: BoardDocument) => {
      clearAutoSaveTimer();
      autoSaveQueuedRef.current = false;
      documentDraftVersionRef.current += 1;
      pendingHistoricalForkRef.current = fork;
      latestRidocRevisionRef.current = fork.originRidocRevision;
      draftDocumentRef.current = document;
      lastStructuredDocumentRef.current = isStructuredDocument(document) ? document : null;
      structureRemovalIntentRef.current = false;
      documentTransactionIdRef.current = null;
      lastDocumentEditAtRef.current = 0;
      isDocumentDirtyRef.current = false;
      isPreviewingRef.current = false;
      ignoredStreamingPreviewRef.current = null;
      setDraftDocument(document);
      setIsDocumentDirty(false);
      setIsPreviewing(false);
      setAutoSaveStatus("idle");
    },
    [clearAutoSaveTimer]
  );

  const getPendingHistoricalFork = useCallback(
    () => pendingHistoricalForkRef.current,
    []
  );

  const restorePendingHistoricalFork = useCallback(() => {
    const fork = pendingHistoricalForkRef.current;
    if (fork) {
      enterHistoricalFork(fork, fork.baseDocument);
    }
  }, [enterHistoricalFork]);

  const prepareReturnToCurrentVersion = useCallback(() => {
    const fork = pendingHistoricalForkRef.current;
    if (!fork || autoSaveInFlightRef.current) {
      return !autoSaveInFlightRef.current;
    }
    clearAutoSaveTimer();
    autoSaveQueuedRef.current = false;
    documentDraftVersionRef.current += 1;
    draftDocumentRef.current = fork.baseDocument;
    lastStructuredDocumentRef.current = isStructuredDocument(fork.baseDocument)
      ? fork.baseDocument
      : null;
    isDocumentDirtyRef.current = false;
    isPreviewingRef.current = false;
    ignoredStreamingPreviewRef.current = null;
    setDraftDocument(fork.baseDocument);
    setIsDocumentDirty(false);
    setIsPreviewing(false);
    setAutoSaveStatus("idle");
    return true;
  }, [clearAutoSaveTimer]);

  const setPreviewDocument = useCallback(
    (document: BoardDocument) => {
      clearAutoSaveTimer();
      documentDraftVersionRef.current += 1;
      draftDocumentRef.current = document;
      isDocumentDirtyRef.current = false;
      isPreviewingRef.current = true;
      ignoredStreamingPreviewRef.current = null;
      structureRemovalIntentRef.current = false;
      setDraftDocument(document);
      setIsDocumentDirty(false);
      setIsPreviewing(true);
      setAutoSaveStatus("idle");
    },
    [clearAutoSaveTimer]
  );

  const exitPreviewToLesson = useCallback(
    (lesson: Lesson | null) => {
      const previewDocument = isPreviewingRef.current ? draftDocumentRef.current : null;
      if (
        previewDocument &&
        lesson?.board_document.id === previewDocument.id &&
        !documentsEqual(previewDocument, lesson.board_document)
      ) {
        ignoredStreamingPreviewRef.current = previewDocument;
      }
      resetToLesson(lesson);
    },
    [resetToLesson]
  );

  const setStreamingDocumentPreview = useCallback(
    (lessonId: string, document: BoardDocument) => {
      if (
        activeLessonRef.current?.id !== lessonId ||
        activeLessonRef.current.board_document.id !== document.id
      ) {
        return false;
      }
      const currentDocument = draftDocumentRef.current;
      const sameVisiblePreview =
        isPreviewingRef.current &&
        currentDocument?.id === document.id &&
        visibleDocumentText(currentDocument) === visibleDocumentText(document);
      if (sameVisiblePreview) {
        return true;
      }
      clearAutoSaveTimer();
      documentDraftVersionRef.current += 1;
      draftDocumentRef.current = document;
      isDocumentDirtyRef.current = false;
      isPreviewingRef.current = true;
      ignoredStreamingPreviewRef.current = document;
      setDraftDocument((current) => {
        if (
          current?.id === document.id &&
          visibleDocumentText(current) === visibleDocumentText(document)
        ) {
          return current;
        }
        return document;
      });
      setIsDocumentDirty(false);
      setIsPreviewing(true);
      setAutoSaveStatus("idle");
      return true;
    },
    [clearAutoSaveTimer]
  );

  const applyAutoSavedPackage = useCallback(
    (delta: DocumentSaveDelta, lessonId: string, savedVersion: number, requestDeletionGeneration: number) => {
      if (requestDeletionGeneration !== getHistoryDeletionGeneration()) return;
      const currentActiveLessonId = activeLessonRef.current?.id ?? null;
      const committedHistoricalFork = pendingHistoricalForkRef.current;
      if (committedHistoricalFork && !delta.changed) {
        draftDocumentRef.current = committedHistoricalFork.baseDocument;
        lastStructuredDocumentRef.current = isStructuredDocument(committedHistoricalFork.baseDocument)
          ? committedHistoricalFork.baseDocument
          : null;
        isDocumentDirtyRef.current = false;
        setDraftDocument(committedHistoricalFork.baseDocument);
        setIsDocumentDirty(false);
        setAutoSaveStatus("idle");
        setError(null);
        return;
      }
      const applied = applyAutoSavedCoursePackage(delta, lessonId, currentActiveLessonId, requestDeletionGeneration);
      latestRidocRevisionRef.current = applied.coursePackage.ridoc_revision ?? null;
      if (applied.ignored) return;
      const { savedLesson } = applied;
      if (delta.changed && committedHistoricalFork) {
        pendingHistoricalForkRef.current = null;
        onHistoricalForkCommitted?.(lessonId);
      }

      if (currentActiveLessonId !== lessonId || !savedLesson) {
        setError(null);
        return;
      }

      activeLessonRef.current = savedLesson;
      if (documentDraftVersionRef.current === savedVersion) {
        writeTemporaryDraft(learningSessionId, lessonId, null, null);
        setDraftDocument(savedLesson.board_document);
        draftDocumentRef.current = savedLesson.board_document;
        if (isStructuredDocument(savedLesson.board_document)) {
          lastStructuredDocumentRef.current = savedLesson.board_document;
        }
        setIsDocumentDirty(false);
        isDocumentDirtyRef.current = false;
        structureRemovalIntentRef.current = false;
        setAutoSaveStatus("saved");
        setError(null);
        return;
      }

      const latestDraft = draftDocumentRef.current;
      if (isStructuredDocument(savedLesson.board_document)) {
        lastStructuredDocumentRef.current = savedLesson.board_document;
      }
      const stillDirty = Boolean(latestDraft && !documentsEqual(latestDraft, savedLesson.board_document));
      writeTemporaryDraft(learningSessionId, lessonId, stillDirty ? latestDraft : null, latestRidocRevisionRef.current, documentTransactionIdRef.current);
      setIsDocumentDirty(stillDirty);
      isDocumentDirtyRef.current = stillDirty;
      setAutoSaveStatus(stillDirty ? "pending" : "saved");
      setError(null);
    },
    [learningSessionId, applyAutoSavedCoursePackage, getHistoryDeletionGeneration, onHistoricalForkCommitted, setError]
  );

  const flushAutoSave = useCallback(
    async (reason: AutoSaveReason): Promise<boolean> => {
      clearAutoSaveTimer();
      if (autoSaveInFlightRef.current) {
        autoSaveQueuedRef.current = true;
        await autoSaveInFlightRef.current;
        if (!isDocumentDirtyRef.current) {
          return true;
        }
        return flushAutoSaveRef.current(reason);
      }

      let lesson = activeLessonRef.current;
      const document = draftDocumentRef.current;
      if (!lesson || !document || !isDocumentDirtyRef.current || isPreviewingRef.current) {
        return true;
      }
      const baselineDocument = baselineDocumentForLesson(lesson);
      if (document.id !== baselineDocument.id) {
        draftDocumentRef.current = baselineDocument;
        isDocumentDirtyRef.current = false;
        setDraftDocument(baselineDocument);
        setIsDocumentDirty(false);
        setAutoSaveStatus("idle");
        return true;
      }
      const ignoredStreamingPreview = ignoredStreamingPreviewRef.current;
      if (
        ignoredStreamingPreview &&
        looksLikeSameRenderedDocument(document, ignoredStreamingPreview) &&
        !documentsEqual(document, baselineDocument)
      ) {
        draftDocumentRef.current = baselineDocument;
        isDocumentDirtyRef.current = false;
        setDraftDocument(baselineDocument);
        setIsDocumentDirty(false);
        setAutoSaveStatus("idle");
        return true;
      }
      const structuredGuard = lastStructuredDocumentRef.current;
      const allowStructureRemoval = structureRemovalIntentRef.current;
      if (
        structuredGuard &&
        structuredGuard.id === document.id &&
        wouldFlattenRenderedDocument(structuredGuard, document, allowStructureRemoval)
      ) {
        draftDocumentRef.current = structuredGuard;
        isDocumentDirtyRef.current = false;
        setDraftDocument(structuredGuard);
        setIsDocumentDirty(false);
        setAutoSaveStatus("idle");
        return true;
      }
      if (documentsEqual(document, baselineDocument)) {
        setIsDocumentDirty(false);
        isDocumentDirtyRef.current = false;
        ignoredStreamingPreviewRef.current = null;
        setAutoSaveStatus("idle");
        return true;
      }

      const pendingBranch = lesson.history_graph.branches[lesson.history_graph.current_branch];
      if (storageKind !== "ridoc" && !pendingHistoricalForkRef.current && pendingBranch?.document_redo_commit_ids?.length) {
        try {
          const branchedPackage = await api.ensureDocumentHistoryBranch(
            lesson.id,
            {
              expected_branch_name: pendingBranch.name,
              expected_head_commit_id: pendingBranch.head_commit_id,
              expected_document_cursor_commit_id:
                pendingBranch.document_cursor_commit_id || pendingBranch.head_commit_id,
            },
            repositoryId
          );
          const applied = applyCoursePackage(branchedPackage, { activeLessonId: lesson.id });
          if (!applied.activeLesson) {
            throw new Error("The branched lesson is unavailable");
          }
          lesson = applied.activeLesson;
          activeLessonRef.current = lesson;
        } catch (branchError) {
          setAutoSaveStatus("error");
          setError(branchError instanceof Error ? branchError.message : "Failed to create a document branch");
          return false;
        }
      }

      const savedVersion = documentDraftVersionRef.current;
      const isManualSave = reason === "manual";
      const baseCommitId = pendingHistoricalForkRef.current?.continuationNodeId ?? currentHeadCommitId(lesson);
      const transactionId = storageKind === "ridoc"
        ? documentTransactionIdRef.current ?? `document-${crypto.randomUUID()}`
        : null;
      if (storageKind === "ridoc") {
        documentTransactionIdRef.current = transactionId;
      }
      const payload = buildDocumentSavePayload(
        document,
        reason,
        baseCommitId,
        allowStructureRemoval,
        storageKind,
        latestRidocRevisionRef.current ?? ridocRevision,
        transactionId,
        pendingHistoricalForkRef.current
      );
      if (isManualSave) {
        setBusyAction("save");
      }
      setAutoSaveStatus("saving");

      const request = (async () => {
        const requestDeletionGeneration = getHistoryDeletionGeneration();
        try {
          const delta = await api.saveDocument(lesson.id, payload, repositoryId);
          applyAutoSavedPackage(delta, lesson.id, savedVersion, requestDeletionGeneration);
          return true;
        } catch (saveError) {
          setAutoSaveStatus("error");
          setError(saveError instanceof Error ? saveError.message : "Autosave failed");
          return false;
        } finally {
          if (isManualSave) {
            setBusyAction((current) => (current === "save" ? null : current));
          }
        }
      })();

      autoSaveInFlightRef.current = request;
      try {
        return await request;
      } finally {
        autoSaveInFlightRef.current = null;
        if (autoSaveQueuedRef.current) {
          autoSaveQueuedRef.current = false;
          if (isDocumentDirtyRef.current) {
            scheduleAutoSaveRef.current("queued");
          }
        }
      }
    },
    [api, applyAutoSavedPackage, applyCoursePackage, baselineDocumentForLesson, clearAutoSaveTimer, getHistoryDeletionGeneration, repositoryId, ridocRevision, setBusyAction, setError, storageKind]
  );

  const scheduleAutoSave = useCallback(
    (reason: AutoSaveReason = "debounce") => {
      clearAutoSaveTimer();
      if (!isDocumentDirtyRef.current || isPreviewingRef.current) {
        return;
      }
      if (autoSaveInFlightRef.current) {
        autoSaveQueuedRef.current = true;
        return;
      }
      setAutoSaveStatus("pending");
      autoSaveTimerRef.current = window.setTimeout(() => {
        autoSaveTimerRef.current = null;
        void flushAutoSave(reason);
      }, AUTO_SAVE_DELAY_MS);
    },
    [clearAutoSaveTimer, flushAutoSave]
  );

  useEffect(() => {
    flushAutoSaveRef.current = flushAutoSave;
    scheduleAutoSaveRef.current = scheduleAutoSave;
  }, [flushAutoSave, scheduleAutoSave]);

  useEffect(() => {
    latestRidocRevisionRef.current = ridocRevision ?? null;
  }, [ridocRevision]);

  const getLatestRidocRevision = useCallback(
    () => latestRidocRevisionRef.current,
    []
  );

  const flushAutoSaveWithBeacon = useCallback(
    (reason: AutoSaveReason = "pagehide") => {
      clearAutoSaveTimer();
      const lesson = activeLessonRef.current;
      const document = draftDocumentRef.current;
      if (pendingHistoricalForkRef.current) {
        return;
      }
      if (!lesson || !document || !isDocumentDirtyRef.current || isPreviewingRef.current) {
        return;
      }
      if (document.id !== lesson.board_document.id) {
        draftDocumentRef.current = lesson.board_document;
        isDocumentDirtyRef.current = false;
        setDraftDocument(lesson.board_document);
        setIsDocumentDirty(false);
        setAutoSaveStatus("idle");
        return;
      }
      const ignoredStreamingPreview = ignoredStreamingPreviewRef.current;
      if (
        ignoredStreamingPreview &&
        looksLikeSameRenderedDocument(document, ignoredStreamingPreview) &&
        !documentsEqual(document, lesson.board_document)
      ) {
        draftDocumentRef.current = lesson.board_document;
        isDocumentDirtyRef.current = false;
        setDraftDocument(lesson.board_document);
        setIsDocumentDirty(false);
        setAutoSaveStatus("idle");
        return;
      }
      const structuredGuard = lastStructuredDocumentRef.current;
      const allowStructureRemoval = structureRemovalIntentRef.current;
      if (
        structuredGuard &&
        structuredGuard.id === document.id &&
        wouldFlattenRenderedDocument(structuredGuard, document, allowStructureRemoval)
      ) {
        draftDocumentRef.current = structuredGuard;
        isDocumentDirtyRef.current = false;
        setDraftDocument(structuredGuard);
        setIsDocumentDirty(false);
        setAutoSaveStatus("idle");
        return;
      }
      if (documentsEqual(document, lesson.board_document)) {
        ignoredStreamingPreviewRef.current = null;
        return;
      }
      const baseCommitId = currentHeadCommitId(lesson);
      const transactionId = storageKind === "ridoc"
        ? documentTransactionIdRef.current ?? `document-${crypto.randomUUID()}`
        : null;
      const payload = buildDocumentSavePayload(
        document,
        reason,
        baseCommitId,
        allowStructureRemoval,
        storageKind,
        ridocRevision,
        transactionId,
        null
      );
      const sent = api.saveDocumentBeacon(lesson.id, payload, repositoryId);
      if (!sent) {
        void api.saveDocumentKeepalive(lesson.id, payload, repositoryId).catch(() => undefined);
      }
    },
    [api, clearAutoSaveTimer, repositoryId, ridocRevision, storageKind]
  );

  const handleLocalDocumentChange = useCallback(
    (nextDocument: BoardDocument) => {
      const lesson = activeLessonRef.current;
      if (isPreviewingRef.current || !lesson) {
        return;
      }
      const baselineDocument = baselineDocumentForLesson(lesson);
      if (nextDocument.id !== baselineDocument.id) {
        return;
      }
      const ignoredStreamingPreview = ignoredStreamingPreviewRef.current;
      const structuredGuard = lastStructuredDocumentRef.current;
      const allowStructureRemoval = structureRemovalIntentRef.current;
      if (
        ignoredStreamingPreview &&
        looksLikeSameRenderedDocument(nextDocument, ignoredStreamingPreview) &&
        !documentsEqual(nextDocument, baselineDocument)
      ) {
        draftDocumentRef.current = baselineDocument;
        isDocumentDirtyRef.current = false;
        setDraftDocument(baselineDocument);
        setIsDocumentDirty(false);
        setAutoSaveStatus("idle");
        return;
      }
      if (
        nextDocument.id === baselineDocument.id &&
        wouldFlattenRenderedDocument(baselineDocument, nextDocument, allowStructureRemoval)
      ) {
        ignoredStreamingPreviewRef.current = null;
        draftDocumentRef.current = baselineDocument;
        isDocumentDirtyRef.current = false;
        setDraftDocument(baselineDocument);
        setIsDocumentDirty(false);
        setAutoSaveStatus("idle");
        return;
      }
      if (
        structuredGuard &&
        structuredGuard.id === nextDocument.id &&
        wouldFlattenRenderedDocument(structuredGuard, nextDocument, allowStructureRemoval)
      ) {
        ignoredStreamingPreviewRef.current = null;
        draftDocumentRef.current = structuredGuard;
        isDocumentDirtyRef.current = false;
        setDraftDocument(structuredGuard);
        setIsDocumentDirty(false);
        setAutoSaveStatus("idle");
        return;
      }
      const hasChanged = !documentsEqual(draftDocumentRef.current, nextDocument);
      const dirty = !documentsEqual(nextDocument, baselineDocument);
      if (!dirty) {
        ignoredStreamingPreviewRef.current = null;
      } else if (
        ignoredStreamingPreview &&
        !looksLikeSameRenderedDocument(nextDocument, ignoredStreamingPreview)
      ) {
        ignoredStreamingPreviewRef.current = null;
      }
      if (hasChanged) {
        documentDraftVersionRef.current += 1;
      }
      if (dirty && storageKind === "ridoc") {
        const now = Date.now();
        if (
          !documentTransactionIdRef.current ||
          now - lastDocumentEditAtRef.current > 30_000
        ) {
          documentTransactionIdRef.current = `document-${crypto.randomUUID()}`;
        }
        lastDocumentEditAtRef.current = now;
      }
      draftDocumentRef.current = nextDocument;
      if (isStructuredDocument(nextDocument)) {
        lastStructuredDocumentRef.current = nextDocument;
      }
      isDocumentDirtyRef.current = dirty;
      writeTemporaryDraft(learningSessionId, lesson.id, dirty ? nextDocument : null, latestRidocRevisionRef.current, documentTransactionIdRef.current);
      setDraftDocument((current) => {
        if (current && current.id === nextDocument.id && documentsEqual(current, nextDocument)) {
          return current;
        }
        return nextDocument;
      });
      setIsDocumentDirty(dirty);
      setAutoSaveStatus(dirty ? "pending" : "idle");
    },
    [learningSessionId, baselineDocumentForLesson, storageKind]
  );

  const markStructureRemovalIntent = useCallback(() => {
    structureRemovalIntentRef.current = true;
  }, []);

  const handleImportDocx = useCallback(
    async (file: File) => {
      let lesson = activeLessonRef.current;
      if (!lesson) {
        return;
      }
      const lessonId = lesson.id;
      if (!(await flushAutoSave("import"))) {
        return;
      }
      lesson = activeLessonRef.current;
      if (!lesson || lesson.id !== lessonId) {
        return;
      }
      setBusyAction("import-docx");
      setError(null);
      try {
        const savedVersion = documentDraftVersionRef.current;
        const requestDeletionGeneration = getHistoryDeletionGeneration();
        const expectedRevision = latestRidocRevisionRef.current;
        const imported = await api.parseDocx(file, baselineDocumentForLesson(lesson).title);
        if (
          activeLessonRef.current?.id !== lessonId ||
          documentDraftVersionRef.current !== savedVersion ||
          latestRidocRevisionRef.current !== expectedRevision ||
          getHistoryDeletionGeneration() !== requestDeletionGeneration
        ) {
          throw new Error("The course changed while importing DOCX. Please import the file again.");
        }
        const pendingBranch = lesson.history_graph.branches[lesson.history_graph.current_branch];
        if (storageKind !== "ridoc" && pendingBranch?.document_redo_commit_ids?.length) {
          const branchedPackage = await api.ensureDocumentHistoryBranch(
            lesson.id,
            {
              expected_branch_name: pendingBranch.name,
              expected_head_commit_id: pendingBranch.head_commit_id,
              expected_document_cursor_commit_id:
                pendingBranch.document_cursor_commit_id || pendingBranch.head_commit_id,
            },
            repositoryId
          );
          const branched = applyCoursePackage(branchedPackage, { activeLessonId: lesson.id });
          if (!branched.activeLesson) {
            throw new Error("The branched lesson is unavailable");
          }
          lesson = branched.activeLesson;
          activeLessonRef.current = lesson;
        }
        const payload = buildDocumentSavePayload(
          { ...imported, id: baselineDocumentForLesson(lesson).id },
          "import",
          pendingHistoricalForkRef.current?.continuationNodeId ?? currentHeadCommitId(lesson),
          true,
          storageKind,
          expectedRevision,
          `import-docx-${crypto.randomUUID()}`,
          pendingHistoricalForkRef.current
        );
        // Any editing during the import save starts after the earlier edit action.
        documentTransactionIdRef.current = null;
        lastDocumentEditAtRef.current = 0;
        const delta = await api.saveDocument(lesson.id, {
          ...payload,
          label: "Import DOCX",
          message: `Imported ${file.name} as the board document`,
          metadata: { kind: "import_docx", filename: file.name, structure_removal_intent: true },
        }, repositoryId);
        applyAutoSavedPackage(delta, lesson.id, savedVersion, requestDeletionGeneration);
        if (
          activeLessonRef.current?.id === lesson.id &&
          documentDraftVersionRef.current === savedVersion &&
          getHistoryDeletionGeneration() === requestDeletionGeneration
        ) {
          resetToLesson(activeLessonRef.current);
          onPackageApplied?.();
        }
      } catch (importError) {
        setError(importError instanceof Error ? importError.message : "Import DOCX failed");
      } finally {
        setBusyAction(null);
      }
    },
    [api, applyAutoSavedPackage, applyCoursePackage, baselineDocumentForLesson, flushAutoSave, getHistoryDeletionGeneration, onPackageApplied, repositoryId, resetToLesson, setBusyAction, setError, storageKind]
  );

  const handleExportDocx = useCallback(async () => {
    const lesson = activeLessonRef.current;
    if (!lesson) {
      return;
    }
    if (!(await flushAutoSave("export"))) {
      return;
    }
    setBusyAction("export-docx");
    try {
      const blob = await api.exportDocx(lesson.id, repositoryId);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${lesson.slug || lesson.id}.docx`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (exportError) {
      setError(exportError instanceof Error ? exportError.message : "Exporting DOCX failed");
    } finally {
      setBusyAction(null);
    }
  }, [api, flushAutoSave, repositoryId, setBusyAction, setError]);

  const handleExportHtml = useCallback(async () => {
    const lesson = activeLessonRef.current;
    if (!lesson) {
      return;
    }
    if (!(await flushAutoSave("export"))) {
      return;
    }
    setBusyAction("export-html");
    try {
      const blob = await api.exportHtml(lesson.id, repositoryId);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${lesson.slug || lesson.id}.html`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (exportError) {
      setError(exportError instanceof Error ? exportError.message : "Export HTML failed");
    } finally {
      setBusyAction(null);
    }
  }, [api, flushAutoSave, repositoryId, setBusyAction, setError]);

  const scheduleAutoSaveEffectEvent = useEffectEvent(() => {
    scheduleAutoSave("debounce");
  });

  const clearAutoSaveTimerEffectEvent = useEffectEvent(() => {
    clearAutoSaveTimer();
  });

  useEffect(() => {
    activeLessonRef.current = activeLesson;
  }, [activeLesson]);

  useEffect(() => {
    if (!isDocumentDirty || isPreviewing) {
      clearAutoSaveTimerEffectEvent();
      return;
    }
    scheduleAutoSaveEffectEvent();
    return () => {
      clearAutoSaveTimerEffectEvent();
    };
  }, [activeLesson?.id, draftDocument, isDocumentDirty, isPreviewing]);

  return {
    draftDocument,
    displayedDocument,
    isDocumentDirty,
    isPreviewing,
    autoSaveStatus,
    resetToLesson,
    exitPreviewToLesson,
    setPreviewDocument,
    enterHistoricalFork,
    getPendingHistoricalFork,
    restorePendingHistoricalFork,
    prepareReturnToCurrentVersion,
    setStreamingDocumentPreview,
    flushAutoSave,
    getLatestRidocRevision,
    flushAutoSaveWithBeacon,
    handleLocalDocumentChange,
    markStructureRemovalIntent,
    handleImportDocx,
    handleExportDocx,
    handleExportHtml,
  };
}
