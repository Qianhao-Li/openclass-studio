"use client";

import clsx from "clsx";
import { Eye, GitBranch, GitCommitHorizontal, Quote, Redo2, Share2, Trash2, Undo2, LockKeyhole } from "lucide-react";
import type { CSSProperties } from "react";
import { documentParentCommitId, historyNodeCommunityShareHref, historyNodeRelativeTime, historyNodeSelection, isChatCommitHidden, isHideableChatCommit } from "./history-graph-utils";
import { HISTORY_GRAPH_ROW_HEIGHT, HISTORY_GRAPH_FALLBACK_COLOR, historyGraphNodeColor, type HistoryGraphNode, type HistoryGraphGeometry } from "./history-graph-layout";
import { HistoryOrderRow } from "./history-order-drag";
import type { HistoryGitGraphProps } from "./history-git-graph";
import type { HistoryEditController as HistoryOrderController } from "../../hooks/course-studio/use-history-edit-session";

const ACTION_BUTTON_CLASS = "inline-flex shrink-0 items-center justify-center rounded border border-gray-200 bg-white text-gray-500 transition hover:border-gray-300 hover:text-black disabled:cursor-not-allowed disabled:opacity-35";

type Props = Pick<HistoryGitGraphProps, "activeLesson" | "currentBranchName" | "readOnly" | "interactionLocked" | "historyActionsDisabled" | "canUseCommunity" | "onPreviewCommit" | "onStepDocumentHistory" | "onSetChatCommitVisibility" | "onDeleteHistoryNode" | "deletionDisabledReason" | "onCreateBranchFromCommit" | "onReferenceHistoryNode" | "onSwitchBranch"> & {
  nodes: HistoryGraphNode[]; branchFocus: string; showMetadataColumns: boolean; graphGeometry: HistoryGraphGeometry;
  gridTemplate: string; selected: string[]; highlighted: Set<string>; actionButtonSize: string;
  onRowActivate: (node: HistoryGraphNode) => void; historyOrder?: HistoryOrderController;
};

