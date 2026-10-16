import clsx from "clsx";
import { X } from "lucide-react";
import type { HTMLAttributes } from "react";
import type { HistoryEditController as HistoryOrderController } from "../../hooks/course-studio/use-history-edit-session";
import type { CoursePrStudioBinding } from "../../hooks/course-studio/use-course-pr-review";

import { SourceImportPanel } from "./source-import-panel";
import { GeometryGenerationPanel } from "./geometry-generation-panel";
import { VersionControlPanel } from "./version-control-panel";
import { LessonMergePanel } from "./lesson-merge-panel";
import { VoiceControlPanel } from "./voice-control-panel";
import { ModelSelectionPanel } from "./model-selection-panel";
import type { LessonPackageControlsProps } from "./lesson-package-controls";
import { useGeometryWorkspace } from "../../hooks/course-studio/use-geometry-workspace";
import { useSourceCatalogCache } from "../../hooks/course-studio/use-source-catalog-cache";
import type { CodexConnectionControls } from "../../hooks/course-studio/use-model-catalog";
import type { SpeechOptionsResponse } from "../../lib/speech-api";
import type {
  AIModelOption,
  AIModelSelection,
  BoardDecision,
  CommitRecord,
  Lesson,
  LessonMergeResolution,
  LessonMergeSessionView,
  RidocHistoryPage,
  SelectionRef,
} from "../../types";

export type CourseStudioSidebarTab = "geometry" | "history" | "models" | "sources" | "voice";

type CourseStudioSidePanelProps = {
  coursePrBinding?: CoursePrStudioBinding;
  prepareCoursePr?: () => Promise<boolean>;
  readOnly?: boolean;
  historyInteractionLocked?: boolean;
  sourcesDisabled?: boolean;
  open: boolean;
  resizeHandleProps: HTMLAttributes<HTMLDivElement>;
  isResizing: boolean;
  sidebarTab: CourseStudioSidebarTab;
  onSidebarTabChange: (tab: CourseStudioSidebarTab) => void;
  onClose: () => void;
  activeLesson: Lesson;
  packageId: string;
  storageKind?: "legacy" | "ridoc";
  ridocHistory: RidocHistoryPage | null;
  ridocHistorySyncing: boolean;
  sourcePackageId?: string;
  previewCommit: CommitRecord | null;
  previewCommitId: string | null;
  activeRequirements: Lesson["learning_requirements"];
  activeBoardTask: Lesson["board_task_requirements"];
  latestBoardDecision: BoardDecision | null;
  newBranchName: string;
  onNewBranchNameChange: (value: string) => void;
  onCreateBranch: () => void | Promise<void>;
  onPreviewCommit: (commit: CommitRecord, documentOnly?: boolean) => void | Promise<void>;
  onStepDocumentHistory: (direction: "undo" | "redo", targetCommitId: string) => void | Promise<void>;
  onSetChatCommitVisibility: (commitId: string, visibility: "hidden" | "visible") => void | Promise<void>;
  onDeleteHistoryNode?: (nodeId: string) => void | Promise<void>;
  deletionDisabledReason?: string | null;
  onCreateBranchFromCommit: (
    commit: CommitRecord,
    continuationNodeId: string,
    branchSourceNodeId: string
  ) => void | Promise<void>;
  onReferenceHistoryNode: (selection: SelectionRef) => void;
  onSwitchBranch: (branchName: string) => void | Promise<void>;
  onMergeBranch: (branchName: string) => void | Promise<void>;
  lessonPackageControls: LessonPackageControlsProps;
  historyOrder?: HistoryOrderController;
  mergeSession: LessonMergeSessionView | null;
  mergeDraftDirty: boolean;
  mergeAIProposing: boolean;
  onResolveMergeConflict: (conflictId: string, resolution: LessonMergeResolution, customValue?: unknown) => void | Promise<void>;
  onProposeMergeWithAI: () => void | Promise<void>;
  onCancelMergeAI: () => void;
  onRecomputeMerge: () => void | Promise<void>;
  onAbandonMerge: () => void | Promise<void>;
  onSubmitMerge: () => void | Promise<void>;
  onMergeOrderChange: (nodeIds: string[]) => void;
  onError: (message: string) => void;
  onSourceReference?: (selection: SelectionRef) => void;
  onAllReadySourcesReference?: () => void;
  geometryReference: SelectionRef | null;
  onGeometryReferenceClear: () => void;
  textModel: AIModelSelection | null;
  catalogModelOptions: AIModelOption[];
  defaultCatalogModel: AIModelSelection;
  selectedTextModel: AIModelSelection;
  selectedTextOption: AIModelOption | null;
  textModelOptions: AIModelOption[];
  onSelectTextModel: (selection: AIModelSelection) => void;
  codexConnectionControls?: CodexConnectionControls;
  speechAutoEnabled: boolean;
  speechIsLoading: boolean;
  speechIsPlaying: boolean;
  speechIsPaused: boolean;
  speechStatusText: string;
  speechOptions: SpeechOptionsResponse;
  speechSelectedVoice: string;
  speechRate: number;
  speechCurrentModel: string;
  speechCurrentText: string;
  speechCurrentTime: number;
  speechDuration: number;
  speechCanSeek: boolean;
  speechCanReplay: boolean;
  onSpeechAutoToggle: () => void;
  onSpeechCancel: () => void;
  onSpeechPause: () => void;
  onSpeechResume: () => void;
  onSpeechReplay: () => void;
  onSpeechSeek: (time: number) => void;
  onSpeechVoiceChange: (voice: string) => void;
  onSpeechRateChange: (rate: number) => void;
};

