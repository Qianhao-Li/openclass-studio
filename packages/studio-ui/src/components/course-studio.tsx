"use client";

import clsx from "clsx";
import { useEffect, useRef, useState, type CSSProperties } from "react";

import { useStudioHost } from "../contexts/studio-host-context";
import { HistoryStudioLeaveDialog } from "./course-studio/history-studio-leave-dialog";
import { BoardEditorPanel } from "./course-studio/board-editor-panel";
import { CourseStudioChatSidebar } from "./course-studio/chat-sidebar";
import { CourseStudioPageShell } from "./course-studio/course-studio-page-shell";
import { StudioWorkspaceEntry } from "./course-studio/studio-workspace-entry";
import { HistoryNodeDeletionDialog } from "./course-studio/history-node-deletion-dialog";
import type { FormulaInkEditorSubmitPayload } from "./course-studio/word-board-editor";
import {
  appendComposerSelection,
  learningClarityFromCommit,
} from "./course-studio/history-utils";
import { useHistoryMessages } from "../hooks/course-studio/use-history-messages";
import {
  samePopoverPosition,
  sameSelection,
  type SelectionPopoverPosition,
} from "./course-studio/selection-utils";
import { StudioNavigation, StudioEmptyState } from "./course-studio/studio-navigation";
import { SelectionPopover } from "./course-studio/selection-popover";
import { useCoursePrReview } from "../contexts/course-pr-review-context";
import { StudioApiProvider, useStudioContext, type StudioContext, type CourseStudioProps } from "../contexts/studio-api-context";
import { useStudioNavigation } from "../hooks/course-studio/use-studio-navigation";
import {
  applySourceReferenceToComposerState,
  sourceQueryScopeFromComposer,
} from "./course-studio/source-query-scope";
import { CourseStudioSidePanel, type CourseStudioSidebarTab } from "./course-studio/studio-side-panel";
import { studioDocumentTransfers } from "../lib/studio-document-transfers";
import { useInterfaceLanguage } from "../contexts/interface-language-context";
import { useBoardDraft } from "../hooks/course-studio/use-board-draft";
import { useChatSpeech } from "../hooks/course-studio/use-chat-speech";
import { useCourseWorkspace, type CoursePackageApplyOptions } from "../hooks/course-studio/use-course-workspace";
import { useLessonChatAgent } from "../hooks/course-studio/use-lesson-chat-agent";
import { useLessonHistory } from "../hooks/course-studio/use-lesson-history";
import { useHistoryNodeDeletion } from "../hooks/course-studio/use-history-node-deletion";
import { useHistoryEditSession } from "../hooks/course-studio/use-history-edit-session";
import { lessonPackageControlProps } from "../lib/lesson-package-control-props";
import { useLessonMerge } from "../hooks/course-studio/use-lesson-merge";
import { useLessonPackage } from "../hooks/course-studio/use-lesson-package";
import { useModelCatalog } from "../hooks/course-studio/use-model-catalog";
import { usePendingHistoricalFork } from "../hooks/course-studio/use-pending-historical-fork";
import {
  applyRealtimeToolStatusUpdate,
  applyRealtimeTranscriptUpdate,
  type RealtimeTranscriptUpdate,
} from "./course-studio/realtime-message-state";
import type { RealtimeToolStatusUpdate } from "../hooks/course-studio/codex-live-task-ui";
import { useRealtimeVoice } from "../hooks/course-studio/use-realtime-voice";
import { useWorkspaceActions } from "../hooks/course-studio/use-workspace-actions";
import { useStudioLifecycle } from "../hooks/course-studio/use-studio-lifecycle";
import { useResizablePanelWidth } from "../hooks/use-resizable-panel-width";
import type {
  AIModelOption,
  BoardFocusRef,
  ChatInteractionMode,
  ChatRequestPayload,
  CoursePackage,
  LearningClarificationStatus,
  RealtimeToolCallResponse,
  SelectionRef,
} from "../types";

const CHAT_PANEL_WIDTH_STORAGE_KEY = "openclass:studio:chat-panel-width";
const CHAT_PANEL_DEFAULT_WIDTH = 380;
const CHAT_PANEL_MIN_WIDTH = 300;
const CHAT_PANEL_MAX_WIDTH = 640;
const RIGHT_SIDEBAR_WIDTH_STORAGE_KEY = "openclass:studio:right-sidebar-width";
const RIGHT_SIDEBAR_DEFAULT_WIDTH = 360;
const RIGHT_SIDEBAR_MIN_WIDTH = 280;
const RIGHT_SIDEBAR_MAX_WIDTH = 900;

export function CourseStudio(props: CourseStudioProps = {}) {
  const context: StudioContext = props.studioContext ?? (props.repositoryId ? { kind: "repository", repositoryId: props.repositoryId } : { kind: "workspace" });
  return <StudioApiProvider context={context}><CourseStudioContent {...props} /></StudioApiProvider>;
}

