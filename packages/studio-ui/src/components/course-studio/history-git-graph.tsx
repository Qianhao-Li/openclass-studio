"use client";

import clsx from "clsx";
import { useCoursePrReview } from "../../contexts/course-pr-review-context";
import { applyCoursePrHistoryView } from "../../lib/course-pr-history-view";
import {
  GitBranch,
  GitCommitHorizontal,
  GitCompare,
  GitMerge,
  Search,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";

import {
  HISTORY_GRAPH_ROW_HEIGHT,
  buildLearningHistoryGraph,
  buildLessonHistoryGraph,
  fitHistoryGraphGeometry,
  historyGraphAncestorIds,
  historyGraphColumnX,
  historyGraphEdgePath,
  historyGraphFocus,
  historyGraphNodeColor,
  historyGraphRowY,
  historyGraphSproutPath,
  historyGraphSproutTip,
  historyGraphWidth,
  type HistoryGraphLayout,
  type HistoryGraphNode,
} from "./history-graph-layout";
import { compactText, formatDate } from "./history-utils";
import { HistoryLearningViewToggle, useHistoryLearningView } from "./history-learning-view";
import type { CommitRecord, Lesson, SelectionRef } from "../../types";
import { HistoryGraphRows } from "./history-graph-rows";
import { HistoryOrderDrag } from "./history-order-drag";
import type { HistoryEditController as HistoryOrderController } from "../../hooks/course-studio/use-history-edit-session";

export type HistoryGitGraphProps = {
  layout: HistoryGraphLayout;
  activeLesson: Lesson;
  currentBranchName: string;
  readOnly: boolean;
  interactionLocked?: boolean;
  historyActionsDisabled: boolean;
  canUseCommunity: boolean;
  emptyMessage: string;
  newBranchName: string;
  canMergeBranches: boolean;
  onNewBranchNameChange: (value: string) => void;
  onCreateBranch: () => void | Promise<void>;
  onPreviewCommit: (commit: CommitRecord) => void | Promise<void>;
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
  historyOrder?: HistoryOrderController;
};

function mergeSourceNodeId(commit: CommitRecord) {
  return typeof commit.metadata?.merge_source_node_id === "string"
    ? commit.metadata.merge_source_node_id : commit.parent_ids[1];
}

function mergedSourceConversation(lesson: Lesson, mergeCommit: CommitRecord) {
  const commits = new Map(lesson.history_graph.commits.map((commit) => [commit.id, commit]));
  const baseId = typeof mergeCommit.metadata?.merge_base_commit_id === "string"
    ? mergeCommit.metadata.merge_base_commit_id
    : null;
  const rows: Array<{ id: string; role: "user" | "assistant"; content: string }> = [];
  const sourceId = mergeSourceNodeId(mergeCommit);
  let cursor = sourceId ? commits.get(sourceId) : undefined;
  const seen = new Set<string>();
  while (cursor && cursor.id !== baseId && !seen.has(cursor.id)) {
    seen.add(cursor.id);
    const user = typeof cursor.metadata?.user_message === "string" ? cursor.metadata.user_message.trim() : "";
    const assistant = typeof cursor.metadata?.assistant_message === "string" ? cursor.metadata.assistant_message.trim() : "";
    if (assistant) rows.unshift({ id: `${cursor.id}:assistant`, role: "assistant", content: assistant });
    if (user) rows.unshift({ id: `${cursor.id}:user`, role: "user", content: user });
    cursor = cursor.parent_ids[0] ? commits.get(cursor.parent_ids[0]) : undefined;
  }
  return rows;
}

function timeGapLabel(older: string, newer: string) {
  const gap = new Date(newer).getTime() - new Date(older).getTime();
  if (!Number.isFinite(gap) || gap <= 0) {
    return "same moment";
  }
  const minutes = Math.round(gap / 60_000);
  if (minutes < 60) {
    return `${Math.max(minutes, 1)} min apart`;
  }
  const hours = Math.round(minutes / 60);
  return hours < 48 ? `${hours} h apart` : `${Math.round(hours / 24)} d apart`;
}

/** Read-only comparison of two nodes; it never touches the lesson history. */
function compareFacts(first: HistoryGraphNode, second: HistoryGraphNode) {
  const [older, newer] = first.row > second.row ? [first, second] : [second, first];
  const facts: Array<{ label: string; value: string }> = [
    { label: "Type", value: older.typeCode === newer.typeCode ? older.typeCode : `${older.typeCode} → ${newer.typeCode}` },
    {
      label: "Branch",
      value: older.branchName === newer.branchName ? older.branchName : `${older.branchName} → ${newer.branchName}`,
    },
    { label: "Distance", value: `${Math.abs(first.row - second.row)} nodes · ${timeGapLabel(older.createdAt, newer.createdAt)}` },
  ];
  if (older.commit && newer.commit) {
    const olderLength = older.commit.snapshot.content_text.trim().length;
    const newerLength = newer.commit.snapshot.content_text.trim().length;
    const delta = newerLength - olderLength;
    facts.push({
      label: "Document",
      value: delta === 0 ? `unchanged length (${newerLength} chars)` : `${delta > 0 ? "+" : ""}${delta} chars (${olderLength} → ${newerLength})`,
    });
  }
  return { older, newer, facts };
}

export function HistoryGitGraph({
  layout: sourceLayout,
  activeLesson,
  currentBranchName,
  readOnly,
  interactionLocked = false,
  historyActionsDisabled,
  canUseCommunity,
  emptyMessage,
  newBranchName,
  canMergeBranches,
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
  historyOrder,
}: HistoryGitGraphProps) {
  const reviewContext = useCoursePrReview();
  const layout = useMemo(() => applyCoursePrHistoryView(sourceLayout, reviewContext?.review.comparison), [sourceLayout, reviewContext?.review.comparison]);
  const [query, setQuery] = useState("");
  const [branchFocus, setBranchFocus] = useState("all");
  const [rounded, setRounded] = useState(true);
  const [compareMode, setCompareMode] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [tableWidth, setTableWidth] = useState(0);
  const tableRef = useRef<HTMLDivElement>(null);

  const sorting = Boolean(historyOrder?.active);
  const { displayedLayout: readingLayout, hasLearningOrder, showingLearningOrder, toggleLearningView } = useHistoryLearningView(
    layout, activeLesson.history_graph.branches[currentBranchName], branchFocus
  );
  const orderDraft = historyOrder?.draft;
  const displayedLayout = useMemo(() => {
    if (reviewContext) return layout;
    if (!orderDraft) return readingLayout;
    const draftLayout = buildLessonHistoryGraph(orderDraft.lesson, null);
    const authoritative = new Map(layout.nodes.map((node) => [node.id, node]));
    const draftNodes = draftLayout.nodes.map((node) => {
      const live = authoritative.get(node.id);
      return { ...node,
        currentDocument: live?.currentDocument ?? node.currentDocument,
        withdrawnDocument: live?.withdrawnDocument ?? node.withdrawnDocument,
        undoDocumentTargetId: live?.undoDocumentTargetId ?? null,
        redoDocument: live?.redoDocument ?? false,
        documentHistoryBlockedReason: live?.documentHistoryBlockedReason,
        pendingDeletion: live?.pendingDeletion ?? node.pendingDeletion,
        comparisonStatus: live?.comparisonStatus,
        color: live?.color ?? node.color,
        nodeReferences: live?.nodeReferences ?? node.nodeReferences,
      };
    });
    return buildLearningHistoryGraph({ ...draftLayout, nodes: draftNodes }, orderDraft.ids);
  }, [orderDraft, readingLayout, layout, reviewContext]);
  const { nodes, edges, sprouts, lanes, columnCount } = displayedLayout;
  const showMetadataColumns = tableWidth >= 720;
  const typeColumnWidth = showMetadataColumns
    ? Math.min(170, Math.max(96, historyGraphWidth(columnCount) + 34))
    : Math.min(96, Math.max(70, tableWidth * 0.24));
  const graphWidth = sorting ? 32 : Math.max(38, typeColumnWidth - 30);
  const graphGeometry = fitHistoryGraphGeometry(columnCount, graphWidth);
  const bodyHeight = Math.max(nodes.length * HISTORY_GRAPH_ROW_HEIGHT, 1);
  const refsColumnWidth = reviewContext && !showMetadataColumns ? 0 : showMetadataColumns ? 96 : Math.min(76, Math.max(48, tableWidth * 0.18));
  const actionsColumnWidth = sorting ? (showMetadataColumns ? 176 : 106) : showMetadataColumns ? 176 : 86;
  const gridTemplate = showMetadataColumns
    ? `${typeColumnWidth}px minmax(0,1fr) ${refsColumnWidth}px 54px 58px ${actionsColumnWidth}px`
    : `${typeColumnWidth}px minmax(0,1fr) ${refsColumnWidth}px ${actionsColumnWidth}px`;

  useEffect(() => {
    const element = tableRef.current;
    if (!element) return;
    const update = () => setTableWidth(element.getBoundingClientRect().width);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const focus = useMemo(
    () => historyGraphFocus(displayedLayout, { query, branchName: branchFocus }),
    [displayedLayout, query, branchFocus]
  );
  const highlighted = useMemo(() => {
    if (!compareMode || !selected.length) {
      return focus.emphasizedIds;
    }
    return new Set([
      ...focus.emphasizedIds,
      ...historyGraphAncestorIds(displayedLayout, selected),
    ]);
  }, [compareMode, focus.emphasizedIds, displayedLayout, selected]);

  const selectedNodes = selected.flatMap((id) => {
    const node = nodes.find((candidate) => candidate.id === id);
    return node ? [node] : [];
  });
  const comparison = !sorting && selectedNodes.length === 2 ? compareFacts(selectedNodes[0], selectedNodes[1]) : null;
  const detailNode = nodes.find((node) => node.active) ?? null;
  const detailPedigree = detailNode?.commit && detailNode.nodeKind === "merge" && mergeSourceNodeId(detailNode.commit)
    ? mergedSourceConversation(activeLesson, detailNode.commit)
    : [];
  const filtering = Boolean(query.trim()) || branchFocus !== "all";
  const focusedLane = lanes.find((lane) => lane.branchName === branchFocus) ?? null;
  const mergeTargetLane = focusedLane && !focusedLane.isCurrent ? focusedLane : null;
  const mergeBlockedReason = mergeTargetLane && [currentBranchName, mergeTargetLane.branchName].some(
    (name) => activeLesson.history_graph.branches[name]?.document_redo_commit_ids?.length
  ) ? "This branch has undone board changes. Redo them, or continue learning on a new branch before merging." : null;
  const actionButtonSize = showMetadataColumns ? "h-6 w-6" : "h-5 w-5";
  const deletedBranchReferences = activeLesson.history_graph.branches[currentBranchName]?.node_refs?.filter((ref) => ref.status === "deleted") ?? [];

  const toggleSelection = (nodeId: string) => {
    setSelected((current) => {
      if (current.includes(nodeId)) {
        return current.filter((id) => id !== nodeId);
      }
      return current.length < 2 ? [...current, nodeId] : [current[1], nodeId];
    });
  };

  const onRowActivate = (node: HistoryGraphNode) => {
    if (compareMode) {
      toggleSelection(node.id);
      return;
    }
    if (!interactionLocked && node.commit) {
      void onPreviewCommit(node.commit);
    }
  };

  return (
    <HistoryOrderDrag controller={historyOrder} nodes={nodes}>
    <div className="mt-4 overflow-hidden rounded-2xl border border-[#2f6bff]/40 bg-white shadow-[0_0_0_3px_rgba(47,107,255,0.06),0_10px_28px_rgba(32,63,120,0.07)]">
      <header className="flex items-start justify-between gap-3 border-b border-gray-100 px-4 py-3.5">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-gray-900">Revision history</h3>
          <p className="mt-1 text-[11px] leading-4 text-gray-500">
            {nodes.length} nodes · {lanes.length} branches · {sorting ? `Editing ${currentBranchName}` : showingLearningOrder && !reviewContext ? "learning order" : "complete lesson graph"}
          </p>
          {showingLearningOrder && !reviewContext ? <p className="mt-1 text-[10px] text-gray-500">Read from bottom to top. Preview opens each event’s original snapshot.</p> : null}
          {reviewContext ? <p className="mt-1 text-[10px] leading-5 text-gray-500">Read from bottom to top. The main branch follows the latest learning order; parent dependencies take priority, and lines preserve original source relationships.</p> : null}
          {deletedBranchReferences.length ? <p className="mt-1 text-[10px] text-gray-500" title={deletedBranchReferences.map((ref) => `${ref.role}: ${ref.lesson_id}/${ref.node_id}`).join("\n")}>Branch source: target deleted</p> : null}
        </div>
        <GitCommitHorizontal className="mt-0.5 h-4 w-4 shrink-0 text-[#2f6bff]" />
      </header>

      {!readOnly ? (
        <div className="flex gap-2 border-b border-gray-100 px-3 py-2.5">
          <input
            value={newBranchName}
            onChange={(event) => onNewBranchNameChange(event.target.value)}
            placeholder="new branch name"
            aria-label="New branch name"
            disabled={interactionLocked}
            className="h-9 min-w-0 flex-1 rounded-lg border border-gray-200 bg-white px-3 text-[11px] text-gray-900 outline-none placeholder:text-gray-400 focus:border-[#2f6bff] disabled:cursor-not-allowed disabled:bg-gray-50 disabled:opacity-60"
          />
          <button
            type="button"
            aria-label="branch"
            disabled={interactionLocked}
            onClick={() => void onCreateBranch()}
            className="inline-flex h-9 shrink-0 items-center gap-1.5 rounded-lg bg-[#1a1a1a] px-3 text-[11px] font-semibold text-white disabled:cursor-not-allowed disabled:opacity-40"
          >
            <GitBranch className="h-3.5 w-3.5" />
            Branch
          </button>
        </div>
      ) : null}

      <div className="grid grid-cols-[minmax(0,1fr)_minmax(104px,auto)] gap-2 border-b border-gray-100 p-3">
        <label className="relative min-w-0">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-gray-400" />
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search history"
            aria-label="Search history nodes"
            className="h-9 w-full min-w-0 rounded-lg border border-gray-200 bg-white pl-8 pr-2 text-[11px] text-gray-900 outline-none placeholder:text-gray-400 focus:border-[#2f6bff]"
          />
        </label>
        <select
          value={branchFocus}
          onChange={(event) => setBranchFocus(event.target.value)}
          aria-label="Focus a branch"
          className="h-9 min-w-0 max-w-40 rounded-lg border border-gray-200 bg-white px-2 text-[11px] text-gray-500 outline-none focus:border-[#2f6bff]"
        >
          <option value="all">All branches</option>
          {lanes.map((lane) => (
            <option key={lane.branchName} value={lane.branchName}>
              {lane.branchName}{lane.archived ? " (archived)" : ""}
            </option>
          ))}
        </select>
        <div className="col-span-2 flex flex-wrap items-center gap-2">
          {hasLearningOrder && !sorting && !reviewContext ? (
            <HistoryLearningViewToggle
              showingLearningOrder={showingLearningOrder}
              onClick={() => {
                toggleLearningView();
                setBranchFocus("all");
                setSelected([]);
              }}
            />
          ) : null}
          <button
            type="button"
            aria-pressed={rounded}
            disabled={sorting}
            onClick={() => setRounded((current) => !current)}
            className={clsx(
              "h-8 rounded-lg border px-2.5 text-[11px] font-medium transition",
              rounded
                ? "border-[#2f6bff]/45 bg-[#f2f6ff] text-[#2f6bff]"
                : "border-gray-200 bg-white text-gray-500 hover:text-black"
            )}
          >
            {rounded ? "Rounded" : "Angular"}
          </button>
          <button
            type="button"
            aria-pressed={compareMode}
            disabled={sorting}
            onClick={() => {
              setCompareMode((current) => !current);
              setSelected([]);
            }}
            className={clsx(
              "inline-flex h-8 items-center gap-1.5 rounded-lg border px-2.5 text-[11px] font-medium transition",
              compareMode
                ? "border-[#8b3dff]/45 bg-[#f7f2ff] text-[#8b3dff]"
                : "border-gray-200 bg-white text-gray-500 hover:text-black"
            )}
          >
            <GitCompare className="h-3.5 w-3.5" />
            {compareMode ? `${selected.length}/2` : "Compare"}
          </button>
          {historyOrder?.enabled && !sorting ? <button type="button" aria-label={historyOrder.recovery ? "Resume unfinished edit" : "Edit mode"} title={historyOrder.disabledReason ?? "Edit course history"} disabled={Boolean(historyOrder.disabledReason) || historyOrder.phase !== "idle"} onClick={() => void historyOrder.begin()} className="h-8 rounded-lg border border-violet-200 bg-violet-50 px-2.5 text-[11px] font-medium text-violet-700 disabled:opacity-40">{historyOrder.phase === "loading" ? "Loading edit…" : historyOrder.recovery ? "Resume unfinished edit" : "Edit mode"}</button> : null}
          {historyOrder?.entering ? <button type="button" aria-label="Cancel History preparation" onClick={historyOrder.cancelBegin} className="h-8 rounded-lg border border-violet-200 px-2.5 text-[11px] text-violet-700">Cancel</button> : null}
          {sorting && historyOrder ? <>
            <button type="button" aria-label="Exit edit mode" title="Exit editing and keep the unfinished draft" onClick={() => void historyOrder.exit()} disabled={historyOrder.phase === "saving" || historyOrder.phase === "loading"} className="h-8 rounded-lg border border-violet-200 px-2.5 text-[11px] text-violet-700 disabled:opacity-40">Exit</button>
            <span className="text-[10px] text-violet-700">{historyOrder.selectedIds.length} conversations selected · {historyOrder.phase === "operating" ? "Saving draft…" : historyOrder.dirty ? "Edits not committed" : "No changes yet"}</span>
          </> : null}
          {historyOrder?.enabled && (sorting || historyOrder.pending) ? <button type="button" aria-label="Save history changes" title={historyOrder.disabledReason ?? (historyOrder.dirty ? "Review and save all History changes" : "No History changes to save")} disabled={!historyOrder.dirty || Boolean(historyOrder.disabledReason) || historyOrder.phase !== "idle"} onClick={() => void historyOrder.save()} className="h-8 rounded-lg bg-violet-600 px-2.5 text-[11px] font-medium text-white disabled:opacity-40">{historyOrder.phase === "saving" ? "Saving…" : "Save"}</button> : null}
          {historyOrder?.enabled && historyOrder.session && historyOrder.stale ? <button type="button" aria-label="Discard unfinished edit" disabled={historyOrder.phase !== "idle"} onClick={() => {
            if (window.confirm("Discard this unfinished History edit? Your draft changes will be removed. Saved course content will stay unchanged.")) void historyOrder.discard();
          }} className="h-8 rounded-lg border border-red-200 px-2.5 text-[11px] text-red-700 disabled:opacity-40">Discard unfinished edit</button> : null}
          {focusedLane && !focusedLane.isCurrent && !readOnly ? (
            <button
              type="button"
              disabled={interactionLocked}
              onClick={() => void onSwitchBranch(focusedLane.branchName)}
              className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-blue-200 bg-blue-50 px-2.5 text-[11px] font-medium text-blue-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <GitCommitHorizontal className="h-3.5 w-3.5" />
              Checkout
            </button>
          ) : null}
          {!readOnly && canMergeBranches && mergeTargetLane && sorting ? (
            <button
              type="button"
              aria-label={`Merge ${mergeTargetLane.branchName} into ${currentBranchName}`}
              title={mergeBlockedReason ?? `Merge ${mergeTargetLane.branchName} into current branch`}
              disabled={interactionLocked || Boolean(mergeBlockedReason)}
              onClick={() => void onMergeBranch(mergeTargetLane.branchName)}
              className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-violet-200 bg-violet-50 px-2.5 text-[11px] font-medium text-violet-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <GitMerge className="h-3.5 w-3.5" />
              <span className="max-w-48 truncate">
                Merge {mergeTargetLane.branchName} → {currentBranchName}
              </span>
            </button>
          ) : null}
          <p className="ml-auto text-[10px] text-gray-400">
            {compareMode
              ? "Select two rows"
              : filtering
                ? `${focus.matchIds.size} / ${nodes.length} direct matches`
                : `${columnCount} active lanes`}
          </p>
        </div>
        {historyOrder?.message || historyOrder?.disabledReason || historyOrder?.stale ? <p role="status" className="col-span-2 text-[11px] leading-5 text-amber-800">{historyOrder.message ?? historyOrder.disabledReason ?? "The course has changed since this edit started. Resume an untouched draft, or discard this edit to start again."}</p> : null}
        {sorting ? <p className="col-span-2 text-[10px] leading-5 text-gray-500">Newest at the top, oldest at the bottom. Select rows and drag their handles. Use Shift for a range or ⌘/Ctrl for multiple selections. Gray nodes are pending deletion and can be restored. Save reviews and applies all changes together. Exit keeps the unfinished draft.</p> : historyOrder?.pending ? <p className="col-span-2 text-[10px] leading-5 text-violet-700">You have unsaved History changes. Resume editing or Save to review and apply them.</p> : null}
        {!readOnly && mergeBlockedReason ? <p role="status" className="col-span-2 text-[11px] text-amber-800">{mergeBlockedReason}</p> : null}
      </div>

      {nodes.length ? (
        <div className="p-3">
          <div
            ref={tableRef}
            data-testid={sorting ? "history-order-list" : "history-graph-viewport"}
            data-history-order-scroll={sorting ? "true" : undefined}
            className={clsx("w-full min-w-0 overflow-x-hidden rounded-lg border border-gray-200", sorting && "max-h-[60vh] overflow-y-auto overscroll-contain")}
          >
          <div role="table" aria-label="Lesson history graph" className="w-full min-w-0">
            <div
              role="row"
              style={{ gridTemplateColumns: gridTemplate } satisfies CSSProperties}
              className="grid h-9 items-center gap-2 border-b border-gray-100 px-3 font-mono text-[10px] font-medium uppercase tracking-[0.08em] text-gray-400"
            >
              <span role="columnheader">Type</span>
              <span role="columnheader">Description</span>
              <span role="columnheader">{refsColumnWidth ? "Refs" : ""}</span>
              {showMetadataColumns ? <span role="columnheader">Actor</span> : null}
              {showMetadataColumns ? <span role="columnheader">Date</span> : null}
              <span role="columnheader" className="text-right">Actions</span>
            </div>

            <div className="relative" role="rowgroup">
              <svg
                aria-hidden="true"
                data-history-graph-canvas="true"
                className="pointer-events-none absolute left-3 top-0 z-[2] overflow-visible"
                width={graphGeometry.width}
                height={bodyHeight}
                viewBox={`0 0 ${graphGeometry.width} ${bodyHeight}`}
              >
                {edges.map((edge) => (
                  <path
                    key={edge.id}
                    d={historyGraphEdgePath(edge, rounded, graphGeometry)}
                    fill="none"
                    stroke={edge.color}
                    strokeWidth={1.25}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    vectorEffect="non-scaling-stroke"
                    opacity={highlighted.has(edge.fromId) && highlighted.has(edge.toId) ? 1 : 0.25}
                    data-history-graph-connector={edge.kind}
                    data-history-graph-direction="up"
                    data-history-graph-from-lane={edge.fromColumn}
                    data-history-graph-to-lane={edge.toColumn}
                  />
                ))}
                {sprouts.map((sprout) => {
                  const tip = historyGraphSproutTip(sprout, graphGeometry);
                  return (
                    <g key={`sprout:${sprout.id}`} opacity={branchFocus === "all" || branchFocus === sprout.branchName ? 1 : 0.25}>
                      <path
                        d={historyGraphSproutPath(sprout, rounded, graphGeometry)}
                        fill="none"
                        stroke={sprout.color}
                        strokeWidth={1.25}
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        strokeDasharray="3 3"
                        vectorEffect="non-scaling-stroke"
                        data-history-graph-connector="branch"
                        data-history-graph-direction="up"
                        data-history-graph-from-lane={sprout.column}
                        data-history-graph-to-lane={sprout.baseColumn}
                      />
                      <circle
                        cx={tip.x}
                        cy={tip.y}
                        r={Math.max(2, graphGeometry.nodeRadius - 1)}
                        fill="var(--surface, #ffffff)"
                        stroke={sprout.color}
                        strokeWidth={1.5}
                        strokeDasharray="2 2"
                      />
                    </g>
                  );
                })}
                {nodes.map((node) => {
                  const cx = historyGraphColumnX(node.column, graphGeometry);
                  const cy = historyGraphRowY(node.row);
                  const color = historyGraphNodeColor(node);
                  return (
                    <g
                      key={node.id}
                      data-history-graph-node={node.id}
                      data-history-graph-kind={node.nodeKind}
                      data-history-graph-color={color}
                      data-history-graph-lane={node.column}
                      data-pr-node-status={node.comparisonStatus}
                      opacity={highlighted.has(node.id) ? 1 : 0.25}
                    >
                      {node.comparisonStatus === "added" || node.comparisonStatus === "reviewer_added" ? <circle className="course-pr-node-glow" cx={cx} cy={cy} r={graphGeometry.nodeRadius + 5} fill="none" stroke="#e6b325" strokeWidth={3} /> : null}
                      {node.head ? (
                        <circle cx={cx} cy={cy} r={graphGeometry.nodeRadius + 3} fill="none" stroke={color} strokeOpacity={0.2} strokeWidth={4} />
                      ) : null}
                      {node.active || (sorting ? historyOrder!.selectedIds : selected).includes(node.id) ? (
                        <circle cx={cx} cy={cy} r={graphGeometry.nodeRadius + 4.5} fill="none" stroke={color} strokeOpacity={0.45} strokeWidth={1.5} />
                      ) : null}
                      <circle cx={cx} cy={cy} r={graphGeometry.nodeRadius} fill="var(--surface, #ffffff)" stroke={color} strokeWidth={2} />
                      {node.nodeKind === "merge" ? (
                        <circle cx={cx} cy={cy} r={Math.max(1.25, graphGeometry.nodeRadius * 0.4)} fill={color} />
                      ) : null}
                    </g>
                  );
                })}
              </svg>

              <HistoryGraphRows {...{ activeLesson, currentBranchName, readOnly, interactionLocked, historyActionsDisabled, canUseCommunity, onPreviewCommit, onStepDocumentHistory, onSetChatCommitVisibility, onDeleteHistoryNode, deletionDisabledReason, onCreateBranchFromCommit, onReferenceHistoryNode, onSwitchBranch, nodes, branchFocus, showMetadataColumns, graphGeometry, gridTemplate, selected, highlighted, actionButtonSize, onRowActivate, historyOrder }} />
            </div>
          </div>
          </div>
        </div>
      ) : (
        <p className="px-4 py-10 text-center text-xs text-gray-500">{emptyMessage}</p>
      )}

      {comparison ? (
        <div className="mx-3 mb-3 rounded-lg border border-[#8b3dff]/30 bg-[#faf6ff] px-3 py-2.5 text-[11px] text-gray-600">
          <p className="text-gray-900">
            <strong className="font-semibold">{comparison.older.title}</strong>
            <span className="mx-1.5 text-gray-400">→</span>
            <strong className="font-semibold">{comparison.newer.title}</strong>
          </p>
          <dl className="mt-1.5 grid gap-x-3 gap-y-1 sm:grid-cols-2">
            {comparison.facts.map((fact) => (
              <div key={fact.label} className="flex min-w-0 gap-1.5">
                <dt className="shrink-0 font-mono text-[10px] uppercase tracking-[0.08em] text-gray-400">{fact.label}</dt>
                <dd className="min-w-0 truncate">{fact.value}</dd>
              </div>
            ))}
          </dl>
        </div>
      ) : null}

      {detailNode ? (
        <div className="mx-3 mb-3 rounded-lg border border-gray-200 bg-[#fbfcfe] px-3 py-2.5">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-gray-400">Previewing</span>
            <span className="min-w-0 flex-1 truncate text-[12px] font-semibold text-gray-900">{detailNode.title}</span>
            <span className="shrink-0 font-mono text-[10px] text-gray-400">{formatDate(detailNode.createdAt)}</span>
          </div>
          {detailNode.summary ? (
            <p className="mt-1.5 text-[11px] leading-5 text-gray-500">{compactText(detailNode.summary, 240)}</p>
          ) : null}
          {detailNode.commit && detailNode.nodeKind === "merge" && mergeSourceNodeId(detailNode.commit) ? (
            <details className="mt-2 rounded border border-violet-100 bg-violet-50/60 px-2 py-1.5 text-[10px] text-violet-700">
              <summary className="cursor-pointer font-semibold">Expand source branch pedigree</summary>
              <p className="mt-1 font-mono">
                {String(detailNode.commit.metadata?.merge_source_branch ?? "source")} · {mergeSourceNodeId(detailNode.commit)?.slice(0, 10)}
              </p>
              {detailPedigree.length ? (
                <div className="mt-2 space-y-1.5 border-t border-violet-100 pt-2 font-sans">
                  {detailPedigree.map((message) => (
                    <p key={message.id} className="rounded bg-white/80 px-2 py-1.5 leading-5 text-stone-600">
                      <strong className="mr-1 text-stone-800">{message.role === "user" ? "User" : "OpenClass"}:</strong>
                      {compactText(message.content, 240)}
                    </p>
                  ))}
                </div>
              ) : null}
            </details>
          ) : null}
        </div>
      ) : null}
    </div>
    </HistoryOrderDrag>
  );
}
