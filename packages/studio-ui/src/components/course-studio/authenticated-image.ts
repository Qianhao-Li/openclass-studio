import type { Node as Extension } from "@tiptap/core";
import type { Node as DocumentNode } from "@tiptap/pm/model";
import type { api } from "../../lib/api";

/** Fetch protected board images through the same course client as resource visual blocks.
 * Node views change display URLs only; serialized document attributes remain portable. */
export function authenticatedImage(extension: Extension, client: typeof api) {
  return extension.extend({
    addNodeView() {
      return ({ node, HTMLAttributes }) => {
        const image = document.createElement("img");
        for (const [key, value] of Object.entries(HTMLAttributes)) {
          if (key !== "src" && value != null) image.setAttribute(key, String(value));
        }
        let lastSource = "";
        let objectUrl: string | null = null;
        let controller: AbortController | null = null;
        let destroyed = false;
        function update(current: DocumentNode) {
          for (const key of ["alt", "title", "width", "height"]) {
            const value = current.attrs[key];
            if (value == null) image.removeAttribute(key); else image.setAttribute(key, String(value));
          }
          const src = String(current.attrs.src || "");
          if (src === lastSource) return;
          lastSource = src;
          controller?.abort();
          if (objectUrl) { URL.revokeObjectURL(objectUrl); objectUrl = null; }
          const assetId = /^\/api\/board-assets\/(basset_[A-Za-z0-9_-]+)\/content$/.exec(src)?.[1];
          if (!assetId) { image.src = src; return; }
          const currentController = new AbortController();
          controller = currentController;
          void client.getBoardAssetContent(assetId, { signal: currentController.signal }).then(blob => {
            if (destroyed || currentController.signal.aborted) return;
            objectUrl = URL.createObjectURL(blob); image.src = objectUrl;
          }).catch(() => { if (!destroyed && !currentController.signal.aborted) image.removeAttribute("src"); });
        }
        update(node);
        return {
          dom: image,
          ignoreMutation: mutation => mutation.type !== "selection",
          update: next => { if (next.type !== node.type) return false; update(next); return true; },
          destroy: () => { destroyed = true; controller?.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); },
        };
      };
    },
  });
}
