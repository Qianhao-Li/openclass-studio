"use client";

import { useDraggable } from "@dnd-kit/core";
import { useSortable } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import clsx from "clsx";
import { GripVertical, LockKeyhole } from "lucide-react";
import type { ComponentPropsWithRef, CSSProperties, MouseEvent, ReactNode } from "react";

import type { LessonMergeEventNode } from "../../types";

export type MergeEventRowProps = {
  node: LessonMergeEventNode;
  scope: string;
  selected: boolean;
  editable: boolean;
  groupDragging?: boolean;
  insertionBefore?: boolean;
  onSelect: (event: MouseEvent, checkbox: boolean) => void;
};

function EventRowContent({
  node, scope, selected, editable, onSelect, dragHandle,
}: MergeEventRowProps & { dragHandle?: ReactNode }) {
  return (
    <>
      {editable && node.kind === "M" ? (
        <input
          type="checkbox"
          aria-label={`Select ${node.title} in ${scope}`}
          checked={selected}
          onChange={() => undefined}
          onClick={(event) => onSelect(event, true)}
          className="h-4 w-4 shrink-0 accent-violet-600"
        />
      ) : (
        <span title={node.kind === "C" ? "Document changes keep their default order" : undefined}>
          {node.kind === "C" ? <LockKeyhole className="h-3.5 w-3.5 text-gray-400" /> : null}
        </span>
      )}
      <span className={clsx("shrink-0 rounded px-1.5 py-0.5 font-mono text-[10px]", node.kind === "M" ? "bg-violet-50 text-violet-700" : "bg-emerald-50 text-emerald-800")}>
        {node.initial ? "C0" : node.kind}
      </span>
      <button
        type="button"
        onClick={(event) => onSelect(event, false)}
        disabled={!editable || node.kind !== "M"}
        className="min-w-0 flex-1 text-left disabled:cursor-default"
        aria-label={`${node.title} in ${scope}`}
      >
        <span className="block truncate text-xs font-semibold text-gray-900" title={node.title}>{node.title}</span>
        <span className="mt-0.5 block truncate text-[10px] leading-4 text-gray-500" title={node.summary || node.branch_name}>{node.summary || node.branch_name}</span>
      </button>
      {dragHandle}
    </>
  );
}

function RowShell({
  selected, groupDragging, insertionBefore, children, ...props
}: ComponentPropsWithRef<"li"> & { selected: boolean; groupDragging?: boolean; insertionBefore?: boolean; children: ReactNode }) {
  return (
    <li
      {...props}
      data-selected={selected ? "true" : "false"}
      className={clsx(
        "relative flex min-h-16 items-center gap-2 rounded-lg border px-2.5 py-2",
        selected ? "border-violet-300 bg-violet-50" : "border-gray-200 bg-white",
        groupDragging && "opacity-40",
        insertionBefore && "before:absolute before:-top-1 before:right-0 before:left-0 before:border-t-2 before:border-violet-500",
      )}
    >{children}</li>
  );
}

export function MergeSourceEventRow(props: MergeEventRowProps) {
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, isDragging } = useDraggable({
    id: `${props.scope}:${props.node.id}`,
    data: { nodeId: props.node.id, scope: props.scope },
    disabled: !props.editable || props.node.kind !== "M",
  });
  return (
    <RowShell
      ref={setNodeRef}
      data-merge-node-id={props.node.id}
      data-merge-scope={props.scope}
      selected={props.selected}
      groupDragging={isDragging || props.groupDragging}
    >
      <EventRowContent {...props} dragHandle={props.editable && props.node.kind === "M" ? (
        <button ref={setActivatorNodeRef} type="button" {...attributes} {...listeners} aria-label={`Drag ${props.node.title} from ${props.scope}`} className="touch-none rounded p-1 text-gray-400 hover:bg-violet-100 hover:text-violet-700">
          <GripVertical className="h-4 w-4" />
        </button>
      ) : null} />
    </RowShell>
  );
}

export function MergeResultEventRow(props: MergeEventRowProps) {
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition, isDragging } = useSortable({
    id: `result:${props.node.id}`,
    data: { nodeId: props.node.id, scope: "result" },
    disabled: { draggable: !props.editable || props.node.kind !== "M", droppable: !props.editable || Boolean(props.node.initial) },
    transition: { duration: 180, easing: "cubic-bezier(0.2, 0, 0, 1)" },
  });
  return (
    <RowShell
      ref={setNodeRef}
      data-merge-node-id={props.node.id}
      data-merge-node-kind={props.node.kind}
      data-merge-scope="result"
      selected={props.selected}
      groupDragging={isDragging || props.groupDragging}
      insertionBefore={props.insertionBefore}
      style={{ transform: CSS.Transform.toString(transform), transition } as CSSProperties}
    >
      <EventRowContent {...props} dragHandle={props.editable && props.node.kind === "M" ? (
        <button ref={setActivatorNodeRef} type="button" {...attributes} {...listeners} aria-label={`Drag ${props.node.title} in result`} className="touch-none rounded p-1 text-gray-400 hover:bg-violet-100 hover:text-violet-700">
          <GripVertical className="h-4 w-4" />
        </button>
      ) : null} />
    </RowShell>
  );
}
