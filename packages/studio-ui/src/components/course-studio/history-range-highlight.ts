import { Extension, type Editor } from "@tiptap/core";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";
import { useEffect, useRef, type RefObject } from "react";
import type { BoardDocument } from "../../types";
import type { DocumentRangeAnchor } from "../../types/content-actions";
import { prepareHistoryDocument, resolveRenderedHistoryAnchor } from "./history-range-rendering";
import { useHistoryRangeGeometry } from "./history-range-geometry";

type Range = { from: number; to: number; kind?: string };
type LocateRequest = {
  documentId: string;
  expectedText: string;
  spans?: Array<{ start: number; end: number }>;
  anchors?: DocumentRangeAnchor[];
  expectedJson?: Record<string, unknown>;
  focus?: DocumentRangeAnchor;
  requestId?: number;
};
const key = new PluginKey<Range[]>("historyRangeLocation");

export const HistoryRangeHighlight = Extension.create({
  name: "historyRangeHighlight",
  addProseMirrorPlugins() {
    return [new Plugin<Range[]>({
      key,
      state: {
        init: () => [],
        apply: (transaction, ranges) => transaction.getMeta(key) ?? (transaction.docChanged ? [] : ranges),
      },
      props: {
        decorations(state) {
          const attrs = { "data-history-range": "true", class: "word-editor__teaching-focus-highlight" };
          return DecorationSet.create(state.doc, (key.getState(state) ?? []).map(({ from, to, kind }) => from < to
            ? kind === "node" && state.doc.nodeAt(from)?.isAtom && state.doc.nodeAt(from)?.nodeSize === to - from ? Decoration.node(from, to, attrs) : Decoration.inline(from, to, attrs)
            : Decoration.widget(from, () => {
              const marker = window.document.createElement("span");
              marker.dataset.historyRange = "true";
              marker.className = attrs.class;
              marker.textContent = "\u200b";
              marker.setAttribute("aria-label", "Deleted content location");
              return marker;
            })));
        },
      },
    })];
  },
});

/** The server uses Unicode scalar tokens plus one separator per text block. */
export function historyRangePositions(editor: Editor, spans: NonNullable<LocateRequest["spans"]>): Range[] {
  const starts: number[] = [], ends: number[] = [];
  editor.state.doc.descendants((node, position) => {
    if (!node.isTextblock) return;
    node.forEach((child, offset) => {
      let local = position + 1 + offset;
      if (child.isText) {
        for (const char of child.text ?? "") {
          starts.push(local); local += char.length; ends.push(local);
        }
      } else {
        starts.push(local); ends.push(local + child.nodeSize);
      }
    });
    starts.push(position + node.nodeSize - 1);
    ends.push(position + node.nodeSize - 1);
    return false;
  });
  if (!starts.length) return [];
  return spans.flatMap((span) => {
    if (!Number.isFinite(span.start) || !Number.isFinite(span.end) || span.start < 0 || span.end > 1 || span.end < span.start) return [];
    const start = Math.min(starts.length - 1, Math.round(span.start * starts.length));
    const end = Math.min(ends.length, Math.round(span.end * starts.length));
    return [{ from: starts[start], to: end > start ? ends[end - 1] : starts[start] }];
  });
}

export function useHistoryRangeLocation(editor: Editor | null, document: BoardDocument, scroll: RefObject<HTMLDivElement | null>) {
  useHistoryRangeGeometry(editor, scroll);
  const latest = useRef(document);
  useEffect(() => { latest.current = document; }, [document]);
  useEffect(() => {
    if (!editor) return;
    let frame = 0;
    const clear = () => {
      cancelAnimationFrame(frame);
      if (!editor.isDestroyed) editor.view.dispatch(editor.state.tr.setMeta(key, []).setMeta("addToHistory", false));
    };
    const receive = (event: Event) => {
      const request = (event as CustomEvent<LocateRequest>).detail;
      if (!request || typeof request.expectedText !== "string" || (!Array.isArray(request.spans) && !Array.isArray(request.anchors))) return;
      clear();
      const failed = (message: string) => window.dispatchEvent(new CustomEvent("openclass:history-range-location-status", { detail: { requestId: request.requestId, message } }));
      const prepared = request.expectedJson ? prepareHistoryDocument(editor.schema, request.expectedJson, editor.extensionManager.extensions.some((extension) => extension.name === "trailingNode")) : null;
      let attempts = 0;
      const locate = () => {
        if (editor.isDestroyed) return;
        // Preview hydration is asynchronous. Never decorate the preceding snapshot.
        const plain = (value: string) => value.replace(/\s+/g, " ").trim();
        const ready = request.documentId === latest.current.id && (request.expectedJson
          ? Boolean(prepared && editor.state.doc.eq(prepared.doc))
          : latest.current.content_text === request.expectedText && plain(editor.getText()) === plain(request.expectedText));
        if (!ready) {
          if (++attempts < 120) frame = requestAnimationFrame(locate);
          else failed("The document snapshot is not ready. Select the action again to locate it.");
          return;
        }
        const resolved = prepared ? request.anchors?.map((anchor) => resolveRenderedHistoryAnchor(prepared, anchor)) : undefined;
        if (resolved?.some((range) => !range)) { failed("Fragment positions in this snapshot could not be verified. No highlight was added."); return; }
        const ranges: Range[] = resolved?.filter((range): range is NonNullable<typeof range> => Boolean(range)) ?? historyRangePositions(editor, request.spans ?? []);
        editor.view.dispatch(editor.state.tr.setMeta(key, ranges).setMeta("addToHistory", false));
        if (ranges.length) {
          const element = scroll.current;
          const focus = request.focus && prepared ? resolveRenderedHistoryAnchor(prepared, request.focus) : null;
          const coordinate = editor.view.coordsAtPos(focus?.from ?? ranges[0].from);
          if (element) element.scrollTo({ top: Math.max(0, element.scrollTop + coordinate.top - element.getBoundingClientRect().top - element.clientHeight / 3), behavior: "auto" });
        }
      };
      frame = requestAnimationFrame(locate);
    };
    window.addEventListener("openclass:locate-history-range", receive);
    window.addEventListener("openclass:clear-history-range", clear);
    return () => { cancelAnimationFrame(frame); window.removeEventListener("openclass:locate-history-range", receive); window.removeEventListener("openclass:clear-history-range", clear); };
  }, [editor, scroll]);
}