export function HistoryGraphRows({ activeLesson, currentBranchName, readOnly, interactionLocked, historyActionsDisabled, canUseCommunity, onPreviewCommit, onStepDocumentHistory, onSetChatCommitVisibility, onDeleteHistoryNode, deletionDisabledReason, onCreateBranchFromCommit, onReferenceHistoryNode, onSwitchBranch, nodes, branchFocus, showMetadataColumns, graphGeometry, gridTemplate, selected, highlighted, actionButtonSize, onRowActivate, historyOrder }: Props) {
  const sorting = Boolean(historyOrder?.active);
  const sortingLocked = Boolean(sorting && (["saving", "loading"].includes(historyOrder?.phase ?? "idle") || historyOrder?.stale || historyOrder?.disabledReason || interactionLocked));
  const selectedIds = sorting ? historyOrder!.selectedIds : selected;
  const selectable = nodes.filter((node) => node.nodeKind === "chat").map((node) => node.id);
  return <>{nodes.map((node) => {
                const chatHidden = Boolean(node.commit && isChatCommitHidden(node.commit));
                const nodeColor = chatHidden ? HISTORY_GRAPH_FALLBACK_COLOR : historyGraphNodeColor(node);
                const comparisonLabel = node.comparisonStatus === "added" ? "Added in PR" : node.comparisonStatus === "reviewer_added" ? "Added by reviewer" : node.comparisonStatus === "removed" ? "Removed in PR" : node.comparisonStatus === "target_only" ? "Merged in main" : "";
                const inactive = node.pendingDeletion || node.withdrawnDocument || chatHidden || node.comparisonStatus === "removed";
                const emphasized = selectedIds.includes(node.id) || node.active;
                const canToggleChat = Boolean(node.commit && isHideableChatCommit(node.commit));
                const nativeDocumentHistory = node.undoDocumentTargetId !== undefined;
                const undoTargetId = nativeDocumentHistory ? node.undoDocumentTargetId
                  : node.commit && node.currentDocument ? documentParentCommitId(activeLesson, node.commit) : null;
                const documentDisabledReason = node.documentHistoryBlockedReason
                  ?? (sorting && historyOrder?.stale ? "The course has changed. Your order draft is preserved. Reload the order first." : null)
                  ?? (sorting ? historyOrder?.disabledReason : null) ?? deletionDisabledReason
                  ?? (interactionLocked ? "An operation is in progress. Wait before undoing or redoing." : null);
                const isCurrentHead = node.refs.some((ref) => ref.isCurrent);
                const checkoutBranch = branchFocus !== "all" && node.refs.some((ref) => ref.branchName === branchFocus)
                  ? branchFocus
                  : node.refs.find((ref) => !ref.isCurrent)?.branchName ??
                    (node.branchName !== currentBranchName && activeLesson.history_graph.branches[node.branchName]
                      ? node.branchName : null);
                return (
                  <HistoryOrderRow node={node}
                    key={node.id}
                    role="row"
                    data-history-node-id={node.id}
                    data-continuation-node-id={node.continuationNodeId}
                    data-branch-source-node-id={node.branchSourceNodeId ?? undefined}
                    data-history-node-kind={node.nodeKind}
                    data-document-withdrawn={node.withdrawnDocument ? "true" : "false"}
                    data-pending-deletion={node.pendingDeletion ? "true" : "false"}
                    data-current-document={node.currentDocument ? "true" : "false"}
                    data-pr-node-status={node.comparisonStatus}
                    data-head={isCurrentHead ? "true" : "false"}
                    data-selected={selectedIds.includes(node.id) ? "true" : "false"}
                    onClick={(event) => sorting && node.nodeKind === "chat" ? historyOrder?.select(node.id, selectable, { range: event.shiftKey, toggle: event.metaKey || event.ctrlKey }) : onRowActivate(node)}
                    style={{ gridTemplateColumns: gridTemplate, height: HISTORY_GRAPH_ROW_HEIGHT,
                      "--history-node-hover": `${nodeColor}0a`,
                      backgroundColor: inactive ? "var(--surface-muted, #f9fafb)" : emphasized ? `${nodeColor}12` : isCurrentHead ? `${nodeColor}08` : undefined,
                      outline: emphasized ? `1px solid ${nodeColor}66` : undefined,
                      outlineOffset: -1,
                    } as CSSProperties}
                    className={clsx(
                      "relative z-[1] grid cursor-pointer items-center gap-2 border-b border-gray-100 px-3 text-left transition last:border-b-0",
                      "bg-white hover:bg-[var(--history-node-hover)]",
                      !highlighted.has(node.id) && "opacity-45",
                      inactive && "opacity-60",
                      (node.comparisonStatus === "added" || node.comparisonStatus === "reviewer_added") && "course-pr-added-row"
                    )}
                  >{(dragHandle) => <>
                    <span
                      role="cell"
                      style={{ paddingLeft: graphGeometry.width, color: nodeColor } satisfies CSSProperties}
                      className="truncate font-mono text-[10px] font-semibold"
                    >
                      {sorting && node.nodeKind === "chat" ? <input type="checkbox" aria-label={`Select ${node.title}`} checked={selectedIds.includes(node.id)} disabled={sortingLocked} onChange={() => undefined} onClick={(event) => { event.stopPropagation(); historyOrder?.select(node.id, selectable, { range: event.shiftKey, toggle: true }); }} className="mr-1 h-3.5 w-3.5 align-middle accent-blue-600" /> : null}
                      {node.typeCode}
                    </span>
                    <span role="cell" className="min-w-0">
                      <button
                        type="button"
                        aria-label={`${node.typeCode} ${node.title}`}
                        disabled={sorting ? sortingLocked || !node.commit : interactionLocked || !node.commit}
                        onClick={(event) => {
                          event.stopPropagation();
                          if (sorting && node.nodeKind === "chat") historyOrder?.select(node.id, selectable, { range: event.shiftKey, toggle: event.metaKey || event.ctrlKey }); else onRowActivate(node);
                        }}
                        className="flex w-full min-w-0 flex-col justify-center text-left disabled:cursor-not-allowed"
                      >
                        <span className="flex min-w-0 items-center gap-1">
                          {showMetadataColumns && comparisonLabel ? <span className="shrink-0 rounded bg-amber-50 px-1 py-0.5 text-[8px] text-stone-700">{comparisonLabel}</span> : null}
                          {node.currentDocument && (!node.comparisonStatus || showMetadataColumns) ? (
                            <span
                              title="Current document"
                              className="shrink-0 rounded bg-emerald-50 px-1 py-0.5 text-[8px] font-bold uppercase tracking-[0.08em] text-emerald-700"
                            >
                              Current
                            </span>
                          ) : null}
                          {node.pendingDeletion ? <span className="shrink-0 rounded bg-gray-100 px-1 py-0.5 text-[8px] font-bold text-gray-500">Pending deletion</span> : node.withdrawnDocument ? (
                            <span className="shrink-0 rounded bg-gray-100 px-1 py-0.5 text-[8px] font-bold uppercase tracking-[0.08em] text-gray-500">
                              {nativeDocumentHistory ? "Withdrawn" : "Withdrawn"}
                            </span>
                          ) : null}
                          {node.nodeReferences.some((ref) => ref.status === "deleted") ? <span title={node.nodeReferences.filter((ref) => ref.status === "deleted").map((ref) => `${ref.role}: ${ref.lesson_id}/${ref.node_id}`).join("\n")} className="shrink-0 rounded bg-gray-100 px-1 text-[8px] text-gray-600">Target deleted</span> : null}
                          {chatHidden ? (
                            <span
                              title="Hidden chat"
                              className="shrink-0 rounded bg-amber-50 px-1 py-0.5 text-[8px] font-bold uppercase tracking-[0.08em] text-amber-700"
                            >
                              Hidden
                            </span>
                          ) : null}
                          {node.commit?.metadata?.source_message_deleted || node.commit?.metadata?.branch_origin_deleted ? (
                            <span className="shrink-0 rounded bg-amber-50 px-1 text-[8px] text-amber-800">
                              {node.commit.metadata.source_message_deleted ? "Original conversation deleted" : "Original source deleted"}
                            </span>
                          ) : null}
                          <span
                            className="min-w-0 flex-1 truncate text-[12px] text-gray-900"
                            title={node.summary && node.summary !== node.title ? `${node.title} — ${node.summary}` : node.title}
                          >
                            <span className="font-semibold">{node.title}</span>
                            {showMetadataColumns && node.summary && node.summary !== node.title ? (
                              <span className="ml-1.5 text-gray-400">{node.summary}</span>
                            ) : null}
                          </span>
                        </span>
                        {!showMetadataColumns ? (
                          <span className="mt-0.5 truncate font-mono text-[9px] text-gray-400">
                            {comparisonLabel ? <span className={node.comparisonStatus === "removed" ? "text-gray-500" : "text-amber-700"}>{comparisonLabel} · </span> : null}{node.actor} · {historyNodeRelativeTime(node.createdAt)}
                          </span>
                        ) : null}
                      </button>
                    </span>
                    <span role="cell" className="flex min-w-0 items-center gap-1 overflow-hidden">
                      {isCurrentHead ? (
                        <span className="shrink-0 rounded bg-black px-1 py-0.5 font-mono text-[9px] font-bold text-white">HEAD</span>
                      ) : null}
                      {node.refs.map((ref) => (
                        <button
                          type="button"
                          key={`${node.id}:${ref.branchName}`}
                          title={ref.isCurrent ? `${ref.branchName} (current)` : `Checkout ${ref.branchName}`}
                          aria-label={ref.branchName}
                          disabled={readOnly || interactionLocked || ref.isCurrent}
                          onClick={(event) => {
                            event.stopPropagation();
                            if (!readOnly && !ref.isCurrent) void onSwitchBranch(ref.branchName);
                          }}
                          className="min-w-0 truncate rounded border px-1 py-0.5 font-mono text-[10px] font-medium disabled:cursor-default"
                          style={{ borderColor: `${ref.color}55`, color: ref.color, backgroundColor: `${ref.color}0f` }}
                        >
                          {ref.branchName}
                        </button>
                      ))}
                    </span>
                    {showMetadataColumns ? (
                      <span role="cell" className="truncate font-mono text-[10px] text-gray-500">{node.actor}</span>
                    ) : null}
                    {showMetadataColumns ? (
                      <span role="cell" className="truncate font-mono text-[10px] text-gray-500">
                        {historyNodeRelativeTime(node.createdAt)}
                      </span>
                    ) : null}
                    <span
                      role="cell"
                      className={clsx(
                        "relative z-[3] min-w-0 w-full bg-inherit pl-1 shadow-[-8px_0_8px_-8px_rgba(16,24,40,0.12)]",
                        showMetadataColumns
                          ? "flex flex-wrap items-center justify-end gap-1"
                          : "grid grid-cols-4 place-items-center gap-0.5"
                      )}
                    >
                      {sorting ? (node.nodeKind === "chat" ? dragHandle : <LockKeyhole className="h-3.5 w-3.5 text-gray-400" aria-label="Board nodes keep their default order" />) : null}
                      <>
                      {node.commit ? (
                        <button
                          type="button"
                          aria-label="Preview"
                          title={node.commit.metadata?.ridoc_native ? "View original event snapshot" : "Preview"}
                          disabled={interactionLocked}
                          onClick={(event) => {
                            event.stopPropagation();
                            if (node.commit) void onPreviewCommit(node.commit);
                          }}
                          className={clsx(ACTION_BUTTON_CLASS, actionButtonSize)}
                        >
                          <Eye className="h-3.5 w-3.5" />
                        </button>
                      ) : null}
                      {node.commit ? (
                        <button
                          type="button"
                          aria-label="Reference to input box"
                          title="Reference to input box"
                          onClick={(event) => {
                            event.stopPropagation();
                            if (node.commit) onReferenceHistoryNode(historyNodeSelection(activeLesson, node.commit));
                          }}
                          className={clsx(ACTION_BUTTON_CLASS, actionButtonSize)}
                        >
                          <Quote className="h-3.5 w-3.5" />
                        </button>
                      ) : null}
                      {canUseCommunity && node.commit ? (
                        <a
                          href={historyNodeCommunityShareHref(activeLesson, node.commit)}
                          target="_blank"
                          rel="noreferrer"
                          aria-label="Share to community"
                          title="Share to community"
                          onClick={(event) => event.stopPropagation()}
                          className={clsx(
                            "inline-flex shrink-0 items-center justify-center rounded border border-sky-200 bg-sky-50 text-sky-700 hover:border-sky-300 hover:bg-sky-100",
                            actionButtonSize
                          )}
                        >
                          <Share2 className="h-3.5 w-3.5" />
                        </a>
                      ) : null}
                      {!readOnly && sorting && node.pendingDeletion && node.typeCode === "M" ? <button type="button" aria-label="Restore this node" title="Restore this node" disabled={interactionLocked || Boolean(historyOrder?.disabledReason)} onClick={(event) => { event.stopPropagation(); void historyOrder?.restoreMessage(node.id); }} className={clsx(ACTION_BUTTON_CLASS, actionButtonSize, "border-blue-200 text-blue-600")}><Undo2 className="h-3.5 w-3.5" /></button> : null}
                      {!readOnly && onDeleteHistoryNode && node.typeCode === "M" && !node.pendingDeletion ? (
                        <button type="button" aria-label="Delete this conversation turn"
                          title={deletionDisabledReason || "Delete this conversation turn"}
                          disabled={Boolean(deletionDisabledReason)}
                          onClick={(event) => { event.stopPropagation(); void onDeleteHistoryNode(node.id); }}
                          className={clsx(ACTION_BUTTON_CLASS, actionButtonSize, "border-rose-200 text-rose-700")}
                        ><Trash2 className="h-3.5 w-3.5" /></button>
                      ) : null}
                      {!readOnly && !onDeleteHistoryNode && canToggleChat && node.commit ? (
                        <button
                          type="button"
                          aria-label={chatHidden ? "Restore chat in conversation" : "Hide conversation"}
                          title={chatHidden ? "Restore chat in conversation" : "Hide conversation"}
                          disabled={interactionLocked}
                          onClick={(event) => {
                            event.stopPropagation();
                            void onSetChatCommitVisibility(node.id, chatHidden ? "visible" : "hidden");
                          }}
                          className={clsx(
                            "inline-flex shrink-0 items-center justify-center rounded border",
                            actionButtonSize,
                            chatHidden
                              ? "border-amber-200 bg-amber-50 text-amber-700 hover:bg-amber-100"
                              : "border-rose-200 bg-rose-50 text-rose-700 hover:bg-rose-100",
                            "disabled:cursor-not-allowed disabled:opacity-35"
                          )}
                        >
                          {chatHidden ? <Eye className="h-3.5 w-3.5" /> : <Trash2 className="h-3.5 w-3.5" />}
                        </button>
                      ) : null}
                      </>
                      {!readOnly && !historyActionsDisabled && undoTargetId ? (
                        <button
                          type="button"
                          aria-label={nativeDocumentHistory ? "Undo board change" : "Undo document change"}
                          title={documentDisabledReason ?? (nativeDocumentHistory ? "Undo board change" : "Undo document change")}
                          disabled={Boolean(documentDisabledReason)}
                          onClick={(event) => {
                            event.stopPropagation();
                            void onStepDocumentHistory("undo", undoTargetId);
                          }}
                          className={clsx(
                            "inline-flex shrink-0 items-center justify-center rounded border border-amber-200 bg-amber-50 text-amber-700 hover:bg-amber-100",
                            actionButtonSize,
                            "disabled:cursor-not-allowed disabled:opacity-35"
                          )}
                        >
                          <Undo2 className="h-3.5 w-3.5" />
                        </button>
                      ) : null}
                      {!readOnly && !historyActionsDisabled && node.redoDocument ? (
                        <button
                          type="button"
                          aria-label={nativeDocumentHistory ? "Redo board change" : "Redo document change"}
                          title={documentDisabledReason ?? (nativeDocumentHistory ? "Redo board change" : "Redo document change")}
                          disabled={Boolean(documentDisabledReason)}
                          onClick={(event) => {
                            event.stopPropagation();
                            void onStepDocumentHistory("redo", node.id);
                          }}
                          className={clsx(
                            "inline-flex shrink-0 items-center justify-center rounded border border-blue-200 bg-blue-50 text-blue-700 hover:bg-blue-100",
                            actionButtonSize,
                            "disabled:cursor-not-allowed disabled:opacity-35"
                          )}
                        >
                          <Redo2 className="h-3.5 w-3.5" />
                        </button>
                      ) : null}
                      {<>
                      {!readOnly && node.commit ? (
                        <button
                          type="button"
                          aria-label="Branch"
                          title={
                            node.branchSourceNodeId
                              ? "Prepare a branch from this history node"
                              : "Atomic event branching is available from a complete AI response or its linked document change"
                          }
                          disabled={interactionLocked}
                          onClick={(event) => {
                            event.stopPropagation();
                            if (!readOnly && node.commit) {
                              void onCreateBranchFromCommit(
                                node.commit,
                                node.continuationNodeId,
                                node.id
                              );
                            }
                          }}
                          className={clsx(ACTION_BUTTON_CLASS, actionButtonSize)}
                        >
                          <GitBranch className="h-3.5 w-3.5" />
                        </button>
                      ) : null}
                      {!readOnly && checkoutBranch ? (
                        <button
                          type="button"
                          aria-label="Checkout"
                          title="Checkout"
                          disabled={interactionLocked}
                          onClick={(event) => {
                            event.stopPropagation();
                            void onSwitchBranch(checkoutBranch);
                          }}
                          className={clsx(ACTION_BUTTON_CLASS, actionButtonSize)}
                        >
                          <GitCommitHorizontal className="h-3.5 w-3.5" />
                        </button>
                      ) : null}
                      </>}
                    </span>
                  </>}
                  </HistoryOrderRow>
                );
              })}</>;
}
