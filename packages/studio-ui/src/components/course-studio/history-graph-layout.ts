import {
  historyNodeActor,
  historyNodeKind,
  historyNodeSummary,
  historyNodeTitle,
  historyNodeTypeCode,
  type HistoryNodeKind,
} from "./history-graph-utils";
import type { CommitRecord, Lesson, RidocHistoryPage, RidocNodeReference } from "../../types";
import type { DocumentHistoryState } from "../../types/document-history";
import { lessonSourceCommits } from "./history-learning-order";
import { canStepDocumentAction, documentActionBlockedReason } from "../../lib/document-action-permissions";

/** Row pitch of the history table; the graph overlay is aligned to the same grid. */
export const HISTORY_GRAPH_ROW_HEIGHT = 54;
/** Horizontal distance between two graph columns. */
export const HISTORY_GRAPH_COLUMN_GAP = 20;
/** Distance from the left edge of the graph gutter to the first column. */
export const HISTORY_GRAPH_ORIGIN_X = 14;
export const HISTORY_GRAPH_MERGE_COLOR = "#8b3dff";
export const HISTORY_GRAPH_FALLBACK_COLOR = "#98a2b3";
export const HISTORY_GRAPH_CHAT_COLOR = "#2563eb";
export const HISTORY_GRAPH_DOCUMENT_COLOR = "#16a34a";

const BRANCH_COLORS = [
  "#2f6bff",
  "#08a85a",
  "#db3b57",
  "#8b3dff",
  "#d97706",
  "#0891b2",
  "#e11d48",
  "#4f46e5",
];

export type HistoryGraphLane = {
  branchName: string;
  headId: string;
  index: number;
  color: string;
  isCurrent: boolean;
  archived: boolean;
};

export type HistoryGraphRef = {
  branchName: string;
  color: string;
  isCurrent: boolean;
};

export type HistoryGraphNode = {
  id: string;
  /** Effective node used when an event branch starts here (M\u2192C rounds continue from C). */
  continuationNodeId: string;
  /** Source M accepted by the atomic deferred-fork API; null means this row cannot safely fork. */
  branchSourceNodeId: string | null;
  /** Display row, 0 is the newest node at the top of the table. */
  row: number;
  /** Graph column the node dot sits on. */
  column: number;
  color: string;
  commit: CommitRecord | null;
  branchName: string;
  nodeKind: HistoryNodeKind;
  typeCode: string;
  title: string;
  summary: string;
  actor: string;
  createdAt: string;
  parentIds: string[];
  refs: HistoryGraphRef[];
  head: boolean;
  active: boolean;
  currentDocument: boolean;
  withdrawnDocument: boolean;
  redoDocument: boolean;
  pendingDeletion: boolean;
  comparisonStatus?: "added" | "removed" | "unchanged" | "target_only" | "reviewer_added";
  nodeReferences: RidocNodeReference[];
  /** Undefined is the legacy predecessor API; native targets identify the action itself. */
  undoDocumentTargetId?: string | null;
  documentHistoryBlockedReason?: string | null;
  searchText: string;
};

export type HistoryGraphEdge = {
  id: string;
  /** `trunk` stays in one column, `branch` rejoins its first parent, `merge` leaves for a second parent. */
  kind: "trunk" | "branch" | "merge";
  color: string;
  fromId: string;
  toId: string;
  fromRow: number;
  fromColumn: number;
  toRow: number;
  toColumn: number;
};

export function historyGraphNodeColor(node: Pick<HistoryGraphNode, "color" | "pendingDeletion" | "withdrawnDocument" | "comparisonStatus">) {
  return node.pendingDeletion || node.withdrawnDocument || node.comparisonStatus === "removed" ? HISTORY_GRAPH_FALLBACK_COLOR : node.color;
}

/** A branch that exists but owns no node yet: drawn as a short stub off its base node. */
export type HistoryGraphSprout = {
  id: string;
  branchName: string;
  color: string;
  column: number;
  baseColumn: number;
  baseRow: number;
};

export type HistoryGraphLayout = {
  lanes: HistoryGraphLane[];
  nodes: HistoryGraphNode[];
  edges: HistoryGraphEdge[];
  sprouts: HistoryGraphSprout[];
  columnCount: number;
};

export type HistoryGraphGeometry = {
  width: number;
  originX: number;
  columnGap: number;
  nodeRadius: number;
};

