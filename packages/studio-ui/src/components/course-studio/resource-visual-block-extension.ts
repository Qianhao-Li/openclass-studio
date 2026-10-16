import { Node, mergeAttributes } from "@tiptap/core";

import { api } from "../../lib/api";

export const ResourceVisualBlock = Node.create<{ client: typeof api }>({
  name: "resourceVisualBlock",
  group: "block",
  atom: true,
  selectable: true,

  addOptions() { return { client: api }; },

  addAttributes() {
    return {
      marker: {
        default: "",
        parseHTML: (element) => element.getAttribute("data-openclass-resource-visual") || "",
        renderHTML: (attributes) =>
          attributes.marker ? { "data-openclass-resource-visual": attributes.marker } : {},
      },
      assetId: {
        default: "",
        parseHTML: (element) => element.getAttribute("data-board-asset-id") || "",
        renderHTML: (attributes) =>
          attributes.assetId ? { "data-board-asset-id": attributes.assetId } : {},
      },
      visualId: {
        default: "",
        parseHTML: (element) => element.getAttribute("data-visual-id") || "",
        renderHTML: (attributes) => (attributes.visualId ? { "data-visual-id": attributes.visualId } : {}),
      },
      sourceIngestionId: {
        default: "",
        parseHTML: (element) => element.getAttribute("data-source-ingestion-id") || "",
        renderHTML: (attributes) =>
          attributes.sourceIngestionId ? { "data-source-ingestion-id": attributes.sourceIngestionId } : {},
      },
      sourceChapterId: {
        default: "",
        parseHTML: (element) => element.getAttribute("data-source-chapter-id") || "",
        renderHTML: (attributes) =>
          attributes.sourceChapterId ? { "data-source-chapter-id": attributes.sourceChapterId } : {},
      },
      sourceTitle: {
        default: "",
        parseHTML: (element) => element.getAttribute("data-source-title") || "",
        renderHTML: (attributes) =>
          attributes.sourceTitle ? { "data-source-title": attributes.sourceTitle } : {},
      },
      kind: {
        default: "image",
        parseHTML: (element) => element.getAttribute("data-visual-kind") || "image",
        renderHTML: (attributes) => ({ "data-visual-kind": attributes.kind || "image" }),
      },
      caption: {
        default: "",
        parseHTML: (element) => element.getAttribute("data-caption") || "",
        renderHTML: (attributes) => (attributes.caption ? { "data-caption": attributes.caption } : {}),
      },
      source: {
        default: "",
        parseHTML: (element) => element.getAttribute("data-source") || "",
        renderHTML: (attributes) => (attributes.source ? { "data-source": attributes.source } : {}),
      },
      sourceLocator: {
        default: "",
        parseHTML: (element) => element.getAttribute("data-source-locator") || "",
        renderHTML: (attributes) =>
          attributes.sourceLocator ? { "data-source-locator": attributes.sourceLocator } : {},
      },
      pageNo: {
        default: null,
        parseHTML: (element) => visualPositiveInteger(element.getAttribute("data-page-no")),
        renderHTML: (attributes) => {
          const pageNo = visualPositiveInteger(attributes.pageNo);
          return pageNo == null ? {} : { "data-page-no": String(pageNo) };
        },
      },
      pageRange: {
        default: "",
        parseHTML: (element) => element.getAttribute("data-page-range") || "",
        renderHTML: (attributes) =>
          attributes.pageRange ? { "data-page-range": attributes.pageRange } : {},
      },
      slideNo: {
        default: null,
        parseHTML: (element) => visualPositiveInteger(element.getAttribute("data-slide-no")),
        renderHTML: (attributes) => {
          const slideNo = visualPositiveInteger(attributes.slideNo);
          return slideNo == null ? {} : { "data-slide-no": String(slideNo) };
        },
      },
      sheetName: {
        default: "",
        parseHTML: (element) => element.getAttribute("data-sheet-name") || "",
        renderHTML: (attributes) =>
          attributes.sheetName ? { "data-sheet-name": attributes.sheetName } : {},
      },
      // These attributes remain parseable so older boards round-trip without
      // losing data. The node view never renders model-authored recreation HTML.
      recreationKind: {
        default: "original",
        parseHTML: (element) => element.getAttribute("data-recreation-kind") || "original",
        renderHTML: (attributes) => ({ "data-recreation-kind": attributes.recreationKind || "original" }),
      },
      recreationStatus: {
        default: "original_only",
        parseHTML: (element) => element.getAttribute("data-recreation-status") || "original_only",
        renderHTML: (attributes) => ({ "data-recreation-status": attributes.recreationStatus || "original_only" }),
      },
      recreationConfidence: {
        default: "0.00",
        parseHTML: (element) => element.getAttribute("data-recreation-confidence") || "0.00",
        renderHTML: (attributes) => ({ "data-recreation-confidence": attributes.recreationConfidence || "0.00" }),
      },
      recreationNote: {
        default: "",
        parseHTML: (element) => element.getAttribute("data-recreation-note") || "",
        renderHTML: (attributes) =>
          attributes.recreationNote ? { "data-recreation-note": attributes.recreationNote } : {},
      },
      recreationHtml: {
        default: "",
        parseHTML: (element) => element.getAttribute("data-recreation-html") || "",
        renderHTML: (attributes) =>
          attributes.recreationHtml ? { "data-recreation-html": attributes.recreationHtml } : {},
      },
      originalSrc: {
        default: "",
        parseHTML: (element) => element.getAttribute("data-original-src") || "",
        renderHTML: (attributes) => (attributes.originalSrc ? { "data-original-src": attributes.originalSrc } : {}),
      },
      originalAlt: {
        default: "",
        parseHTML: (element) => element.getAttribute("data-original-alt") || "",
        renderHTML: (attributes) => (attributes.originalAlt ? { "data-original-alt": attributes.originalAlt } : {}),
      },
      originalInitiallyCollapsed: {
        default: false,
        parseHTML: () => false,
        renderHTML: () => ({ "data-original-initially-collapsed": "false" }),
      },
    };
  },

  parseHTML() {
    return [{ tag: 'section[data-type="resource-visual-block"]' }];
  },

  renderHTML({ HTMLAttributes, node }) {
    const caption = visualAttr(node.attrs.caption) || "Information visual material";
    const sourceLabel = visualSourceLabel(node.attrs);
    const imageSource = serializableImageSource(visualAttr(node.attrs.originalSrc));
    const figure = imageSource
      ? [
          "figure",
          {},
          [
            "img",
            {
              src: imageSource,
              alt: visualAttr(node.attrs.originalAlt) || caption,
              class: "word-editor__resource-visual-image",
            },
          ],
          ["figcaption", {}, caption],
        ]
      : ["figure", {}, ["figcaption", {}, caption]];
    return [
      "section",
      mergeAttributes(HTMLAttributes, {
        "data-type": "resource-visual-block",
        class: "word-editor__resource-visual",
      }),
      figure,
      ["p", { class: "word-editor__resource-visual-source" }, sourceLabel],
    ];
  },

  renderText({ node }) {
    const caption = visualAttr(node.attrs.caption) || "Information visual material";
    return `${visualKindLabel(node.attrs.kind)}：${caption}\n${visualSourceLabel(node.attrs)}`;
  },

  addNodeView() {
    return ({ node }) => {
      const attrs = node.attrs;
      const assetId = visualAttr(attrs.assetId);
      const visualId = visualAttr(attrs.visualId);
      const caption = visualAttr(attrs.caption) || "Information visual material";
      const originalSrc = visualAttr(attrs.originalSrc);
      const originalAlt = visualAttr(attrs.originalAlt) || caption;
      const abortController = new AbortController();
      let objectUrl = "";
      let destroyed = false;

      const dom = document.createElement("section");
      dom.className = "word-editor__resource-visual";
      dom.setAttribute("data-type", "resource-visual-block");
      if (assetId) {
        dom.setAttribute("data-board-asset-id", assetId);
      }
      if (visualId) {
        dom.setAttribute("data-visual-id", visualId);
      }
      dom.contentEditable = "false";

      const eyebrow = document.createElement("div");
      eyebrow.className = "word-editor__resource-visual-eyebrow";
      eyebrow.textContent = visualKindLabel(attrs.kind);
      dom.appendChild(eyebrow);

      const title = document.createElement("p");
      title.className = "word-editor__resource-visual-title";
      title.textContent = caption;
      dom.appendChild(title);

      const media = document.createElement("div");
      media.className = "word-editor__resource-visual-media";
      const status = document.createElement("p");
      status.className = "word-editor__resource-visual-status";
      status.setAttribute("role", "status");
      const image = document.createElement("img");
      image.className = "word-editor__resource-visual-image";
      image.alt = originalAlt;
      image.decoding = "async";
      image.loading = "eager";
      image.referrerPolicy = "no-referrer";
      image.hidden = true;
      media.append(status, image);
      dom.appendChild(media);

      const sourceLine = document.createElement("p");
      sourceLine.className = "word-editor__resource-visual-source";
      sourceLine.textContent = visualSourceLabel(attrs);
      dom.appendChild(sourceLine);

      function showImage(src: string) {
        image.onload = () => {
          if (destroyed) {
            return;
          }
          image.hidden = false;
          status.hidden = true;
        };
        image.onerror = () => {
          if (destroyed) {
            return;
          }
          image.hidden = true;
          status.hidden = false;
          status.classList.add("word-editor__resource-visual-status--error");
          status.textContent = "Image content is not available";
        };
        image.src = src;
      }

      if (assetId) {
        status.textContent = "Loading data pictures";
        void this.options.client
          .getBoardAssetContent(assetId, { signal: abortController.signal })
          .then((blob) => {
            if (destroyed || abortController.signal.aborted) {
              return;
            }
            if (!blob.size || (blob.type && !blob.type.startsWith("image/"))) {
              throw new Error("The board asset is not a displayable image");
            }
            objectUrl = URL.createObjectURL(blob);
            showImage(objectUrl);
          })
          .catch((error: unknown) => {
            if (destroyed || abortController.signal.aborted) {
              return;
            }
            status.classList.add("word-editor__resource-visual-status--error");
            status.textContent = error instanceof Error && error.message ? error.message : "Image content is not available";
          });
      } else if (isSafeLegacyImageSource(originalSrc)) {
        status.textContent = "Loading old version data pictures";
        showImage(originalSrc);
      } else {
        status.classList.add("word-editor__resource-visual-status--error");
        status.textContent = "Image content is not available";
      }

      return {
        dom,
        destroy() {
          destroyed = true;
          abortController.abort();
          image.onload = null;
          image.onerror = null;
          image.removeAttribute("src");
          if (objectUrl) {
            URL.revokeObjectURL(objectUrl);
          }
        },
      };
    };
  },
});

