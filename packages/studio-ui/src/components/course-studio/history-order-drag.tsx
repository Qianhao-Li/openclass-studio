"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ComponentPropsWithRef, type ReactNode } from "react";
import { closestCenter, DndContext, DragOverlay, PointerSensor, pointerWithin, useSensor, useSensors, type CollisionDetection, type DragEndEvent, type DragMoveEvent, type DragStartEvent } from "@dnd-kit/core";
import { SortableContext, useSortable } from "@dnd-kit/sortable";
import { getEventCoordinates } from "@dnd-kit/utilities";
import { GripVertical } from "lucide-react";
import clsx from "clsx";
import { MergeKeyboardSensor } from "./merge-keyboard-sensor";
import { useHistoryRowFlip } from "../../hooks/course-studio/use-history-row-flip";
import { moveMergeMessages, sameEventOrder, stepMergeMessages } from "../../lib/merge-event-order";
import type { HistoryEditController as HistoryOrderController } from "../../hooks/course-studio/use-history-edit-session";
import { HISTORY_GRAPH_CHAT_COLOR, historyGraphNodeColor, type HistoryGraphNode } from "./history-graph-layout";

type DragDraft = { original: string[]; ids: string[]; beforeId: string | null; activeId: string; width: number; height: number };
type DragContextValue = { active: boolean; register: (id: string, node: HTMLElement | null) => void; dragged: string[]; beforeId: string | null };
const DragContext = createContext<DragContextValue>({ active: false, register: () => undefined, dragged: [], beforeId: null });
const noDisplacement = () => null;