export type HistoryGraphFocus = {
  /** Nodes that directly satisfy the active query and branch controls. */
  matchIds: Set<string>;
  /** Direct matches plus the ancestors needed to understand their topology. */
  emphasizedIds: Set<string>;
};

type HistoryGraphInputNode = {
  id: string;
  continuationNodeId: string;
  branchSourceNodeId: string | null;
  parentIds: string[];
  branchName: string;
  createdAt: string;
  nodeKind: HistoryNodeKind;
  colorKind?: HistoryNodeKind;
  typeCode: string;
  title: string;
  summary: string;
  actor: string;
  commit: CommitRecord | null;
};

type HistoryGraphInputBranch = {
  name: string;
  headId: string;
  createdAt: string;
  archived: boolean;
};

type HistoryGraphInput = {
  /** Oldest node first, the order the store appends nodes in. */
  nodes: HistoryGraphInputNode[];
  branches: HistoryGraphInputBranch[];
  currentBranch: string;
  activeId: string | null;
  currentDocumentId: string;
  redoIds: string[];
  nativeDocumentHistory?: boolean;
  documentHistory?: DocumentHistoryState;
};

function sortBranches(branches: HistoryGraphInputBranch[]) {
  return [...branches].sort((left, right) => {
    if (left.name === "main") {
      return -1;
    }
    if (right.name === "main") {
      return 1;
    }
    const createdDelta = new Date(left.createdAt).getTime() - new Date(right.createdAt).getTime();
    if (Number.isFinite(createdDelta) && createdDelta !== 0) {
      return createdDelta;
    }
    return left.name.localeCompare(right.name, "en-US", { numeric: true });
  });
}

/**
 * Classic git-graph column packing over the whole node list, newest first.
 *
 * Every column holds the id of the node it is currently waiting for, so a lane
 * stays reserved from the first child that descends into it down to the shared
 * parent. Nothing is filtered out: each node lands on exactly one column and
 * every parent link keeps a free path to its target.
 */
function assignColumns(nodesNewestFirst: HistoryGraphInputNode[]) {
  const reserved: Array<string | null> = [];
  const columnById = new Map<string, number>();
  const claimFreeColumn = () => {
    const free = reserved.indexOf(null);
    if (free !== -1) {
      return free;
    }
    reserved.push(null);
    return reserved.length - 1;
  };

  nodesNewestFirst.forEach((node) => {
    const claimed = reserved.flatMap((waitingFor, index) => (waitingFor === node.id ? [index] : []));
    const column = claimed.length ? claimed[0] : claimFreeColumn();
    columnById.set(node.id, column);
    claimed.forEach((index) => {
      reserved[index] = null;
    });
    reserved[column] = null;

    node.parentIds.forEach((parentId, index) => {
      if (columnById.has(parentId)) {
        return;
      }
      if (index === 0) {
        // Every child keeps its own lane down to the shared parent, so the
        // parent lands on the leftmost lane waiting for it and the side lanes
        // are the ones that bend in.
        reserved[column] = parentId;
        return;
      }
      if (reserved.includes(parentId)) {
        return;
      }
      reserved[claimFreeColumn()] = parentId;
    });
  });

  return { columnById, columnCount: Math.max(reserved.length, 1) };
}

