import { useEffect, type RefObject } from "react";
import type { Editor } from "@tiptap/core";
import { EditorState } from "@tiptap/pm/state";
import { EditorView, type NodeViewConstructor } from "@tiptap/pm/view";
import type { BoardDocument } from "../../types";
import type { ContentAction, ContentActionSpan } from "../../types/content-actions";
import { prepareHistoryDocument, resolveRenderedHistoryAnchor } from "./history-range-rendering";
import { normalizePageSettings, pagePreviewMetrics } from "./page-settings";

export type GeometryRequest = { key: string; documentId: string; document: BoardDocument; actions: ContentAction[] };
export type GeometryResult = { key: string; spans: Record<string, ContentActionSpan[]>; error?: string };

/** A detached read-only view uses the same schema, node views and page CSS.
 * It measures the reference document even while the visible editor previews an older C.
 */
export function useHistoryRangeGeometry(editor: Editor | null, scroll: RefObject<HTMLDivElement | null>) {
  useEffect(() => {
    if (!editor) return;
    let request: GeometryRequest | null = null, view: EditorView | null = null, shell: HTMLElement | null = null;
    let frame = 0, previous = "";
    let observer: ResizeObserver | null = null;
    let prepared: ReturnType<typeof prepareHistoryDocument> | null = null;
    const dispose = () => { observer?.disconnect(); observer = null; view?.destroy(); view = null; shell?.remove(); shell = null; };
    const publish = () => {
      frame = 0;
      if (!request || !view || !shell || !prepared) return;
      const root = view.dom.getBoundingClientRect();
      const result: GeometryResult = { key: request.key, spans: {} };
      for (const action of request.actions) {
        const measured: ContentActionSpan[] = [];
        for (const fragment of action.range_projection?.fragments ?? []) {
          if (fragment.precision !== "exact" || fragment.reference_precision !== "exact") continue;
          const ranges = fragment.reference.map((a) => resolveRenderedHistoryAnchor(prepared!, a));
          if (ranges.some((r) => !r)) continue;
          const rects = ranges.map((r) => {
            const range = r!;
            const element = range.kind === "node" ? view!.nodeDOM(range.from) : null;
            if (element instanceof HTMLElement) return element.getBoundingClientRect();
            const first = view!.coordsAtPos(range.from), last = view!.coordsAtPos(range.to, -1);
            return { top: Math.min(first.top, last.top), bottom: range.from === range.to ? first.top : Math.max(first.bottom, last.bottom) };
          }).sort((a, b) => a.top - b.top);
          // Adjacent sibling blocks include their normal paragraph spacing.
          let group: { start: number; end: number; fragment_id: string } | null = null;
          for (const rect of rects) {
            const next = { start: Math.max(0, Math.min(1, (rect.top - root.top) / Math.max(1, root.height))), end: Math.max(0, Math.min(1, (rect.bottom - root.top) / Math.max(1, root.height))), fragment_id: fragment.id };
            if (group && (next.start - group.end) * root.height <= 32) group.end = Math.max(group.end, next.end);
            else { group = next; measured.push(group); }
          }
        }
        result.spans[action.id] = measured;
      }
      const identity = JSON.stringify(result);
      if (identity !== previous) { previous = identity; window.dispatchEvent(new CustomEvent("openclass:history-range-geometry", { detail: result })); }
    };
    const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(publish); };
    const mount = () => {
      if (!request || editor.isDestroyed) return;
      const page = editor.view.dom.closest<HTMLElement>(".word-editor__page");
      const content = editor.view.dom.parentElement?.parentElement;
      if (!page || !content) return;
      dispose();
      shell = page.cloneNode(false) as HTMLElement;
      shell.setAttribute("aria-hidden", "true"); shell.inert = true;
      Object.assign(shell.style, { position: "fixed", left: "-100000px", top: "0", pointerEvents: "none", visibility: "hidden", minHeight: "0" });
      const settings = normalizePageSettings(request.document.page_settings);
      const metrics = pagePreviewMetrics(settings);
      shell.style.width = `${metrics.width}px`;
      shell.style.setProperty("--page-padding-x", `${metrics.paddingX}px`);
      shell.style.setProperty("--page-padding-y", `${metrics.paddingY}px`);
      shell.style.setProperty("--page-content-min-height", `${metrics.contentMinHeight}px`);
      shell.classList.toggle("word-editor__page--columns-2", settings.columns === 2);
      const body = content.cloneNode(false) as HTMLElement;
      const mountPoint = window.document.createElement("div");
      body.appendChild(mountPoint); shell.appendChild(body); page.parentElement!.appendChild(shell);
      const nodeViews: Record<string, NodeViewConstructor> = {};
      editor.view.someProp("nodeViews", (items) => { Object.assign(nodeViews, items); });
      prepared = prepareHistoryDocument(editor.schema, request.document.content_json, editor.extensionManager.extensions.some((extension) => extension.name === "trailingNode"));
      const doc = prepared.doc;
      view = new EditorView(mountPoint, { state: EditorState.create({ doc }), editable: () => false, nodeViews,
        attributes: { class: editor.view.dom.className, "data-history-measurement": "true" } });
      observer = new ResizeObserver(schedule); observer.observe(view.dom);
      shell.addEventListener("load", schedule, true);
      schedule();
    };
    const receive = (event: Event) => {
      const next = (event as CustomEvent<GeometryRequest>).detail;
      if (!next?.document?.content_json || !next.key) return;
      request = next; previous = "";
      try { mount(); } catch { dispose(); window.dispatchEvent(new CustomEvent("openclass:history-range-geometry", { detail: { key: next.key, spans: {}, error: "Document ranges cannot be measured right now. Reopen the course." } })); }
    };
    let viewportWidth = 0;
    const resize = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width ?? 0;
      if (width !== viewportWidth) { viewportWidth = width; if (request) mount(); }
    });
    if (scroll.current) resize.observe(scroll.current);
    const page = editor.view.dom.closest<HTMLElement>(".word-editor__page");
    const styleObserver = new MutationObserver(() => { if (request) mount(); });
    if (page) styleObserver.observe(page, { attributes: true, attributeFilter: ["style", "class"] });
    window.addEventListener("openclass:measure-history-ranges", receive);
    window.document.fonts?.addEventListener("loadingdone", schedule);
    window.dispatchEvent(new Event("openclass:history-editor-ready"));
    return () => { cancelAnimationFrame(frame); resize.disconnect(); styleObserver.disconnect(); dispose(); window.removeEventListener("openclass:measure-history-ranges", receive); window.document.fonts?.removeEventListener("loadingdone", schedule); };
  }, [editor, scroll]);
}
