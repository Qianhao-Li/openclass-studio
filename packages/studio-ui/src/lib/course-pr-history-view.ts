import type { HistoryGraphLayout, HistoryGraphNode } from "../components/course-studio/history-graph-layout";
import type { BoardDocument, CommitRecord } from "../types";
import type { CoursePrComparison, CoursePrDiffNode } from "../types/course-pr";

export function coursePrNodeCommit(diff: CoursePrDiffNode): CommitRecord | null {
  const document = diff.document;
  if (!document || typeof document !== "object" || !("content_text" in document) || !("content_json" in document)) return null;
  return {
    id: diff.id, label: diff.title, message: diff.summary ?? "", branch_name: diff.branch_name ?? "main",
    created_at: diff.created_at ?? "", parent_ids: diff.parent_ids, operations: [], snapshot: document as BoardDocument,
    metadata: { history_node_kind: diff.kind === "M" ? "chat" : "document", kind: diff.node?.initial ? "initial_document" : diff.kind === "M" ? "chat_turn" : "document_change",
      user_message: diff.node?.user_message, assistant_message: diff.node?.assistant_message, comparison_snapshot: true },
  };
}

/** A complete read-only comparison projection. Missing rows come from validated PR snapshots, never synthetic history facts. */
export function applyCoursePrHistoryView(layout: HistoryGraphLayout, comparison?: CoursePrComparison): HistoryGraphLayout {
  if (!comparison) return layout;
  const byId = new Map(layout.nodes.map((node) => [node.id, { ...node }]));
  for (const diff of comparison.diff_nodes) {
    const old = byId.get(diff.id);
    const added = diff.status === "added" || diff.status === "reviewer_added";
    const commit = old?.commit ?? coursePrNodeCommit(diff);
    const node: HistoryGraphNode = {
      id: diff.id, continuationNodeId: diff.id, branchSourceNodeId: null, row: 0, column: 0,
      color: diff.kind === "M" ? "#2563eb" : "#16a34a", commit,
      branchName: diff.branch_name ?? "main", nodeKind: diff.kind === "M" ? "chat" : "document",
      typeCode: diff.node?.initial === true ? "C0" : diff.kind, title: diff.title, summary: diff.summary ?? "",
      actor: diff.status === "reviewer_added" ? "Reviewer" : added ? "Contributor" : "Author",
      createdAt: diff.created_at ?? "", parentIds: diff.parent_ids, refs: [], head: false, active: false,
      currentDocument: false, withdrawnDocument: false, redoDocument: false, pendingDeletion: false,
      nodeReferences: [], searchText: `${diff.title} ${diff.summary ?? ""} ${diff.id}`.toLowerCase(),
      ...old,
      comparisonStatus: diff.status,
    };
    node.parentIds = diff.parent_ids;
    node.branchName = diff.branch_name ?? old?.branchName ?? "main";
    node.pendingDeletion = false;
    node.withdrawnDocument = false;
    if (added) node.color = "#d6a016";
    byId.set(diff.id, node);
  }
  // Preserve the target learning sequence first, while actual parent dependencies still come first.
  const chronological: HistoryGraphNode[] = [];
  const visited = new Set<string>();
  function visit(id: string) {
    if (visited.has(id)) return;
    visited.add(id);
    const node = byId.get(id);
    if (!node) return;
    node.parentIds.forEach(visit);
    chronological.push(node);
  }
  comparison.target_node_ids?.forEach(visit);
  comparison.source_node_ids?.forEach(visit);
  [...byId.values()].sort((a, b) => a.createdAt.localeCompare(b.createdAt)).forEach((node) => visit(node.id));
  const branchNames = [...new Set(chronological.map((node) => node.branchName))].sort((a, b) => a === "main" ? -1 : b === "main" ? 1 : a.localeCompare(b));
  const lanes = branchNames.map((branchName, index) => {
    const old = layout.lanes.find((lane) => lane.branchName === branchName);
    const members = chronological.filter((node) => node.branchName === branchName);
    const headId = old && byId.has(old.headId) ? old.headId : members.at(-1)?.id ?? "";
    return { branchName, index, headId, color: members.some((node) => node.comparisonStatus === "added" || node.comparisonStatus === "reviewer_added") ? "#d6a016" : "#2563eb", isCurrent: old?.isCurrent ?? false, archived: false };
  });
  const nodes = chronological.reverse().map((node, row) => {
    const lane = lanes.find((item) => item.branchName === node.branchName)!;
    const refs = lanes.filter((item) => item.headId === node.id).map((item) => ({ branchName: item.branchName, color: item.color, isCurrent: item.isCurrent }));
    return { ...node, row, column: lane.index, refs, head: refs.length > 0 };
  });
  const positioned = new Map(nodes.map((node) => [node.id, node]));
  const edges = nodes.flatMap((node) => node.parentIds.flatMap((id, parentIndex) => {
    const parent = positioned.get(id);
    return parent ? [{ id: `pr:${node.id}:${id}`, kind: parentIndex > 0 ? "merge" as const : node.column === parent.column ? "trunk" as const : "branch" as const,
      fromId: node.id, toId: id, fromRow: node.row, fromColumn: node.column, toRow: parent.row, toColumn: parent.column,
      color: node.comparisonStatus === "removed" ? "#98a2b3" : node.color }] : [];
  }));
  return { nodes, edges, lanes, sprouts: [], columnCount: Math.max(1, lanes.length) };
}