function layoutHistoryGraph(input: HistoryGraphInput): HistoryGraphLayout {
  const orderedBranches = sortBranches(input.branches);
  const lanes: HistoryGraphLane[] = orderedBranches.map((branch, index) => ({
    branchName: branch.name,
    headId: branch.headId,
    index,
    color: BRANCH_COLORS[index % BRANCH_COLORS.length],
    isCurrent: branch.name === input.currentBranch,
    archived: branch.archived,
  }));
  const laneByBranchName = new Map(lanes.map((lane) => [lane.branchName, lane]));
  const refsByNodeId = new Map<string, HistoryGraphRef[]>();
  orderedBranches.forEach((branch) => {
    if (!branch.headId) {
      return;
    }
    const lane = laneByBranchName.get(branch.name);
    const refs = refsByNodeId.get(branch.headId) ?? [];
    refs.push({
      branchName: branch.name,
      color: lane?.color ?? HISTORY_GRAPH_FALLBACK_COLOR,
      isCurrent: branch.name === input.currentBranch,
    });
    refsByNodeId.set(branch.headId, refs);
  });

  const nodesNewestFirst = [...input.nodes].reverse();
  const { columnById, columnCount } = assignColumns(nodesNewestFirst);
  const rowById = new Map(nodesNewestFirst.map((node, row) => [node.id, row]));
  const redoIds = new Set(input.redoIds);
  const colorOf = (node: HistoryGraphInputNode) =>
    laneByBranchName.get(node.branchName)?.color ?? HISTORY_GRAPH_FALLBACK_COLOR;

  const nodes: HistoryGraphNode[] = nodesNewestFirst.map((node, row) => {
    const refs = refsByNodeId.get(node.id) ?? [];
    const colorKind = node.colorKind ?? node.nodeKind;
    return {
      id: node.id,
      continuationNodeId: node.continuationNodeId,
      branchSourceNodeId: node.branchSourceNodeId,
      row,
      column: columnById.get(node.id) ?? 0,
      color: colorKind === "chat" ? HISTORY_GRAPH_CHAT_COLOR
        : colorKind === "document" || colorKind === "restore" || node.typeCode === "C0" ? HISTORY_GRAPH_DOCUMENT_COLOR
        : colorKind === "merge" ? HISTORY_GRAPH_MERGE_COLOR : HISTORY_GRAPH_FALLBACK_COLOR,
      commit: node.commit,
      branchName: node.branchName,
      nodeKind: node.nodeKind,
      typeCode: node.typeCode,
      title: node.title,
      summary: node.summary,
      actor: node.actor,
      createdAt: node.createdAt,
      parentIds: node.parentIds,
      refs,
      head: refs.length > 0,
      active: node.id === input.activeId,
      currentDocument: node.id === input.currentDocumentId,
      withdrawnDocument: redoIds.has(node.id),
      pendingDeletion: node.commit?.metadata?.pending_deletion === true,
      nodeReferences: Array.isArray(node.commit?.metadata?.node_refs) ? node.commit.metadata.node_refs as RidocNodeReference[] : [],
      redoDocument: input.nativeDocumentHistory
        ? canStepDocumentAction(input.documentHistory, node.id, "redo") : input.redoIds[0] === node.id,
      undoDocumentTargetId: input.nativeDocumentHistory
        ? canStepDocumentAction(input.documentHistory, node.id, "undo") ? node.id : null : undefined,
      documentHistoryBlockedReason: documentActionBlockedReason(input.documentHistory, node.id),
      searchText: [
        node.typeCode,
        node.title,
        node.summary,
        node.actor,
        node.branchName,
        ...refs.map((ref) => ref.branchName),
        node.id,
      ]
        .join(" ")
        .toLocaleLowerCase(),
    };
  });

  const nodeById = new Map(nodesNewestFirst.map((node) => [node.id, node]));
  const edges: HistoryGraphEdge[] = [];
  nodesNewestFirst.forEach((node, row) => {
    node.parentIds.forEach((parentId, index) => {
      const toRow = rowById.get(parentId);
      const parent = nodeById.get(parentId);
      if (toRow === undefined || !parent) {
        return;
      }
      const fromColumn = columnById.get(node.id) ?? 0;
      const toColumn = columnById.get(parentId) ?? 0;
      const kind = fromColumn === toColumn ? "trunk" : index === 0 ? "branch" : "merge";
      edges.push({
        id: `${node.id}:${parentId}`,
        kind,
        color: kind === "merge" ? colorOf(parent) : colorOf(node),
        fromId: node.id,
        toId: parentId,
        fromRow: row,
        fromColumn,
        toRow,
        toColumn,
      });
    });
  });

  const ownedBranchNames = new Set(nodesNewestFirst.map((node) => node.branchName));
  const sprouts: HistoryGraphSprout[] = [];
  let sproutColumn = columnCount;
  orderedBranches.forEach((branch) => {
    const baseRow = rowById.get(branch.headId);
    if (ownedBranchNames.has(branch.name) || baseRow === undefined) {
      return;
    }
    sprouts.push({
      id: branch.name,
      branchName: branch.name,
      color: laneByBranchName.get(branch.name)?.color ?? HISTORY_GRAPH_FALLBACK_COLOR,
      column: sproutColumn,
      baseColumn: columnById.get(branch.headId) ?? 0,
      baseRow,
    });
    sproutColumn += 1;
  });

  return { lanes, nodes, edges, sprouts, columnCount: Math.max(columnCount, sproutColumn) };
}