export function CourseStudioSidePanel({
  coursePrBinding,
  prepareCoursePr,
  readOnly = false,
  historyInteractionLocked = false,
  sourcesDisabled = false,
  open,
  resizeHandleProps,
  isResizing,
  sidebarTab,
  onSidebarTabChange,
  onClose,
  activeLesson,
  packageId,
  storageKind = "legacy",
  ridocHistory,
  ridocHistorySyncing,
  sourcePackageId,
  previewCommit,
  previewCommitId,
  activeRequirements,
  activeBoardTask,
  latestBoardDecision,
  newBranchName,
  onNewBranchNameChange,
  onCreateBranch,
  onPreviewCommit,
  onStepDocumentHistory,
  onSetChatCommitVisibility,
  onDeleteHistoryNode,
  deletionDisabledReason,
  onCreateBranchFromCommit,
  onReferenceHistoryNode,
  onSwitchBranch,
  onMergeBranch,
  lessonPackageControls,
  historyOrder,
  mergeSession,
  mergeDraftDirty,
  mergeAIProposing,
  onResolveMergeConflict,
  onProposeMergeWithAI,
  onCancelMergeAI,
  onRecomputeMerge,
  onAbandonMerge,
  onSubmitMerge,
  onMergeOrderChange,
  onError,
  onSourceReference,
  onAllReadySourcesReference,
  geometryReference,
  onGeometryReferenceClear,
  textModel,
  catalogModelOptions,
  defaultCatalogModel,
  selectedTextModel,
  selectedTextOption,
  textModelOptions,
  onSelectTextModel,
  codexConnectionControls,
  speechAutoEnabled,
  speechIsLoading,
  speechIsPlaying,
  speechIsPaused,
  speechStatusText,
  speechOptions,
  speechSelectedVoice,
  speechRate,
  speechCurrentModel,
  speechCurrentText,
  speechCurrentTime,
  speechDuration,
  speechCanSeek,
  speechCanReplay,
  onSpeechAutoToggle,
  onSpeechCancel,
  onSpeechPause,
  onSpeechResume,
  onSpeechReplay,
  onSpeechSeek,
  onSpeechVoiceChange,
  onSpeechRateChange,
}: CourseStudioSidePanelProps) {
  const geometryWorkspace = useGeometryWorkspace({
    lessonId: activeLesson.id,
    incomingSelection: geometryReference,
    textModel,
    onClearSelection: onGeometryReferenceClear,
  });
  const sourceCatalogCache = useSourceCatalogCache();

  return (
    <aside
      data-testid="course-workbench-sidebar"
      className={clsx(
        "h-full min-h-0 min-w-0 flex-col border-l border-gray-200 bg-[#fcfcfc]",
        open
          ? "fixed inset-y-0 right-0 z-50 flex w-[min(92vw,420px)] shadow-2xl xl:relative xl:inset-auto xl:z-auto xl:w-auto xl:shadow-none"
          : "hidden"
      )}
    >
      <div
        {...resizeHandleProps}
        className={clsx(
          "group absolute inset-y-0 left-[-6px] z-30 hidden w-3 cursor-col-resize items-center justify-center outline-none xl:flex",
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

      <div className="flex h-12 items-center justify-between border-b border-gray-200 bg-white px-5">
        <h4 className="text-[10px] font-bold uppercase tracking-widest text-gray-500">

          Course workbench assistance
        </h4>
        <button
          type="button"
          onClick={onClose}
          className="rounded-md p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-black"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>

      <div className="flex overflow-x-auto border-b border-gray-200 bg-white">
        {[
          { value: "sources", label: "Sources" },
          { value: "history", label: "History" },
          { value: "voice", label: "Voice" },
          { value: "geometry", label: "Geometry" },
          { value: "models", label: "Models" },
        ].map((tab) => (
          <button
            key={tab.value}
            type="button"
            onClick={() => onSidebarTabChange(tab.value as CourseStudioSidebarTab)}
            className={clsx(
              "min-w-20 flex-1 py-3 text-[10px] font-bold uppercase tracking-wider transition-colors",
              sidebarTab === tab.value
                ? "border-b-2 border-black text-black"
                : "text-gray-400 hover:text-black"
            )}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-5 custom-scrollbar">
        {sidebarTab === "models" ? (
          <ModelSelectionPanel
            options={textModelOptions}
            selectedModel={selectedTextModel}
            selectedOption={selectedTextOption}
            onSelect={onSelectTextModel}
            codexConnectionControls={codexConnectionControls}
          />
        ) : sidebarTab === "geometry" ? (
          <GeometryGenerationPanel
            packageId={packageId}
            selection={geometryWorkspace.selection}
            instructions={geometryWorkspace.instructions}
            attachments={geometryWorkspace.attachments}
            scene={geometryWorkspace.scene}
            error={geometryWorkspace.error}
            isGenerating={geometryWorkspace.isGenerating}
            onInstructionsChange={geometryWorkspace.setInstructions}
            onAttachmentsChange={geometryWorkspace.setAttachments}
            onAttachmentError={geometryWorkspace.reportError}
            onGenerate={geometryWorkspace.generate}
            onClear={geometryWorkspace.clear}
          />
        ) : sidebarTab === "sources" ? (
          <SourceImportPanel
            key={sourcePackageId ?? packageId}
            packageId={sourcePackageId ?? packageId}
            catalogCache={sourceCatalogCache}
            catalogModelOptions={catalogModelOptions}
            defaultCatalogModel={defaultCatalogModel}
            disabled={sourcesDisabled}
            onError={onError}
            onSourceReference={onSourceReference}
            onAllReadySourcesReference={onAllReadySourcesReference}
          />
        ) : sidebarTab === "history" && mergeSession ? (
          <LessonMergePanel
            session={mergeSession}
            isDraftDirty={mergeDraftDirty}
            isAIProposing={mergeAIProposing}
            onResolveConflict={onResolveMergeConflict}
            onProposeWithAI={onProposeMergeWithAI}
            onCancelAI={onCancelMergeAI}
            onRecompute={onRecomputeMerge}
            onAbandon={onAbandonMerge}
            onSubmit={onSubmitMerge}
            onOrderChange={onMergeOrderChange}
          />
        ) : sidebarTab === "history" ? (
          <VersionControlPanel
            coursePrBinding={coursePrBinding}
            prepareCoursePr={prepareCoursePr}
            readOnly={readOnly}
            interactionLocked={historyInteractionLocked}
            storageKind={storageKind}
            ridocHistory={ridocHistory}
            ridocHistorySyncing={ridocHistorySyncing}
            activeLesson={activeLesson}
            previewCommit={previewCommit}
            previewCommitId={previewCommitId}
            activeRequirements={activeRequirements}
            activeBoardTask={activeBoardTask}
            latestBoardDecision={latestBoardDecision}
            newBranchName={newBranchName}
            onNewBranchNameChange={onNewBranchNameChange}
            onCreateBranch={onCreateBranch}
            onPreviewCommit={onPreviewCommit}
            onStepDocumentHistory={onStepDocumentHistory}
            onSetChatCommitVisibility={onSetChatCommitVisibility}
            onDeleteHistoryNode={onDeleteHistoryNode}
            deletionDisabledReason={deletionDisabledReason}
            onCreateBranchFromCommit={onCreateBranchFromCommit}
            onReferenceHistoryNode={onReferenceHistoryNode}
            onSwitchBranch={onSwitchBranch}
            onMergeBranch={onMergeBranch}
            lessonPackageControls={lessonPackageControls}
            historyOrder={historyOrder}
          />
        ) : sidebarTab === "voice" ? (
          speechOptions.available === false ? <p className="p-4 text-sm text-stone-500">Voice playback is unavailable in this workspace.</p> : <VoiceControlPanel
            autoEnabled={speechAutoEnabled}
            isLoading={speechIsLoading}
            isPlaying={speechIsPlaying}
            isPaused={speechIsPaused}
            statusText={speechStatusText}
            model={speechCurrentModel}
            currentText={speechCurrentText}
            currentTime={speechCurrentTime}
            duration={speechDuration}
            canSeek={speechCanSeek}
            canReplay={speechCanReplay}
            options={speechOptions}
            selectedVoice={speechSelectedVoice}
            speechRate={speechRate}
            onAutoToggle={onSpeechAutoToggle}
            onCancel={onSpeechCancel}
            onPause={onSpeechPause}
            onResume={onSpeechResume}
            onReplay={onSpeechReplay}
            onSeek={onSpeechSeek}
            onVoiceChange={onSpeechVoiceChange}
            onSpeechRateChange={onSpeechRateChange}
          />
        ) : null}

      </div>
    </aside>
  );
}
