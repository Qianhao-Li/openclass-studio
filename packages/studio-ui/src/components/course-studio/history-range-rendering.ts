import { EditorState } from "@tiptap/pm/state";
import type { Schema } from "@tiptap/pm/model";
import { editorMathTransaction } from "../../lib/math-content";
import { resolveHistoryAnchor, type EditorRange } from "./history-range-anchors";
import type { DocumentRangeAnchor } from "../../types/content-actions";

/** Match the editor's existing display normalization without writing a document. */
export function prepareHistoryDocument(schema: Schema, json: Record<string, unknown>, withTrailingNode = false) {
  const source = schema.nodeFromJSON(json);
  const state = EditorState.create({ doc: source });
  let transaction = editorMathTransaction(state, true);
  const doc = transaction?.doc ?? source;
  const trailing = schema.topNodeType.contentMatch.defaultType;
  if (withTrailingNode && trailing && doc.lastChild?.type !== trailing) {
    const empty = trailing.createAndFill();
    if (empty) transaction = (transaction ?? state.tr).insert(doc.content.size, empty);
  }
  return { source, doc: transaction?.doc ?? source, transaction };
}

export function resolveRenderedHistoryAnchor(prepared: ReturnType<typeof prepareHistoryDocument>, anchor: DocumentRangeAnchor): EditorRange | null {
  const range = resolveHistoryAnchor(prepared.source, anchor);
  if (!range) return null;
  let { from, to } = range;
  const uncertain = prepared.transaction?.getMeta("historyRangeUncertainSteps") as number[] | undefined;
  for (const [index, map] of (prepared.transaction?.mapping.maps ?? []).entries()) {
    let left = map.map(from, 1), right = map.map(to, -1), invalid = false;
    map.forEach((oldStart, oldEnd, newStart, newEnd) => {
      if (from < oldEnd && to > oldStart) {
        if (range.kind === "text" && uncertain?.includes(index)) invalid = true;
        left = Math.min(left, newStart); right = Math.max(right, newEnd);
      }
    });
    if (invalid) return null;
    from = Math.min(left, right); to = range.from === range.to ? from : Math.max(left, right);
  }
  const atom = prepared.doc.nodeAt(from);
  return { from, to, kind: atom?.isAtom && atom.nodeSize === to - from ? "node" : range.kind };
}
