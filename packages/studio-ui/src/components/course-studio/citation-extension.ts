import { Extension, Node, mergeAttributes } from "@tiptap/core";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";
import type { EditorView } from "@tiptap/pm/view";
import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import { citationReferenceFoldingPlugin, expandCitationReferenceGroup } from "./citation-reference-folding";
import "./citation-reference-folding.css";

/**
 * A board document cites the material it was generated from the way a
 * publication does: a numbered marker inside the passage, and one reference
 * entry per number at the end of the document. The backend owns the Markdown
 * form of that pair (`[^1]` and `[^1]: ...`); these two nodes are its editor
 * form, and the plugin below makes a marker jump to its entry.
 */

const CITATION_ANCHOR_PREFIX = "openclass-ref-";
const HIGHLIGHT_DURATION_MS = 1800;

function citationAnchorId(label: string) {
  return `${CITATION_ANCHOR_PREFIX}${label}`;
}

function citationLabel(element: HTMLElement) {
  return (element.getAttribute("data-citation-label") || "").trim();
}

export const CitationRef = Node.create({
  name: "citationRef",
  // Ahead of the StarterKit rules, so the backend's own markup is recognized
  // before a generic `p`/text rule claims it.
  priority: 200,
  group: "inline",
  inline: true,
  atom: true,
  selectable: true,
  draggable: false,

  addAttributes() {
    return {
      label: {
        default: "",
        parseHTML: (element) => citationLabel(element),
        renderHTML: (attributes) => ({ "data-citation-label": attributes.label || "" }),
      },
      anchorId: {
        default: "",
        parseHTML: (element) =>
          (element.getAttribute("data-citation-anchor") || "").trim() ||
          citationAnchorId(citationLabel(element)),
        renderHTML: (attributes) => ({
          "data-citation-anchor": attributes.anchorId || citationAnchorId(attributes.label || ""),
        }),
      },
    };
  },

  parseHTML() {
    return [{ tag: 'sup[data-type="citation-ref"]' }];
  },

  renderHTML({ node, HTMLAttributes }) {
    return [
      "sup",
      mergeAttributes(HTMLAttributes, {
        "data-type": "citation-ref",
        class: "word-editor__citation-ref",
        role: "link",
        tabindex: "0",
        title: "Go to citation source",
      }),
      `[${node.attrs.label || ""}]`,
    ];
  },
});

export const CitationReference = Node.create({
  name: "citationReference",
  priority: 200,
  group: "block",
  content: "inline*",
  defining: true,

  addAttributes() {
    return {
      label: {
        default: "",
        parseHTML: (element) => citationLabel(element),
        renderHTML: (attributes) => ({ "data-citation-label": attributes.label || "" }),
      },
      anchorId: {
        default: "",
        parseHTML: (element) =>
          (element.getAttribute("id") || "").trim() || citationAnchorId(citationLabel(element)),
        renderHTML: (attributes) => ({
          id: attributes.anchorId || citationAnchorId(attributes.label || ""),
        }),
      },
    };
  },

  parseHTML() {
    return [
      {
        tag: 'p[data-type="citation-reference"]',
        // The backend ships the visible "[1]" as its own span so that plain-text
        // and .docx projections keep the number; in the editor the number is
        // drawn from the attribute instead, and must not become editable text.
        contentElement: (element) => {
          const clone = element.cloneNode(true) as HTMLElement;
          clone
            .querySelectorAll('[data-type="citation-label"]')
            .forEach((label) => label.remove());
          return clone;
        },
      },
    ];
  },

  renderHTML({ HTMLAttributes }) {
    return [
      "p",
      mergeAttributes(HTMLAttributes, {
        "data-type": "citation-reference",
        class: "word-editor__citation-reference",
      }),
      0,
    ];
  },
});

function referencePosition(doc: ProseMirrorNode, label: string) {
  let target: number | null = null;
  doc.descendants((node, pos) => {
    if (target !== null) {
      return false;
    }
    if (node.type.name === "citationReference" && String(node.attrs.label) === label) {
      target = pos;
      return false;
    }
    return true;
  });
  return target;
}

function scrollReferenceIntoView(view: EditorView, pos: number) {
  window.requestAnimationFrame(() => {
    const dom = view.nodeDOM(pos);
    const element =
      dom instanceof HTMLElement ? dom : dom?.parentElement instanceof HTMLElement ? dom.parentElement : null;
    if (element) {
      element.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    try {
      view.dispatch(view.state.tr.scrollIntoView());
    } catch {
      // A concurrent edit removed the entry; the highlight below already cleared.
    }
  });
}

const citationJumpKey = new PluginKey<number | null>("citationJump");

function jumpToReference(view: EditorView, label: string) {
  const target = label ? referencePosition(view.state.doc, label) : null;
  if (target === null) return false;
  view.dispatch(expandCitationReferenceGroup(view.state, target).setMeta(citationJumpKey, target));
  scrollReferenceIntoView(view, target);
  window.setTimeout(() => {
    if (!view.isDestroyed) view.dispatch(view.state.tr.setMeta(citationJumpKey, null).setMeta("addToHistory", false));
  }, HIGHLIGHT_DURATION_MS);
  return true;
}

const citationJumpPlugin = () =>
  new Plugin<number | null>({
    key: citationJumpKey,
    state: {
      init: () => null,
      apply: (transaction, current) => {
        const next = transaction.getMeta(citationJumpKey);
        if (next !== undefined) {
          return next as number | null;
        }
        if (transaction.docChanged && current !== null) {
          return transaction.mapping.map(current);
        }
        return current;
      },
    },
    props: {
      decorations(state) {
        const pos = citationJumpKey.getState(state);
        if (pos === null || pos === undefined) {
          return DecorationSet.empty;
        }
        const node = state.doc.nodeAt(pos);
        if (!node || node.type.name !== "citationReference") {
          return DecorationSet.empty;
        }
        return DecorationSet.create(state.doc, [
          Decoration.node(pos, pos + node.nodeSize, {
            class: "word-editor__citation-reference--active",
          }),
        ]);
      },
      handleClickOn(view, _pos, node, _nodePos, event, direct) {
        if (!direct || node.type.name !== "citationRef") {
          return false;
        }
        if (!jumpToReference(view, String(node.attrs.label || ""))) return false;
        event.preventDefault();
        return true;
      },
      handleKeyDown(view, event) {
        if (event.key !== "Enter" && event.key !== " ") return false;
        const element = event.target instanceof HTMLElement ? event.target.closest<HTMLElement>('sup[data-type="citation-ref"]') : null;
        if (!element || !view.dom.contains(element) || !jumpToReference(view, citationLabel(element))) return false;
        event.preventDefault();
        return true;
      },
    },
  });

export const CitationNavigation = Extension.create({
  name: "citationNavigation",
  priority: 250,
  addProseMirrorPlugins() {
    return [citationReferenceFoldingPlugin(), citationJumpPlugin()];
  },
});