function CourseStudioContent({ initialCoursePackage = null, readOnly = false, repositoryId = null, repositoryProjects = [] }: CourseStudioProps) {
  const host = useStudioHost();
  const studioContext = useStudioContext();
  const reviewOperationBusy = useCoursePrReview()?.operationBusy ?? false;
  const { texts: txt } = useInterfaceLanguage();
  const studioTexts = txt.studio;
  const mainContainerRef = useRef<HTMLDivElement | null>(null);
  const chatInputRef = useRef<HTMLTextAreaElement | null>(null);
  const chatScrollEndRef = useRef<HTMLDivElement | null>(null);

  const workspace = useCourseWorkspace(repositoryId, initialCoursePackage);
  const {
    coursePackage,
    isLoading,
    error,
    setError,
    lessonMap,
    activeLesson,
    openLessons,
    activeMessages,
    activeComposerState,
    updateLessonMessages,
    updateLessonComposerState,
    updateActiveLessonComposerState,
    applyCoursePackage: applyWorkspaceCoursePackage,
  } = workspace;
  const modelSelection = useModelCatalog();
  const {
    modelCatalog,
    selectedTextModel,
    selectedRealtimeModel,
    selectedTextOption,
    selectedRealtimeOption,
    selectedRealtimeTransport,
    codexConnectionControls,
    openModelMenu,
    setOpenModelMenu,
    selectTextModel,
    selectRealtimeModel,
  } = modelSelection;
  const textModelReady = Boolean(selectedTextOption?.enabled);
  const [selection, setSelection] = useState<SelectionRef | null>(null);
  const [geometryReference, setGeometryReference] = useState<SelectionRef | null>(null);
  const [selectionPopover, setSelectionPopover] = useState<SelectionPopoverPosition | null>(null);
  const [realtimeTeachingFocusState, setRealtimeTeachingFocusState] = useState<{
    lessonId: string;
    focus: BoardFocusRef;
  } | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [topCollapsed, setTopCollapsed] = useState(false);
  const [rightSidebarOpen, setRightSidebarOpen] = useState(studioContext.kind === "review");
  const {
    width: chatPanelWidth,
    isResizing: isChatPanelResizing,
    dragHandleProps: chatPanelResizeHandleProps,
  } = useResizablePanelWidth({
    storageKey: CHAT_PANEL_WIDTH_STORAGE_KEY,
    defaultWidth: CHAT_PANEL_DEFAULT_WIDTH,
    minWidth: CHAT_PANEL_MIN_WIDTH,
    maxWidth: CHAT_PANEL_MAX_WIDTH,
    label: "Adjust Chatbot width",
  });
  const {
    width: rightSidebarWidth,
    isResizing: isRightSidebarResizing,
    dragHandleProps: rightSidebarResizeHandleProps,
  } = useResizablePanelWidth({
    storageKey: RIGHT_SIDEBAR_WIDTH_STORAGE_KEY,
    defaultWidth: RIGHT_SIDEBAR_DEFAULT_WIDTH,
    minWidth: RIGHT_SIDEBAR_MIN_WIDTH,
    maxWidth: RIGHT_SIDEBAR_MAX_WIDTH,
    dragDirection: "grow-left",
    label: "Adjust course workbench auxiliary width",
  });
  const [sidebarTab, setSidebarTab] = useState<CourseStudioSidebarTab>("history");
  function clearPendingHistoricalForkState(lessonId: string) {
    updateLessonComposerState(lessonId, (current) => ({
      ...current,
      pendingHistoricalFork: null,
    }));
  }
  const boardDraft = useBoardDraft({
    repositoryId,
    storageKind: coursePackage?.storage_kind, ridocRevision: coursePackage?.ridoc_revision,
    activeLesson,
    setError,
    setBusyAction,
    applyCoursePackage: applyWorkspaceCoursePackage,
    applyAutoSavedCoursePackage: workspace.applyAutoSavedCoursePackage,
    getHistoryDeletionGeneration: workspace.getHistoryDeletionGeneration,
    onPackageApplied: resetTransientUi,
    onHistoricalForkCommitted: clearPendingHistoricalForkState,
  });
  function updateCoursePackage(nextPackage: CoursePackage, options?: CoursePackageApplyOptions) {
    const result = applyWorkspaceCoursePackage(nextPackage, options);
    const isBackgroundLessonUpdate =
      Boolean(options?.mergeLessonId) && result.activeLesson?.id !== options?.mergeLessonId;
    if (!isBackgroundLessonUpdate) {
      boardDraft.resetToLesson(result.activeLesson);
      resetTransientUi();
    }
    return result;
  }

  const historicalFork = usePendingHistoricalFork({
    repositoryId,
    storageKind: coursePackage?.storage_kind,
    activeLesson,
    activeMessages,
    activeComposerState,
    flushAutoSave: boardDraft.flushAutoSave,
    applyCoursePackage: updateCoursePackage,
    updateLessonComposerState,
    enterHistoricalFork: boardDraft.enterHistoricalFork,
    prepareReturnToCurrentVersion: boardDraft.prepareReturnToCurrentVersion,
    setError,
    setBusyAction,
  });

  const lessonMerge = useLessonMerge({
    disabled: readOnly, repositoryId,
    storageKind: coursePackage?.storage_kind, ridocRevision: coursePackage?.ridoc_revision,
    activeLesson,
    selectedTextModel,
    flushBoardAutoSave: boardDraft.flushAutoSave,
    applyCoursePackage: updateCoursePackage,
    setError,
    setBusyAction,
  });
  const history = useLessonHistory({
    repositoryId,
    storageKind: coursePackage?.storage_kind,
    ridocRevision: coursePackage?.ridoc_revision,
    activeLesson,
    flushAutoSave: boardDraft.flushAutoSave,
    resetDraftToLesson: boardDraft.exitPreviewToLesson,
    setPreviewDocument: boardDraft.setPreviewDocument,
    applyCoursePackage: updateCoursePackage,
    setError,
    setBusyAction,
  });
  const {
    flushAutoSave,
    flushAutoSaveWithBeacon,
    handleLocalDocumentChange,
    markStructureRemovalIntent,
    handleImportDocx,
    handleExportDocx,
    handleExportHtml,
  } = boardDraft;
  const {
    previewCommitId,
    newBranchName,
    setNewBranchName,
    handleCreateBranch,
    handlePreviewCommit,
    exitPreviewMode,
    handleCreateBranchFromCommit,
    handleSwitchBranch,
    handleSetChatCommitVisibility,
    handleEnsureDocumentHistoryBranch,
    ridocHistory, ridocHistorySyncing,
  } = history;

  const lessonPackage = useLessonPackage({
    repositoryId,
    ridocRevision: coursePackage?.ridoc_revision,
    activeLesson,
    mergeActive: lessonMerge.isActive,
    flushAutoSave: boardDraft.flushAutoSave,
    setPreviewCommitId: history.setPreviewCommitId,
    setPreviewDocument: boardDraft.setPreviewDocument,
    resetPreview: history.exitPreviewMode,
    createBranchFromCommit: history.handleCreateBranchFromCommit,
    applyCoursePackage: updateCoursePackage,
    setError,
    setBusyAction,
  });

  const documentTransfers = studioDocumentTransfers({
    pendingFork: Boolean(historicalFork.pendingFork), mergeActive: lessonMerge.isActive, onError: setError,
    importDocx: handleImportDocx, exportDocx: handleExportDocx, exportHtml: handleExportHtml,
    importRidoc: lessonPackage.importRidoc, exportRidoc: lessonPackage.exportRidoc,
  });
  const previewCommit = history.previewCommit;
  const activeHeadCommit = history.activeHeadCommit;
  const isPreviewMode = history.isPreviewMode;
  const isDraftPreviewMode = !isPreviewMode && boardDraft.isPreviewing;
  const displayedDocument = lessonMerge.draftDocument ?? boardDraft.displayedDocument;
  const displayedMessageCommitId =
    lessonPackage.playbackMessageCommitId ??
    history.chatPreviewCommit?.id ??
    historicalFork.pendingFork?.sourceNodeId ??
    null;
  const displayedMessages = useHistoryMessages(lessonPackage.playbackMessages, activeLesson,
    history.previewLesson, displayedMessageCommitId, activeMessages);
  const persistedRequirements = lessonMerge.session?.draft_runtime.learning_requirements ?? activeLesson?.learning_requirements ?? null;
  const persistedBoardTask = lessonMerge.session?.draft_runtime.board_task_requirements ?? activeLesson?.board_task_requirements ?? null;
  const previewLearningClarity = learningClarityFromCommit(history.chatPreviewCommit);
  const persistedLearningClarity = learningClarityFromCommit(activeHeadCommit);
  const currentRequirementCleared =
    !history.chatPreviewCommit && !persistedRequirements && activeHeadCommit?.metadata?.requirement_cleared === true;
  const latestAssistantMessage = [...activeMessages].reverse().find((message) => message.role === "assistant");
  const composerSelections = activeComposerState.composerSelections?.length
    ? activeComposerState.composerSelections
    : activeComposerState.composerSelection
      ? [activeComposerState.composerSelection]
      : [];
  const composerSelection = composerSelections[composerSelections.length - 1] ?? null;
  const composerAttachments = activeComposerState.composerAttachments;
  const sourceQuerySelections = activeComposerState.sourceQuerySelections ?? [];
  const sourceQueryAllReady = activeComposerState.sourceQueryAllReady ?? false;
  const sourceQueryScope = sourceQueryScopeFromComposer(sourceQuerySelections, sourceQueryAllReady);
  function exitAnyPreviewMode() {
    if (lessonPackage.isPlaybackActive) {
      lessonPackage.exitPlayback();
      return;
    }
    if (isDraftPreviewMode) {
      boardDraft.exitPreviewToLesson(activeLesson);
      return;
    }
    exitPreviewMode();
  }
  const chatAgent = useLessonChatAgent({
    repositoryId,
    storageKind: coursePackage?.storage_kind, ridocRevision: coursePackage?.ridoc_revision,
    activeLesson,
    activeMessages,
    activeComposerState,
    composerSelections,
    currentBoardDocument: displayedDocument,
    selectedTextModel,
    textModelReady,
    isPreviewMode: isPreviewMode || isDraftPreviewMode || lessonMerge.isActive,
    flushAutoSave,
    getLatestRidocRevision: boardDraft.getLatestRidocRevision,
    getPendingHistoricalFork: boardDraft.getPendingHistoricalFork,
    restorePendingHistoricalFork: boardDraft.restorePendingHistoricalFork,
    exitPreviewMode: exitAnyPreviewMode,
    updateCoursePackage,
    updateLessonMessages,
    updateLessonComposerState,
    sourceQueryScope,
    clearSelection: clearTurnSelection,
    setStreamingDocumentPreview: boardDraft.setStreamingDocumentPreview,
    onTransientBoardFocusChange: (lessonId, focus) => {
      setRealtimeTeachingFocusState(focus ? { lessonId, focus } : null);
    },
    setError,
    setBusyAction,
  });
  const {
    chatInput,
    composerMode,
    includeSelectionInPrompt,
    isChatBusy,
    clarificationQuestions,
    learningClarity,
    streamedRequirementSheet,
    streamedBoardTaskSheet,
    currentNeedPending,
    latestBoardDecision,
    handleSubmitChat,
    handleStopChat,
    handleEditMessage,
    handleRegenerateMessage,
    handleContinueTeaching,
  } = chatAgent;
  const historicalForkReturnDisabled =
    isChatBusy || boardDraft.autoSaveStatus === "saving";
  const historicalForkTransitioning =
    busyAction === "prepare-history-fork" || busyAction === "return-current-version";
  function handleReturnToCurrentVersion() {
    if (historicalForkReturnDisabled) {
      setError("Stop the active reply or wait for the branch edit to finish saving before returning.");
      return;
    }
    void historicalFork.returnToCurrentVersion();
  }
  const activeRequirements = streamedRequirementSheet ?? persistedRequirements;
  const activeBoardTask = streamedBoardTaskSheet ?? persistedBoardTask;
  const voice = useRealtimeVoice({
    repositoryId,
    activeLesson,
    latestAssistantMessageContent: latestAssistantMessage?.content ?? null,
    selectedTextModel,
    selectedRealtimeModel,
    selectedRealtimeOption,
    selectedRealtimeTransport,
    busyAction,
    setBusyAction,
    setError,
    flushAutoSave,
    ensureDocumentHistoryBranch: handleEnsureDocumentHistoryBranch,
    isChatBusy,
    onSubmitTranscript: (message) => {
      void handleSubmitChat({
        message,
        interaction_mode: "ask",
      });
    },
    currentSelection: includeSelectionInPrompt ? composerSelection : null,
    currentSelections: includeSelectionInPrompt ? composerSelections : [],
    onTranscriptUpdate: handleRealtimeTranscriptUpdate,
    onToolStatusUpdate: handleRealtimeToolStatusUpdate,
    onToolResult: handleRealtimeToolResult,
  });
  const {
    remoteAudioRef,
    voiceActive,
    voiceStatusText,
    handleVoiceToggle,
    stopRealtimeSession,
    sendRealtimeText,
    codexLiveTaskState,
    resolveCodexLiveTask,
  } = voice;
  const historyDeletion = useHistoryNodeDeletion({
    repositoryId, coursePackage, lessonId: activeLesson?.id ?? null,
    readOnly, isChatBusy, voiceActive, busyAction,
    autoSaveStatus: boardDraft.autoSaveStatus,
    mergeActive: lessonMerge.isActive, playbackActive: lessonPackage.isPlaybackActive,
    flushAutoSave, applyCoursePackage: updateCoursePackage, setBusyAction, setError,
    onDeleted: (lessonId) => {
      history.setPreviewCommitId(null);
      historicalFork.clearPendingFork(lessonId);
      clearSelection();
      setGeometryReference(null);
    },
  });
  const historyOrder = useHistoryEditSession({
    repositoryId, coursePackage, lesson: activeLesson, ridocHistory, getCoursePackageSnapshot: workspace.getCoursePackageSnapshot,
    readOnly, flushAutoSave, applyCoursePackage: updateCoursePackage,
    endPreview: () => { lessonPackage.exitPlayback(); history.exitPreviewMode(); boardDraft.exitPreviewToLesson(activeLesson); },
    busyReason: isChatBusy ? "Wait for the reply to finish, or stop generation." : voiceActive || busyAction === "voice-connect" ? "End the voice session first."
      : lessonMerge.isActive ? "Complete or discard the current merge first." : historicalFork.pendingFork || historicalForkTransitioning ? "Return to the current version first."
      : isDraftPreviewMode ? "Finish the current board draft first." : historyDeletion.preview || historyDeletion.phase !== "idle" ? "Finish the current deletion first."
      : boardDraft.autoSaveStatus === "saving" ? "The board is being saved. Please wait." : busyAction ? "The current operation is still running. Please wait." : null,
  });
  const chatSpeech = useChatSpeech({
    lessonId: activeLesson?.id ?? null,
    messages: activeMessages,
  });

  const latestDisplayedMessage = displayedMessages[displayedMessages.length - 1];
  useEffect(() => {
    chatScrollEndRef.current?.scrollIntoView({ block: "end" });
  }, [activeLesson?.id, displayedMessages.length, latestDisplayedMessage?.content, isChatBusy]);

  const realtimeTeachingFocus =
    realtimeTeachingFocusState && realtimeTeachingFocusState.lessonId === activeLesson?.id
      ? realtimeTeachingFocusState.focus
      : null;

  function handleRealtimeTranscriptUpdate(update: RealtimeTranscriptUpdate) {
    updateLessonMessages(update.lessonId, (current) => applyRealtimeTranscriptUpdate(current, update));
  }

  function handleRealtimeToolStatusUpdate(update: RealtimeToolStatusUpdate) {
    updateLessonMessages(update.lessonId, (current) => applyRealtimeToolStatusUpdate(current, update));
  }

  function handleRealtimeToolResult(lessonId: string, result: RealtimeToolCallResponse) {
    if (historicalFork.pendingFork) {
      setError("A realtime result arrived after historical branching began. Return to the current version and choose the branch point again.");
      return;
    }
    if (result.course_package) {
      updateCoursePackage(result.course_package, { activeLessonId: lessonId });
    }
    if (result.resolved_focus?.source === "board") {
      setRealtimeTeachingFocusState({ lessonId, focus: result.resolved_focus });
    } else {
      setRealtimeTeachingFocusState(null);
    }
  }

  async function handleStudioSubmitChat(payloadOverride?: ChatRequestPayload) {
    const realtimePayload = payloadOverride ?? {
      message: chatInput.trim(),
      interaction_mode: composerMode,
      selection: includeSelectionInPrompt ? composerSelection : null,
      selections: includeSelectionInPrompt ? composerSelections : [],
      attachments: composerAttachments,
      source_query_scope: sourceQueryScope ?? undefined,
    };
    const canUseRealtimeText =
      !historicalFork.pendingFork &&
      voiceActive &&
      realtimePayload.interaction_mode !== "direct_edit" &&
      !realtimePayload.attachments?.length &&
      !realtimePayload.formula_ink &&
      !realtimePayload.board_generation_action &&
      !realtimePayload.teaching_action;
    const canUseRealtimeSourceFreeText = canUseRealtimeText && !realtimePayload.source_query_scope;
    if (canUseRealtimeSourceFreeText && sendRealtimeText(realtimePayload.message)) {
      updateActiveLessonComposerState((current) => ({
        ...current,
        chatInput: "",
        composerAttachments: [],
      }));
      return;
    }
    await handleSubmitChat(payloadOverride);
  }

  const clarityStatus: LearningClarificationStatus =
    previewLearningClarity ??
    learningClarity ??
    (currentRequirementCleared ? null : persistedLearningClarity) ?? {
      progress: 0,
      label: "",
      reason: "",
      missing_items: [],
      can_start: false,
      forced_start: false,
      summary: "",
      key_facts: [],
      checklist: [],
      next_question: "",
      ready_for_board: false,
    };
  const clarityBarTone =
    clarityStatus.progress >= 90
      ? "bg-emerald-500"
      : clarityStatus.can_start
      ? "bg-blue-500"
      : "bg-amber-500";

  function applySelection(nextSelection: SelectionRef, popoverPosition?: SelectionPopoverPosition | null) {
    setSelection((current) => (sameSelection(current, nextSelection) ? current : nextSelection));
    setSelectionPopover((current) => {
      const nextPosition = popoverPosition ?? null;
      return samePopoverPosition(current, nextPosition) ? current : nextPosition;
    });
    updateActiveLessonComposerState((current) => ({
      ...current,
      composerMode: "ask",
      composerSelection: null,
    }));
  }

  function clearTransientSelection() {
    setSelection((current) => (current ? null : current));
    setSelectionPopover((current) => (current ? null : current));
  }

  function clearSelection() {
    clearTransientSelection();
    updateActiveLessonComposerState((current) => ({
      ...current,
      composerMode: "ask",
      includeSelectionInPrompt: true,
      composerSelection: null,
      composerSelections: [],
      sourceQuerySelections: [],
      sourceQueryAllReady: false,
    }));
  }

  function clearTurnSelection() {
    clearTransientSelection();
    updateActiveLessonComposerState((current) => ({
      ...current,
      composerMode: "ask",
      includeSelectionInPrompt: true,
      composerSelection: null,
      composerSelections: [],
      sourceQuerySelections: [],
      sourceQueryAllReady: false,
    }));
  }

  async function focusComposerWithSelection(nextMode: ChatInteractionMode, explicitSelection?: SelectionRef) {
    if (historyOrder.active && !(await historyOrder.guardNavigation())) return;
    const selectionToFocus = explicitSelection ?? selection;
    if (!selectionToFocus) {
      return;
    }
    const normalizedSelection: SelectionRef =
      selectionToFocus.kind === "board"
        ? {
            ...selectionToFocus,
            location_kind: selectionToFocus.location_kind === "insertion_anchor" ? "insertion_anchor" : "target_range",
          }
        : selectionToFocus;
    setSelection(normalizedSelection);
    updateActiveLessonComposerState((current) => ({
      ...current,
      composerMode: nextMode,
      includeSelectionInPrompt: true,
      composerSelection: normalizedSelection,
      composerSelections:
        nextMode === "direct_edit"
          ? [normalizedSelection]
          : appendComposerSelection(current.composerSelections ?? [], normalizedSelection),
    }));
    setSelectionPopover(null);
    window.requestAnimationFrame(() => {
      chatInputRef.current?.focus();
    });
  }

  async function openGeometryWithSelection(explicitSelection?: SelectionRef) {
    if (historyOrder.active && !(await historyOrder.guardNavigation())) return;
    const selectionToOpen = explicitSelection ?? selection;
    if (!selectionToOpen || selectionToOpen.kind !== "board") {
      return;
    }
    const normalizedSelection: SelectionRef = {
      ...selectionToOpen,
      location_kind: selectionToOpen.location_kind === "insertion_anchor" ? "insertion_anchor" : "target_range",
    };
    setSelection(normalizedSelection);
    setSelectionPopover(null);
    setGeometryReference({ ...normalizedSelection });
    setSidebarTab("geometry");
    setRightSidebarOpen(true);
  }

  function applySourceReference(sourceReference: SelectionRef) {
    if (
      sourceReference.kind !== "source" ||
      !sourceReference.source_ingestion_id ||
      !sourceReference.source_content_hash
    ) {
      return;
    }
    setSelection(sourceReference);
    setSelectionPopover(null);
    updateActiveLessonComposerState((current) =>
      applySourceReferenceToComposerState(current, sourceReference)
    );
    window.requestAnimationFrame(() => {
      chatInputRef.current?.focus();
    });
  }

  function applyAllReadySourcesReference() {
    setSelection(null);
    setSelectionPopover(null);
    updateActiveLessonComposerState((current) => ({
      ...current,
      composerMode: "ask",
      sourceQuerySelections: [],
      sourceQueryAllReady: true,
    }));
    window.requestAnimationFrame(() => chatInputRef.current?.focus());
  }

  function handleFormulaInkSubmit(payload: FormulaInkEditorSubmitPayload) {
    if (!activeLesson || lessonMerge.isActive || !textModelReady || isChatBusy) {
      return false;
    }
    const formulaSelection: SelectionRef = {
      kind: "board",
      location_kind: payload.selection.locationKind,
      lesson_id: activeLesson.id,
      document_id: payload.selection.documentId,
      excerpt: payload.selection.excerpt,
      before_text: payload.selection.beforeText,
      after_text: payload.selection.afterText,
    };
    setSelection(formulaSelection);
    setSelectionPopover(null);
    const isReplaceAction = payload.action === "replace";
    void handleSubmitChat({
      message: isReplaceAction
        ? "Please recognize my handwritten formula and change the currently selected formula to the recognition result."
        : "Please identify my handwritten formula and answer with the currently selected formula.",
      selection: formulaSelection,
      interaction_mode: isReplaceAction ? "direct_edit" : "ask",
      formula_ink: {
        action: payload.action,
        image_data_url: payload.imageDataUrl,
        source_latex: payload.sourceLatex,
      },
    });
    return true;
  }

  const { adjustComposerHeight } = useStudioLifecycle({
    chatInputRef, chatInput, selectionExcerpt: composerSelection?.excerpt,
    mergeActive: lessonMerge.isActive,
    openMergeHistory: () => { setSidebarTab("history"); setRightSidebarOpen(true); },
  });

  function resetTransientUi() {
    history.setPreviewCommitId(null);
    chatAgent.resetAgentState();
  }

  function resetTransientUiForLessonSwitch() {
    history.setPreviewCommitId(null);
    chatAgent.resetAgentState({ clearComposerSelection: false });
    clearTransientSelection();
  }

  const workspaceActions = useWorkspaceActions({
    repositoryId,
    coursePackage,
    activeLesson,
    lessonMap,
    flushAutoSave,
    updateCoursePackage,
    selectLocalLesson: workspace.selectLocalLesson,
    resetDraftToLesson: boardDraft.resetToLesson,
    resetTransientUi: resetTransientUiForLessonSwitch,
    setError,
    setBusyAction,
  });
  const {
    handleCreateLesson,
    handleCloseLesson,
    handleSelectLesson,
  } = workspaceActions;

  function handleSelectRealtimeModel(option: AIModelOption) {
    if (!option.enabled) {
      return;
    }
    if (voiceActive || busyAction === "voice-connect") {
      stopRealtimeSession("The real-time speech model has been switched and the current session has been disconnected");
    }
    selectRealtimeModel(option);
  }

  const navigation = useStudioNavigation({
    revision: coursePackage?.ridoc_revision,
    documentDirty: boardDraft.isDocumentDirty,
    chatBusy: isChatBusy || voiceActive,
    mergeActive: lessonMerge.isActive,
    historyDirty: historyOrder.pending,
    prepareLeave: async () => {
      if (historicalFork.pendingFork) { setError("Return to the current version before leaving this lesson."); return false; }
      return historyOrder.requestLeave();
    },
    flushAutoSave, flushBeacon: flushAutoSaveWithBeacon,
    flushMerge: lessonMerge.flushDraft, stopChat: () => { if (voiceActive) stopRealtimeSession("Leaving temporary learning"); return chatAgent.handleStopChat(); },
  });

  if (isLoading) {
    return <div className="flex min-h-screen items-center justify-center text-gray-500">{studioTexts.loading}</div>;
  }

  if (!coursePackage) return <StudioWorkspaceEntry empty={workspace.isEmptyWorkspace} error={error} onReload={workspace.reload} />;

  const workspaceTitle = coursePackage.is_standalone ? null : coursePackage.title;
  const sourcePackageId = repositoryId
    ? `repo__${repositoryId}__${coursePackage.id}`
    : coursePackage.id;
  const lessonTabs = <StudioNavigation
    repositoryId={repositoryId} repositoryProjects={repositoryProjects}
    readOnly={readOnly || studioContext.kind === "review" || historyOrder.lockOfficialWrites} texts={studioTexts} lessons={openLessons} activeLessonId={activeLesson?.id ?? null}
    beforeNavigate={historyOrder.pause} beforeLeave={historyOrder.requestLeave} creating={busyAction === "generate"} pendingFork={Boolean(historicalFork.pendingFork)} mergeActive={lessonMerge.isActive}
    onError={setError} onSelectLesson={handleSelectLesson} onCloseLesson={handleCloseLesson}
    onCreateLesson={handleCreateLesson} onNavigate={(path) => void navigation.requestLeave(path)}
  />;
  const selectionPopoverNode = (
    <SelectionPopover
      selection={selection}
      position={selectionPopover}
      onFocusComposerWithSelection={() => focusComposerWithSelection("ask")}
      onOpenGeometryWithSelection={() => openGeometryWithSelection()}
    />
  );


  return (
    <CourseStudioPageShell
      texts={studioTexts}
      workspaceTitle={workspaceTitle}
      topCollapsed={topCollapsed}
      rightSidebarOpen={rightSidebarOpen}
      error={error}
      tabs={lessonTabs}
      accountMenu={host.renderAccountMenu?.(navigation) ?? host.accountMenu}
      selectionPopover={selectionPopoverNode}
      onReturnHome={() => void navigation.returnHome()}
      onTopCollapsedChange={setTopCollapsed}
      onRightSidebarOpenChange={async (open) => { if (open || await historyOrder.guardNavigation()) setRightSidebarOpen(open); }}
      onClearError={() => setError(null)}
    >
      {host.collaborationChrome?.(navigation)}
      {!activeLesson || !displayedDocument ? (
        <StudioEmptyState texts={studioTexts} creating={busyAction === "generate"}
          onCreate={() => { setTopCollapsed(false); void handleCreateLesson(); }} />
      ) : <>
      <HistoryStudioLeaveDialog controller={historyOrder.leave} course={historyOrder.session?.draft_course_package ?? coursePackage} />
      <HistoryNodeDeletionDialog
        preview={historyDeletion.preview}
        busy={historyDeletion.phase === "deleting"}
        message={historyDeletion.message}
        disabledReason={historyDeletion.disabledReason}
        onCancel={historyDeletion.cancel}
        onConfirm={historyDeletion.confirm}
      />
      <div
        ref={mainContainerRef}
        style={
          {
            "--chat-panel-width": `${chatPanelWidth}px`,
            "--right-sidebar-width": `${rightSidebarWidth}px`,
          } as CSSProperties
        }
        className={clsx(
          "grid min-h-0 flex-1 grid-cols-[var(--chat-panel-width)_minmax(0,1fr)] overflow-hidden transition-[grid-template-columns]",
          isChatPanelResizing || isRightSidebarResizing ? "duration-0" : "duration-300",
          rightSidebarOpen && "xl:grid-cols-[var(--chat-panel-width)_minmax(0,1fr)_var(--right-sidebar-width)]"
        )}
      >
        <CourseStudioChatSidebar
          readOnly={readOnly}
          attachmentsDisabled={readOnly || reviewOperationBusy}
          packageId={sourcePackageId}
          resizeHandleProps={chatPanelResizeHandleProps}
          isResizing={isChatPanelResizing}
          clarityBarTone={clarityBarTone}
          clarityStatus={clarityStatus}
          activeLesson={activeLesson}
          targetCommitId={currentRequirementCleared ? null : history.chatPreviewCommit?.id ?? activeHeadCommit?.id ?? null}
          previewCommit={history.chatPreviewCommit}
          displayedMessages={displayedMessages}
          forkSourceMessage={historicalFork.sourceMessage}
          forkReturnDisabled={historicalForkReturnDisabled}
          ridocBranchingEnabled={!repositoryId && coursePackage?.storage_kind === "ridoc" && Boolean(coursePackage.ridoc_revision)}
          isPreviewMode={Boolean(history.chatPreviewCommit)}
          interactionLocked={readOnly || reviewOperationBusy || historyOrder.lockOfficialWrites || lessonMerge.isActive || historicalForkTransitioning || Boolean(historyDeletion.preview) || historyDeletion.phase !== "idle" || Boolean(busyAction?.startsWith("document-history-"))}
          isChatBusy={isChatBusy}
          clarificationQuestions={clarificationQuestions}
          activeBoardTask={activeBoardTask}
          activeRequirementSheet={activeRequirements}
          currentNeedPending={currentNeedPending}
          latestBoardDecision={latestBoardDecision}
          chatScrollEndRef={chatScrollEndRef}
          chatInputRef={chatInputRef}
          remoteAudioRef={remoteAudioRef}
          modelCatalog={modelCatalog}
          selectedTextModel={selectedTextModel}
          selectedRealtimeModel={selectedRealtimeModel}
          selectedTextOption={selectedTextOption}
          selectedRealtimeOption={selectedRealtimeOption}
          openModelMenu={openModelMenu}
          setOpenModelMenu={setOpenModelMenu}
          voiceActive={voiceActive}
          voiceStatusText={voiceStatusText}
          codexLiveTaskState={codexLiveTaskState}
          chatInput={chatInput}
          composerAttachments={composerAttachments}
          composerMode={composerMode}
          composerSelection={composerSelection}
          composerSelections={composerSelections}
          sourceQuerySelections={sourceQuerySelections}
          sourceQueryAllReady={sourceQueryAllReady}
          includeSelectionInPrompt={includeSelectionInPrompt}
          onApplySelection={applySelection}
          onContinueTeaching={() => void handleContinueTeaching()}
          onSubmitChat={(payload) => handleStudioSubmitChat(payload)}
          onStopChat={handleStopChat}
          onEditMessage={(message, nextContent) => handleEditMessage(message, nextContent)}
          onRegenerateMessage={(message) => handleRegenerateMessage(message)}
          onStartForkMessage={(message) => {
            if (voiceActive) {
              stopRealtimeSession("Historical branching uses the versioned text workflow.");
            }
            void historicalFork.startPendingFork(message).then(() => {
              chatInputRef.current?.focus();
            });
          }}
          onCancelFork={handleReturnToCurrentVersion}
          onHideChatCommit={(commitId) => handleSetChatCommitVisibility(commitId, "hidden")}
          onDeleteHistoryNode={historyDeletion.enabled ? historyDeletion.open : undefined}
          deletionDisabledReason={historyOrder.lockOfficialWrites ? "Finish or discard pending History changes before deleting." : historyDeletion.disabledReason}
          onSelectTextModel={selectTextModel}
          onSelectRealtimeModel={handleSelectRealtimeModel}
          onVoiceToggle={() => {
            if (historyOrder.lockOfficialWrites) { setError("Finish or discard pending History changes before starting voice."); return; }
            if (historicalFork.pendingFork) {
              setError("Return to the current version before starting realtime voice.");
              return;
            }
            void handleVoiceToggle();
          }}
          onResolveCodexLiveTask={resolveCodexLiveTask}
          onSpeakMessage={chatSpeech.speakMessage}
          onExitPreviewMode={() => { if (lessonPackage.isPlaybackActive) lessonPackage.exitPlayback(); else history.exitChatPreviewMode(); }}
          onClearSelection={clearSelection}
          onUpdateComposerState={updateActiveLessonComposerState}
          onAdjustComposerHeight={adjustComposerHeight}
          onError={setError}
        />

        <BoardEditorPanel
          readOnly={readOnly || reviewOperationBusy || historyOrder.lockOfficialWrites || Boolean(lessonMerge.session?.event_plan) || historicalForkTransitioning || Boolean(historyDeletion.preview) || historyDeletion.phase !== "idle" || Boolean(busyAction?.startsWith("document-history-"))}
          activeLesson={activeLesson}
          document={lessonPackage.isPlaybackActive ? displayedDocument : historyOrder.displayedDocument ?? displayedDocument}
          historyPending={historyOrder.pending}
          pendingHistoricalFork={historicalFork.pendingFork}
          historicalForkReturnDisabled={historicalForkReturnDisabled}
          isPreviewMode={isPreviewMode || Boolean(historyOrder.previewNodeId)}
          isDraftPreviewMode={isDraftPreviewMode}
          isMergeMode={lessonMerge.isActive}
          previewCommit={historyOrder.previewCommit ?? previewCommit}
          toolbarCollapsed={topCollapsed}
          transientTeachingFocus={realtimeTeachingFocus}
          onExitPreviewMode={() => { if (historyOrder.previewNodeId) historyOrder.resetPreview(); else exitAnyPreviewMode(); }}
          onReturnToCurrentVersion={handleReturnToCurrentVersion}
          onDocumentChange={lessonMerge.isActive ? lessonMerge.handleDocumentChange : handleLocalDocumentChange}
          onStructureRemovalIntent={markStructureRemovalIntent}
          onApplySelection={applySelection}
          onClearTransientSelection={clearTransientSelection}
          {...documentTransfers}
          onReferenceFormula={(formulaSelection) => focusComposerWithSelection("ask", formulaSelection)}
          onReferenceFormulaToGeometry={(formulaSelection) => openGeometryWithSelection(formulaSelection)}
          onFormulaInkSubmit={handleFormulaInkSubmit}
        />

        <CourseStudioSidePanel
          coursePrBinding={studioContext.kind === "review" ? { coursePackage, flush: flushAutoSave, apply: updateCoursePackage,
            historyPending: historyOrder.pending, prepareHistory: historyOrder.requestLeave,
            busyReason: isChatBusy || voiceActive ? "Wait for the AI reply to finish, or end the voice session." : busyAction ? "The current operation is still running." : null } : undefined}
          readOnly={readOnly}
          historyInteractionLocked={reviewOperationBusy || Boolean(historicalFork.pendingFork) || historicalForkTransitioning || Boolean(busyAction) || isChatBusy || voiceActive}
          sourcesDisabled={readOnly || reviewOperationBusy}
          open={rightSidebarOpen}
          resizeHandleProps={rightSidebarResizeHandleProps}
          isResizing={isRightSidebarResizing}
          sidebarTab={sidebarTab}
          onSidebarTabChange={async (tab) => { if (tab === "history" || await historyOrder.guardNavigation()) setSidebarTab(tab); }}
          onClose={async () => { if (await historyOrder.guardNavigation()) setRightSidebarOpen(false); }}
          activeLesson={activeLesson}
          packageId={coursePackage.id}
          storageKind={coursePackage.storage_kind}
          ridocHistory={ridocHistory}
          ridocHistorySyncing={ridocHistorySyncing}
          sourcePackageId={sourcePackageId}
          previewCommit={previewCommit}
          previewCommitId={previewCommitId}
          activeRequirements={activeRequirements}
          activeBoardTask={activeBoardTask}
          latestBoardDecision={latestBoardDecision}
          newBranchName={newBranchName}
          onNewBranchNameChange={setNewBranchName}
          onCreateBranch={() => handleCreateBranch()}
          onPreviewCommit={(commit, documentOnly) => handlePreviewCommit(commit, documentOnly)}
          onStepDocumentHistory={(direction, targetCommitId) =>
            historyOrder.step(direction, targetCommitId).then(() => undefined)
          }
          onSetChatCommitVisibility={(commitId, visibility) =>
            handleSetChatCommitVisibility(commitId, visibility)
          }
          onDeleteHistoryNode={historyDeletion.enabled ? historyDeletion.open : undefined}
          deletionDisabledReason={historyOrder.lockOfficialWrites ? "Finish or discard pending History changes before deleting." : historyDeletion.disabledReason}
          onCreateBranchFromCommit={(commit, continuationNodeId, branchSourceNodeId) => {
            if (!repositoryId && coursePackage.storage_kind === "ridoc") {
              void historicalFork.startPendingForkFromNode(branchSourceNodeId, continuationNodeId);
              return;
            }
            void handleCreateBranchFromCommit(commit);
          }}
          onReferenceHistoryNode={(historySelection) => focusComposerWithSelection("ask", historySelection)}
          onSwitchBranch={(branchName) => handleSwitchBranch(branchName)}
          onMergeBranch={(branchName) => lessonMerge.startMerge(branchName)}
          historyOrder={historyOrder}
          prepareCoursePr={() => flushAutoSave("manual")}
          lessonPackageControls={lessonPackageControlProps(lessonPackage)}
          mergeSession={lessonMerge.session}
          mergeDraftDirty={lessonMerge.isDraftDirty}
          mergeAIProposing={lessonMerge.isAIProposing}
          onResolveMergeConflict={lessonMerge.resolveConflict}
          onProposeMergeWithAI={lessonMerge.proposeWithAI}
          onCancelMergeAI={lessonMerge.cancelAI}
          onRecomputeMerge={lessonMerge.recompute}
          onAbandonMerge={lessonMerge.abandon}
          onSubmitMerge={lessonMerge.submit}
          onMergeOrderChange={lessonMerge.handleOrderChange}
          onError={setError}
          onSourceReference={applySourceReference}
          onAllReadySourcesReference={applyAllReadySourcesReference}
          geometryReference={geometryReference}
          onGeometryReferenceClear={() => setGeometryReference(null)}
          textModel={selectedTextModel}
          catalogModelOptions={modelCatalog.text}
          defaultCatalogModel={modelCatalog.defaults.text}
          selectedTextModel={selectedTextModel}
          selectedTextOption={selectedTextOption}
          textModelOptions={modelCatalog.text}
          onSelectTextModel={selectTextModel}
          codexConnectionControls={codexConnectionControls}
          speechAutoEnabled={chatSpeech.autoSpeakEnabled}
          speechIsLoading={chatSpeech.isSpeechLoading}
          speechIsPlaying={chatSpeech.isSpeechPlaying}
          speechIsPaused={chatSpeech.isSpeechPaused}
          speechStatusText={chatSpeech.speechStatusText}
          speechOptions={chatSpeech.speechOptions}
          speechSelectedVoice={chatSpeech.selectedVoice}
          speechRate={chatSpeech.speechRate}
          speechCurrentModel={chatSpeech.currentModel}
          speechCurrentText={chatSpeech.currentSpeechText}
          speechCurrentTime={chatSpeech.currentTime}
          speechDuration={chatSpeech.duration}
          speechCanSeek={chatSpeech.canSeekSpeech}
          speechCanReplay={chatSpeech.canReplaySpeech}
          onSpeechAutoToggle={chatSpeech.toggleAutoSpeak}
          onSpeechCancel={chatSpeech.stopSpeech}
          onSpeechPause={chatSpeech.pauseSpeech}
          onSpeechResume={chatSpeech.resumeSpeech}
          onSpeechReplay={chatSpeech.replayCurrentSpeech}
          onSpeechSeek={chatSpeech.seekSpeech}
          onSpeechVoiceChange={chatSpeech.selectVoice}
          onSpeechRateChange={chatSpeech.selectSpeechRate}
        />
      </div>
      </>}
    </CourseStudioPageShell>
  );
}
