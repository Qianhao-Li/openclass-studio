"use client";

import { useState } from "react";
import { ArrowDown, ArrowUp, GripVertical } from "lucide-react";
import type { CoursePrComparison } from "../../types/course-pr";

type Unit = { id: string; nodeIds: string[]; title: string; initial: boolean; added: boolean; reviewer: boolean };

function actionUnits(comparison: CoursePrComparison): Unit[] {
  const nodes = new Map(comparison.diff_nodes.map((node) => [node.id, node]));
  const groups = new Map(comparison.action_groups.flatMap((group) => group.kind === "add" ? group.node_ids.map((id) => [id, group] as const) : []));
  const units: Unit[] = [];
  const byAction = new Map<string, Unit>();
  for (const id of comparison.ordered_node_ids ?? []) {
    const node = nodes.get(id), group = groups.get(id);
    const transaction = typeof node?.node?.transaction_id === "string" ? node.node.transaction_id : id;
    const key = group?.id ?? transaction;
    const current = byAction.get(key);
    if (current) { current.nodeIds.push(id); continue; }
    const unit = { id: key, nodeIds: [id], title: group?.title ?? node?.title ?? id.slice(0, 10), initial: node?.node?.initial === true || units.length === 0,
      added: node?.status === "added" || node?.status === "reviewer_added", reviewer: node?.status === "reviewer_added" };
    byAction.set(key, unit); units.push(unit);
  }
  return units;
}

/** Move entire transactions, including existing main-chain M→C actions, as one unit. */
export function CoursePrOrderEditor({ comparison, disabled, onChange }: { comparison: CoursePrComparison; disabled: boolean; onChange: (order: string[]) => void | Promise<void> }) {
  const [dragged, setDragged] = useState<string | null>(null);
  const units = actionUnits(comparison);
  function move(id: string, to: number) {
    const from = units.findIndex((unit) => unit.id === id);
    if (disabled || from < 0 || units[from].initial || to <= 0 || to >= units.length || from === to) return;
    const next = [...units]; const [unit] = next.splice(from, 1); next.splice(to, 0, unit);
    void onChange(next.flatMap((item) => item.nodeIds));
  }
  return <section className="mt-4 border-t pt-3" aria-label="Merged learning order"><h3 className="text-xs font-semibold">Merged learning order</h3><p className="mt-1 text-xs text-stone-500">Learn from top to bottom. Drag action groups or use the up and down buttons. Existing main-branch actions stay together.</p>
    <ol className="mt-2 space-y-1.5">{units.map((unit, index) => <li key={unit.id} data-pr-order-action={unit.id}
      draggable={!disabled && !unit.initial} onDragStart={() => setDragged(unit.id)} onDragEnd={() => setDragged(null)}
      onDragOver={(event) => { if (dragged && !unit.initial) event.preventDefault(); }} onDrop={(event) => { event.preventDefault(); if (dragged) move(dragged, index); setDragged(null); }}
      className={`flex items-center gap-2 rounded-lg border px-3 py-2 ${unit.added ? "border-amber-200 bg-amber-50" : "border-stone-200 bg-stone-50"}`}>
      <GripVertical className="h-3.5 w-3.5 shrink-0 text-stone-400" /><span className="min-w-0 flex-1 text-xs"><span className="font-semibold">{unit.title}</span><span className="ml-2 text-stone-500">{unit.initial ? "Initial document" : unit.reviewer ? "Added by reviewer" : unit.added ? "Added in PR" : "Main-branch action"} · {unit.nodeIds.length} nodes</span></span>
      <button type="button" disabled={disabled || unit.initial || index <= 1} title={index <= 1 ? "The initial document stays first" : undefined} aria-label={`Move merged action ${unit.title} up`} onClick={() => move(unit.id, index - 1)} className="rounded border bg-white p-1 disabled:opacity-30"><ArrowUp className="h-3.5 w-3.5" /></button>
      <button type="button" disabled={disabled || unit.initial || index === units.length - 1} aria-label={`Move merged action ${unit.title} down`} onClick={() => move(unit.id, index + 1)} className="rounded border bg-white p-1 disabled:opacity-30"><ArrowDown className="h-3.5 w-3.5" /></button>
    </li>)}</ol>
  </section>;
}
