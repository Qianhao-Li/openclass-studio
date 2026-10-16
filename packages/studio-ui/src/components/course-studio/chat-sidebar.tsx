import clsx from "clsx";
import {
  AudioLines,
  Check,
  ChevronDown,
  LoaderCircle,
  PencilLine,
  Send,
  Square,
  TextQuote,
  Volume2,
  X,
} from "lucide-react";
import { useRef, useState, type Dispatch, type HTMLAttributes, type ReactNode, type RefObject, type SetStateAction } from "react";

import { CourseChatMessage } from "../chatbot";
import { ChatAttachmentChips, ChatAttachmentMenu } from "./chat-attachment-menu";
import { CodexModelSettingsPicker } from "./codex-model-settings-picker";
import {
  modelButtonLabel,
  modelOptionKey,
  modelSelectionKey,
  PROVIDER_LABELS,
} from "./model-catalog";
import {
  latestExecutedNeedSnapshot,
  latestGenerationFailureSnapshot,
} from "./current-need-snapshot";
import { chatHistoryDeletionAction } from "./chat-history-deletion-action";
import { popoverPositionFromDomSelection } from "./selection-utils";
import { sourceQuerySelectionLabel } from "./source-query-scope";
import { LearningClarityCard } from "../learning-clarity-card";
import { useStudioApi } from "../../contexts/studio-api-context";
import { boardWorkflowLabel } from "../../lib/learning-requirement-display";
import { openSourceEvidencePreview } from "../../lib/source-evidence-preview";
import {
  codexLiveTaskHasVisibleActivity,
  codexLiveTaskIsLoading,
  codexLiveTaskSummary,
  type CodexLiveTaskAction,
  type CodexLiveTaskState,
} from "../../hooks/course-studio/codex-live-task-ui";
import type {
  AIModelCatalog,
  AIModelOption,
  AIModelSelection,
  BoardDecision,
  BoardTaskRequirementSheet,
  ChatAttachmentRef,
  ChatInteractionMode,
  ChatRequestPayload,
  CommitRecord,
  LearningClarificationStatus,
  LearningRequirementSheet,
  Lesson,
  SelectionRef,
  SourceCitation,
} from "../../types";
import type { ChatMessage, LessonComposerState } from "./history-utils";

type ModelMenu = "text" | "realtime" | null;

const BOARD_TASK_ACTION_LABELS: Partial<Record<NonNullable<BoardTaskRequirementSheet["requested_action"]>, string>> = {
  write: "write",
  edit: "Revise",
  explain: "explain",
  delete: "delete",
  interact: "interactive",
  chat: "interactive",
};

const BOARD_EXTENT_LABELS: Record<NonNullable<BoardTaskRequirementSheet["content_extent"]>, string> = {
  sentence: "sentence",
  paragraph: "paragraph",
  section: "Section",
  article: "article",
  whole_board: "The whole board content",
};

const BOARD_DESTINATION_LABELS: Record<BoardTaskRequirementSheet["document_destination"], string> = {
  current_lesson: "current lesson",
  new_lesson: "new lesson",
  unresolved: "To be confirmed",
};

function boardTaskActionLabel(action: BoardTaskRequirementSheet["requested_action"]) {
  return action ? BOARD_TASK_ACTION_LABELS[action] ?? action : "To be confirmed";
}

function boardTaskLocationLabel(task: BoardTaskRequirementSheet) {
  const kindLabels: Record<NonNullable<BoardTaskRequirementSheet["location_kind"]>, string> = {
    target_range: "target range",
    insertion_anchor: "insertion position",
    unspecified: "To be confirmed",
  };
  const kind = kindLabels[task.location_kind ?? "unspecified"];
  const hint = task.target_hint || task.target_location?.display_label || task.location_status;
  return `${kind} · ${hint}`;
}

export function boardTaskConfirmationPayload(
  confirmation: NonNullable<ChatRequestPayload["board_task_confirmation"]>
): ChatRequestPayload {
  return {
    message: confirmation === "confirm" ? "Confirm execution of current board content task" : "Cancel the current board content task",
    interaction_mode: "ask",
    board_task_confirmation: confirmation,
  };
}

function composerSelectionLabel(selection: SelectionRef) {
  if (selection.kind === "source") {
    return selection.source_scope_kind === "source" ? "entire information" : "Information section";
  }
  if (selection.kind === "board" && selection.location_kind === "target_range") {
    return "TargetRange";
  }
  if (selection.kind === "board" && selection.location_kind === "insertion_anchor") {
    return "InsertionAnchor";
  }
  return selection.kind === "board" ? "board selection" : "Conversation Quote";
}

function composerSelectionToggleLabel(selection: SelectionRef, included: boolean) {
  if (selection.kind === "source") {
    return included ? "Contains information" : "Ignore data";
  }
  return included ? "Contains selection" : "Ignore selection";
}

function hasVisibleLearningClarity(
  clarityStatus: LearningClarificationStatus,
  activeRequirementSheet: LearningRequirementSheet | null
) {
  return (
    clarityStatus.progress > 0 ||
    clarityStatus.ready_for_board ||
    Boolean(clarityStatus.summary.trim()) ||
    clarityStatus.key_facts.length > 0 ||
    clarityStatus.checklist.length > 0 ||
    Boolean(activeRequirementSheet?.work_mode && activeRequirementSheet.work_mode !== "unknown")
  );
}

