"use client";

import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";

import { isMissingChatStreamFinalError } from "../../lib/api";
import { useStudioApi } from "../../contexts/studio-api-context";
import {
  mergeAgentActivityEvent,
  normalizeAgentActivityEvents,
  publicAgentActivityLabel,
} from "../../lib/agent-activity";
import { createTextChatSessionId, freezeTextChatTurnIdentity } from "../../lib/chat-turn-identity";
import { streamingMarkdownToHtml } from "../../lib/streaming-rich-document";
import {
  activeLessonIdForAsyncPackage,
  resolvedBoardFocusForTurn,
} from "./chat-turn-ui-state";
import {
  buildLessonMessagesFromHistory,
  createChatMessage,
  createChatWaitingQueueItem,
  isBoardDocumentEmpty,
  learningClarityFromCommit,
  nextEditBranchName,
  type ChatMessage,
  type LessonComposerState,
} from "../../components/course-studio/history-utils";
import type { AutoSaveReason } from "./use-board-draft";
import type { CoursePackageApplyOptions } from "./use-course-workspace";
import type {
  AgentActivityEvent,
  AIModelSelection,
  BoardDocument,
  BoardDecision,
  BoardFocusRef,
  BoardTaskRequirementSheet,
  ChatAttachmentRef,
  ChatRequestPayload,
  CommitRecord,
  CoursePackage,
  LearningClarificationStatus,
  LearningRequirementSheet,
  Lesson,
  SelectionRef,
  SourceQueryScope,
} from "../../types";

type UseLessonChatAgentOptions = {
  repositoryId?: string | null;
  storageKind?: "legacy" | "ridoc";
  ridocRevision?: string | null;
  activeLesson: Lesson | null;
  activeMessages: ChatMessage[];
  activeComposerState: LessonComposerState;
  composerSelections: SelectionRef[];
  sourceQueryScope: SourceQueryScope | null;
  currentBoardDocument: BoardDocument | null;
  selectedTextModel: AIModelSelection;
  textModelReady: boolean;
  isPreviewMode: boolean;
  flushAutoSave: (reason: AutoSaveReason) => Promise<boolean>;
  getLatestRidocRevision: () => string | null;
  getPendingHistoricalFork: () => LessonComposerState["pendingHistoricalFork"];
  restorePendingHistoricalFork: () => void;
  exitPreviewMode: () => void;
  updateCoursePackage: (
    nextPackage: CoursePackage,
    options?: CoursePackageApplyOptions
  ) => { activeLesson: Lesson | null } | void;
  updateLessonMessages: (lessonId: string, updater: (messages: ChatMessage[]) => ChatMessage[]) => void;
  updateLessonComposerState: (lessonId: string, updater: (current: LessonComposerState) => LessonComposerState) => void;
  setStreamingDocumentPreview: (lessonId: string, document: BoardDocument) => boolean;
  onTransientBoardFocusChange: (lessonId: string, focus: BoardFocusRef | null) => void;
  clearSelection: () => void;
  setError: Dispatch<SetStateAction<string | null>>;
  setBusyAction: Dispatch<SetStateAction<string | null>>;
};

type ChatTurnBusyAction = "chat" | "agent-edit" | "chat-edit";

type ActiveChatTurn = {
  abortController: AbortController;
  abortRequested: boolean;
  cancellation: {
    lessonId: string;
    sessionId: string;
    inputEventId: string;
  } | null;
  prompt: string;
};

const DEFAULT_LEARNING_REQUIREMENT_FAILURE_REASON = "This round of learning requirements was not updated successfully, please try again the input just now.";
const DEFAULT_BOARD_GENERATION_FAILURE_REASON =
  "Board generation did not finish. The confirmed learning requirements were retained and can be retried.";
const INTERRUPTED_CHAT_RECOVERY_DELAYS_MS = [
  0,
  500,
  1000,
  2000,
  4000,
  8000,
  15000,
  30000,
  30000,
] as const;

function waitForInterruptedChatRecovery(delayMs: number) {
  if (delayMs <= 0) {
    return Promise.resolve();
  }
  return new Promise<void>((resolve) => window.setTimeout(resolve, delayMs));
}

export function ridocRevisionAfterPreflightAutosave(
  storageKind: "legacy" | "ridoc",
  requestRevision: string | null | undefined,
  latestRevision: string | null,
) {
  return storageKind === "ridoc" ? latestRevision : requestRevision;
}

export function recoveredLearningRequirementFailureReason(commit: CommitRecord | null): string | null {
  const metadata = commit?.metadata;
  if (metadata?.kind === "learning_requirement_generation_failed") {
    const generationFailureReason = metadata.generation_failure_reason;
    return typeof generationFailureReason === "string" && generationFailureReason.trim()
      ? generationFailureReason.trim()
      : DEFAULT_BOARD_GENERATION_FAILURE_REASON;
  }
  if (
    metadata?.learning_requirement_operation_status !== "failed" &&
    metadata?.refinement_route !== "refinement_failed"
  ) {
    return null;
  }
  const failureReason = metadata.learning_requirement_operation_failure_reason;
  return typeof failureReason === "string" && failureReason.trim()
    ? failureReason.trim()
    : DEFAULT_LEARNING_REQUIREMENT_FAILURE_REASON;
}

