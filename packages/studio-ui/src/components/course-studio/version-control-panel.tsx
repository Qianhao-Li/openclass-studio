"use client";

import { useDocumentActionNavigation } from "../../hooks/course-studio/use-document-action-navigation";

import { ContentActionCoverageMount } from "./content-action-coverage-mount";

import clsx from "clsx";
import { GitBranch } from "lucide-react";
import { useMemo } from "react";
import { CoursePrSubmit } from "../course-pr-submit";
import { CoursePrReviewControls } from "./course-pr-review-controls";
import type { CoursePrStudioBinding } from "../../hooks/course-studio/use-course-pr-review";
import type { HistoryEditController as HistoryOrderController } from "../../hooks/course-studio/use-history-edit-session";

import {
  buildLearningRequirementDisplay,
  learningRequirementStatusLabel,
  type LearningRequirementDisplayFactor,
} from "../../lib/learning-requirement-display";
import {
  buildLessonHistoryGraph,
  buildRidocHistoryGraph,
  type HistoryGraphLayout,
} from "./history-graph-layout";
import { HistoryGitGraph } from "./history-git-graph";
import { HistoryEditDialogs } from "./history-edit-dialogs";
import { compactText } from "./history-utils";
import { useWorkspacePrincipal } from "../../contexts/studio-host-context";
import {
  LessonPackageControls,
  type LessonPackageControlsProps,
} from "./lesson-package-controls";
import type {
  BoardDecision,
  CommitRecord,
  Lesson,
  RidocHistoryPage,
  SelectionRef,
} from "../../types";

const EMPTY_HISTORY_GRAPH: HistoryGraphLayout = { lanes: [], nodes: [], edges: [], sprouts: [], columnCount: 1 };

type VersionControlPanelProps = {
  coursePrBinding?: CoursePrStudioBinding;
  prepareCoursePr?: () => Promise<boolean>;
  readOnly?: boolean;
  interactionLocked?: boolean;
  storageKind?: "legacy" | "ridoc";
  ridocHistory?: RidocHistoryPage | null;
  ridocHistorySyncing?: boolean;
  activeLesson: Lesson;
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
};