/** Full commit graph of a legacy lesson: every commit, every branch, every parent link. */
export function buildLessonHistoryGraph(lesson: Lesson, previewCommitId: string | null): HistoryGraphLayout {
  const currentBranch = lesson.history_graph.branches[lesson.history_graph.current_branch];
  return layoutHistoryGraph({
    nodes: lesson.history_graph.commits.map((commit) => ({
      id: commit.id,
      continuationNodeId: commit.id,
      branchSourceNodeId: commit.id,
      parentIds: commit.parent_ids,
      branchName: commit.branch_name,
      createdAt: commit.created_at,
      nodeKind: historyNodeKind(commit),
      colorKind: commit.metadata?.document_changed === true ? "document" : historyNodeKind(commit),
      typeCode: historyNodeTypeCode(commit),
      title: historyNodeTitle(commit),
      summary: historyNodeSummary(commit),
      actor: historyNodeActor(commit),
      commit,
    })),
    branches: Object.values(lesson.history_graph.branches).map((branch) => ({
      name: branch.name,
      headId: branch.head_commit_id,
      createdAt: branch.created_at,
      archived: false,
    })),
    currentBranch: lesson.history_graph.current_branch,
    activeId: previewCommitId,
    currentDocumentId: currentBranch?.document_cursor_commit_id || currentBranch?.head_commit_id || "",
    redoIds: currentBranch?.document_redo_commit_ids ?? [],
  });
}

/** Same graph for a RIDOC package, where the M/C node list is the authoritative history. */
export function buildRidocHistoryGraph(
  lesson: Lesson,
  history: RidocHistoryPage,
  previewCommitId: string | null
): HistoryGraphLayout {
  const commitById = new Map(lesson.history_graph.commits.map((commit) => [commit.id, commit]));
  const currentHeadId = history.branches.find((branch) => branch.name === history.current_branch)?.head_node_id;
  const learningIds = history.learning_node_ids?.length
    ? history.learning_node_ids
    : lessonSourceCommits(lesson, currentHeadId).map((commit) => commit.id);
  const documentIds = new Set(history.nodes.filter((node) => node.kind === "C").map((node) => node.id));
  const currentDocumentId = history.document_history?.current_document_node_id
    ?? learningIds.findLast((id) => documentIds.has(id)) ?? "";
  const sourceMessageByContinuationId = new Map(
    history.nodes
      .filter((node) => node.kind === "M")
      .map((node) => [node.continuation_node_id || node.id, node.id])
  );
  return layoutHistoryGraph({
    nodes: history.nodes.map((node) => {
      const sourceCommit = commitById.get(node.id);
      const commit = sourceCommit ? {
        ...sourceCommit,
        metadata: {
          ...sourceCommit.metadata,
          pending_deletion: node.pending_deletion ?? sourceCommit.metadata?.pending_deletion,
          node_refs: node.node_refs ?? sourceCommit.metadata?.node_refs,
          ...(node.branch_origin_deleted ? { branch_origin_deleted: true } : {}),
          ...(node.source_message_deleted ? { source_message_deleted: true } : {}),
          ...(node.merge_source_branch ? {
            merge_source_branch: node.merge_source_branch,
            merge_source_node_id: node.merge_source_node_id,
            merge_target_node_id: node.merge_target_node_id,
          } : {}),
        },
      } : null;
      const nodeKind: HistoryNodeKind = node.kind === "E" || node.merge_source_branch || node.parent_ids.length > 1
        ? "merge"
        : commit
          ? historyNodeKind(commit)
          : node.kind === "M"
            ? "chat"
            : "document";
      return {
        id: node.id,
        continuationNodeId: node.continuation_node_id || node.id,
        branchSourceNodeId:
          node.kind === "M"
            ? node.id
            : node.kind === "C"
              ? sourceMessageByContinuationId.get(node.id) ?? null
              : null,
        parentIds: node.parent_ids,
        branchName: node.branch_name,
        createdAt: node.created_at,
        nodeKind,
        colorKind: node.kind === "M" ? "chat" : node.kind === "C" ? "document" : "merge",
        typeCode: node.initial ? "C0" : nodeKind === "merge" ? "Merge" : node.kind,
        title: node.title,
        summary: node.summary,
        actor: commit
          ? historyNodeActor(commit)
          : node.user_message
            ? "You"
            : node.assistant_message
              ? "AI"
              : "System",
        commit,
      };
    }),
    branches: history.branches.map((branch) => ({
      name: branch.name,
      headId: branch.head_node_id,
      createdAt: "",
      archived: branch.archived,
    })),
    currentBranch: history.current_branch,
    activeId: previewCommitId,
    currentDocumentId,
    redoIds: history.document_history?.withdrawn_node_ids ?? [],
    nativeDocumentHistory: true,
    documentHistory: history.document_history,
  });
}

