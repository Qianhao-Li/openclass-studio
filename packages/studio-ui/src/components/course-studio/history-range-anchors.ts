import type { Node } from "@tiptap/pm/model";
import type { DocumentRangeAnchor } from "../../types/content-actions";

export type EditorRange = { from: number; to: number; kind: DocumentRangeAnchor["kind"] };

/** Convert typed tree anchors, never normalized Markdown offsets, to ProseMirror. */
export function resolveHistoryAnchor(doc: Node, anchor: DocumentRangeAnchor): EditorRange | null {
  let node = doc, position = -1;
  for (const index of anchor.path) {
    if (!Number.isInteger(index) || index < 0 || index >= node.childCount) return null;
    let offset = 0;
    for (let i = 0; i < index; i++) offset += node.child(i).nodeSize;
    position += 1 + offset;
    node = node.child(index);
  }
  if (anchor.kind === "node") return { from: Math.max(0, position), to: position < 0 ? doc.content.size : position + node.nodeSize, kind: "node" };
  if (anchor.kind === "gap") {
    if (!Number.isInteger(anchor.start) || anchor.start < 0 || anchor.start > node.childCount) return null;
    let offset = 0;
    for (let i = 0; i < anchor.start; i++) offset += node.child(i).nodeSize;
    return { from: position + 1 + offset, to: position + 1 + offset, kind: "gap" };
  }
  if (!node.isTextblock) return null;
  const boundaries = [position + 1];
  node.forEach((child, offset) => {
    let cursor = position + 1 + offset;
    if (child.isText) for (const char of child.text ?? "") { cursor += char.length; boundaries.push(cursor); }
    else boundaries.push(cursor + child.nodeSize);
  });
  if (![anchor.start, anchor.end].every(Number.isInteger) || anchor.start < 0 || anchor.end < anchor.start || anchor.end >= boundaries.length) return null;
  return { from: boundaries[anchor.start], to: boundaries[anchor.end], kind: "text" };
}
