"use client";

import { Box, TextQuote } from "lucide-react";
import { useSyncExternalStore } from "react";

import type { SelectionRef } from "../../types";
import type { SelectionPopoverPosition } from "./selection-utils";

function subscribeToSelectionChanges(onChange: () => void) {
  const onFocusOut = () => queueMicrotask(onChange);
  document.addEventListener("selectionchange", onChange);
  document.addEventListener("focusin", onChange);
  document.addEventListener("focusout", onFocusOut);
  return () => {
    document.removeEventListener("selectionchange", onChange);
    document.removeEventListener("focusin", onChange);
    document.removeEventListener("focusout", onFocusOut);
  };
}

function hasActiveTextSelection(kind: SelectionRef["kind"] | undefined) {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !selection.toString().trim()) return false;
  const anchor = selection.anchorNode instanceof Element
    ? selection.anchorNode
    : selection.anchorNode?.parentElement;
  const editor = anchor?.closest('[contenteditable="true"]');
  if (kind === "board" && (!editor?.classList.contains("ProseMirror") || !editor.contains(selection.focusNode))) {
    return false;
  }
  return !editor || editor.contains(document.activeElement) ||
    Boolean(document.activeElement?.closest("[data-selection-popover]"));
}

const noServerSelection = () => false;

type SelectionPopoverProps = {
  selection: SelectionRef | null;
  position: SelectionPopoverPosition | null;
  onFocusComposerWithSelection: () => void;
  onOpenGeometryWithSelection: () => void;
};

export function SelectionPopover({
  selection,
  position,
  onFocusComposerWithSelection,
  onOpenGeometryWithSelection,
}: SelectionPopoverProps) {
  const hasSelection = useSyncExternalStore(
    subscribeToSelectionChanges,
    () => hasActiveTextSelection(selection?.kind),
    noServerSelection
  );
  if (!selection || !position || selection.location_kind === "insertion_anchor" || !hasSelection) {
    return null;
  }

  return (
    <div
      data-selection-popover
      className="fixed z-[90] flex -translate-x-1/2 items-center overflow-hidden rounded-xl border border-gray-200 bg-white text-[13px] font-medium text-gray-800 shadow-lg"
      style={{ left: position.left, top: position.top }}
      onMouseDown={(event) => event.preventDefault()}
    >
      <button
        type="button"
        onClick={onFocusComposerWithSelection}
        className="inline-flex h-10 items-center gap-2 px-3.5 transition-colors hover:bg-gray-50"
      >
        <TextQuote className="h-4 w-4" />

        Reference to input box
      </button>
      <span className="h-5 w-px bg-gray-200" aria-hidden="true" />
      <button
        type="button"
        onClick={onOpenGeometryWithSelection}
        className="inline-flex h-10 items-center gap-2 px-3.5 transition-colors hover:bg-gray-50"
      >
        <Box className="h-4 w-4" />

        Reference to graphics
      </button>
    </div>
  );
}