/** Reading order is a view over the same events, never a rewrite of their ancestry. */
export function buildLearningHistoryGraph(
  source: HistoryGraphLayout,
  learningNodeIds: string[]
): HistoryGraphLayout {
  const byId = new Map(source.nodes.map((node) => [node.id, node]));
  if (new Set(learningNodeIds).size !== learningNodeIds.length || learningNodeIds.some((id) => !byId.has(id))) {
    return { ...source, nodes: [], edges: [], sprouts: [], columnCount: 1 };
  }
  const ordered = learningNodeIds.map((id, index) => ({
    ...byId.get(id)!,
    row: learningNodeIds.length - index - 1,
    column: 0,
    parentIds: index > 0 ? [learningNodeIds[index - 1]] : [],
  }));
  // Learning order changes message presentation only; the authoritative cursor stays intact.
  const edges: HistoryGraphEdge[] = ordered.slice(1).map((node, index) => ({
    id: `learning:${node.id}:${ordered[index].id}`,
    kind: "trunk",
    color: source.lanes.find((lane) => lane.branchName === node.branchName)?.color ?? HISTORY_GRAPH_FALLBACK_COLOR,
    fromId: node.id,
    toId: ordered[index].id,
    fromRow: node.row,
    toRow: ordered[index].row,
    fromColumn: 0,
    toColumn: 0,
  }));
  return { ...source, nodes: ordered.reverse(), edges, sprouts: [], columnCount: 1 };
}

export function historyGraphAncestorIds(layout: HistoryGraphLayout, nodeIds: Iterable<string>) {
  const parentIdsByNode = new Map(layout.nodes.map((node) => [node.id, node.parentIds]));
  const ancestors = new Set<string>();
  const pending = [...nodeIds];
  while (pending.length) {
    const nodeId = pending.pop();
    if (!nodeId || ancestors.has(nodeId) || !parentIdsByNode.has(nodeId)) {
      continue;
    }
    ancestors.add(nodeId);
    pending.push(...(parentIdsByNode.get(nodeId) ?? []));
  }
  return ancestors;
}

/**
 * Search and branch focus never remove rows. The matching path is emphasized
 * and every other node remains in place, so filtering cannot invent a new
 * parent/child connection or hide the point where a branch diverged.
 */
export function historyGraphFocus(
  layout: HistoryGraphLayout,
  options: { query: string; branchName: string }
): HistoryGraphFocus {
  const normalizedQuery = options.query.trim().toLocaleLowerCase();
  const filteringByBranch = options.branchName !== "all";
  const branch = filteringByBranch
    ? layout.lanes.find((lane) => lane.branchName === options.branchName)
    : null;
  const branchTopology = branch
    ? historyGraphAncestorIds(layout, [branch.headId])
    : new Set(layout.nodes.map((node) => node.id));
  const matchIds = new Set(
    layout.nodes
      .filter((node) => branchTopology.has(node.id) && (!normalizedQuery || node.searchText.includes(normalizedQuery)))
      .map((node) => node.id)
  );
  if (!normalizedQuery && !filteringByBranch) {
    return { matchIds, emphasizedIds: new Set(matchIds) };
  }
  return {
    matchIds,
    emphasizedIds: normalizedQuery ? historyGraphAncestorIds(layout, matchIds) : branchTopology,
  };
}

export function historyGraphWidth(columnCount: number) {
  return HISTORY_GRAPH_ORIGIN_X * 2 + Math.max(columnCount - 1, 0) * HISTORY_GRAPH_COLUMN_GAP;
}

