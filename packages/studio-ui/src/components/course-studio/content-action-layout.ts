import type { ContentAction, ContentActionSpan } from "../../types/content-actions";

export const COVERAGE_BASE_SCALE = 0.75;

export interface CoverageSpan extends ContentActionSpan {
  top: number;
  height: number;
}

/** Dense fragments share a hit target; their painted ranges remain separate. */
export function coverageHitGroups(spans: CoverageSpan[]) {
  const groups: { top: number; height: number; parts: CoverageSpan[] }[] = [];
  for (const span of spans) {
    const last = groups.at(-1);
    if (last && span.height < 12 && last.parts.every((part) => part.height < 12) && span.top - last.top - last.height < 12) {
      last.parts.push(span); last.height = Math.max(last.height, span.top + span.height - last.top);
    } else groups.push({ top: span.top, height: span.height, parts: [span] });
  }
  return groups;
}

export interface CoverageNode {
  action: ContentAction;
  x: number;
  width: number;
  spans: CoverageSpan[];
  anchorY: number;
}

export interface CoverageEdge {
  id: string;
  fromId: string;
  toId: string;
  path: string;
}

export interface CoverageLayout {
  width: number;
  height: number;
  documentTop: number;
  documentHeight: number;
  nodes: CoverageNode[];
  edges: CoverageEdge[];
  ranks: { rank: number; x: number; width: number }[];
}

/** C0 remains visible; reducing the viewport never changes dependency facts. */
export function visibleContentActions(actions: ContentAction[], showAll: boolean, limit = 20): ContentAction[] {
  const ordered = [...actions].sort((a, b) => a.sequence - b.sequence || a.id.localeCompare(b.id));
  if (showAll) return ordered;
  const recent = limit > 0 ? ordered.filter((action) => !action.initial).slice(-limit) : [];
  const recentIds = new Set(recent.map((action) => action.id));
  return ordered.filter((action) => action.initial || recentIds.has(action.id));
}

/** Bulk removal includes only the exposed eligible nodes in this snapshot. */
export function exposedUndoIds(actions: ContentAction[]): string[] {
  return actions.filter((action) => !action.initial && action.status === "active" && action.can_undo).map((action) => action.id);
}

function normalizedSpans(action: ContentAction): ContentActionSpan[] {
  if (action.display_spans !== undefined) return action.display_spans;
  if (action.initial || action.scope === "document") return [{ start: 0, end: 1 }];
  return action.spans
    .filter((span) => Number.isFinite(span.start) && Number.isFinite(span.end) && span.end >= span.start)
    .map((span) => ({ start: Math.max(0, Math.min(1, span.start)), end: Math.max(0, Math.min(1, span.end)) }))
    .sort((a, b) => a.start - b.start || a.end - b.end);
}

/** Actual document geometry determines placement; links retain server dependencies. */
export function layoutContentActions(actions: ContentAction[], zoom = 1): CoverageLayout {
  const scale = COVERAGE_BASE_SCALE * Math.max(0.75, Math.min(2, Number.isFinite(zoom) ? zoom : 1));
  const documentTop = 48;
  const documentHeight = 352 * scale;
  const width = 48 * scale;
  const laneGap = 20 * scale;
  const ranks: CoverageLayout["ranks"] = [];
  const nodes: CoverageNode[] = [];
  const lanes: CoverageSpan[][] = [];
  const ordered = [...actions].sort((a, b) => a.sequence - b.sequence);
  for (const action of ordered) {
    const measured = normalizedSpans(action).sort((a, b) => a.start - b.start || a.end - b.end);
    const merged: ContentActionSpan[] = [];
    for (const span of measured) {
      const last = merged.at(-1);
      if (last && span.start <= last.end && span.start !== span.end) last.end = Math.max(last.end, span.end);
      else merged.push({ ...span });
    }
    const spans = merged.map((span) => ({ ...span, top: documentTop + span.start * documentHeight,
      height: Math.max(1, (span.end - span.start) * documentHeight) }));
    if (action.initial && action.display_spans !== undefined) {
      nodes.push({ action, x: 22, width, spans, anchorY: documentTop });
      continue;
    }
    // Position overlaps allocate lanes. A causal dependency alone does not
    // move an inserted, non-overlapping fragment into a separate column.
    let lane = lanes.findIndex((items) => spans.every((span) => items.every((other) => span.end <= other.start || span.start >= other.end)));
    if (lane < 0 || !spans.length) { lane = lanes.length; lanes.push([]); }
    lanes[lane].push(...spans);
    nodes.push({ action, x: 68 + lane * (width + laneGap), width, spans, anchorY: spans[0]?.top ?? documentTop });
  }
  for (let rank = 0; rank < lanes.length; rank++) ranks.push({ rank, x: 68 + rank * (width + laneGap), width });
  const x = 68 + lanes.length * (width + laneGap);
  const byId = new Map(nodes.map((node) => [node.action.id, node]));
  const edges: CoverageEdge[] = [];
  for (const node of nodes) {
    for (const dependencyId of node.action.dependencies) {
      const dependency = byId.get(dependencyId);
      if (!dependency) continue;
      const targetY = node.spans[0]?.top ?? node.anchorY;
      const sourceY = dependency.spans.length ? dependency.spans.map((span) => Math.max(span.top, Math.min(targetY, span.top + span.height))).sort((a, b) => Math.abs(a - targetY) - Math.abs(b - targetY))[0] : dependency.anchorY;
      const sameLane = dependency.x === node.x;
      const startX = sameLane ? dependency.x : dependency.x + dependency.width;
      const endX = node.x;
      const middleX = sameLane ? startX - 18 : (startX + endX) / 2;
      edges.push({
        id: `${dependencyId}:${node.action.id}`,
        fromId: dependencyId,
        toId: node.action.id,
        path: `M ${startX} ${sourceY} C ${middleX} ${sourceY}, ${middleX} ${targetY}, ${endX} ${targetY}`,
      });
    }
  }
  return { width: Math.max(480, x + 24), height: documentTop + documentHeight + 30, documentTop, documentHeight, nodes, edges, ranks };
}
