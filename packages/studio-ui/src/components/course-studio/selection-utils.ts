import type { SelectionRef } from "../../types";
import type { Node as ProseMirrorNode } from "@tiptap/pm/model";

export function boardSelectionText(doc: ProseMirrorNode, from: number, to: number) {
  return doc.textBetween(from, to, " ", (node) => {
    if (node.type.name === "inlineMath" || node.type.name === "blockMath") {
      return String(node.attrs.latex ?? "");
    }
    return node.type.spec.leafText?.(node) ?? "";
  });
}

export type SelectionPopoverPosition = {
  top: number;
  left: number;
};

export function sameSelection(left: SelectionRef | null, right: SelectionRef | null) {
  if (!left || !right) {
    return left === right;
  }
  return (
    left.kind === right.kind &&
    left.location_kind === right.location_kind &&
    left.lesson_id === right.lesson_id &&
    left.block_id === right.block_id &&
    left.document_id === right.document_id &&
    left.segment_id === right.segment_id &&
    left.before_text === right.before_text &&
    left.after_text === right.after_text &&
    left.text_hash === right.text_hash &&
    left.source_ingestion_id === right.source_ingestion_id &&
    left.source_title === right.source_title &&
    left.source_uri === right.source_uri &&
    left.source_chapter_id === right.source_chapter_id &&
    left.source_chapter_number === right.source_chapter_number &&
    left.source_chapter_title === right.source_chapter_title &&
    left.source_page_range === right.source_page_range &&
    left.source_locator === right.source_locator &&
    left.source_page_start === right.source_page_start &&
    left.source_page_end === right.source_page_end &&
    left.source_scope_kind === right.source_scope_kind &&
    left.catalog_version === right.catalog_version &&
    left.source_content_hash === right.source_content_hash &&
    JSON.stringify(left.source_range ?? null) === JSON.stringify(right.source_range ?? null) &&
    (left.heading_path ?? []).join("\n") === (right.heading_path ?? []).join("\n") &&
    left.excerpt === right.excerpt
  );
}

export function samePopoverPosition(
  left: SelectionPopoverPosition | null,
  right: SelectionPopoverPosition | null
) {
  if (!left || !right) {
    return left === right;
  }
  return Math.abs(left.left - right.left) < 1 && Math.abs(left.top - right.top) < 1;
}

function clampSelectionPopover(left: number, top: number): SelectionPopoverPosition {
  if (typeof window === "undefined") {
    return { left, top };
  }
  return {
    left: Math.max(88, Math.min(left, window.innerWidth - 88)),
    top: Math.max(12, Math.min(top, window.innerHeight - 80)),
  };
}

export function popoverPositionFromRect(rect: DOMRect | { left: number; width: number; top: number }): SelectionPopoverPosition {
  return clampSelectionPopover(rect.left + rect.width / 2, rect.top - 44);
}

export function popoverPositionFromDomSelection(): SelectionPopoverPosition | null {
  if (typeof window === "undefined") {
    return null;
  }
  const activeSelection = window.getSelection();
  if (!activeSelection || activeSelection.rangeCount === 0) {
    return null;
  }
  const rect = activeSelection.getRangeAt(0).getBoundingClientRect();
  if (!rect.width && !rect.height) {
    return null;
  }
  return clampSelectionPopover(rect.left + rect.width / 2, rect.top - 44);
}

export function popoverPositionFromCaretRect(rect: { left: number; right: number; top: number }): SelectionPopoverPosition {
  return clampSelectionPopover((rect.left + rect.right) / 2, rect.top - 44);
}
