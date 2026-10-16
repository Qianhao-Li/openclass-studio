import type { LessonMergeEventNode } from "../types/index";

export function sameEventOrder(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((id, index) => id === right[index]);
}

/** Move existing M references as one block. C references are never picked up. */
export function moveMergeMessages(
  order: readonly string[],
  selectedIds: readonly string[],
  beforeId: string | null,
  nodes: readonly Pick<LessonMergeEventNode, "id" | "kind" | "initial">[],
): string[] {
  const messages = new Set(nodes.filter((node) => node.kind === "M").map((node) => node.id));
  const selected = new Set(selectedIds.filter((id) => messages.has(id)));
  const moving = order.filter((id) => selected.has(id));
  if (!moving.length || (beforeId !== null && (!order.includes(beforeId) || selected.has(beforeId) || nodes.some((node) => node.id === beforeId && node.initial)))) {
    return [...order];
  }
  const remaining = order.filter((id) => !selected.has(id));
  const index = beforeId === null ? remaining.length : remaining.indexOf(beforeId);
  return [...remaining.slice(0, index), ...moving, ...remaining.slice(index)];
}

/** Keyboard/toolbar movement uses the same insertion operation as pointer drag. */
export function stepMergeMessages(
  order: readonly string[],
  selectedIds: readonly string[],
  direction: "up" | "down",
  nodes: readonly Pick<LessonMergeEventNode, "id" | "kind" | "initial">[],
): string[] {
  const selectable = new Set(nodes.filter((node) => node.kind === "M").map((node) => node.id));
  const selected = new Set(selectedIds.filter((id) => selectable.has(id)));
  const indices = order.flatMap((id, index) => selected.has(id) ? [index] : []);
  if (!indices.length) return [...order];
  const first = indices[0];
  const last = indices[indices.length - 1];
  if (direction === "up") {
    return first === 0 ? [...order] : moveMergeMessages(order, selectedIds, order[first - 1], nodes);
  }
  if (last === order.length - 1) return [...order];
  return moveMergeMessages(order, selectedIds, order[last + 2] ?? null, nodes);
}

export type MergeNodeSelection = { ids: string[]; anchorId: string | null; anchorScope: string | null };
export const EMPTY_MERGE_SELECTION: MergeNodeSelection = { ids: [], anchorId: null, anchorScope: null };

export function selectMergeMessage(
  current: MergeNodeSelection,
  nodeId: string,
  scope: string,
  scopeIds: readonly string[],
  options: { range?: boolean; toggle?: boolean },
): MergeNodeSelection {
  if (!scopeIds.includes(nodeId)) return current;
  if (options.range && current.anchorScope === scope && current.anchorId !== null) {
    const from = scopeIds.indexOf(current.anchorId);
    const to = scopeIds.indexOf(nodeId);
    if (from !== -1) {
      const range = scopeIds.slice(Math.min(from, to), Math.max(from, to) + 1);
      return { ...current, ids: [...new Set([...current.ids, ...range])] };
    }
  }
  return {
    ids: options.toggle
      ? current.ids.includes(nodeId) ? current.ids.filter((id) => id !== nodeId) : [...current.ids, nodeId]
      : [nodeId],
    anchorId: nodeId,
    anchorScope: scope,
  };
}