function FactorList({ title, factors }: { title: string; factors: LearningRequirementDisplayFactor[] }) {
  if (!factors.length) {
    return null;
  }
  return (
    <div className="mt-4 rounded-lg border border-gray-200 bg-white p-4">
      <p className="text-[10px] font-bold uppercase tracking-widest text-gray-400">{title}</p>
      <dl className="mt-3 space-y-2 text-[11px] leading-6">
        {factors.map((factor) => (
          <div key={factor.key} className="grid grid-cols-[72px_minmax(0,1fr)] gap-3">
            <dt className={clsx("font-semibold", factor.required ? "text-gray-700" : "text-gray-500")}>
              {factor.label}
            </dt>
            <dd className={clsx("min-w-0 break-words", factor.filled ? "text-gray-900" : "text-gray-400")}>
              {factor.value}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

export function VersionControlPanel({
  coursePrBinding,
  prepareCoursePr,
  readOnly = false,
  interactionLocked = false,
  storageKind = "legacy",
  ridocHistory: officialHistory = null,
  ridocHistorySyncing = false,
  activeLesson: officialLesson,
  previewCommit,
  previewCommitId,
  activeRequirements,
  activeBoardTask,
  latestBoardDecision,
  newBranchName,
  onNewBranchNameChange,
  onPreviewCommit,
  deletionDisabledReason,
  onReferenceHistoryNode,
  lessonPackageControls,
  historyOrder,
}: VersionControlPanelProps) {
  const activeLesson = historyOrder?.draftLesson ?? officialLesson;
  const ridocHistory = historyOrder?.draftHistory ?? officialHistory;
  const editing = Boolean(historyOrder?.active);
  const effectivePreviewId = editing || historyOrder?.pending ? historyOrder?.previewNodeId ?? null : previewCommitId;
  const documentNavigation = useDocumentActionNavigation({
    lesson: activeLesson, history: ridocHistory, previewId: effectivePreviewId,
    draftVersion: historyOrder?.active || historyOrder?.pending ? historyOrder.session?.version : undefined,
    onPreview: async (commit, documentOnly) => { if (editing || historyOrder?.pending) historyOrder?.preview(commit); else await onPreviewCommit(commit, documentOnly); },
  });
  const currentUser = useWorkspacePrincipal();
  const canUseCommunity = currentUser?.kind === "platform" && !currentUser.isGuest;
  const learningRequirementDisplay = activeRequirements
    ? buildLearningRequirementDisplay({ requirementSheet: activeRequirements })
    : null;
  const lessonGraph = useMemo(
    () => buildLessonHistoryGraph(activeLesson, effectivePreviewId),
    [activeLesson, effectivePreviewId]
  );
  const ridocGraph = useMemo(
    () => (storageKind === "ridoc" && ridocHistory
      ? buildRidocHistoryGraph(activeLesson, ridocHistory, effectivePreviewId)
      : null),
    [storageKind, ridocHistory, activeLesson, effectivePreviewId]
  );
  const historyGraph = storageKind === "ridoc" ? ridocGraph ?? EMPTY_HISTORY_GRAPH : lessonGraph;
  const currentBranchName =
    historyGraph.lanes.find((lane) => lane.isCurrent)?.branchName ?? activeLesson.history_graph.current_branch;
  const historyEmptyMessage = storageKind === "ridoc" && !ridocHistory
    ? ridocHistorySyncing
      ? "History is syncing to the latest RIDOC revision…"
      : "RIDOC history is unavailable."
    : "This lesson has no history nodes yet.";

  return (
    <div className="space-y-8">
      {coursePrBinding ? <CoursePrReviewControls {...coursePrBinding} /> : null}
      <div inert={editing || undefined} className={clsx(editing && "opacity-50")}><LessonPackageControls {...lessonPackageControls} readOnly /></div>
      {editing || historyOrder?.pending ? <p role="status" className="rounded-lg bg-violet-50 px-3 py-2 text-xs text-violet-800">Previewing a History draft. Gray nodes are pending deletion. Exiting edit mode saves the draft; choose whether to keep it when leaving Studio.</p> : null}
      {historyOrder ? <HistoryEditDialogs controller={historyOrder} /> : null}

      <section>
        {!readOnly && ridocHistory?.document_history?.blocked_reason ? (
          <p role="status" className="mb-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
            {ridocHistory.document_history.blocked_reason}
          </p>
        ) : null}
        <>
        {!readOnly && storageKind === "ridoc" ? <CoursePrSubmit key={`pr-submit:${officialLesson.id}`} lesson={officialLesson} prepare={prepareCoursePr} disabledReason={historyOrder?.pending || editing ? "Keep or discard the History draft before submitting a PR." : interactionLocked ? "The current operation is still running." : null} /> : null}
        <ContentActionCoverageMount activeLesson={officialLesson} ridocHistory={officialHistory} readOnly={readOnly} interactionLocked={interactionLocked || lessonPackageControls.isPlaybackActive} historyOrder={historyOrder} navigation={documentNavigation} canUseCommunity={canUseCommunity} />
        <HistoryGitGraph
          key={activeLesson.id}
          layout={historyGraph}
          activeLesson={activeLesson}
          currentBranchName={currentBranchName}
          readOnly={readOnly || !editing}
          interactionLocked={interactionLocked || lessonPackageControls.isPlaybackActive || Boolean(historyOrder && ["loading", "saving"].includes(historyOrder.phase))}
          historyOrder={historyOrder}
          historyActionsDisabled={(storageKind !== "ridoc" && Boolean(previewCommitId)) || lessonPackageControls.isPlaybackActive}
          canUseCommunity={canUseCommunity}
          emptyMessage={historyEmptyMessage}
          newBranchName={newBranchName}
          canMergeBranches
          onNewBranchNameChange={onNewBranchNameChange}
          onCreateBranch={async () => { if (editing && historyOrder && await historyOrder.createBranch(newBranchName)) onNewBranchNameChange(""); }}
          onPreviewCommit={(commit) => documentNavigation.activate(commit)}
          onStepDocumentHistory={async (direction, id) => { if (editing) await historyOrder?.step(direction, id); }}
          onSetChatCommitVisibility={() => undefined}
          onDeleteHistoryNode={editing ? historyOrder?.askDelete : undefined}
          deletionDisabledReason={editing ? historyOrder?.disabledReason : deletionDisabledReason}
          onCreateBranchFromCommit={async (commit) => { if (editing) await historyOrder?.createBranch(newBranchName, commit.id); }}
          onReferenceHistoryNode={onReferenceHistoryNode}
          onSwitchBranch={async (name) => { if (editing) await historyOrder?.checkout(name); }}
          onMergeBranch={async (name) => { if (editing) await historyOrder?.startMerge(name); }}
        />
      </>
      </section>

      <section
        className={clsx(
          "border-t border-gray-200 pt-6",
          lessonPackageControls.isPlaybackActive && "pointer-events-none opacity-60"
        )}
      >
        <div className="flex items-center gap-2">
          <GitBranch className="h-4 w-4 text-gray-400" />
          <p className="text-[10px] font-bold uppercase tracking-widest text-gray-400">current context</p>
        </div>
        {previewCommit ? (
          <div className="mt-4 rounded-lg border border-gray-200 bg-white p-4">
            <p className="text-xs font-semibold text-gray-900">{previewCommit.label}</p>
            <p className="mt-2 text-[11px] leading-6 text-gray-500">{compactText(previewCommit.message, 180)}</p>
          </div>
        ) : null}
        {activeBoardTask ? (
          <>
            <p className="mt-4 text-sm leading-7 text-gray-700">{activeBoardTask.question_or_topic}</p>
            <div className="mt-4 rounded-lg border border-gray-200 bg-white p-4">
              <p className="text-xs font-semibold text-gray-900">{activeBoardTask.requested_action ?? "No tasks to be performed yet"}</p>
              <p className="mt-2 text-[11px] leading-6 text-gray-500">
                {activeBoardTask.target_hint || "After execution is completed, the current list will be archived to history and cleared."}
              </p>
            </div>
          </>
        ) : learningRequirementDisplay ? (
          <>
            <div className="mt-4 rounded-lg border border-gray-200 bg-white p-4">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <p className="text-[10px] font-bold uppercase tracking-widest text-gray-400">Teaching type</p>
                  <p className="mt-1 text-sm font-semibold text-gray-900">{learningRequirementDisplay.teachingType}</p>
                </div>
                <span className="rounded-full bg-gray-100 px-2.5 py-1 text-[11px] font-semibold text-gray-600">
                  {learningRequirementStatusLabel(learningRequirementDisplay.status)}
                </span>
              </div>
              {learningRequirementDisplay.summary ? (
                <p className="mt-3 text-[11px] leading-6 text-gray-500">{learningRequirementDisplay.summary}</p>
              ) : null}
            </div>
            <FactorList title="core factors" factors={learningRequirementDisplay.coreFactors} />
            <FactorList title="auxiliary factors" factors={learningRequirementDisplay.auxiliaryFactors} />
          </>
        ) : (
          <p className="mt-4 text-sm leading-7 text-gray-700">

            Waiting for the next task requirement: describe the location to be operated, the type of action, and how you want it to be explained or written.
          </p>
        )}
        {latestBoardDecision ? (
          <div className="mt-4 rounded-lg border border-gray-200 bg-white p-4">
            <p className="text-[10px] font-bold uppercase tracking-widest text-gray-400">Current board decisions</p>
            <p className="mt-2 text-xs font-semibold text-gray-900">{latestBoardDecision.action}</p>
            <p className="mt-2 text-[11px] leading-6 text-gray-500">{latestBoardDecision.reason}</p>
          </div>
        ) : null}
      </section>
    </div>
  );
}