/** Fit every lane into a fixed gutter without introducing horizontal scrolling. */
export function fitHistoryGraphGeometry(columnCount: number, maxWidth: number): HistoryGraphGeometry {
  const count = Math.max(1, columnCount);
  const width = Math.max(28, maxWidth);
  const originX = Math.min(HISTORY_GRAPH_ORIGIN_X, width * 0.22);
  const available = Math.max(0, width - originX * 2);
  const columnGap = count > 1
    ? Math.min(HISTORY_GRAPH_COLUMN_GAP, available / (count - 1))
    : HISTORY_GRAPH_COLUMN_GAP;
  return {
    width,
    originX,
    columnGap,
    nodeRadius: Math.max(2, Math.min(5, columnGap * 0.4)),
  };
}

const DEFAULT_GEOMETRY: HistoryGraphGeometry = {
  width: historyGraphWidth(1),
  originX: HISTORY_GRAPH_ORIGIN_X,
  columnGap: HISTORY_GRAPH_COLUMN_GAP,
  nodeRadius: 5,
};

export function historyGraphColumnX(column: number, geometry: HistoryGraphGeometry = DEFAULT_GEOMETRY) {
  return geometry.originX + column * geometry.columnGap;
}

export function historyGraphRowY(row: number) {
  return row * HISTORY_GRAPH_ROW_HEIGHT + HISTORY_GRAPH_ROW_HEIGHT / 2;
}

/** Edge geometry: branch links rejoin their parent lane late, merge links leave the child lane early. */
export function historyGraphEdgePath(
  edge: HistoryGraphEdge,
  rounded: boolean,
  geometry: HistoryGraphGeometry = DEFAULT_GEOMETRY
) {
  const fromX = historyGraphColumnX(edge.fromColumn, geometry);
  const fromY = historyGraphRowY(edge.fromRow);
  const toX = historyGraphColumnX(edge.toColumn, geometry);
  const toY = historyGraphRowY(edge.toRow);
  if (edge.fromColumn === edge.toColumn) {
    return `M ${fromX} ${fromY} V ${toY}`;
  }
  const pitch = HISTORY_GRAPH_ROW_HEIGHT;
  if (edge.kind === "merge") {
    const bendY = Math.min(fromY + pitch, toY);
    if (!rounded) {
      return `M ${fromX} ${fromY} V ${fromY + pitch / 2} H ${toX} V ${toY}`;
    }
    return `M ${fromX} ${fromY} C ${fromX} ${fromY + pitch * 0.55}, ${toX} ${bendY - pitch * 0.55}, ${toX} ${bendY} V ${toY}`;
  }
  const bendY = Math.max(toY - pitch, fromY);
  if (!rounded) {
    return `M ${fromX} ${fromY} V ${toY - pitch / 2} H ${toX} V ${toY}`;
  }
  return `M ${fromX} ${fromY} V ${bendY} C ${fromX} ${bendY + pitch * 0.45}, ${toX} ${toY - pitch * 0.55}, ${toX} ${toY}`;
}

/** Tip of a sprout, kept inside the canvas even when it hangs off the newest row. */
export function historyGraphSproutTip(
  sprout: HistoryGraphSprout,
  geometry: HistoryGraphGeometry = DEFAULT_GEOMETRY
) {
  return {
    x: historyGraphColumnX(sprout.column, geometry),
    y: Math.max(historyGraphRowY(sprout.baseRow) - HISTORY_GRAPH_ROW_HEIGHT * 0.62, 6),
  };
}

/** Stub for a branch with no node of its own, rising out of the node it was cut from. */
export function historyGraphSproutPath(
  sprout: HistoryGraphSprout,
  rounded: boolean,
  geometry: HistoryGraphGeometry = DEFAULT_GEOMETRY
) {
  const baseX = historyGraphColumnX(sprout.baseColumn, geometry);
  const baseY = historyGraphRowY(sprout.baseRow);
  const tip = historyGraphSproutTip(sprout, geometry);
  const pitch = Math.max(baseY - tip.y, 1);
  if (!rounded) {
    return `M ${tip.x} ${tip.y} V ${baseY - pitch * 0.5} H ${baseX} V ${baseY}`;
  }
  return `M ${tip.x} ${tip.y} C ${tip.x} ${tip.y + pitch * 0.5}, ${baseX} ${baseY - pitch * 0.55}, ${baseX} ${baseY}`;
}