export function HistoryOrderDrag({ controller, nodes, children }: { controller?: HistoryOrderController; nodes: HistoryGraphNode[]; children: ReactNode }) {
  const [dragging, setDragging] = useState<DragDraft | null>(null);
  const draggedNode = nodes.find((node) => node.id === dragging?.activeId);
  const dragColor = draggedNode ? historyGraphNodeColor(draggedNode) : HISTORY_GRAPH_CHAT_COLOR;
  const dragRef = useRef<DragDraft | null>(null);
  const pointerRef = useRef<{ x: number; y: number } | null>(null);
  const active = Boolean(controller?.active && !["saving", "loading"].includes(controller.phase) && !controller.stale && !controller.disabledReason);
  const order = controller?.draft?.ids ?? [];
  const kinds = nodes.map((node) => ({ id: node.id, kind: node.nodeKind === "chat" ? "M" as const : "C" as const, initial: node.typeCode === "C0" }));
  const { register, capture } = useHistoryRowFlip(order.join("\u0000"), active, dragging?.ids ?? []);
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 6 } }), useSensor(MergeKeyboardSensor, { onStep: keyboardStep }));
  useEffect(() => {
    if (!active) return;
    const track = (event: PointerEvent) => { pointerRef.current = { x: event.clientX, y: event.clientY }; };
    document.addEventListener("pointermove", track, true);
    return () => document.removeEventListener("pointermove", track, true);
  }, [active]);
  const collisionDetection: CollisionDetection = (args) => {
    const point = args.pointerCoordinates;
    const bounds = args.droppableContainers[0]?.node.current?.closest("[data-history-order-scroll]")?.getBoundingClientRect();
    if (point && bounds && (point.x < bounds.left || point.x > bounds.right || point.y < bounds.top || point.y > bounds.bottom)) return [];
    return point ? pointerWithin(args) : closestCenter(args);
  };

  function keyboardStep(direction: "up" | "down") {
    const drag = dragRef.current;
    if (!drag || !controller) return;
    capture();
    controller.change(stepMergeMessages(controller.draft!.ids, drag.ids, direction === "up" ? "down" : "up", kinds));
  }
  function start(event: DragStartEvent) {
    const id = String(event.active.id);
    if (!active || !controller || !kinds.some((node) => node.id === id && node.kind === "M")) return;
    const ids = controller.selectedIds.includes(id) ? order.filter((nodeId) => controller.selectedIds.includes(nodeId)) : [id];
    if (!controller.selectedIds.includes(id)) controller.select(id, nodes.filter((node) => node.nodeKind === "chat").map((node) => node.id));
    const rect = event.active.rect.current.initial;
    pointerRef.current = getEventCoordinates(event.activatorEvent);
    const next = { original: [...order], ids, beforeId: null, activeId: id, width: rect?.width ?? 280, height: rect?.height ?? 56 };
    capture(); dragRef.current = next; setDragging(next);
  }
  function move(event: DragMoveEvent) {
    if (event.activatorEvent.type === "keydown") return;
    const drag = dragRef.current;
    if (!drag || !controller?.draft || !event.over) return;
    const target = String(event.over.id);
    if (drag.ids.includes(target)) return;
    const origin = getEventCoordinates(event.activatorEvent);
    const pointerY = pointerRef.current?.y ?? (origin ? origin.y + event.delta.y : event.active.rect.current.translated?.top ?? 0);
    const visualBefore = pointerY < event.over.rect.top + event.over.rect.height / 2;
    const remaining = controller.draft.ids.filter((id) => !drag.ids.includes(id));
    const beforeId = visualBefore ? remaining[remaining.indexOf(target) + 1] ?? null : target;
    const next = moveMergeMessages(drag.original, drag.ids, beforeId, kinds);
    if (sameEventOrder(next, controller.draft.ids)) return;
    capture(); controller.change(next);
    const updated = { ...drag, beforeId: target };
    dragRef.current = updated; setDragging(updated);
  }
  function end(event: DragEndEvent) {
    if (dragRef.current && event.activatorEvent.type !== "keydown" && !event.over) {
      capture(); controller?.change(dragRef.current.original);
    }
    dragRef.current = null; setDragging(null);
  }
  function cancel() {
    if (dragRef.current) { capture(); controller?.change(dragRef.current.original); }
    dragRef.current = null; setDragging(null);
  }
  return <DndContext sensors={sensors} collisionDetection={collisionDetection} onDragStart={start} onDragMove={move} onDragOver={move} onDragEnd={end} onDragCancel={cancel} autoScroll={{ threshold: { x: 0, y: 0.15 }, acceleration: 8, canScroll: (element) => element.getAttribute("data-history-order-scroll") === "true" }}>
    <SortableContext items={nodes.map((node) => node.id)} strategy={noDisplacement}>
      <DragContext value={{ active, register, dragged: dragging?.ids ?? [], beforeId: dragging?.beforeId ?? null }}>{children}</DragContext>
    </SortableContext>
    <DragOverlay dropAnimation={{ duration: 180, easing: "ease-out" }}>
      {dragging ? <div data-testid="history-order-drag-overlay" style={{ width: dragging.width, minHeight: dragging.height, maxWidth: "calc(100vw - 32px)", borderColor: `${dragColor}66` }} className={clsx("relative flex items-center gap-3 rounded-lg border bg-white px-3 py-2 shadow-xl", dragging.ids.length > 1 && "shadow-[4px_4px_0_0_#dbeafe,0_10px_24px_#0002]")}>
        <span className="shrink-0 font-mono text-xs" style={{ color: dragColor }}>M</span>
        <span className="min-w-0 flex-1"><strong className="block truncate text-xs">{nodes.find((node) => node.id === dragging.activeId)?.title}</strong><span className="block truncate text-[10px] text-gray-400">{nodes.find((node) => node.id === dragging.activeId)?.summary}</span></span>
        {dragging.ids.length > 1 ? <span className="rounded-full px-2 py-1 text-[11px] font-bold text-white" style={{ backgroundColor: dragColor }}>{dragging.ids.length}</span> : null}<GripVertical className="h-4 w-4 shrink-0" style={{ color: dragColor }} />
      </div> : null}
    </DragOverlay>
  </DndContext>;
}

export function HistoryOrderRow({ node, children, className, ...props }: Omit<ComponentPropsWithRef<"div">, "children"> & { node: HistoryGraphNode; children: (handle: ReactNode) => ReactNode }) {
  const context = useContext(DragContext);
  const color = historyGraphNodeColor(node);
  const { setNodeRef: bindSortable, attributes, listeners, setActivatorNodeRef } = useSortable({ id: node.id, disabled: { draggable: !context.active || node.nodeKind !== "chat", droppable: !context.active }, animateLayoutChanges: () => false });
  const register = context.register;
  const setNodeRef = useCallback((element: HTMLDivElement | null) => { bindSortable(element); register(node.id, element); }, [bindSortable, register, node.id]);
  return <div {...props} ref={setNodeRef} style={{ ...props.style, ...(context.active ? { transition: "none" } : {}) }} className={clsx(className, context.dragged.includes(node.id) && "opacity-35", context.beforeId === node.id && "shadow-[inset_0_2px_0_#2563eb]")}>{children(<button type="button" ref={setActivatorNodeRef} {...attributes} {...listeners} aria-label={`Drag ${node.title}`} title="Drag to reorder, or press Space and use the arrow keys" onClick={(event) => event.stopPropagation()} className="touch-none rounded border p-1" style={{ borderColor: `${color}33`, backgroundColor: `${color}0d`, color }}><GripVertical className="h-4 w-4" /></button>)}</div>;
}