function CurrentNeedCard({
  activeBoardTask,
  activeRequirementSheet,
  barTone,
  clarityStatus,
  currentNeedPending,
  isChatBusy,
  lesson,
  onSubmitChat,
  targetCommitId,
}: {
  activeBoardTask: BoardTaskRequirementSheet | null;
  activeRequirementSheet: LearningRequirementSheet | null;
  barTone: string;
  clarityStatus: LearningClarificationStatus;
  currentNeedPending: boolean;
  isChatBusy: boolean;
  lesson: Lesson;
  onSubmitChat: (payload?: ChatRequestPayload) => void | Promise<void>;
  targetCommitId: string | null;
}) {
  if (currentNeedPending) {
    return (
      <div className="rounded-xl border border-sky-200 bg-sky-50 p-4">
        <div className="flex items-center justify-between gap-3">
          <p className="text-[11px] font-bold uppercase tracking-widest text-sky-700">current task</p>
          <span className="inline-flex items-center gap-1 rounded-full bg-white px-2 py-1 text-[11px] font-semibold text-sky-700">
            <LoaderCircle className="h-3 w-3 animate-spin" />

            Recognizing
          </span>
        </div>
        <div className="mt-3 h-2 rounded-full bg-white">
          <div className="h-full w-1/3 rounded-full bg-sky-500 transition-all" />
        </div>
        <p className="mt-3 text-xs leading-6 text-sky-950">Your new questions are being organized into where, what, and what to do.</p>
      </div>
    );
  }

  const executedNeed =
    !activeBoardTask && !activeRequirementSheet && !currentNeedPending
      ? latestExecutedNeedSnapshot(lesson, targetCommitId)
      : null;
  const generationFailure = !activeBoardTask
    ? latestGenerationFailureSnapshot(lesson, targetCommitId)
    : null;

  if (executedNeed?.kind === "board_task") {
    const task = executedNeed.boardTask;
    return (
      <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-4">
        <div className="flex items-center justify-between gap-3">
          <p className="text-[11px] font-bold uppercase tracking-widest text-emerald-700">current task</p>
          <span className="rounded-full bg-white px-2 py-1 text-[11px] font-semibold text-emerald-700">

            has been executed
          </span>
        </div>
        <div className="mt-3 h-2 rounded-full bg-white">
          <div className="h-full w-full rounded-full bg-emerald-500 transition-all" />
        </div>
        <div className="mt-3 grid gap-2 text-xs leading-5 text-emerald-950">
          <p>link:{boardWorkflowLabel(task.board_workflow ?? "act_on_existing_board")}</p>
          <p>Location:{boardTaskLocationLabel(task)}</p>
          <p>action:{boardTaskActionLabel(task.requested_action)}</p>
          <p>content:{task.question_or_topic || "Board content tasks executed"}</p>
        </div>
      </div>
    );
  }

  if (executedNeed?.kind === "learning_requirement") {
    return (
      <LearningClarityCard
        activeRequirementSheet={executedNeed.requirementSheet}
        barTone="bg-emerald-500"
        clarityStatus={executedNeed.clarityStatus}
        lesson={lesson}
        statusLabelOverride="has been executed"
        targetCommitId={executedNeed.commit.id}
      />
    );
  }

  if (activeBoardTask) {
    const progress = Math.max(0, Math.min(100, activeBoardTask.progress));
    const statusLabel = isChatBusy
      ? "Executing"
      : activeBoardTask.confirmation_status === "awaiting"
        ? "Waiting for confirmation"
        : progress >= 100
          ? "Completed"
          : "Collecting";
    return (
      <div className="rounded-xl border border-sky-200 bg-sky-50 p-4">
        <div className="flex items-center justify-between gap-3">
          <p className="text-[11px] font-bold uppercase tracking-widest text-sky-700">current task</p>
          <span className="rounded-full bg-white px-2 py-1 text-[11px] font-semibold text-sky-700">
            {statusLabel} · {progress}%
          </span>
        </div>
        <div className="mt-3 h-2 rounded-full bg-white">
          <div className="h-full rounded-full bg-sky-500 transition-all" style={{ width: `${progress}%` }} />
        </div>
        <div className="mt-3 grid gap-2 text-xs leading-5 text-sky-950">
          <p>link:{boardWorkflowLabel(activeBoardTask.board_workflow ?? "act_on_existing_board")}</p>
          <p>Location:{boardTaskLocationLabel(activeBoardTask)}</p>
          <p>action:{boardTaskActionLabel(activeBoardTask.requested_action)}</p>
          <p>content:{activeBoardTask.question_or_topic || "To be confirmed"}</p>
          <p>space:{activeBoardTask.content_extent ? BOARD_EXTENT_LABELS[activeBoardTask.content_extent] : "To be confirmed"}</p>
          <p>Attribution:{BOARD_DESTINATION_LABELS[activeBoardTask.document_destination]}</p>
          {activeBoardTask.special_interaction_requirements && activeBoardTask.special_interaction_requirements !== "none" ? (
            <p>Interaction requirements:{activeBoardTask.special_interaction_requirements}</p>
          ) : null}
        </div>
        {activeBoardTask.confirmation_status === "awaiting" ? (
          <div className="mt-3 rounded-lg border border-sky-200 bg-white p-3">
            <p className="text-xs leading-6 text-sky-900">This board task requires your explicit confirmation.</p>
            <div className="mt-2 flex flex-wrap gap-2">
              <button
                type="button"
                disabled={isChatBusy}
                onClick={() => void onSubmitChat(boardTaskConfirmationPayload("confirm"))}
                className="inline-flex items-center gap-1.5 rounded-md bg-sky-700 px-3 py-2 text-xs font-semibold text-white transition hover:bg-sky-800 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Check className="h-3.5 w-3.5" />

                Confirm execution
              </button>
              <button
                type="button"
                disabled={isChatBusy}
                onClick={() => void onSubmitChat(boardTaskConfirmationPayload("decline"))}
                className="inline-flex items-center gap-1.5 rounded-md border border-sky-200 bg-white px-3 py-2 text-xs font-semibold text-sky-900 transition hover:border-sky-400 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <X className="h-3.5 w-3.5" />

                Cancel task
              </button>
            </div>
          </div>
        ) : null}
        {activeBoardTask.missing_items.length ? (
          <p className="mt-3 text-xs leading-6 text-sky-900">To be added:{activeBoardTask.missing_items.join(", ")}</p>
        ) : null}
      </div>
    );
  }

  if (hasVisibleLearningClarity(clarityStatus, activeRequirementSheet)) {
    return (
      <LearningClarityCard
        activeRequirementSheet={activeRequirementSheet}
        barTone={generationFailure ? "bg-red-500" : barTone}
        clarityStatus={clarityStatus}
        lesson={lesson}
        statusLabelOverride={generationFailure ? "Generation failed" : undefined}
        statusMessageOverride={generationFailure?.reason}
        statusTone={generationFailure ? "error" : "default"}
        targetCommitId={targetCommitId}
      />
    );
  }

  if (lesson.board_document.content_text.trim()) {
    return (
      <div className="rounded-xl border border-gray-200 bg-gray-50 p-4">
        <div className="flex items-center justify-between gap-3">
          <p className="text-[11px] font-bold uppercase tracking-widest text-gray-600">current task</p>
          <span className="rounded-full bg-white px-2 py-1 text-[11px] font-semibold text-gray-600">To be entered</span>
        </div>
        <div className="mt-3 h-2 rounded-full bg-white">
          <div className="h-full w-0 rounded-full bg-gray-400" />
        </div>
        <p className="mt-3 text-xs leading-6 text-gray-700">Waiting for new board tasks.</p>
      </div>
    );
  }

  return (
    <LearningClarityCard
      activeRequirementSheet={activeRequirementSheet}
      barTone={generationFailure ? "bg-red-500" : barTone}
      clarityStatus={clarityStatus}
      lesson={lesson}
      statusLabelOverride={generationFailure ? "Generation failed" : undefined}
      statusMessageOverride={generationFailure?.reason}
      statusTone={generationFailure ? "error" : "default"}
      targetCommitId={targetCommitId}
    />
  );
}