function visualAttr(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function visualPositiveInteger(value: unknown): number | null {
  if (typeof value === "number" && Number.isInteger(value) && value > 0) {
    return value;
  }
  if (typeof value === "string" && /^\d+$/.test(value.trim())) {
    const parsed = Number(value);
    return parsed > 0 ? parsed : null;
  }
  return null;
}

function visualKindLabel(value: unknown): string {
  switch (visualAttr(value)) {
    case "table":
      return "Information form";
    case "chart":
      return "Information chart";
    case "image":
      return "Data pictures";
    default:
      return "Data icon";
  }
}

function visualSourceLabel(attrs: Record<string, unknown>): string {
  const sourceLocator = visualAttr(attrs.sourceLocator);
  const title = visualAttr(attrs.sourceTitle) || visualAttr(attrs.source) || sourceLocator;
  const pageRange = visualAttr(attrs.pageRange);
  const pageNo = visualPositiveInteger(attrs.pageNo);
  const slideNo = visualPositiveInteger(attrs.slideNo);
  const sheetName = visualAttr(attrs.sheetName);
  const location =
    pageRange ||
    (pageNo != null
      ? `Page ${pageNo}`
      : slideNo != null
        ? `Slide ${slideNo}`
        : sheetName
          ? `Worksheet ${sheetName}`
          : sourceLocator && sourceLocator !== title
            ? sourceLocator
            : "");
  const detail = [title, location].filter(Boolean).join(" / ");
  return detail ? `Source: ${detail}` : "Source: Analytical data";
}

function isSafeLegacyImageSource(value: string): boolean {
  const source = value.trim();
  if (!source) {
    return false;
  }
  if (/^data:image\/(?:png|jpe?g|gif|webp);base64,/i.test(source)) {
    return true;
  }
  if (/^(?:https?:|blob:)/i.test(source)) {
    return true;
  }
  return source.startsWith("/") && !source.startsWith("//");
}

function serializableImageSource(value: string): string {
  const source = value.trim();
  if (/^\/api\/board-assets\/basset_[A-Za-z0-9_-]+\/content$/.test(source)) {
    return source;
  }
  return /^data:image\/(?:png|jpe?g|gif|webp);base64,/i.test(source) ? source : "";
}