export function useLessonChatAgent({
  repositoryId,
  storageKind = "legacy",
  ridocRevision = null,
  activeLesson,
  activeMessages,
  activeComposerState,
  composerSelections,
  sourceQueryScope,
  currentBoardDocument,
  selectedTextModel,
  textModelReady,
  isPreviewMode,
  flushAutoSave,
  getLatestRidocRevision,
  getPendingHistoricalFork,
  restorePendingHistoricalFork,
  exitPreviewMode,
  updateCoursePackage,
  updateLessonMessages,
  updateLessonComposerState,
  setStreamingDocumentPreview,
  onTransientBoardFocusChange,
  clearSelection,
  setError,
  setBusyAction,
}: UseLessonChatAgentOptions) {
  const api = useStudioApi();
  const [clarificationQuestions, setClarificationQuestions] = useState<string[]>([]);
  const [learningClarity, setLearningClarity] = useState<LearningClarificationStatus | null>(null);
  const [streamedRequirementSheet, setStreamedRequirementSheet] = useState<LearningRequirementSheet | null>(null);
  const [streamedBoardTaskSheet, setStreamedBoardTaskSheet] = useState<BoardTaskRequirementSheet | null>(null);
  const currentNeedPending = false;
  const [latestBoardDecision, setLatestBoardDecision] = useState<BoardDecision | null>(null);
  const activeLessonIdRef = useRef<string | null>(activeLesson?.id ?? null);
  const activeChatTurnsRef = useRef<Map<string, ActiveChatTurn>>(new Map());
  const [chatBusyByLesson, setChatBusyByLesson] = useState<Record<string, ChatTurnBusyAction>>({});
  const textChatSessionIdsRef = useRef<Map<string, string>>(new Map());
  const guideRestartLessonIdsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    activeLessonIdRef.current = activeLesson?.id ?? null;
  }, [activeLesson?.id]);

  const chatInput = activeComposerState.chatInput;
  const composerMode = activeComposerState.composerMode;
  const includeSelectionInPrompt = activeComposerState.includeSelectionInPrompt;
  const composerAttachments = activeComposerState.composerAttachments;
  const sourceQuerySelections = activeComposerState.sourceQuerySelections ?? [];
  const waitingQueue = activeComposerState.waitingQueue ?? [];
  const activeLessonId = activeLesson?.id ?? null;
  const isChatBusy = activeLessonId ? Boolean(chatBusyByLesson[activeLessonId]) : false;
  const pendingHistoricalFork = activeComposerState.pendingHistoricalFork ?? null;
  const forkConversationMessages = pendingHistoricalFork && activeLesson
    ? buildLessonMessagesFromHistory(activeLesson, pendingHistoricalFork.sourceNodeId)
    : null;
  const forkSourceMessage = pendingHistoricalFork
    ? forkConversationMessages?.find(
        (message) =>
          message.id === pendingHistoricalFork.sourceMessageId ||
          (message.role === "assistant" && message.commitId === pendingHistoricalFork.sourceNodeId)
      ) ?? null
    : null;

  function clearPendingHistoricalFork() {
    if (!activeLesson) {
      return;
    }
    updateLessonComposerState(activeLesson.id, (current) => ({
      ...current,
      pendingHistoricalFork: null,
    }));
  }

  function waitUntilChatIdle(lessonId: string, timeoutMs = 15_000) {
    const started = Date.now();
    return new Promise<void>((resolve, reject) => {
      const tick = () => {
        if (!activeChatTurnsRef.current.has(lessonId)) {
          resolve();
          return;
        }
        if (Date.now() - started >= timeoutMs) {
          reject(new Error("Timed out waiting for the current agent turn to stop."));
          return;
        }
        window.setTimeout(tick, 40);
      };
      tick();
    });
  }

  function handleEnqueueWaitingMessage(rawText?: string) {
    if (!activeLesson) {
      return false;
    }
    const text = (rawText ?? chatInput).trim();
    if (!text) {
      return false;
    }
    const item = createChatWaitingQueueItem(text);
    updateLessonComposerState(activeLesson.id, (current) => ({
      ...current,
      chatInput: rawText === undefined ? "" : current.chatInput,
      waitingQueue: [...(current.waitingQueue ?? []), item],
    }));
    return true;
  }

  function handleRemoveWaitingMessage(itemId: string) {
    if (!activeLesson) {
      return;
    }
    updateLessonComposerState(activeLesson.id, (current) => ({
      ...current,
      waitingQueue: (current.waitingQueue ?? []).filter((item) => item.id !== itemId),
    }));
  }

  function handlePromoteWaitingMessage(itemId: string) {
    if (!activeLesson) {
      return;
    }
    updateLessonComposerState(activeLesson.id, (current) => {
      const queue = [...(current.waitingQueue ?? [])];
      const index = queue.findIndex((item) => item.id === itemId);
      if (index <= 0) {
        return current;
      }
      const [item] = queue.splice(index, 1);
      queue.unshift(item);
      return { ...current, waitingQueue: queue };
    });
  }

  function handleRestoreWaitingMessageToInput(itemId: string) {
    if (!activeLesson) {
      return;
    }
    updateLessonComposerState(activeLesson.id, (current) => {
      const queue = current.waitingQueue ?? [];
      const item = queue.find((entry) => entry.id === itemId);
      if (!item) {
        return current;
      }
      return {
        ...current,
        chatInput: item.text,
        waitingQueue: queue.filter((entry) => entry.id !== itemId),
      };
    });
  }

  async function handleGuideWaitingMessage(itemId: string) {
    if (!textModelReady || !activeLesson || guideRestartLessonIdsRef.current.has(activeLesson.id)) {
      return;
    }
    const item = (activeComposerState.waitingQueue ?? []).find((entry) => entry.id === itemId);
    if (!item?.text.trim()) {
      return;
    }
    const lessonId = activeLesson.id;
    guideRestartLessonIdsRef.current.add(lessonId);
    const guideText = item.text.trim();
    const basePrompt = activeChatTurnsRef.current.get(lessonId)?.prompt.trim() ?? "";
    updateLessonComposerState(lessonId, (current) => ({
      ...current,
      waitingQueue: (current.waitingQueue ?? []).filter((entry) => entry.id !== itemId),
    }));
    try {
      if (activeChatTurnsRef.current.has(lessonId)) {
        handleStopChat();
        await waitUntilChatIdle(lessonId);
      }
      const message = basePrompt
        ? `${basePrompt}\n\n[User guidance] Continue or adjust the current task using these requirements:\n${guideText}`
        : `[User guidance] Follow these requirements:\n${guideText}`;
      await handleSubmitChat({
        message,
        interaction_mode: composerMode,
      });
    } catch (error) {
      setError(error instanceof Error ? error.message : "Failed to guide the running agent");
    } finally {
      guideRestartLessonIdsRef.current.delete(lessonId);
    }
  }

  type ChatTurnBeforeRequestResult = {
    lesson?: Lesson | null;
    document?: BoardDocument | null;
  };
  type ChatTurnBeforeRequestContext = {
    lessonId: string;
    pendingMessageId: string;
  };
  type RunChatTurnOptions = {
    lesson: Lesson;
    payload: ChatRequestPayload;
    conversationMessages: ChatMessage[];
    userMessageContent: string;
    submittedSelection: SelectionRef | null;
    submittedSourceSelections?: SelectionRef[];
    busyActionName: ChatTurnBusyAction;
    flushReason: AutoSaveReason;
    clearComposerInput?: boolean;
    restoreComposerInput?: string;
    restoreComposerAttachments?: ChatAttachmentRef[];
    rollbackMessages?: ChatMessage[];
    rollbackOnFailure?: boolean;
    beforeRequest?: (context: ChatTurnBeforeRequestContext) => Promise<ChatTurnBeforeRequestResult | void>;
    messageListUpdater?: (current: ChatMessage[], userMessage: ChatMessage, pendingAssistant: ChatMessage) => ChatMessage[];
    onSuccess?: () => void;
  };

  function updatePendingAssistant(
    lessonId: string,
    messageId: string,
    patch: Partial<Pick<ChatMessage, "agentActivity" | "content" | "statusLabel">>
  ) {
    updateLessonMessages(lessonId, (current) =>
      current.map((message) => (message.id === messageId ? { ...message, ...patch } : message))
    );
  }

  function restoreComposerInputIfUntouched(lessonId: string, value: string) {
    updateLessonComposerState(lessonId, (current) =>
      current.chatInput.length > 0
        ? current
        : {
            ...current,
            chatInput: value,
          }
    );
  }

  function restoreComposerAttachmentsIfUntouched(lessonId: string, value: ChatAttachmentRef[]) {
    updateLessonComposerState(lessonId, (current) =>
      current.composerAttachments.length
        ? current
        : {
            ...current,
            composerAttachments: value,
          }
    );
  }

  function clearSelectionForLesson(lessonId: string) {
    updateLessonComposerState(lessonId, (current) => ({
      ...current,
      composerMode: "ask",
      includeSelectionInPrompt: true,
      composerSelection: null,
      composerSelections: [],
      sourceQuerySelections: [],
      sourceQueryAllReady: false,
    }));
    if (activeLessonIdRef.current === lessonId) {
      clearSelection();
    }
  }

  function conversationFromMessages(messages: ChatMessage[]) {
    return messages.slice(-8).map(({ role, content }) => ({ role, content }));
  }

  function displayContentForPayload(payload: ChatRequestPayload) {
    if (payload.board_generation_action === "start") {
      return "Start generating board content";
    }
    if (payload.teaching_action === "continue") {
      return "Continue to the next title";
    }
    if (payload.teaching_action === "restart") {
      return "Retelling from the first title";
    }
    const content = payload.interaction_mode === "direct_edit" ? `Direct lesson edit: ${payload.message}` : payload.message;
    if (!payload.attachments?.length) {
      return content;
    }
    return `${content}\n\nAttachments: ${payload.attachments.map((attachment) => attachment.name).join(", ")}`;
  }

  function latestCommitFromPackage(coursePackage: CoursePackage, lessonId: string): CommitRecord | null {
    const lesson = coursePackage.lessons.find((item) => item.id === lessonId);
    if (!lesson) {
      return null;
    }
    const branch = lesson.history_graph.branches[lesson.history_graph.current_branch];
    const commitId = branch?.head_commit_id ?? lesson.history_graph.commits[lesson.history_graph.commits.length - 1]?.id;
    return lesson.history_graph.commits.find((commit) => commit.id === commitId) ?? null;
  }

  function lessonFromPackage(coursePackage: CoursePackage, lessonId: string) {
    return coursePackage.lessons.find((item) => item.id === lessonId) ?? null;
  }

  function recoveredCommitForTurn(lesson: Lesson, submittedMessage: string, requestStartedAtMs: number) {
    const earliestCommitMs = requestStartedAtMs - 5000;
    const normalizedMessage = submittedMessage.trim();
    return (
      [...lesson.history_graph.commits]
        .reverse()
        .find((commit) => {
          const userMessage = commit.metadata?.user_message;
          if (typeof userMessage !== "string" || userMessage.trim() !== normalizedMessage) {
            return false;
          }
          return new Date(commit.created_at).getTime() >= earliestCommitMs;
        }) ?? null
    );
  }

  function shouldStreamDocumentPreview(payload: ChatRequestPayload, document: BoardDocument | null) {
    if (payload.interaction_mode === "direct_edit") {
      return false;
    }
    return (
      payload.board_generation_action === "start" ||
      isBoardDocumentEmpty(document)
    );
  }

  function resetAgentState(options?: { clearComposerSelection?: boolean }) {
    setClarificationQuestions([]);
    setLearningClarity(null);
    setStreamedRequirementSheet(null);
    setStreamedBoardTaskSheet(null);
    setLatestBoardDecision(null);
    if (options?.clearComposerSelection !== false) {
      clearSelection();
    }
  }

  async function runChatTurn({
    lesson,
    payload,
    conversationMessages,
    userMessageContent,
    submittedSelection,
    submittedSourceSelections = [],
    busyActionName,
    flushReason,
    clearComposerInput = false,
    restoreComposerInput,
    restoreComposerAttachments,
    rollbackMessages,
    rollbackOnFailure = false,
    beforeRequest,
    messageListUpdater,
    onSuccess,
  }: RunChatTurnOptions) {
    if (!textModelReady) {
      return;
    }
    const lessonId = lesson.id;
    let textChatSessionId = textChatSessionIdsRef.current.get(lessonId);
    if (!textChatSessionId) {
      textChatSessionId = createTextChatSessionId();
      textChatSessionIdsRef.current.set(lessonId, textChatSessionId);
    }
    const identifiedPayload = freezeTextChatTurnIdentity(payload, textChatSessionId);
    const payloadWithConversation: ChatRequestPayload = {
      ...identifiedPayload,
      expected_ridoc_revision:
        storageKind === "ridoc" ? ridocRevision : identifiedPayload.expected_ridoc_revision,
      post_generation_action: identifiedPayload.post_generation_action ?? "stop_after_generation",
      text_model: identifiedPayload.text_model ?? selectedTextModel,
      conversation: identifiedPayload.conversation ?? conversationFromMessages(conversationMessages),
    };
    const hasSubmittedSourceScope = Boolean(payloadWithConversation.source_query_scope);

    if (!payloadWithConversation.message.trim()) {
      return;
    }
    if (activeChatTurnsRef.current.has(lessonId)) {
      return;
    }

    const pendingBranch = lesson.history_graph.branches[lesson.history_graph.current_branch];
    if (storageKind !== "ridoc" && !payloadWithConversation.history_branch_action && pendingBranch?.document_redo_commit_ids?.length) {
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
        const applied = updateCoursePackage(branchedPackage, {
          activeLessonId: lesson.id,
          rebuildMessageLessonIds: [],
        });
        const branchedLesson = applied?.activeLesson ?? branchedPackage.lessons.find((item) => item.id === lesson.id);
        if (!branchedLesson) {
          throw new Error("The branched lesson is unavailable");
        }
        lesson = branchedLesson;
      } catch (branchError) {
        setError(branchError instanceof Error ? branchError.message : "Failed to create a document branch");
        return;
      }
    }

    const userMessage = createChatMessage(
      "user",
      userMessageContent,
      "ready",
      undefined,
      submittedSelection,
      null,
      { sourceSelections: submittedSourceSelections }
    );
    const pendingAssistantMessage: ChatMessage = {
      ...createChatMessage("assistant", "", "pending"),
      statusLabel: payloadWithConversation.source_query_scope ? "Retrieving original information" : "Saving current document",
    };
    let requestStarted = false;
    let streamedChatContent = "";
    let streamedDocumentText = "";
    let streamedDocumentPreviewFrame: number | null = null;
    let streamedAgentActivity: AgentActivityEvent[] = [];
    let sawReadyForBoardRequirementUpdate = false;
    let requestLesson = lesson;
    let baseStreamingDocument = currentBoardDocument ?? lesson.board_document;
    let requestStartedAtMs = Date.now();
    const abortController = new AbortController();
    const activeTurn: ActiveChatTurn = {
      abortController,
      abortRequested: false,
      cancellation: null,
      prompt: payloadWithConversation.message.trim(),
    };
    let canStreamDocumentPreview = false;
    const isRequestLessonActive = () => activeLessonIdRef.current === lessonId;

    function flushStreamingDocumentPreview() {
      if (!canStreamDocumentPreview || !streamedDocumentText) {
        return;
      }
      setStreamingDocumentPreview(requestLesson.id, {
        ...baseStreamingDocument,
        content_json: {},
        content_html: streamingMarkdownToHtml(streamedDocumentText),
        content_text: streamedDocumentText,
      });
    }

    function scheduleStreamingDocumentPreview() {
      if (!canStreamDocumentPreview) {
        return;
      }
      if (streamedDocumentPreviewFrame !== null) {
        return;
      }
      streamedDocumentPreviewFrame = window.requestAnimationFrame(() => {
        streamedDocumentPreviewFrame = null;
        flushStreamingDocumentPreview();
      });
    }

    function clearStreamingDocumentPreviewFrame() {
      if (streamedDocumentPreviewFrame === null) {
        return;
      }
      window.cancelAnimationFrame(streamedDocumentPreviewFrame);
      streamedDocumentPreviewFrame = null;
    }

    function finishCancelledTurn() {
      clearStreamingDocumentPreviewFrame();
      if (rollbackOnFailure && rollbackMessages) {
        updateLessonMessages(lessonId, () => rollbackMessages);
        restorePendingHistoricalFork();
        if (restoreComposerInput !== undefined) {
          restoreComposerInputIfUntouched(lessonId, restoreComposerInput);
        }
        if (restoreComposerAttachments?.length) {
          restoreComposerAttachmentsIfUntouched(lessonId, restoreComposerAttachments);
        }
        return;
      }
      const stoppedContent = streamedChatContent.trim();
      updateLessonMessages(lessonId, (current) =>
        current
          .map((message) =>
            message.id === pendingAssistantMessage.id
              ? {
                  ...message,
                  content: streamedChatContent,
                  agentActivity: streamedAgentActivity,
                  status: "ready" as const,
                  statusLabel: undefined,
                }
              : message
          )
          .filter((message) => message.id !== pendingAssistantMessage.id || Boolean(stoppedContent))
      );
      if (isRequestLessonActive()) {
        setError(
          payloadWithConversation.board_generation_action === "start" || sawReadyForBoardRequirementUpdate
            ? "Board generation was stopped. Confirmed learning requirements were retained and can be retried."
            : null
        );
      }
    }

    activeChatTurnsRef.current.set(lessonId, activeTurn);
    setChatBusyByLesson((current) => ({ ...current, [lessonId]: busyActionName }));
    setBusyAction(busyActionName);
    setError(null);
    onTransientBoardFocusChange(lessonId, null);
    if (!isBoardDocumentEmpty(currentBoardDocument ?? lesson.board_document)) {
      setLearningClarity(null);
      setStreamedRequirementSheet(null);
      setStreamedBoardTaskSheet(null);
    }
    if (clearComposerInput) {
      updateLessonComposerState(lessonId, (current) => ({
        ...current,
        chatInput: "",
        composerAttachments: [],
      }));
    }
    updateLessonMessages(lessonId, (current) =>
      messageListUpdater
        ? messageListUpdater(current, userMessage, pendingAssistantMessage)
        : [...current, userMessage, pendingAssistantMessage]
    );

    try {
      if (!(await flushAutoSave(flushReason))) {
        if (rollbackMessages) {
          updateLessonMessages(lessonId, () => rollbackMessages);
        } else {
          updateLessonMessages(lessonId, (current) =>
            current.filter((message) => message.id !== pendingAssistantMessage.id && message.id !== userMessage.id)
          );
        }
        if (restoreComposerInput !== undefined) {
          restoreComposerInputIfUntouched(lessonId, restoreComposerInput);
        }
        return;
      }
      if (payloadWithConversation.history_branch_action?.kind === "fork") {
        const pendingAfterFlush = getPendingHistoricalFork();
        if (
          !pendingAfterFlush ||
          pendingAfterFlush.sourceNodeId !== payloadWithConversation.history_branch_action.source_node_id
        ) {
          delete payloadWithConversation.history_branch_action;
        }
      }
      if (storageKind === "ridoc") {
        const latestRevision = ridocRevisionAfterPreflightAutosave(
          storageKind,
          payloadWithConversation.expected_ridoc_revision,
          getLatestRidocRevision(),
        );
        if (!latestRevision) {
          throw new Error("The latest RIDOC revision is unavailable; reload before chatting");
        }
        payloadWithConversation.expected_ridoc_revision = latestRevision;
      }
      if (payloadWithConversation.source_query_scope) {
        clearSelectionForLesson(lessonId);
      }
      const beforeRequestResult = await beforeRequest?.({
        lessonId,
        pendingMessageId: pendingAssistantMessage.id,
      });
      if (beforeRequestResult?.lesson) {
        requestLesson = beforeRequestResult.lesson;
      }
      if (beforeRequestResult?.document !== undefined) {
        baseStreamingDocument = beforeRequestResult.document ?? baseStreamingDocument;
      } else if (beforeRequestResult?.lesson) {
        baseStreamingDocument = beforeRequestResult.lesson.board_document;
      }
      canStreamDocumentPreview = shouldStreamDocumentPreview(payloadWithConversation, baseStreamingDocument);
      if (abortController.signal.aborted) {
        finishCancelledTurn();
        return;
      }
      requestStarted = true;
      requestStartedAtMs = Date.now();
      activeTurn.cancellation = {
        lessonId: requestLesson.id,
        sessionId: identifiedPayload.session_id,
        inputEventId: identifiedPayload.input_event_id,
      };
      updatePendingAssistant(lessonId, pendingAssistantMessage.id, { statusLabel: "Replying" });
      const response = await api.streamChatOnLesson(
        requestLesson.id,
        payloadWithConversation,
        {
          onPhase(label) {
            updatePendingAssistant(lessonId, pendingAssistantMessage.id, { statusLabel: label });
          },
          onAgentActivity(event) {
            streamedAgentActivity = mergeAgentActivityEvent(streamedAgentActivity, event);
            updatePendingAssistant(lessonId, pendingAssistantMessage.id, {
              agentActivity: streamedAgentActivity,
              statusLabel: publicAgentActivityLabel(event.label),
            });
          },
          onChatDelta(delta) {
            streamedChatContent += delta;
            updatePendingAssistant(lessonId, pendingAssistantMessage.id, {
              content: streamedChatContent,
              agentActivity: streamedAgentActivity,
              statusLabel:
                hasSubmittedSourceScope &&
                sawReadyForBoardRequirementUpdate &&
                Boolean(streamedDocumentText.trim())
                  ? "Starting from the first title"
                  : "Replying",
            });
          },
          onDocumentDelta(delta) {
            if (!canStreamDocumentPreview) {
              return;
            }
            streamedDocumentText += delta;
            if (hasSubmittedSourceScope) {
              updatePendingAssistant(lessonId, pendingAssistantMessage.id, { statusLabel: "Generating board content" });
            }
            scheduleStreamingDocumentPreview();
          },
          onDocumentSnapshot(snapshot) {
            if (!snapshot.content_text) {
              return;
            }
            // Native teaching tasks may resume from an already non-empty partial board.
            // A committed snapshot is authoritative and should always replace the preview.
            canStreamDocumentPreview = true;
            streamedDocumentText = snapshot.content_text;
            updatePendingAssistant(lessonId, pendingAssistantMessage.id, {
              statusLabel: "Board saved; continuing",
            });
            scheduleStreamingDocumentPreview();
          },
          onRequirementUpdate(payload) {
            if (payload.learning_clarification?.ready_for_board) {
              sawReadyForBoardRequirementUpdate = true;
              updatePendingAssistant(lessonId, pendingAssistantMessage.id, {
                statusLabel:
                  payload.requirement_phase === "frozen" ? "Generating board content" : "Learning needs confirmed",
              });
            }
            if (isRequestLessonActive()) {
              setClarificationQuestions(payload.clarification_questions);
              setLearningClarity(payload.learning_clarification);
              setStreamedRequirementSheet(payload.active_requirement_sheet ?? payload.learning_requirement_sheet);
            }
            if (hasSubmittedSourceScope) {
              updatePendingAssistant(lessonId, pendingAssistantMessage.id, { statusLabel: "Data range has been located" });
            }
          },
          onBoardTaskUpdate(payload) {
            const nextTask = payload.active_board_task_sheet ?? payload.board_task_sheet;
            if (isRequestLessonActive()) {
              setStreamedRequirementSheet(null);
              setLearningClarity(null);
              setClarificationQuestions([]);
              setStreamedBoardTaskSheet(nextTask);
              onTransientBoardFocusChange(
                lessonId,
                resolvedBoardFocusForTurn("learning_need", nextTask)
              );
            }
          },
        },
        { signal: abortController.signal, repositoryId }
      );
      clearStreamingDocumentPreviewFrame();
      flushStreamingDocumentPreview();
      const failedStreamingDocumentPreview =
        canStreamDocumentPreview &&
        streamedDocumentText.trim() &&
        response.board_document_operation_status === "failed"
          ? {
              ...baseStreamingDocument,
              content_json: {},
              content_html: streamingMarkdownToHtml(streamedDocumentText),
              content_text: streamedDocumentText,
            }
          : null;
      const responseCommit = latestCommitFromPackage(response.course_package, requestLesson.id);
      const committedUserMessage: ChatMessage = responseCommit
        ? {
            ...userMessage,
            id: `${responseCommit.id}:user`,
            commitId: responseCommit.id,
            parentCommitIds: responseCommit.parent_ids,
            editableContent: payloadWithConversation.message,
            interactionMode: payloadWithConversation.interaction_mode ?? "ask",
            editedFromCommitId: payloadWithConversation.chat_edit_source_commit_id ?? null,
          }
        : userMessage;
      updateCoursePackage(response.course_package, {
        mergeLessonId: requestLesson.id,
        activeLessonId: activeLessonIdForAsyncPackage(
          response.course_package,
          requestLesson.id,
          activeLessonIdRef.current,
          response.course_package.active_lesson_id ?? requestLesson.id
        ),
      });
      if (failedStreamingDocumentPreview) {
        setStreamingDocumentPreview(requestLesson.id, failedStreamingDocumentPreview);
      }
      if (isRequestLessonActive() && response.board_document_operation_status === "failed") {
        setError(response.board_document_operation_failure_reason ?? "The document generation on the right failed, please try again.");
      }
      if (isRequestLessonActive() && response.learning_requirement_operation_status === "failed") {
        setError(
          response.learning_requirement_operation_failure_reason ??
            "This round of learning requirements was not updated successfully, please try again."
        );
      }
      if (isRequestLessonActive() && response.auto_teaching_operation_status === "failed") {
        setError("The board content has been generated, but the automatic explanation has not been completed; you can send \"Restart from the first title\" to try again." +
          (response.auto_teaching_operation_failure_reason ?? ""));
      }
      const nextBoardTaskSheet = response.active_board_task_sheet ?? response.board_task_sheet ?? null;
      if (isRequestLessonActive()) {
        setLatestBoardDecision(response.board_decision);
        setClarificationQuestions(response.clarification_questions);
        setLearningClarity(response.learning_clarification);
        setStreamedRequirementSheet(
          response.requirement_cleared || nextBoardTaskSheet
            ? null
            : response.active_requirement_sheet ?? response.learning_requirement_sheet
        );
        setStreamedBoardTaskSheet(nextBoardTaskSheet);
        onTransientBoardFocusChange(
          lessonId,
          resolvedBoardFocusForTurn(response.turn_decision?.intent, nextBoardTaskSheet)
        );
      }
      const chatbotMessage = response.chatbot_message.trim();
      const streamedFallbackMessage = streamedChatContent.trim();
      const finalAgentActivity = normalizeAgentActivityEvents(
        response.agent_activity?.length ? response.agent_activity : streamedAgentActivity
      );
      const assistantMessages: ChatMessage[] = [];
      if (chatbotMessage) {
        assistantMessages.push(
          createChatMessage(
            "assistant",
            chatbotMessage,
            "ready",
            responseCommit ? `${responseCommit.id}:assistant` : undefined,
            null,
            response.teaching_progress ?? null,
            responseCommit
              ? {
                  agentActivity: finalAgentActivity,
                  guidedRequirementDiscovery: response.guided_requirement_discovery ?? null,
                  followUpSuggestions: response.follow_up_suggestions ?? [],
                  sourceCitations: response.source_citations ?? [],
                  commitId: responseCommit.id,
                  parentCommitIds: responseCommit.parent_ids,
                }
              : {
                  agentActivity: finalAgentActivity,
                  guidedRequirementDiscovery: response.guided_requirement_discovery ?? null,
                  followUpSuggestions: response.follow_up_suggestions ?? [],
                  sourceCitations: response.source_citations ?? [],
                }
          )
        );
      } else if (streamedFallbackMessage) {
        assistantMessages.push(
          createChatMessage(
            "assistant",
            streamedFallbackMessage,
            "ready",
            responseCommit ? `${responseCommit.id}:assistant` : undefined,
            null,
            response.teaching_progress ?? null,
            responseCommit
              ? {
                  agentActivity: finalAgentActivity,
                  guidedRequirementDiscovery: response.guided_requirement_discovery ?? null,
                  followUpSuggestions: response.follow_up_suggestions ?? [],
                  sourceCitations: response.source_citations ?? [],
                  commitId: responseCommit.id,
                  parentCommitIds: responseCommit.parent_ids,
                }
              : {
                  agentActivity: finalAgentActivity,
                  guidedRequirementDiscovery: response.guided_requirement_discovery ?? null,
                  followUpSuggestions: response.follow_up_suggestions ?? [],
                  sourceCitations: response.source_citations ?? [],
                }
          )
        );
      }
      updateLessonMessages(lessonId, (current) => [
        ...current
          .map((message) => (message.id === userMessage.id ? committedUserMessage : message))
          .filter((message) => message.id !== pendingAssistantMessage.id),
        ...assistantMessages,
      ]);
      clearSelectionForLesson(lessonId);
      onSuccess?.();
    } catch (chatError) {
      if (abortController.signal.aborted && activeTurn.abortRequested) {
        finishCancelledTurn();
        return;
      }
      const rawErrorMessage = chatError instanceof Error ? chatError.message : "Chat failed";
      const isTransientNetworkError =
        rawErrorMessage.toLowerCase().includes("network error") ||
        rawErrorMessage.toLowerCase().includes("failed to fetch");
      const userFacingError =
        payloadWithConversation.board_generation_action === "start" && isTransientNetworkError
          ? "The board generation connection was interrupted. Select \"Start generating board\" to retry; your confirmed learning needs will be retained."
          : sawReadyForBoardRequirementUpdate && isTransientNetworkError
            ? "Your learning needs are confirmed, but board generation was interrupted. Select \"Start generating board\" to continue."
            : rawErrorMessage;
      if (isMissingChatStreamFinalError(chatError) || isTransientNetworkError) {
        try {
          updatePendingAssistant(lessonId, pendingAssistantMessage.id, {
            statusLabel: "Restoring saved result",
          });
          let refreshedPackage: CoursePackage | null = null;
          let refreshedLesson: Lesson | null = null;
          let recoveredCommit: CommitRecord | null = null;
          let turnStatus: "running" | "finished" | null = null;
          let lastRefreshError: unknown = null;
          for (const delayMs of INTERRUPTED_CHAT_RECOVERY_DELAYS_MS) {
            await waitForInterruptedChatRecovery(delayMs);
            if (abortController.signal.aborted && activeTurn.abortRequested) {
              finishCancelledTurn();
              return;
            }
            try {
              const status = await api.getChatTurnStatus(requestLesson.id, {
                session_id: identifiedPayload.session_id,
                input_event_id: identifiedPayload.input_event_id,
              }, repositoryId);
              turnStatus = status.status;
            } catch (statusError) {
              lastRefreshError = statusError;
            }
            try {
              const candidatePackage = await api.getCoursePackage(repositoryId);
              const candidateLesson = lessonFromPackage(candidatePackage, requestLesson.id);
              const candidateCommit =
                candidateLesson !== null
                  ? recoveredCommitForTurn(
                      candidateLesson,
                      payloadWithConversation.message,
                      requestStartedAtMs
                    )
                  : null;
              refreshedPackage = candidatePackage;
              refreshedLesson = candidateLesson;
              recoveredCommit = candidateCommit;
            } catch (refreshAttemptError) {
              lastRefreshError = refreshAttemptError;
            }
            if (recoveredCommit || (turnStatus === "finished" && refreshedPackage)) {
              break;
            }
          }
          if (!refreshedPackage) {
            throw lastRefreshError ?? new Error("Refresh failed");
          }
          if (recoveredCommit || !rollbackOnFailure) {
            updateCoursePackage(refreshedPackage, {
              mergeLessonId: requestLesson.id,
              activeLessonId: activeLessonIdForAsyncPackage(
                refreshedPackage,
                requestLesson.id,
                activeLessonIdRef.current,
                refreshedPackage.active_lesson_id ?? requestLesson.id
              ),
              rebuildMessageLessonIds: recoveredCommit ? [requestLesson.id] : undefined,
            });
          }
          if (refreshedLesson && isRequestLessonActive()) {
            setStreamedRequirementSheet(
              recoveredCommit || refreshedLesson.board_task_requirements
                ? null
                : refreshedLesson.learning_requirements ?? null
            );
            setStreamedBoardTaskSheet(refreshedLesson.board_task_requirements ?? null);
            setLearningClarity(recoveredCommit ? learningClarityFromCommit(recoveredCommit) : null);
            setClarificationQuestions([]);
          }
          if (recoveredCommit) {
            onSuccess?.();
            if (isRequestLessonActive()) {
              setError(recoveredLearningRequirementFailureReason(recoveredCommit));
            }
            return;
          }
          updateLessonMessages(lessonId, (current) =>
            rollbackOnFailure && rollbackMessages
              ? rollbackMessages
              : current.filter(
                  (message) =>
                    message.id !== pendingAssistantMessage.id && (requestStarted || message.id !== userMessage.id)
                )
          );
          if (isRequestLessonActive()) {
            setError(
              turnStatus === "running"
                ? "The chat connection was interrupted, but generation is still running in the background. Refresh later to load the saved result."
                : "The chat connection was interrupted before the final result was returned, and no history was written this round; you can try again."
            );
          }
          if (rollbackOnFailure) {
            restorePendingHistoricalFork();
            if (restoreComposerInput !== undefined) {
              restoreComposerInputIfUntouched(lessonId, restoreComposerInput);
            }
            if (restoreComposerAttachments?.length) {
              restoreComposerAttachmentsIfUntouched(lessonId, restoreComposerAttachments);
            }
          }
          return;
        } catch (refreshError) {
          const refreshMessage = refreshError instanceof Error ? refreshError.message : "Refresh failed";
          updateLessonMessages(lessonId, (current) =>
            rollbackOnFailure && rollbackMessages
              ? rollbackMessages
              : current.filter(
                  (message) =>
                    message.id !== pendingAssistantMessage.id && (requestStarted || message.id !== userMessage.id)
                )
          );
          if (isRequestLessonActive()) {
            setError(`${rawErrorMessage}; could not refresh the latest history: ${refreshMessage}`);
          }
          if (rollbackOnFailure) {
            restorePendingHistoricalFork();
            if (restoreComposerInput !== undefined) {
              restoreComposerInputIfUntouched(lessonId, restoreComposerInput);
            }
            if (restoreComposerAttachments?.length) {
              restoreComposerAttachmentsIfUntouched(lessonId, restoreComposerAttachments);
            }
          }
          return;
        }
      }
      if (restoreComposerInput !== undefined && !sawReadyForBoardRequirementUpdate) {
        restoreComposerInputIfUntouched(lessonId, restoreComposerInput);
      }
      if (restoreComposerAttachments?.length && !sawReadyForBoardRequirementUpdate) {
        restoreComposerAttachmentsIfUntouched(lessonId, restoreComposerAttachments);
      }
      if ((rollbackOnFailure || !requestStarted) && rollbackMessages) {
        updateLessonMessages(lessonId, () => rollbackMessages);
      } else {
        updateLessonMessages(lessonId, (current) =>
          current.filter(
            (message) =>
              message.id !== pendingAssistantMessage.id &&
              (restoreComposerInput === undefined || message.id !== userMessage.id)
          )
        );
      }
      if (isRequestLessonActive()) {
        setError(userFacingError);
      }
      if (rollbackOnFailure) {
        restorePendingHistoricalFork();
      }
    } finally {
      clearStreamingDocumentPreviewFrame();
      if (activeChatTurnsRef.current.get(lessonId) === activeTurn) {
        activeChatTurnsRef.current.delete(lessonId);
        setChatBusyByLesson((current) => {
          const next = { ...current };
          delete next[lessonId];
          return next;
        });
      }
      if (activeLessonIdRef.current === lessonId) {
        setBusyAction(null);
      }
    }
  }

  function handleStopChat() {
    const lessonId = activeLessonIdRef.current;
    const activeTurn = lessonId ? activeChatTurnsRef.current.get(lessonId) : null;
    if (!activeTurn) {
      return;
    }
    activeTurn.abortRequested = true;
    const activeCancellation = activeTurn.cancellation;
    if (activeCancellation) {
      void api
        .cancelChatOnLesson(activeCancellation.lessonId, {
          session_id: activeCancellation.sessionId,
          input_event_id: activeCancellation.inputEventId,
        }, repositoryId)
        .catch(() => undefined);
    }
    activeTurn.abortController.abort();
  }

  async function handleSubmitChat(payloadOverride?: ChatRequestPayload) {
    if (!textModelReady || !activeLesson || activeChatTurnsRef.current.has(activeLesson.id)) {
      return;
    }
    // React busy state can lag one frame behind the per-lesson turn registry after stop/guide.
    if (isChatBusy && !guideRestartLessonIdsRef.current.has(activeLesson.id)) {
      return;
    }
    if (isPreviewMode) {
      exitPreviewMode();
    }
    const pendingForkBeforeSubmit = pendingHistoricalFork;
    if (pendingForkBeforeSubmit && !(await flushAutoSave("chat"))) {
      return;
    }
    const pendingForkAfterPreflight = getPendingHistoricalFork();
    const historicalForkStillPending = Boolean(
      pendingForkBeforeSubmit &&
      pendingForkAfterPreflight?.sourceNodeId === pendingForkBeforeSubmit.sourceNodeId
    );
    const submittedInput = chatInput;
    const submittedAttachments = composerAttachments;
    const includedSelections = includeSelectionInPrompt ? composerSelections : [];
    const basePayload =
      payloadOverride ??
      ({
        message:
          chatInput.trim() ||
          (composerAttachments.length
            ? "Please see the attachment I added."
            : includedSelections.length
              ? "Please answer based on what I quoted."
              : ""),
        selection: includedSelections[includedSelections.length - 1] ?? null,
        selections: includedSelections,
        attachments: composerAttachments,
        interaction_mode: composerMode,
      } satisfies ChatRequestPayload);
    const payload: ChatRequestPayload =
      sourceQueryScope &&
      basePayload.source_query_scope === undefined &&
      basePayload.interaction_mode !== "direct_edit" &&
      !basePayload.board_generation_action
        ? { ...basePayload, source_query_scope: sourceQueryScope }
        : basePayload;
    const submittedSelection = payload.selection ?? payload.selections?.at(-1) ?? null;
    const payloadMessage = payload.message.trim();
    if (!payloadMessage) {
      return;
    }
    const activeForkSource = forkSourceMessage;
    if (pendingHistoricalFork && !activeForkSource) {
      setError("The historical branch source is unavailable; return to the current version and try again.");
      return;
    }
    const forkSourceIndex = activeForkSource && forkConversationMessages
      ? forkConversationMessages.findIndex((message) => message.id === activeForkSource.id)
      : -1;
    if (activeForkSource && (!activeForkSource.commitId || forkSourceIndex < 0)) {
      setError("This response is no longer available for branching");
      return;
    }
    const forkContextMessages =
      activeForkSource && forkConversationMessages && forkSourceIndex >= 0
        ? forkConversationMessages.slice(0, forkSourceIndex + 1)
        : null;
    const payloadForTurn: ChatRequestPayload = {
      ...payload,
      message: payloadMessage,
      history_branch_action: historicalForkStillPending && activeForkSource?.commitId
        ? { kind: "fork", source_node_id: activeForkSource.commitId }
        : payload.history_branch_action,
    };
    const isBoardGenerationControl = payloadForTurn.board_generation_action === "start";

    await runChatTurn({
      lesson: activeLesson,
      payload: payloadForTurn,
      conversationMessages: forkContextMessages ?? activeMessages,
      userMessageContent: displayContentForPayload(payloadForTurn),
      submittedSelection,
      submittedSourceSelections:
        payloadForTurn.source_query_scope?.mode === "all_ready_sources"
          ? [{ kind: "source", excerpt: "All available sources in this course", source_scope_kind: "source" }]
          : payloadForTurn.source_query_scope
            ? sourceQuerySelections
            : [],
      busyActionName: payloadForTurn.interaction_mode === "direct_edit" ? "agent-edit" : "chat",
      flushReason: "chat",
      clearComposerInput: !payloadOverride || isBoardGenerationControl,
      restoreComposerInput: payloadOverride || isBoardGenerationControl ? undefined : submittedInput,
      restoreComposerAttachments: payloadOverride || isBoardGenerationControl ? undefined : submittedAttachments,
      rollbackMessages: forkContextMessages && historicalForkStillPending ? activeMessages : undefined,
      rollbackOnFailure: Boolean(forkContextMessages && historicalForkStillPending),
      messageListUpdater: forkContextMessages
        ? (_current, userMessage, pendingAssistant) => [
            ...forkContextMessages,
            userMessage,
            pendingAssistant,
          ]
        : undefined,
      onSuccess: forkContextMessages && historicalForkStillPending
        ? () => clearPendingHistoricalFork()
        : undefined,
    });
  }

  async function handleEditMessage(sourceMessage: ChatMessage, nextContent: string) {
    if (!textModelReady || !activeLesson || activeChatTurnsRef.current.has(activeLesson.id) || isChatBusy || isPreviewMode) {
      return;
    }
    const editedMessage = nextContent.trim();
    const sourceCommitId = sourceMessage.commitId;
    const baseCommitId = sourceMessage.parentCommitIds?.[0];
    if (!sourceCommitId || !baseCommitId || !editedMessage) {
      setError("This message lacks a forkable history");
      return;
    }
    const sourceIndex = activeMessages.findIndex((message) => message.id === sourceMessage.id);
    if (sourceIndex < 0) {
      setError("No historical message found for editing");
      return;
    }
    const originalMessage = sourceMessage.editableContent ?? sourceMessage.content;
    if (editedMessage === originalMessage.trim()) {
      return;
    }
    const prefixMessages = activeMessages.slice(0, sourceIndex);
    const rollbackMessages = activeMessages;
    const payload: ChatRequestPayload = {
      message: editedMessage,
      selection: sourceMessage.selection ?? null,
      interaction_mode: sourceMessage.interactionMode ?? "ask",
      chat_edit_source_commit_id: sourceCommitId,
      chat_edit_base_commit_id: baseCommitId,
      chat_edit_original_message: originalMessage,
    };

    await runChatTurn({
      lesson: activeLesson,
      payload,
      conversationMessages: prefixMessages,
      userMessageContent: editedMessage,
      submittedSelection: sourceMessage.selection ?? null,
      busyActionName: "chat-edit",
      flushReason: "chat",
      rollbackMessages,
      messageListUpdater: (_current, userMessage, pendingAssistant) => [
        ...prefixMessages,
        userMessage,
        pendingAssistant,
      ],
      beforeRequest: async ({ lessonId, pendingMessageId }) => {
        updatePendingAssistant(lessonId, pendingMessageId, { statusLabel: "Creating new link" });
        const branchName = nextEditBranchName(activeLesson);
        const branchedPackage = await api.createBranch(activeLesson.id, branchName, baseCommitId, repositoryId);
        const applied = updateCoursePackage(branchedPackage, {
          activeLessonId: activeLesson.id,
        });
        const branchedLesson =
          applied?.activeLesson ?? branchedPackage.lessons.find((lesson) => lesson.id === activeLesson.id) ?? null;
        return {
          lesson: branchedLesson,
          document: branchedLesson?.board_document ?? null,
        };
      },
    });
  }

  function pairedUserMessage(sourceAssistant: ChatMessage) {
    const assistantIndex = activeMessages.findIndex(
      (message) => message.id === sourceAssistant.id
    );
    if (assistantIndex < 0 || !sourceAssistant.commitId) {
      return null;
    }
    for (let index = assistantIndex - 1; index >= 0; index -= 1) {
      const candidate = activeMessages[index];
      if (
        candidate.role === "user" &&
        candidate.commitId === sourceAssistant.commitId
      ) {
        return { message: candidate, index };
      }
    }
    return null;
  }

  async function handleRegenerateMessage(sourceAssistant: ChatMessage) {
    if (
      storageKind !== "ridoc" ||
      !ridocRevision ||
      !textModelReady ||
      !activeLesson ||
      isChatBusy ||
      isPreviewMode ||
      !sourceAssistant.commitId
    ) {
      return;
    }
    const paired = pairedUserMessage(sourceAssistant);
    if (!paired) {
      setError("This response has no complete source question");
      return;
    }
    const originalQuestion = (
      paired.message.editableContent ?? paired.message.content
    ).trim();
    if (!originalQuestion) {
      return;
    }
    const prefixMessages = activeMessages.slice(0, paired.index);
    const rollbackMessages = activeMessages;
    clearPendingHistoricalFork();
    await runChatTurn({
      lesson: activeLesson,
      payload: {
        message: originalQuestion,
        interaction_mode: paired.message.interactionMode ?? "ask",
        selection: paired.message.selection ?? null,
        history_branch_action: {
          kind: "regenerate",
          source_node_id: sourceAssistant.commitId,
        },
      },
      conversationMessages: prefixMessages,
      userMessageContent: originalQuestion,
      submittedSelection: paired.message.selection ?? null,
      submittedSourceSelections: paired.message.sourceSelections ?? [],
      busyActionName: "chat",
      flushReason: "chat",
      rollbackMessages,
      rollbackOnFailure: true,
      messageListUpdater: (_current, userMessage, pendingAssistant) => [
        ...prefixMessages,
        userMessage,
        pendingAssistant,
      ],
    });
  }

  async function handleContinueTeaching() {
    if (!textModelReady || !activeLesson) {
      return;
    }
    await handleSubmitChat({
      message: "Continue to next item",
      interaction_mode: "ask",
      teaching_action: "continue",
    });
  }

  return {
    chatInput,
    composerMode,
    includeSelectionInPrompt,
    isChatBusy,
    waitingQueue,
    clarificationQuestions,
    learningClarity,
    streamedRequirementSheet,
    streamedBoardTaskSheet,
    currentNeedPending,
    latestBoardDecision,
    forkSourceMessage,
    resetAgentState,
    handleSubmitChat,
    handleStopChat,
    handleEnqueueWaitingMessage,
    handleGuideWaitingMessage,
    handleRemoveWaitingMessage,
    handlePromoteWaitingMessage,
    handleRestoreWaitingMessageToInput,
    handleEditMessage,
    handleRegenerateMessage,
    handleContinueTeaching,
  };
}
