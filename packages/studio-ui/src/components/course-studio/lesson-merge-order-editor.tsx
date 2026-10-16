"use client";

import {
  closestCenter,
  DndContext,
  DragOverlay,
  PointerSensor,
  pointerWithin,
  useDroppable,
  useSensor,
  useSensors,
  type CollisionDetection,
  type DragEndEvent,
  type DragMoveEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import { SortableContext, type SortingStrategy } from "@dnd-kit/sortable";
import { getEventCoordinates } from "@dnd-kit/utilities";
import { ArrowDown, ArrowUp, GripVertical, RotateCcw } from "lucide-react";
import { useEffect, useRef, useState, type MouseEvent } from "react";

import { MergeResultEventRow, MergeSourceEventRow } from "./merge-event-row";
import { MergeKeyboardSensor } from "./merge-keyboard-sensor";
import { useMergeNodeSelection } from "../../hooks/course-studio/use-merge-node-selection";
import { moveMergeMessages, sameEventOrder, stepMergeMessages } from "../../lib/merge-event-order";
import type { LessonMergeEventPlan } from "../../types";

type LessonMergeOrderEditorProps = {
  plan: LessonMergeEventPlan;
  disabled?: boolean;
  onOrderChange: (nodeIds: string[]) => void;
};

type DragDraft = { original: string[]; order: string[]; ids: string[]; beforeId: string | null };
const BUTTON = "inline-flex items-center justify-center gap-1 rounded-lg border border-gray-200 bg-white px-2.5 py-2 text-[11px] font-medium text-gray-700 hover:border-violet-300 disabled:cursor-not-allowed disabled:opacity-40";
// The group order is already previewed in the DOM. useSortable animates those
// index changes; a single-item displacement strategy would move every row twice.
const plannedPositions: SortingStrategy = () => null;

const resultCollision: CollisionDetection = (args) => {
  const droppableContainers = args.droppableContainers.filter((container) => container.data.current?.scope === "result");
  const filtered = { ...args, droppableContainers };
  return args.pointerCoordinates ? pointerWithin(filtered) : closestCenter(filtered);
};

function EndInsertionTarget({ disabled }: { disabled: boolean }) {
  const { setNodeRef, isOver } = useDroppable({ id: "result:end", data: { scope: "result", nodeId: null }, disabled });
  return <li ref={setNodeRef} className={`h-10 rounded-lg border border-dashed text-center text-[10px] leading-10 ${isOver ? "border-violet-500 bg-violet-50 text-violet-700" : "border-gray-200 text-gray-400"}`}>End of learning sequence</li>;
}

export function LessonMergeOrderEditor({ plan, disabled = false, onOrderChange }: LessonMergeOrderEditorProps) {
  const [editing, setEditing] = useState(() => !sameEventOrder(plan.ordered_node_ids, plan.default_node_ids));
  const [drag, setDrag] = useState<DragDraft | null>(null);
  const [insertBefore, setInsertBefore] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const dragRef = useRef<DragDraft | null>(null);
  const resultRef = useRef<HTMLOListElement | null>(null);
  const pointerRef = useRef<{ x: number; y: number } | null>(null);
  const { selectedIds, select, clear } = useMergeNodeSelection();
  const nodes = new Map(plan.nodes.map((node) => [node.id, node]));
  const editable = editing && !disabled;
  useEffect(() => {
    if (!editable) return;
    const trackPointer = (event: PointerEvent) => { pointerRef.current = { x: event.clientX, y: event.clientY }; };
    document.addEventListener("pointermove", trackPointer, true);
    return () => document.removeEventListener("pointermove", trackPointer, true);
  }, [editable]);
  const order = drag?.order ?? plan.ordered_node_ids;
  const selected = new Set(selectedIds);
  const selectedMessages = order.filter((id) => selected.has(id) && nodes.get(id)?.kind === "M");
  const insertionAnchor = insertBefore && order.includes(insertBefore) && !selected.has(insertBefore) && !nodes.get(insertBefore)?.initial ? insertBefore : "";
  const sources = [
    { scope: "target", label: "Current branch", ids: plan.target_node_ids },
    { scope: "source", label: "Source branch", ids: plan.source_node_ids },
  ];
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(MergeKeyboardSensor, { onStep: stepKeyboardDrag }),
  );
  const collisionDetection: CollisionDetection = (args) => {
    const point = args.pointerCoordinates;
    const bounds = resultRef.current?.getBoundingClientRect();
    if (point && bounds && (point.x < bounds.left || point.x > bounds.right || point.y < bounds.top || point.y > bounds.bottom)) return [];
    return resultCollision(args);
  };

  function choose(id: string, scope: string, ids: string[], event: MouseEvent, checkbox: boolean) {
    if (!editable) return;
    select(id, scope, ids.filter((candidate) => nodes.get(candidate)?.kind === "M"), {
      range: event.shiftKey,
      toggle: checkbox || event.metaKey || event.ctrlKey,
    });
  }

  function applyOrder(next: string[]) {
    if (disabled || sameEventOrder(next, plan.ordered_node_ids)) return;
    onOrderChange(next);
    setAnnouncement(`Learning sequence updated. ${selectedMessages.length} conversations selected.`);
  }

  function startDrag(event: DragStartEvent) {
    const id = event.active.data.current?.nodeId as string | undefined;
    if (!editable || !id || nodes.get(id)?.kind !== "M") return;
    const ids = selected.has(id) ? selectedMessages : [id];
    if (!selected.has(id)) select(id, "result", plan.ordered_node_ids.filter((candidate) => nodes.get(candidate)?.kind === "M"));
    const next = { original: [...plan.ordered_node_ids], order: [...plan.ordered_node_ids], ids, beforeId: null };
    pointerRef.current = getEventCoordinates(event.activatorEvent);
    dragRef.current = next;
    setDrag(next);
    setAnnouncement(`Picked up ${ids.length} conversations. Use arrow keys to move, Space to drop, or Escape to cancel.`);
  }

  function stepKeyboardDrag(direction: "up" | "down") {
    const current = dragRef.current;
    if (!current) return;
    const nextOrder = stepMergeMessages(current.order, current.ids, direction, plan.nodes);
    const lastMovedIndex = Math.max(...current.ids.map((id) => nextOrder.indexOf(id)));
    const next = { ...current, order: nextOrder, beforeId: nextOrder[lastMovedIndex + 1] ?? null };
    dragRef.current = next;
    setDrag(next);
    setAnnouncement(`Moved ${current.ids.length} conversations ${direction}. Press Space to drop or Escape to cancel.`);
  }

  function moveDrag(event: DragMoveEvent) {
    if (event.activatorEvent.type === "keydown") return;
    const current = dragRef.current;
    const over = event.over;
    if (!current || !over || over.data.current?.scope !== "result") return;
    const overId = over.data.current?.nodeId as string | null;
    if (overId && current.ids.includes(overId)) return;
    let beforeId = overId;
    if (overId) {
      const translated = event.active.rect.current.translated;
      const midpoint = pointerRef.current?.y ?? (translated ? translated.top + translated.height / 2 : over.rect.top);
      if (midpoint > over.rect.top + over.rect.height / 2) {
        const remaining = current.order.filter((id) => !current.ids.includes(id));
        beforeId = remaining[remaining.indexOf(overId) + 1] ?? null;
      }
    }
    const nextOrder = moveMergeMessages(current.original, current.ids, beforeId, plan.nodes);
    if (current.beforeId === beforeId && sameEventOrder(nextOrder, current.order)) return;
    const next = { ...current, order: nextOrder, beforeId };
    dragRef.current = next;
    setDrag(next);
  }

  function endDrag(event: DragEndEvent) {
    const current = dragRef.current;
    if (current && (event.activatorEvent.type === "keydown" || event.over?.data.current?.scope === "result")) applyOrder(current.order);
    else setAnnouncement("Move cancelled. Learning sequence unchanged.");
    dragRef.current = null;
    pointerRef.current = null;
    setDrag(null);
  }

  function cancelDrag() {
    dragRef.current = null;
    pointerRef.current = null;
    setDrag(null);
    setAnnouncement("Move cancelled. Learning sequence unchanged.");
  }

  return (
    <section aria-label="Merge event order" className="space-y-3" data-testid="merge-order-editor">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">Learning sequence</h3>
          <p className="mt-1 text-[11px] text-gray-500">{order.length} events · earliest first</p>
        </div>
        <button type="button" disabled={disabled} onClick={() => { setEditing((value) => !value); clear(); }} className={BUTTON}>{editing ? "Preview order" : "Adjust conversation order"}</button>
      </div>
      <p className="text-[11px] leading-5 text-gray-500">
        {editing ? "Select conversations across either branch or the result, then drag them together to an insertion point. Document changes keep their default order." : "Source events are inserted after their branch point. You can adjust the conversation order before committing."}
      </p>
      <DndContext sensors={sensors} collisionDetection={collisionDetection} onDragStart={startDrag} onDragMove={moveDrag} onDragOver={moveDrag} onDragEnd={endDrag} onDragCancel={cancelDrag} autoScroll={{ threshold: { x: 0, y: 0.15 }, acceleration: 8, canScroll: (element) => {
        const point = pointerRef.current;
        const bounds = resultRef.current?.getBoundingClientRect();
        const inResult = point && bounds && point.x >= bounds.left && point.x <= bounds.right && point.y >= bounds.top && point.y <= bounds.bottom;
        return !inResult || element === resultRef.current;
      } }}>
        {editing ? (
          <>
            <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit,minmax(min(100%,220px),1fr))" }}>
              {sources.map(({ scope, label, ids }) => (
                <section key={scope} aria-label={label} className="min-w-0 rounded-xl border border-gray-200 p-2.5">
                  <h4 className="mb-2 text-[11px] font-semibold text-gray-700">{label}</h4>
                  <ol className="max-h-48 space-y-1.5 overflow-y-auto overscroll-contain pr-1" data-testid={`merge-${scope}-list`}>
                    {ids.map((id) => {
                      const node = nodes.get(id);
                      return node ? <MergeSourceEventRow key={id} node={node} scope={scope} selected={selected.has(id)} editable={editable} groupDragging={Boolean(drag?.ids.includes(id))} onSelect={(event, checkbox) => choose(id, scope, ids, event, checkbox)} /> : null;
                    })}
                  </ol>
                </section>
              ))}
            </div>
            <div className="space-y-2 rounded-xl border border-violet-100 bg-white p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="mr-auto text-[11px] font-semibold text-violet-700">{selectedMessages.length} conversations selected</span>
                <button type="button" onClick={clear} disabled={!selectedMessages.length || disabled} className={BUTTON}>Clear selection</button>
                <button type="button" onClick={() => { applyOrder([...plan.default_node_ids]); clear(); }} disabled={disabled || sameEventOrder(plan.ordered_node_ids, plan.default_node_ids)} className={BUTTON}><RotateCcw className="h-3.5 w-3.5" />Default order</button>
              </div>
              <div className="flex flex-wrap gap-2">
                <button type="button" disabled={!editable || !selectedMessages.length} onClick={() => applyOrder(stepMergeMessages(order, selectedMessages, "up", plan.nodes))} className={BUTTON}><ArrowUp className="h-3.5 w-3.5" />Move up</button>
                <button type="button" disabled={!editable || !selectedMessages.length} onClick={() => applyOrder(stepMergeMessages(order, selectedMessages, "down", plan.nodes))} className={BUTTON}><ArrowDown className="h-3.5 w-3.5" />Move down</button>
                <select aria-label="Insertion point" value={insertionAnchor} onChange={(event) => setInsertBefore(event.target.value)} disabled={!editable || !selectedMessages.length} className="min-w-0 flex-1 basis-40 rounded-lg border border-gray-200 bg-white px-2 text-[11px] disabled:opacity-40">
                  <option value="">At the end</option>
                  {order.filter((id) => !selected.has(id) && !nodes.get(id)?.initial).map((id) => <option key={id} value={id}>Before {nodes.get(id)?.title ?? id}</option>)}
                </select>
                <button type="button" disabled={!editable || !selectedMessages.length} onClick={() => applyOrder(moveMergeMessages(order, selectedMessages, insertionAnchor || null, plan.nodes))} className={BUTTON}>Insert selected</button>
              </div>
              <p className="text-[10px] text-gray-400">Shift selects a range. ⌘/Ctrl adds to the selection. Focus a result drag handle and press Space to move with the keyboard.</p>
            </div>
          </>
        ) : null}
        <div className="rounded-xl border border-violet-200 bg-violet-50/30 p-2.5">
          <h4 className="mb-2 text-[11px] font-semibold text-violet-800">Merged result</h4>
          <SortableContext items={order.map((id) => `result:${id}`)} strategy={plannedPositions}>
            <ol ref={resultRef} aria-label="Merged learning sequence" className="max-h-[32rem] space-y-1.5 overflow-y-auto overscroll-contain pr-1" data-testid="merge-result-list">
              {order.map((id) => {
                const node = nodes.get(id);
                return node ? <MergeResultEventRow key={id} node={node} scope="result" selected={selected.has(id)} editable={editable} insertionBefore={Boolean(drag && drag.beforeId === id)} groupDragging={Boolean(drag?.ids.includes(id))} onSelect={(event, checkbox) => choose(id, "result", order, event, checkbox)} /> : null;
              })}
              {editing ? <EndInsertionTarget disabled={!editable} /> : null}
            </ol>
          </SortableContext>
        </div>
        <DragOverlay dropAnimation={{ duration: 180, easing: "ease-out" }}>
          {drag ? <div data-testid="merge-drag-overlay" className="flex max-w-72 items-center gap-2 rounded-xl border border-violet-300 bg-white px-4 py-3 shadow-xl"><GripVertical className="h-4 w-4 text-violet-600" /><span className="truncate text-xs font-semibold">{nodes.get(drag.ids[0])?.title}</span><span className="rounded-full bg-violet-600 px-2 py-1 text-[11px] font-bold text-white">{drag.ids.length}</span></div> : null}
        </DragOverlay>
      </DndContext>
      <p aria-live="polite" className="sr-only">{announcement}</p>
    </section>
  );
}