function CodexLiveTaskQueuePanel({
  state,
  onResolve,
}: {
  state: CodexLiveTaskState;
  onResolve: (delegationId: string, action: CodexLiveTaskAction) => boolean;
}) {
  const summary = codexLiveTaskSummary(state);
  if (!codexLiveTaskHasVisibleActivity(state)) {
    return null;
  }
  const actionLabels: Array<[CodexLiveTaskAction, string]> = [
    ["supplement", "Supplement current tasks"],
    ["replace", "Replace current task"],
    ["queue", "Move to next item"],
    ["dismiss", "neglect"],
  ];
  return (
    <div className="mb-2 rounded-2xl border border-sky-200 bg-sky-50 p-3" aria-live="polite">
      <div className="flex items-center justify-between gap-3">
        <p className="text-xs font-semibold text-sky-950">Codex Live tasks</p>
        <span className="inline-flex items-center gap-1 text-[11px] font-medium text-sky-700">
          {summary.runningCount ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" /> : null}

          run {summary.runningCount}  · queue {summary.queuedCount}  · To be confirmed {summary.pendingCount}
        </span>
      </div>
      {state.pendingTasks.map((task) => (
        <div key={task.delegationId} className="mt-2 rounded-xl border border-sky-100 bg-white p-2.5">
          <p className="text-xs leading-5 text-gray-800">{task.text || "New voice input detected"}</p>
          <p className="mt-1 text-[11px] text-gray-500">The current task is still running, please clarify the purpose of this paragraph.</p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {actionLabels.map(([action, label]) => (
              <button
                key={action}
                type="button"
                onClick={() => onResolve(task.delegationId, action)}
                className={clsx(
                  "rounded-full border px-2.5 py-1 text-[11px] font-medium transition",
                  action === "replace"
                    ? "border-amber-200 bg-amber-50 text-amber-800 hover:bg-amber-100"
                    : action === "dismiss"
                      ? "border-gray-200 text-gray-500 hover:bg-gray-50"
                      : "border-sky-200 text-sky-800 hover:bg-sky-50"
                )}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function CodexLiveLoadingMessage({
  state,
  statusText,
}: {
  state: CodexLiveTaskState;
  statusText: string;
}) {
  if (!codexLiveTaskIsLoading(state)) {
    return null;
  }
  return (
    <article className="flex gap-3" aria-live="polite" aria-label="Codex Live is loading">
      <div className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-sky-200 bg-sky-50 text-sky-700 shadow-sm">
        <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
      </div>
      <div className="min-w-0 max-w-[calc(100%-2.5rem)] flex-1 space-y-2">
        <p className="text-[11px] font-semibold text-sky-800">Codex Live is loading</p>
        <div className="rounded-2xl rounded-tl-md border border-sky-200 bg-sky-50 px-4 py-3 text-[12px] leading-5 text-sky-900 shadow-sm">
          <div className="flex items-center gap-2">
            <span className="flex gap-1" aria-hidden="true">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-sky-500" />
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-sky-500 [animation-delay:120ms]" />
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-sky-500 [animation-delay:240ms]" />
            </span>
            <span>{statusText || "The task is still running, please wait"}</span>
          </div>
        </div>
      </div>
    </article>
  );
}

type CourseStudioChatSidebarProps = {
  attachmentsDisabled?: boolean;
  packageId: string;
  resizeHandleProps: HTMLAttributes<HTMLDivElement>;
  isResizing: boolean;
  clarityBarTone: string;
  clarityStatus: LearningClarificationStatus;
  activeLesson: Lesson;
  targetCommitId: string | null;
  previewCommit: CommitRecord | null;
  displayedMessages: ChatMessage[];
  forkSourceMessage: ChatMessage | null;
  forkReturnDisabled?: boolean;
  ridocBranchingEnabled: boolean;
  isPreviewMode: boolean;
  interactionLocked?: boolean;
  isChatBusy: boolean;
  clarificationQuestions: string[];
  activeBoardTask: BoardTaskRequirementSheet | null;
  activeRequirementSheet: LearningRequirementSheet | null;
  currentNeedPending: boolean;
  latestBoardDecision: BoardDecision | null;
  chatScrollEndRef: RefObject<HTMLDivElement | null>;
  chatInputRef: RefObject<HTMLTextAreaElement | null>;
  remoteAudioRef: RefObject<HTMLAudioElement | null>;
  modelCatalog: AIModelCatalog;
  selectedTextModel: AIModelSelection;
  selectedRealtimeModel: AIModelSelection;
  selectedTextOption: AIModelOption | null;
  selectedRealtimeOption: AIModelOption | null;
  openModelMenu: ModelMenu;
  setOpenModelMenu: Dispatch<SetStateAction<ModelMenu>>;
  voiceActive: boolean;
  voiceStatusText: string;
  codexLiveTaskState: CodexLiveTaskState;
  chatInput: string;
  composerAttachments: ChatAttachmentRef[];
  composerMode: ChatInteractionMode;
  composerSelection: SelectionRef | null;
  composerSelections: SelectionRef[];
  sourceQuerySelections: SelectionRef[];
  sourceQueryAllReady: boolean;
  includeSelectionInPrompt: boolean;
  onApplySelection: (selection: SelectionRef, popoverPosition: ReturnType<typeof popoverPositionFromDomSelection>) => void;
  onContinueTeaching: () => void;
  onSubmitChat: (payload?: ChatRequestPayload) => void | Promise<void>;
  onStopChat: () => void;
  onEditMessage: (message: ChatMessage, nextContent: string) => void | Promise<void>;
  onRegenerateMessage: (message: ChatMessage) => void | Promise<void>;
  onStartForkMessage: (message: ChatMessage) => void;
  onCancelFork: () => void;
  onHideChatCommit: (commitId: string) => void | Promise<void>;
  onDeleteHistoryNode?: (nodeId: string) => void | Promise<void>;
  deletionDisabledReason?: string | null;
  readOnly?: boolean;
  onSelectTextModel: (selection: AIModelSelection) => void;
  onSelectRealtimeModel: (option: AIModelOption) => void;
  onVoiceToggle: () => void | Promise<void>;
  onResolveCodexLiveTask: (delegationId: string, action: CodexLiveTaskAction) => boolean;
  onSpeakMessage: (content: string) => void | Promise<void>;
  onExitPreviewMode: () => void;
  onClearSelection: () => void;
  onUpdateComposerState: (updater: (current: LessonComposerState) => LessonComposerState) => void;
  onAdjustComposerHeight: () => void;
  onError: (message: string) => void;
};

export function CourseStudioChatSidebar({
  attachmentsDisabled = false,
  packageId,
  resizeHandleProps,
  isResizing,
  clarityBarTone,
  clarityStatus,
  activeLesson,
  targetCommitId,
  previewCommit,
  displayedMessages,
  forkSourceMessage,
  forkReturnDisabled = false,
  ridocBranchingEnabled,
  isPreviewMode,
  interactionLocked = false,
  isChatBusy,
  clarificationQuestions,
  activeBoardTask,
  activeRequirementSheet,
  currentNeedPending,
  latestBoardDecision,
  chatScrollEndRef,
  chatInputRef,
  remoteAudioRef,
  modelCatalog,
  selectedTextModel,
  selectedRealtimeModel,
  selectedTextOption,
  selectedRealtimeOption,
  openModelMenu,
  setOpenModelMenu,
  voiceActive,
  voiceStatusText,
  codexLiveTaskState,
  chatInput,
  composerAttachments,
  composerMode,
  composerSelection,
  composerSelections,
  sourceQuerySelections,
  sourceQueryAllReady,
  includeSelectionInPrompt,
  onApplySelection,
  onContinueTeaching,
  onSubmitChat,
  onStopChat,
  onEditMessage,
  onRegenerateMessage,
  onStartForkMessage,
  onCancelFork,
  onHideChatCommit,
  onDeleteHistoryNode,
  deletionDisabledReason = null,
  readOnly = false,
  onSelectTextModel,
  onSelectRealtimeModel,
  onVoiceToggle,
  onResolveCodexLiveTask,
  onSpeakMessage,
  onExitPreviewMode,
  onClearSelection,
  onUpdateComposerState,
  onAdjustComposerHeight,
  onError,
}: CourseStudioChatSidebarProps) {
  const api = useStudioApi();
  const modelControlsRef = useRef<HTMLDivElement | null>(null);
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null);
  const [editingMessageContent, setEditingMessageContent] = useState("");
  const textModelReady = Boolean(selectedTextOption?.enabled);
  const attachmentsReady = composerAttachments.every(
    (attachment) => attachment.kind === "image" || attachment.status === "ready"
  );

  async function openSourceCitation(citation: SourceCitation) {
    if (citation.evidence_ref) {
      try { await openSourceEvidencePreview(api, packageId, activeLesson.id, citation.evidence_ref); }
      catch (error) { onError(error instanceof Error ? error.message : "Could not load the course citation snapshot."); }
      return;
    }
    if (attachmentsDisabled) {
      onError("The original workspace source file was not copied into this repository.");
      return;
    }
    try {
      const blob = await api.downloadPackageSource(packageId, citation.source_id);
      const objectUrl = URL.createObjectURL(blob);
      const page = citation.page_start && citation.page_start > 0 ? citation.page_start : 1;
      window.open(`${objectUrl}#page=${page}`, "_blank", "noopener,noreferrer");
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
    } catch (error) {
      onError(error instanceof Error ? error.message : "Unable to open citation");
    }
  }

  function startEditingMessage(message: ChatMessage) {
    setEditingMessageId(message.id);
    setEditingMessageContent(message.editableContent ?? message.content);
  }

  async function submitEditedMessage(message: ChatMessage) {
    const nextContent = editingMessageContent.trim();
    if (!nextContent || isChatBusy || !textModelReady) {
      return;
    }
    await onEditMessage(message, nextContent);
    setEditingMessageId(null);
    setEditingMessageContent("");
  }

  return (
    <aside className="relative flex h-full min-h-0 flex-col border-r border-gray-200 bg-white">
      <div
        {...resizeHandleProps}
        className={clsx(
          "group absolute inset-y-0 right-[-6px] z-30 flex w-3 cursor-col-resize items-center justify-center outline-none",
          isResizing && "bg-gray-100/60"
        )}
      >
        <span
          className={clsx(
            "h-14 w-1 rounded-full bg-gray-200 opacity-0 transition group-hover:opacity-100 group-focus-visible:opacity-100",
            isResizing && "bg-gray-400 opacity-100"
          )}
        />
      </div>
      <div className="flex-1 overflow-y-auto p-6 custom-scrollbar">
        <div className="space-y-6">
          <CurrentNeedCard
            activeBoardTask={!isPreviewMode ? activeBoardTask : null}
            activeRequirementSheet={!isPreviewMode ? activeRequirementSheet : null}
            barTone={clarityBarTone}
            clarityStatus={clarityStatus}
            currentNeedPending={!isPreviewMode && currentNeedPending}
            isChatBusy={isChatBusy}
            lesson={activeLesson}
            onSubmitChat={onSubmitChat}
            targetCommitId={targetCommitId}
          />
          <div className="space-y-5">
            {previewCommit ? (
              <div className="rounded-xl border border-violet-200 bg-violet-50 px-4 py-3 text-xs leading-6 text-violet-800">

                Viewing {previewCommit.label}  communication records at that time.
              </div>
            ) : null}

            {displayedMessages.map((message, index) => (
              <div
                key={message.id}
                onMouseUp={() => {
                  const excerpt = window.getSelection()?.toString().trim();
                  if (excerpt) {
                    onApplySelection(
                      {
                        kind: "chat",
                        lesson_id: activeLesson.id,
                        excerpt,
                      },
                      popoverPositionFromDomSelection()
                    );
                  }
                }}
              >
                <CourseChatMessage
                  message={message}
                  onRegenerate={
                    ridocBranchingEnabled &&
                    !forkSourceMessage &&
                    !isPreviewMode &&
                    !interactionLocked &&
                    !isChatBusy &&
                    message.role === "assistant" &&
                    message.status === "ready" &&
                    Boolean(message.commitId)
                      ? () => void onRegenerateMessage(message)
                      : undefined
                  }
                  onFork={
                    ridocBranchingEnabled &&
                    !forkSourceMessage &&
                    !isPreviewMode &&
                    !interactionLocked &&
                    !isChatBusy &&
                    message.role === "assistant" &&
                    message.status === "ready" &&
                    Boolean(message.commitId)
                      ? () => onStartForkMessage(message)
                      : undefined
                  }
                  onStartEdit={
                    !isPreviewMode &&
                    !interactionLocked &&
                    !forkSourceMessage &&
                    !isChatBusy &&
                    message.role === "user" &&
                    Boolean(message.commitId && message.parentCommitIds?.[0])
                      ? () => startEditingMessage(message)
                      : undefined
                  }
                  onDelete={chatHistoryDeletionAction({
                    message, lesson: activeLesson, readOnly,
                    legacyBlocked: isPreviewMode || interactionLocked || Boolean(forkSourceMessage) || isChatBusy,
                    onDelete: onDeleteHistoryNode, onHide: onHideChatCommit,
                  })}
                  deleteLabel={onDeleteHistoryNode ? "Delete this conversation turn" : "Hide conversation"}
                  deleteDisabledReason={onDeleteHistoryNode ? deletionDisabledReason : null}
                  isEditing={editingMessageId === message.id}
                  editingContent={editingMessageContent}
                  onEditingContentChange={setEditingMessageContent}
                  onCancelEdit={() => {
                    setEditingMessageId(null);
                    setEditingMessageContent("");
                  }}
                  onSubmitEdit={() => void submitEditedMessage(message)}
                  isEditDisabled={!editingMessageContent.trim() || isChatBusy}
                  onContinueTeaching={
                    !isPreviewMode &&
                    !interactionLocked &&
                    index === displayedMessages.length - 1 &&
                    message.role === "assistant" &&
                    message.teachingProgress?.has_next_section
                      ? onContinueTeaching
                      : undefined
                  }
                  onFollowUpSuggestion={
                    !isPreviewMode &&
                    !interactionLocked &&
                    !isChatBusy &&
                    textModelReady &&
                    index === displayedMessages.length - 1 &&
                    message.role === "assistant"
                      ? (suggestion) => void onSubmitChat({
                          message: suggestion,
                          interaction_mode: "ask",
                        })
                      : undefined
                  }
                  onOpenSourceCitation={(citation) => openSourceCitation(citation)}
                />
                {message.role === "assistant" && message.status === "ready" && message.content.trim() ? (
                  <button
                    type="button"
                    onClick={() => void onSpeakMessage(message.content)}
                    className="ml-11 mt-1 inline-flex items-center gap-1.5 rounded-full px-2 py-1 text-[11px] text-gray-500 transition hover:bg-gray-100 hover:text-gray-900"
                    aria-label="Report this reply"
                  >
                    <Volume2 className="h-3 w-3" />

                    broadcast
                  </button>
                ) : null}
              </div>
            ))}
            <CodexLiveLoadingMessage state={codexLiveTaskState} statusText={voiceStatusText} />
            <div ref={chatScrollEndRef} aria-hidden="true" />
          </div>

          {!isPreviewMode && !interactionLocked && clarificationQuestions.length ? (
            <div className="rounded-xl border border-sky-200 bg-sky-50 p-4">
              <p className="text-[11px] font-bold uppercase tracking-widest text-sky-700">Requirements clarification</p>
              <p className="mt-2 text-xs leading-6 text-sky-900">
                {latestBoardDecision?.reason ?? "AI still needs to confirm a few more learning goals before it can decide on the subsequent lecture strategy."}
              </p>
              <div className="mt-3 space-y-2">
                {clarificationQuestions.map((question, index) => (
                  <div key={`${question}-${index}`} className="rounded-lg bg-white px-3 py-2 text-xs leading-6 text-gray-700">
                    {index + 1}. {question}
                  </div>
                ))}
              </div>
            </div>
          ) : null}

        </div>
      </div>

      <div className="shrink-0 border-t border-gray-100 bg-white px-3 py-3">
        <CodexLiveTaskQueuePanel
          state={codexLiveTaskState}
          onResolve={onResolveCodexLiveTask}
        />
        <div
          ref={modelControlsRef}
          className="mb-2 grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_40px] items-center gap-2"
        >
          <CodexModelSettingsPicker
            open={openModelMenu === "text"}
            onOpenChange={(open) => setOpenModelMenu(open ? "text" : null)}
            selectedModel={selectedTextModel}
            selectedOption={selectedTextOption}
            defaultSelection={modelCatalog.defaults.text}
            options={modelCatalog.text}
            onChange={onSelectTextModel}
          />
          <ModelPicker
            kind="realtime"
            label="speech model"
            icon={<Volume2 className="h-4 w-4 shrink-0 text-gray-600" />}
            openModelMenu={openModelMenu}
            setOpenModelMenu={setOpenModelMenu}
            selectedModel={selectedRealtimeModel}
            selectedOption={selectedRealtimeOption}
            options={modelCatalog.realtime}
            onSelect={onSelectRealtimeModel}
          />

          <button
            type="button"
            onClick={() => void onVoiceToggle()}
            disabled={interactionLocked}
            aria-label={voiceStatusText}
            title={voiceStatusText}
            className={clsx(
              "flex h-10 w-10 items-center justify-center rounded-full text-white shadow-sm transition-all hover:scale-105 hover:shadow-md",
              voiceActive ? "bg-gray-800 ring-2 ring-gray-200" : "bg-[#1a1a1a]",
              interactionLocked && "cursor-not-allowed opacity-40"
            )}
          >
            <AudioLines className="h-4.5 w-4.5" />
          </button>
        </div>
        <p
          className={clsx(
            "mb-2 rounded-lg px-2 py-1.5 text-center text-xs leading-5",
            codexLiveTaskHasVisibleActivity(codexLiveTaskState)
              ? "bg-sky-50 font-medium text-sky-800"
              : "text-gray-500"
          )}
          aria-live="polite"
        >
          {voiceStatusText}
        </p>
        <audio ref={remoteAudioRef} autoPlay className="hidden" />

        {forkSourceMessage ? (
          <div className="mb-2 flex items-start justify-between gap-3 rounded-xl border border-violet-200 bg-violet-50 px-3 py-2 text-[11px] text-violet-900">
            <div className="min-w-0">
              <p className="font-semibold">Preparing a new branch from this response</p>
              <p className="mt-0.5 truncate text-violet-700">
                {forkSourceMessage.content.replace(/\s+/g, " ").slice(0, 120)}
              </p>
            </div>
            <button
              type="button"
              onClick={onCancelFork}
              disabled={forkReturnDisabled}
              className="shrink-0 rounded-md border border-violet-200 bg-white px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-violet-700 hover:bg-violet-100 disabled:cursor-wait disabled:opacity-45"
              aria-label="Return to current version"
              title="Return to current version"
            >
              Return
            </button>
          </div>
        ) : null}

        {sourceQueryAllReady || sourceQuerySelections.length || composerSelections.length ? (
          <div className="mb-2 space-y-2">
            {sourceQueryAllReady || sourceQuerySelections.length ? (
              <div className="space-y-1.5">
                <div className="flex items-center justify-between px-1 text-[10px] text-blue-700">
                  <span>Data question and answer scope</span>
                  <button
                    type="button"
                    onClick={() =>
                      onUpdateComposerState((current) => ({
                        ...current,
                        sourceQuerySelections: [],
                        sourceQueryAllReady: false,
                      }))
                    }
                    className="hover:text-blue-950"
                  >

                    clear range
                  </button>
                </div>
                {sourceQueryAllReady ? (
                  <div className="flex items-center justify-between gap-2 rounded-xl border border-blue-100 bg-blue-50 px-2.5 py-1.5">
                    <div className="flex min-w-0 items-center gap-2">
                      <TextQuote className="h-4 w-4 shrink-0 text-blue-600" />
                      <span className="rounded bg-blue-100 px-1.5 py-0.5 text-[10px] font-bold text-blue-800">All information</span>
                      <p className="truncate text-xs text-blue-900">All available Q&A materials for the current course</p>
                    </div>
                  </div>
                ) : sourceQuerySelections.map((selection, index) => (
                  <div
                    key={`${selection.source_ingestion_id}:${selection.source_chapter_id ?? selection.source_scope_kind}:${index}`}
                    className="flex items-center justify-between gap-2 rounded-xl border border-blue-100 bg-blue-50 px-2.5 py-1.5"
                  >
                    <div className="flex min-w-0 items-center gap-2">
                      <TextQuote className="h-4 w-4 shrink-0 text-blue-600" />
                      <span className="shrink-0 rounded bg-blue-100 px-1.5 py-0.5 text-[10px] font-bold text-blue-800">
                        {selection.source_scope_kind === "chapter" ? "chapter" : selection.source_scope_kind === "page_range" ? "page segment" : "material"}
                      </span>
                      <p className="truncate text-xs text-blue-900">{sourceQuerySelectionLabel(selection)}</p>
                    </div>
                    <button
                      type="button"
                      onClick={() =>
                        onUpdateComposerState((current) => ({
                          ...current,
                          sourceQuerySelections: current.sourceQuerySelections.filter((_item, itemIndex) => itemIndex !== index),
                        }))
                      }
                      aria-label={`Remove source Q&A scope ${index + 1}`}
                      className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-blue-500 transition hover:bg-white hover:text-blue-900"
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                ))}
              </div>
            ) : null}
            {composerSelections.length ? (
              <div className="space-y-1.5">
                {composerSelections.length > 1 ? (
                  <div className="flex items-center justify-between px-1 text-[10px] text-gray-500">
                    <span>Reserved {composerSelections.length}  citations</span>
                    <button type="button" onClick={onClearSelection} className="hover:text-black">

                      Clear all
                    </button>
                  </div>
                ) : null}
                {composerSelections.map((selection, index) => (
                  <div
                    key={`${selection.document_id ?? selection.source_ingestion_id ?? selection.kind}:${selection.segment_id ?? selection.text_hash ?? selection.excerpt}:${index}`}
                    className="flex items-center justify-between gap-2 rounded-xl bg-gray-50 px-2.5 py-1.5"
                  >
                    <div className="flex min-w-0 items-center gap-2">
                      {composerMode === "direct_edit" ? (
                        <PencilLine className="h-4 w-4 shrink-0 text-amber-600" />
                      ) : (
                        <TextQuote className="h-4 w-4 shrink-0 text-gray-500" />
                      )}
                      <span className="shrink-0 rounded bg-sky-100 px-1.5 py-0.5 text-[10px] font-bold text-sky-700">

                        Quote {index + 1} · {composerSelectionLabel(selection)}
                      </span>
                      <p className="min-w-0 truncate text-xs leading-5 text-gray-700">
                        “{selection.excerpt.replace(/\s+/g, " ").slice(0, 160)}”
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() =>
                        onUpdateComposerState((current) => {
                          const nextSelections = (current.composerSelections ?? []).filter(
                            (_item, itemIndex) => itemIndex !== index
                          );
                          return {
                            ...current,
                            composerSelections: nextSelections,
                            composerSelection: nextSelections[nextSelections.length - 1] ?? null,
                            includeSelectionInPrompt: nextSelections.length ? current.includeSelectionInPrompt : true,
                          };
                        })
                      }
                      aria-label={`Remove reference ${index + 1}`}
                      className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-gray-400 transition-colors hover:bg-white hover:text-black"
                      title="Remove this reference"
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}

        <div
          className={clsx(
            "overflow-hidden rounded-2xl border bg-white shadow-sm transition-colors focus-within:ring-1",
            composerMode === "direct_edit"
              ? "border-amber-200 focus-within:border-amber-500 focus-within:ring-amber-500"
              : "border-gray-200 focus-within:border-black focus-within:ring-black"
          )}
        >
          <ChatAttachmentChips
            attachments={composerAttachments}
            disabled={isChatBusy || interactionLocked}
            onRemove={(sourceId) =>
              onUpdateComposerState((current) => ({
                ...current,
                composerAttachments: current.composerAttachments.filter(
                  (attachment) => attachment.source_ingestion_id !== sourceId
                ),
              }))
            }
          />

          <textarea
            ref={chatInputRef}
            value={chatInput}
            rows={1}
            onFocus={() => {
              if (isPreviewMode && !interactionLocked) {
                onExitPreviewMode();
              }
            }}
            readOnly={interactionLocked}
            onChange={(event) =>
              onUpdateComposerState((current) => ({
                ...current,
                chatInput: event.target.value,
              }))
            }
            onInput={() => onAdjustComposerHeight()}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                if (
                  interactionLocked ||
                  isChatBusy ||
                  !textModelReady ||
                  !attachmentsReady ||
                  (!chatInput.trim() && !composerAttachments.length && !composerSelections.length)
                ) {
                  return;
                }
                event.preventDefault();
                void onSubmitChat();
              }
            }}
            placeholder={
              interactionLocked
                ? "Finish or cancel the current operation before continuing the conversation."
                : isPreviewMode
                  ? "Clicking enter will return to the current version and continue the conversation"
                  : composerMode === "direct_edit"
                    ? "Describe how you would change this piece of board content, or simply say \"rewrite the entire article\"..."
                    : sourceQueryAllReady || sourceQuerySelections.length
                      ? "Ask about the content of the selected data"
                    : composerSelection
                      ? composerSelection.kind === "source"
                        ? "Continue to ask questions based on the cited chapter"
                        : "Continue to ask questions based on the selected content"
                      : "Send a message to OpenClass..."
            }
            className="custom-scrollbar block w-full resize-none border-0 bg-transparent px-3.5 py-2.5 text-[13px] leading-relaxed outline-none placeholder:text-gray-400"
          />
          <div className="flex items-center justify-between gap-2 px-2.5 pb-2.5">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <ChatAttachmentMenu
                packageId={packageId}
                attachments={composerAttachments}
                disabled={attachmentsDisabled || isChatBusy || isPreviewMode || interactionLocked}
                menuAboveRef={modelControlsRef}
                showInkBoard
                onChange={(attachments) =>
                  onUpdateComposerState((current) => ({
                    ...current,
                    composerAttachments: attachments,
                  }))
                }
                onError={onError}
              />
              {composerSelection && composerSelections.length ? (
                <button
                  type="button"
                  onClick={() =>
                    onUpdateComposerState((current) => ({
                      ...current,
                      includeSelectionInPrompt: !current.includeSelectionInPrompt,
                    }))
                  }
                  className={clsx(
                    "inline-flex h-8 items-center gap-1.5 rounded-md border px-2.5 text-[11px] font-semibold transition-colors",
                    includeSelectionInPrompt
                      ? "border-gray-200 bg-gray-50 text-gray-600"
                      : "border-gray-200 bg-white text-gray-400"
                  )}
                >
                  <TextQuote className="h-3.5 w-3.5" />
                  {composerSelections.length > 1
                    ? includeSelectionInPrompt
                      ? `Include ${composerSelections.length} references`
                      : `Ignore ${composerSelections.length} references`
                    : composerSelectionToggleLabel(composerSelection, includeSelectionInPrompt)}
                </button>
              ) : null}
            </div>
            <button
              type="button"
              onClick={() => {
                if (isChatBusy) {
                  onStopChat();
                  return;
                }
                if (!interactionLocked) {
                  void onSubmitChat();
                }
              }}
              aria-label={isChatBusy ? "Stop replying" : "Send message"}
              title={isChatBusy ? "Stop replying" : "Send message"}
              disabled={
                !isChatBusy &&
                (interactionLocked ||
                  (!chatInput.trim() && !composerAttachments.length && !composerSelections.length) ||
                  !textModelReady ||
                  !attachmentsReady)
              }
              className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-[#1a1a1a] text-white shadow-sm transition-colors hover:bg-black disabled:cursor-not-allowed disabled:opacity-60"
            >
              {isChatBusy ? (
                <Square className="h-3.5 w-3.5 fill-current" />
              ) : (
                <Send className="h-4 w-4 -translate-x-[1px]" />
              )}
            </button>
          </div>
        </div>
      </div>
    </aside>
  );
}

function ModelPicker({
  kind,
  label,
  icon,
  openModelMenu,
  setOpenModelMenu,
  selectedModel,
  selectedOption,
  options,
  onSelect,
}: {
  kind: "text" | "realtime";
  label: string;
  icon: ReactNode;
  openModelMenu: ModelMenu;
  setOpenModelMenu: Dispatch<SetStateAction<ModelMenu>>;
  selectedModel: AIModelSelection;
  selectedOption: AIModelOption | null;
  options: AIModelOption[];
  onSelect: (option: AIModelOption) => void;
}) {
  return (
    <div className="relative min-w-0">
      <button
        type="button"
        data-testid={`${kind}-model-picker-button`}
        aria-expanded={openModelMenu === kind}
        aria-label={`${label}, current model ${modelButtonLabel(selectedOption, selectedModel)}`}
        onClick={() => setOpenModelMenu((current) => (current === kind ? null : kind))}
        className="flex h-10 w-full items-center justify-center gap-1.5 rounded-full bg-gray-100 px-3 text-sm text-gray-900 transition-colors hover:bg-gray-200"
      >
        <span className="flex min-w-0 items-center gap-2">
          {icon}
          <span className="truncate font-medium">{label}</span>
        </span>
        <ChevronDown
          className={clsx(
            "ml-auto h-4 w-4 shrink-0 text-gray-400 transition-transform",
            openModelMenu === kind && "rotate-180"
          )}
        />
      </button>

      {openModelMenu === kind ? (
        <div
          data-testid={`${kind}-model-picker-menu`}
          className="absolute bottom-full left-0 z-40 mb-2 max-h-[420px] w-56 overflow-y-auto rounded-xl border border-gray-200 bg-white p-1.5 shadow-[0_18px_50px_rgba(0,0,0,0.16)]"
        >
          <p className="px-2.5 pb-1 pt-1.5 text-sm text-gray-400">{label}</p>
          <div>
            {options.map((option) => {
              const selected = modelOptionKey(option) === modelSelectionKey(selectedModel);
              return (
                <button
                  key={`${kind}-${modelOptionKey(option)}`}
                  type="button"
                  onClick={() => onSelect(option)}
                  disabled={!option.enabled}
                  className={clsx(
                    "flex min-h-10 w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left text-sm text-gray-900 transition-colors hover:bg-gray-50",
                    !option.enabled && "cursor-not-allowed opacity-45 hover:bg-transparent"
                  )}
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium">{option.label}</span>
                    <span className="mt-0.5 block truncate text-xs leading-4 text-gray-400">
                      {PROVIDER_LABELS[option.provider] ?? option.provider} / {option.model}
                      {option.configured ? "" : "/ not configured"}
                    </span>
                  </span>
                  {selected ? <Check className="h-4 w-4 shrink-0 text-gray-900" /> : null}
                </button>
              );
            })}
          </div>
        </div>
      ) : null}
    </div>
  );
}
