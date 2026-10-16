import { Extension, Node, type Editor as TiptapEditor } from "@tiptap/core";
import Color from "@tiptap/extension-color";
import Highlight from "@tiptap/extension-highlight";
import ImageExtension from "@tiptap/extension-image";
import LinkExtension from "@tiptap/extension-link";
import { BlockMath, InlineMath } from "@tiptap/extension-mathematics";
import { Table } from "@tiptap/extension-table";
import TableCell from "@tiptap/extension-table-cell";
import TableHeader from "@tiptap/extension-table-header";
import TableRow from "@tiptap/extension-table-row";
import TextAlign from "@tiptap/extension-text-align";
import { TextStyle } from "@tiptap/extension-text-style";
import UnderlineExtension from "@tiptap/extension-underline";
import { NodeSelection, Plugin, PluginKey, type Transaction } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";
import { EditorContent, useEditor } from "@tiptap/react";
import { BoardDocumentOutline } from "./board-document-outline";
import StarterKit from "@tiptap/starter-kit";
import { HistoryRangeHighlight, useHistoryRangeLocation } from "./history-range-highlight";
import clsx from "clsx";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  AlignCenter,
  AlignHorizontalSpaceAround,
  AlignLeft,
  AlignRight,
  ArrowLeft,
  ArrowRight,
  Bold,
  ChevronDown,
  ChevronUp,
  ClipboardList,
  Columns2,
  Columns3,
  Download,
  FilePlus,
  Files,
  FileText,
  Frame,
  Hash,
  Highlighter,
  ImagePlus,
  Italic,
  LayoutTemplate,
  Link as LinkIcon,
  List,
  ListOrdered,
  PaintBucket,
  PanelTop,
  PencilLine,
  Quote,
  RectangleHorizontal,
  RectangleVertical,
  Redo2,
  Rows3,
  Stamp,
  Table2,
  TableCellsMerge,
  TableCellsSplit,
  TextCursorInput,
  Trash2,
  Type,
  Underline,
  Undo2,
  Upload,
} from "lucide-react";

import {
  PAGE_BACKGROUND_OPTIONS,
  PAGE_MARGIN_OPTIONS,
  PAGE_SIZE_OPTIONS,
  PAGE_ZOOM_DEFAULT,
  PAGE_ZOOM_WHEEL_SENSITIVITY,
  normalizePageSettings,
  normalizePageZoom,
  pagePreviewMetrics,
} from "./page-settings";
import {
  boardSelectionText,
  popoverPositionFromCaretRect,
  popoverPositionFromDomSelection,
  popoverPositionFromRect,
  type SelectionPopoverPosition,
} from "./selection-utils";
import { FormulaInkPopover, type FormulaInkSubmitPayload } from "./formula-ink-popover";
import {
  RibbonActionButton,
  RibbonTabButton,
  ToolbarButton,
  WordPageZoomControls,
} from "./word-editor-toolbar";
import {
  CitationNavigation,
  CitationRef,
  CitationReference,
} from "./citation-extension";
import { ResourceVisualBlock } from "./resource-visual-block-extension";
import { useStudioApi } from "../../contexts/studio-api-context";
import { authenticatedImage } from "./authenticated-image";
import { editorUpdateBelongsToDocument } from "../../hooks/course-studio/chat-turn-ui-state";
import "../../lib/katex-mhchem";
import { MATH_TEXT_SERIALIZERS, normalizeEditorMath } from "../../lib/math-content";
import { RichCodeBlock } from "../../lib/rich-code-block-extension";
import { EditorDocumentChangeGate, editorDocumentMatchesJson } from "../../lib/editor-document-change-gate";
import { RIDOC_FILE_ACCEPT } from "../../lib/ridoc-file";
import type {
  BoardDocument,
  BoardFocusRef,
  BoardTaskLocationKind,
  DocumentPageSettings,
} from "../../types";

type WordRibbonTab = "home" | "insert" | "page";

type WordBoardSelectionPayload = {
  locationKind: BoardTaskLocationKind;
  excerpt: string;
  position: SelectionPopoverPosition | null;
  documentId: string;
  beforeText: string;
  afterText: string;
};

type ActiveFormulaSelection = WordBoardSelectionPayload & {
  latex: string;
  nodeType: "inlineMath" | "blockMath";
};

export type FormulaInkEditorSubmitPayload = FormulaInkSubmitPayload & {
  selection: WordBoardSelectionPayload;
};

declare module "@tiptap/core" {
  interface Commands<ReturnType> {
    teachingFocusHighlight: {
      setTeachingFocusHighlight: (range: { from: number; to: number }) => ReturnType;
      clearTeachingFocusHighlight: () => ReturnType;
    };
    fontSize: {
      setFontSize: (fontSize: string) => ReturnType;
      unsetFontSize: () => ReturnType;
    };
    fontFamily: {
      setFontFamily: (fontFamily: string) => ReturnType;
      unsetFontFamily: () => ReturnType;
    };
  }
}

const FontSize = Extension.create({
  name: "fontSize",
  addGlobalAttributes() {
    return [
      {
        types: ["textStyle"],
        attributes: {
          fontSize: {
            default: null,
            parseHTML: (element) => element.style.fontSize || null,
            renderHTML: (attributes) => {
              if (!attributes.fontSize) {
                return {};
              }
              return { style: `font-size: ${attributes.fontSize}` };
            },
          },
        },
      },
    ];
  },
  addCommands() {
    return {
      setFontSize:
        (fontSize: string) =>
        ({ chain }) =>
          chain().setMark("textStyle", { fontSize }).run(),
      unsetFontSize:
        () =>
        ({ chain }) =>
          chain().setMark("textStyle", { fontSize: null }).removeEmptyTextStyle().run(),
    };
  },
});

const FontFamily = Extension.create({
  name: "fontFamily",
  addGlobalAttributes() {
    return [
      {
        types: ["textStyle"],
        attributes: {
          fontFamily: {
            default: null,
            parseHTML: (element) => element.style.fontFamily || null,
            renderHTML: (attributes) => {
              if (!attributes.fontFamily) {
                return {};
              }
              return { style: `font-family: ${attributes.fontFamily}` };
            },
          },
        },
      },
    ];
  },
  addCommands() {
    return {
      setFontFamily:
        (fontFamily: string) =>
        ({ chain }) =>
          chain().setMark("textStyle", { fontFamily }).run(),
      unsetFontFamily:
        () =>
        ({ chain }) =>
          chain().setMark("textStyle", { fontFamily: null }).removeEmptyTextStyle().run(),
    };
  },
});

type TeachingFocusHighlightRange = { from: number; to: number };

const teachingFocusHighlightPluginKey = new PluginKey<TeachingFocusHighlightRange | null>("teachingFocusHighlight");

const TeachingFocusHighlight = Extension.create({
  name: "teachingFocusHighlight",
  addProseMirrorPlugins() {
    return [
      new Plugin<TeachingFocusHighlightRange | null>({
        key: teachingFocusHighlightPluginKey,
        state: {
          init: (): TeachingFocusHighlightRange | null => null,
          apply(transaction, currentRange): TeachingFocusHighlightRange | null {
            const meta = transaction.getMeta(teachingFocusHighlightPluginKey) as
              | { type: "set"; range: { from: number; to: number } }
              | { type: "clear" }
              | undefined;
            if (meta?.type === "clear") {
              return null;
            }
            if (meta?.type === "set") {
              return meta.range.from < meta.range.to ? meta.range : null;
            }
            if (!currentRange || !transaction.docChanged) {
              return currentRange;
            }
            const from = transaction.mapping.map(currentRange.from, -1);
            const to = transaction.mapping.map(currentRange.to, 1);
            return from < to && to <= transaction.doc.content.size ? { from, to } : null;
          },
        },
        props: {
          decorations(state) {
            const range = teachingFocusHighlightPluginKey.getState(state);
            if (!range || range.from >= range.to) {
              return null;
            }
            return DecorationSet.create(state.doc, [
              Decoration.inline(range.from, range.to, {
                class: "word-editor__teaching-focus-highlight",
                "data-teaching-focus": "true",
              }),
            ]);
          },
        },
      }),
    ];
  },
  addCommands() {
    return {
      setTeachingFocusHighlight:
        (range) =>
        ({ tr, dispatch }) => {
          if (dispatch) {
            dispatch(
              tr
                .setMeta(teachingFocusHighlightPluginKey, { type: "set", range })
                .setMeta("addToHistory", false)
            );
          }
          return true;
        },
      clearTeachingFocusHighlight:
        () =>
        ({ tr, dispatch }) => {
          if (dispatch) {
            dispatch(
              tr
                .setMeta(teachingFocusHighlightPluginKey, { type: "clear" })
                .setMeta("addToHistory", false)
            );
          }
          return true;
        },
    };
  },
});

const PageBreak = Node.create({
  name: "pageBreak",
  group: "block",
  atom: true,
  selectable: true,
  parseHTML() {
    return [{ tag: 'div[data-type="page-break"]' }];
  },
  renderHTML() {
    return ["div", { "data-type": "page-break", class: "word-editor__page-break" }];
  },
});

const FONT_FAMILY_OPTIONS = [
  { label: "Satoshi", value: '"Satoshi","Avenir Next","PingFang SC","Microsoft YaHei",sans-serif' },
  { label: "Serif", value: '"Iowan Old Style","Songti SC","Times New Roman",serif' },
  { label: "Mono", value: '"IBM Plex Mono","SFMono-Regular","Menlo",monospace' },
];

const FONT_SIZE_OPTIONS = [
  { label: "12", value: "12px" },
  { label: "14", value: "14px" },
  { label: "16", value: "16px" },
  { label: "18", value: "18px" },
  { label: "24", value: "24px" },
];

const TABLE_DIMENSION_MIN = 1;
const TABLE_DIMENSION_MAX = 12;

function normalizeTableDimension(value: number) {
  if (!Number.isFinite(value)) {
    return TABLE_DIMENSION_MIN;
  }
  return Math.min(TABLE_DIMENSION_MAX, Math.max(TABLE_DIMENSION_MIN, Math.round(value)));
}

const WORD_EDITOR_EXTENSIONS = [
  StarterKit.configure({
    heading: { levels: [1, 2, 3] },
    link: false,
    underline: false,
    codeBlock: false,
  }),
  RichCodeBlock,
  TextStyle,
  Color,
  Highlight.configure({ multicolor: true }),
  UnderlineExtension,
  LinkExtension.configure({
    autolink: true,
    openOnClick: false,
    defaultProtocol: "https",
  }),
  ImageExtension.configure({
    allowBase64: true,
    HTMLAttributes: {
      class: "word-editor__image",
    },
  }),
  TextAlign.configure({ types: ["heading", "paragraph"] }),
  Table.configure({ resizable: true, cellMinWidth: 72, lastColumnResizable: true }),
  TableRow,
  TableHeader,
  TableCell,
  CitationRef,
  CitationReference,
  CitationNavigation,
  TeachingFocusHighlight,
  HistoryRangeHighlight,
  BlockMath.configure({
    katexOptions: {
      displayMode: true,
      throwOnError: false,
    },
  }),
  InlineMath.configure({
    katexOptions: {
      throwOnError: false,
    },
  }),
  PageBreak,
  ResourceVisualBlock,
  FontSize,
  FontFamily,
];

const WORD_EDITOR_PROPS = {
  attributes: {
    class: "word-editor__content",
  },
};

type FlatEditorChar = {
  text: string;
  pos: number | null;
};

type NormalizedEditorText = {
  text: string;
  map: number[];
};

function normalizeLookupText(value: string) {
  return value.replace(/\s+/g, " ").trim().toLocaleLowerCase();
}

function appendBlockBoundary(chars: FlatEditorChar[]) {
  const last = chars[chars.length - 1]?.text ?? "";
  if (chars.length && !/\s/.test(last)) {
    chars.push({ text: " ", pos: null });
  }
}

function flattenEditorText(editor: TiptapEditor) {
  const chars: FlatEditorChar[] = [];
  editor.state.doc.descendants((node, pos) => {
    if (node.isBlock) {
      appendBlockBoundary(chars);
    }
    if (!node.isText || !node.text) {
      return;
    }
    for (let offset = 0; offset < node.text.length; offset += 1) {
      chars.push({ text: node.text[offset] ?? "", pos: pos + offset });
    }
  });
  return chars;
}

function normalizedEditorText(chars: FlatEditorChar[]): NormalizedEditorText {
  let text = "";
  const map: number[] = [];
  let lastWasSpace = true;
  chars.forEach((char, index) => {
    if (/\s/.test(char.text)) {
      if (!lastWasSpace && text.length) {
        text += " ";
        map.push(index);
        lastWasSpace = true;
      }
      return;
    }
    text += char.text.toLocaleLowerCase();
    map.push(index);
    lastWasSpace = false;
  });
  if (text.endsWith(" ")) {
    return { text: text.slice(0, -1), map: map.slice(0, -1) };
  }
  return { text, map };
}

function uniqueNeedles(values: Array<string | undefined | null>) {
  const seen = new Set<string>();
  return values.flatMap((value) => {
    const normalized = normalizeLookupText(value ?? "");
    if (normalized.length < 2 || seen.has(normalized)) {
      return [];
    }
    seen.add(normalized);
    return [normalized];
  });
}

function allMatchIndexes(haystack: string, needle: string) {
  const indexes: number[] = [];
  let fromIndex = 0;
  while (fromIndex < haystack.length) {
    const index = haystack.indexOf(needle, fromIndex);
    if (index < 0) {
      break;
    }
    indexes.push(index);
    fromIndex = index + Math.max(needle.length, 1);
  }
  return indexes;
}

function contextScore(text: string, start: number, end: number, focus: BoardFocusRef) {
  const before = normalizeLookupText(focus.before_text).slice(-96);
  const after = normalizeLookupText(focus.after_text).slice(0, 96);
  const previous = text.slice(Math.max(0, start - 160), start);
  const next = text.slice(end, Math.min(text.length, end + 160));
  let score = 0;
  if (before && previous.endsWith(before)) {
    score += 2;
  } else if (before && previous.includes(before.slice(-48))) {
    score += 1;
  }
  if (after && next.startsWith(after)) {
    score += 2;
  } else if (after && next.includes(after.slice(0, 48))) {
    score += 1;
  }
  return score;
}

function rangeFromNormalizedMatch(
  chars: FlatEditorChar[],
  map: number[],
  start: number,
  length: number
): { from: number; to: number } | null {
  const startFlatIndex = map[start];
  const endFlatIndex = map[start + length - 1];
  if (startFlatIndex === undefined || endFlatIndex === undefined) {
    return null;
  }
  let from: number | null = null;
  let to: number | null = null;
  for (let index = startFlatIndex; index <= endFlatIndex; index += 1) {
    const pos = chars[index]?.pos;
    if (typeof pos !== "number") {
      continue;
    }
    from ??= pos;
    to = pos + 1;
  }
  return from !== null && to !== null && from < to ? { from, to } : null;
}

function findTeachingFocusRange(editor: TiptapEditor, focus: BoardFocusRef): { from: number; to: number } | null {
  const headingRange = findTeachingHeadingSectionRange(editor, focus);
  if (headingRange) {
    return headingRange;
  }
  const chars = flattenEditorText(editor);
  const normalized = normalizedEditorText(chars);
  const lastHeading = focus.heading_path[focus.heading_path.length - 1];
  const needles = uniqueNeedles([focus.excerpt, focus.display_label, lastHeading]);
  for (const needle of needles) {
    const matches = allMatchIndexes(normalized.text, needle)
      .map((index) => ({
        index,
        score: contextScore(normalized.text, index, index + needle.length, focus),
      }))
      .sort((a, b) => b.score - a.score || a.index - b.index);
    for (const match of matches) {
      const range = rangeFromNormalizedMatch(chars, normalized.map, match.index, needle.length);
      if (range) {
        return range;
      }
    }
  }
  return null;
}

function findTeachingHeadingSectionRange(
  editor: TiptapEditor,
  focus: BoardFocusRef
): { from: number; to: number } | null {
  if (focus.kind !== "heading") {
    return null;
  }
  const targetHeading = normalizeLookupText(
    focus.display_label || focus.heading_path[focus.heading_path.length - 1] || ""
  );
  if (!targetHeading) {
    return null;
  }
  const headings: Array<{ pos: number; level: number; text: string }> = [];
  editor.state.doc.descendants((node, pos) => {
    if (node.type.name === "heading") {
      headings.push({
        pos,
        level: typeof node.attrs.level === "number" ? node.attrs.level : 1,
        text: normalizeLookupText(node.textContent),
      });
    }
  });
  const teachableHeadings = headings[0]?.level === 1 ? headings.slice(1) : headings;
  const orderedHeading =
    typeof focus.order_start === "number" ? teachableHeadings[focus.order_start] : null;
  const heading =
    orderedHeading?.text === targetHeading
      ? orderedHeading
      : headings.find((candidate) => candidate.text === targetHeading);
  if (!heading) {
    return null;
  }
  const nextBoundary =
    headings.find(
      (candidate) => candidate.pos > heading.pos && candidate.level <= heading.level
    )?.pos ?? editor.state.doc.content.size;
  let from: number | null = null;
  let to: number | null = null;
  editor.state.doc.nodesBetween(heading.pos, nextBoundary, (node, pos) => {
    if (!node.isText || !node.text) {
      return;
    }
    from ??= pos;
    to = pos + node.nodeSize;
  });
  return from !== null && to !== null && from < to ? { from, to } : null;
}

function scrollTeachingFocusIntoView(
  editor: TiptapEditor,
  pageScroll: HTMLDivElement | null,
  range: { from: number; to: number }
) {
  if (!pageScroll) {
    return;
  }
  window.requestAnimationFrame(() => {
    try {
      const coords = editor.view.coordsAtPos(range.from);
      const containerRect = pageScroll.getBoundingClientRect();
      const targetTop = pageScroll.scrollTop + coords.top - containerRect.top - pageScroll.clientHeight * 0.34;
      pageScroll.scrollTo({ top: Math.max(0, targetTop), behavior: "auto" });
    } catch {
      editor.commands.scrollIntoView();
    }
  });
}

function teachingFocusKey(focus: BoardFocusRef | null | undefined) {
  if (!focus) {
    return "";
  }
  return [
    focus.document_id ?? "",
    focus.segment_id ?? "",
    focus.text_hash ?? "",
    focus.excerpt_hash ?? "",
    focus.heading_path.join("/"),
    focus.display_label ?? "",
    focus.excerpt,
  ].join("\u001f");
}

function applyTeachingFocus(
  editor: TiptapEditor,
  documentId: string,
  focus: BoardFocusRef | null | undefined,
  pageScroll: HTMLDivElement | null,
  shouldScrollIntoView: boolean
) {
  if (
    !focus ||
    focus.source !== "board" ||
    (focus.document_id && focus.document_id !== documentId)
  ) {
    editor.commands.clearTeachingFocusHighlight();
    return;
  }
  const range = findTeachingFocusRange(editor, focus);
  if (!range) {
    editor.commands.clearTeachingFocusHighlight();
    return false;
  }
  editor.commands.setTeachingFocusHighlight(range);
  if (shouldScrollIntoView) {
    scrollTeachingFocusIntoView(editor, pageScroll, range);
  }
  return true;
}

function compactAnchorContext(value: string, maxLength = 90) {
  const compact = value.replace(/\s+/g, " ").trim();
  return compact.length > maxLength ? compact.slice(-maxLength) : compact;
}

function insertionAnchorExcerpt(beforeText: string, afterText: string) {
  const before = compactAnchorContext(beforeText);
  const after = compactAnchorContext(afterText);
  if (before && after) {
    return `${before}｜${after}`;
  }
  return before || after || "Current cursor position";
}

function popoverPositionFromEditorCaret(editor: TiptapEditor, position: number) {
  try {
    return popoverPositionFromCaretRect(editor.view.coordsAtPos(position));
  } catch {
    return popoverPositionFromDomSelection();
  }
}

function isFormulaNodeName(value: string): value is ActiveFormulaSelection["nodeType"] {
  return value === "inlineMath" || value === "blockMath";
}

function formulaPopoverPositionFromNode(editor: TiptapEditor, position: number) {
  const dom = editor.view.nodeDOM(position);
  if (dom instanceof Element) {
    return popoverPositionFromRect(dom.getBoundingClientRect());
  }
  return popoverPositionFromEditorCaret(editor, position);
}

function activeFormulaSelectionFromEditor(
  editor: TiptapEditor,
  {
    beforeText,
    afterText,
    documentId,
  }: {
    beforeText: string;
    afterText: string;
    documentId: string;
  }
): ActiveFormulaSelection | null {
  const { selection } = editor.state;
  if (!(selection instanceof NodeSelection) || !isFormulaNodeName(selection.node.type.name)) {
    return null;
  }
  const latex = String(selection.node.attrs.latex ?? "").trim();
  if (!latex) {
    return null;
  }
  return {
    locationKind: "target_range",
    excerpt: latex,
    position: formulaPopoverPositionFromNode(editor, selection.from),
    documentId,
    beforeText,
    afterText,
    latex,
    nodeType: selection.node.type.name,
  };
}

export function WordBoardEditor({
  document,
  readOnly,
  teachingFocus,
  toolbarCollapsed,
  onDocumentChange,
  onStructureRemovalIntent,
  onSelectionChange,
  onImportDocx,
  onExportDocx,
  onExportHtml,
  onImportRidoc,
  onExportRidoc,
  onFormulaReference,
  onFormulaGeometryReference,
  onFormulaInkSubmit,
}: {
  document: BoardDocument;
  readOnly: boolean;
  teachingFocus?: BoardFocusRef | null;
  toolbarCollapsed: boolean;
  onDocumentChange: (document: BoardDocument) => void;
  onStructureRemovalIntent: () => void;
  onSelectionChange: (
    selection: {
      locationKind: BoardTaskLocationKind;
      excerpt: string;
      position: SelectionPopoverPosition | null;
      documentId: string;
      beforeText: string;
      afterText: string;
    } | null
  ) => void;
  onImportDocx: (file: File) => void;
  onExportDocx: () => void;
  onExportHtml: () => void;
  onImportRidoc: (file: File) => void;
  onExportRidoc: () => void;
  onFormulaReference?: (selection: WordBoardSelectionPayload) => void;
  onFormulaGeometryReference?: (selection: WordBoardSelectionPayload) => void;
  onFormulaInkSubmit?: (payload: FormulaInkEditorSubmitPayload) => boolean;
}) {
  const importRef = useRef<HTMLInputElement | null>(null);
  const studioApi = useStudioApi();
  const scopedExtensions = useMemo(() => WORD_EDITOR_EXTENSIONS.map(extension =>
    extension.name === ResourceVisualBlock.name ? ResourceVisualBlock.configure({ client: studioApi }) : extension instanceof Node && extension.name === "image" ? authenticatedImage(extension, studioApi) : extension
  ), [studioApi]);
  const ridocImportRef = useRef<HTMLInputElement | null>(null);
  const imageUploadRef = useRef<HTMLInputElement | null>(null);
  const pageScrollRef = useRef<HTMLDivElement | null>(null);
  const pageZoomRef = useRef(PAGE_ZOOM_DEFAULT);
  const [activeRibbonTab, setActiveRibbonTab] = useState<WordRibbonTab>("home");
  const [tableRows, setTableRows] = useState(3);
  const [tableCols, setTableCols] = useState(3);
  const [tableHasHeaderRow, setTableHasHeaderRow] = useState(true);
  const [isTableActive, setIsTableActive] = useState(false);
  const [pageZoom, setPageZoom] = useState(PAGE_ZOOM_DEFAULT);
  const [activeFormulaSelection, setActiveFormulaSelection] = useState<ActiveFormulaSelection | null>(null);
  const documentJson =
    document.content_json && Object.keys(document.content_json).length ? document.content_json : null;
  const editorContent = documentJson ?? (document.content_html.trim() || "<p></p>");
  const latestDocumentRef = useRef(document);
  const editorDocumentIdRef = useRef<string | null>(document.id);
  const latestReadOnlyRef = useRef(readOnly);
  const latestOnDocumentChangeRef = useRef(onDocumentChange);
  const latestOnSelectionChangeRef = useRef(onSelectionChange);
  const latestTeachingFocusRef = useRef(teachingFocus);
  const suppressEditorUpdatesRef = useRef(false);
  const editorSyncGenerationRef = useRef(0);
  const documentChangeGateRef = useRef(new EditorDocumentChangeGate());
  const lastAutoScrolledTeachingFocusKeyRef = useRef("");
  const pageSettings = normalizePageSettings(document.page_settings);
  const pageMetrics = pagePreviewMetrics(pageSettings);
  const pageZoomScale = pageZoom / 100;
  const pageStyle = {
    "--page-padding-x": `${pageMetrics.paddingX}px`,
    "--page-padding-y": `${pageMetrics.paddingY}px`,
    "--page-content-min-height": `${pageMetrics.contentMinHeight}px`,
    "--word-page-preview-height": `${pageMetrics.height}px`,
    "--word-page-zoom": pageZoomScale.toString(),
    width: `${pageMetrics.width}px`,
    minHeight: `${pageMetrics.height}px`,
  } as CSSProperties;
  const pageChromeStyle = {
    paddingLeft: "var(--page-padding-x)",
    paddingRight: "var(--page-padding-x)",
  } as CSSProperties;
  const titleStyle = {
    ...pageChromeStyle,
    paddingTop: "calc(var(--page-padding-y) * 0.72)",
    paddingBottom: "calc(var(--page-padding-y) * 0.56)",
  } as CSSProperties;
  const contentStyle = {
    ...pageChromeStyle,
    paddingTop: "var(--page-padding-y)",
    paddingBottom: "calc(var(--page-padding-y) * 0.9)",
  } as CSSProperties;

  useEffect(() => {
    latestDocumentRef.current = document;
    latestReadOnlyRef.current = readOnly;
    latestOnDocumentChangeRef.current = onDocumentChange;
    latestOnSelectionChangeRef.current = onSelectionChange;
    latestTeachingFocusRef.current = teachingFocus;
  }, [document, onDocumentChange, onSelectionChange, readOnly, teachingFocus]);

  const handleEditorUpdate = useCallback(({ editor: currentEditor, transaction, appendedTransactions = [] }: { editor: TiptapEditor; transaction: Transaction; appendedTransactions?: Transaction[] }) => {
    if (latestReadOnlyRef.current || suppressEditorUpdatesRef.current) {
      return;
    }
    const targetDocument = latestDocumentRef.current;
    if (!editorUpdateBelongsToDocument(editorDocumentIdRef.current, targetDocument.id)) {
      return;
    }
    if (!documentChangeGateRef.current.accept(targetDocument.id, transaction.docChanged || appendedTransactions.some((item) => item.docChanged), currentEditor.state.doc)) return;
    setIsTableActive(currentEditor.isActive("table"));
    latestOnDocumentChangeRef.current({
      ...targetDocument,
      ...(documentChangeGateRef.current.originalContent(targetDocument.id, currentEditor.state.doc) ?? {
      content_json: currentEditor.getJSON() as Record<string, unknown>,
      content_html: currentEditor.getHTML(),
      content_text: currentEditor.getText({ textSerializers: MATH_TEXT_SERIALIZERS }),
      }),
    });
  }, []);

  const handleEditorSelectionUpdate = useCallback(({ editor: currentEditor }: { editor: TiptapEditor }) => {
    if (!editorUpdateBelongsToDocument(editorDocumentIdRef.current, latestDocumentRef.current.id)) {
      return;
    }
    if (latestReadOnlyRef.current) {
      setIsTableActive(false);
      setActiveFormulaSelection(null);
      latestOnSelectionChangeRef.current(null);
      return;
    }
    setIsTableActive(currentEditor.isActive("table"));
    const { from, to } = currentEditor.state.selection;
    const beforeText = boardSelectionText(currentEditor.state.doc, Math.max(0, from - 240), from).trim();
    const afterText = boardSelectionText(currentEditor.state.doc, to, Math.min(currentEditor.state.doc.content.size, to + 240)).trim();
    const formulaSelection = activeFormulaSelectionFromEditor(currentEditor, {
      beforeText,
      afterText,
      documentId: latestDocumentRef.current.id,
    });
    if (formulaSelection) {
      setActiveFormulaSelection(formulaSelection);
      latestOnSelectionChangeRef.current({ ...formulaSelection, position: null });
      return;
    }
    setActiveFormulaSelection(null);
    if (from === to) {
      if (!currentEditor.isFocused) {
        latestOnSelectionChangeRef.current(null);
        return;
      }
      latestOnSelectionChangeRef.current({
        locationKind: "insertion_anchor",
        excerpt: insertionAnchorExcerpt(beforeText, afterText),
        position: null,
        documentId: latestDocumentRef.current.id,
        beforeText,
        afterText,
      });
      return;
    }
    const excerpt = boardSelectionText(currentEditor.state.doc, from, to).trim();
    if (!excerpt) {
      latestOnSelectionChangeRef.current(null);
      return;
    }
    latestOnSelectionChangeRef.current({
      locationKind: "target_range",
      excerpt,
      position: popoverPositionFromDomSelection(),
      documentId: latestDocumentRef.current.id,
      beforeText,
      afterText,
    });
  }, []);

  const editor = useEditor({
    immediatelyRender: false,
    editable: !readOnly,
    extensions: scopedExtensions,
    editorProps: WORD_EDITOR_PROPS,
    onUpdate: handleEditorUpdate,
    onSelectionUpdate: handleEditorSelectionUpdate,
  });
  useHistoryRangeLocation(editor, document, pageScrollRef);

  useEffect(() => {
    if (!editor) {
      return;
    }
    if (editor.isEditable === readOnly) {
      editor.setEditable(!readOnly, false);
    }
    const incomingHtml = document.content_html.trim();
    const incomingPlainText = (document.content_text || incomingHtml.replace(/<[^>]+>/g, " "))
      .replace(/\s+/g, " ")
      .trim();
    const editorPlainText = editor.getText({ textSerializers: MATH_TEXT_SERIALIZERS }).replace(/\s+/g, " ").trim();
    const matchesIncomingDocument = documentJson
      ? editorDocumentMatchesJson(editor.state.doc, documentJson)
      : readOnly
        ? incomingPlainText === editorPlainText
      : incomingHtml
        ? editor.getHTML() === incomingHtml
        : false;
    if (matchesIncomingDocument) {
      editorSyncGenerationRef.current += 1;
      suppressEditorUpdatesRef.current = false;
      editorDocumentIdRef.current = document.id;
      documentChangeGateRef.current.hydrate(document.id, editor.state.doc, latestDocumentRef.current);
      return;
    }
    const content = editorContent;
    const docId = document.id;
    const syncGeneration = ++editorSyncGenerationRef.current;
    suppressEditorUpdatesRef.current = true;
    let cancelled = false;
    queueMicrotask(() => {
      if (cancelled || editor.isDestroyed || latestDocumentRef.current.id !== docId) {
        return;
      }
      editorDocumentIdRef.current = docId;
      editor.chain().setMeta("addToHistory", false).setContent(content, { emitUpdate: false }).run();
      normalizeEditorMath(editor);
      documentChangeGateRef.current.hydrate(docId, editor.state.doc, latestDocumentRef.current);
      const focus = latestTeachingFocusRef.current;
      const focusKey = teachingFocusKey(focus);
      const shouldScrollIntoView =
        Boolean(focusKey) && lastAutoScrolledTeachingFocusKeyRef.current !== focusKey;
      const didApplyFocus = applyTeachingFocus(
        editor,
        docId,
        focus,
        pageScrollRef.current,
        shouldScrollIntoView
      );
      if (didApplyFocus && shouldScrollIntoView) {
        lastAutoScrolledTeachingFocusKeyRef.current = focusKey;
      }
      queueMicrotask(() => {
        if (editorSyncGenerationRef.current === syncGeneration) {
          suppressEditorUpdatesRef.current = false;
        }
      });
    });
    return () => {
      cancelled = true;
      if (editorSyncGenerationRef.current === syncGeneration) {
        suppressEditorUpdatesRef.current = false;
      }
    };
  }, [document.id, document.content_html, document.content_text, documentJson, editor, editorContent, readOnly]);

  const currentTeachingFocusKey = teachingFocusKey(teachingFocus);

  useEffect(() => {
    if (!editor) {
      return;
    }
    const focus = latestTeachingFocusRef.current;
    const shouldScrollIntoView =
      Boolean(currentTeachingFocusKey) &&
      lastAutoScrolledTeachingFocusKeyRef.current !== currentTeachingFocusKey;
    const didApplyFocus = applyTeachingFocus(
      editor,
      document.id,
      focus,
      pageScrollRef.current,
      shouldScrollIntoView
    );
    if (!currentTeachingFocusKey) {
      lastAutoScrolledTeachingFocusKeyRef.current = "";
    } else if (didApplyFocus && shouldScrollIntoView) {
      lastAutoScrolledTeachingFocusKeyRef.current = currentTeachingFocusKey;
    }
  }, [currentTeachingFocusKey, document.id, editor]);

  const currentFontSize =
    ((editor?.getAttributes("textStyle").fontSize as string | null) ?? "14px").replace("px", "");
  const currentFontFamily = (editor?.getAttributes("textStyle").fontFamily as string | null) ?? FONT_FAMILY_OPTIONS[0].value;
  const currentPageNumberLabel = pageSettings.show_page_number ? "Page 1" : "";

  const updatePageSettings = useCallback(
    (patch: Partial<DocumentPageSettings>) => {
      if (readOnly) {
        return;
      }
      onDocumentChange({
        ...document,
        page_settings: {
          ...pageSettings,
          ...patch,
        },
      });
    },
    [document, onDocumentChange, pageSettings, readOnly]
  );

  const updatePageZoom = useCallback((value: number) => {
    const nextZoom = normalizePageZoom(value);
    pageZoomRef.current = nextZoom;
    setPageZoom(nextZoom);
  }, []);

  const fitPageToWidth = useCallback(() => {
    const viewportWidth = pageScrollRef.current?.clientWidth ?? 0;
    if (!viewportWidth) {
      updatePageZoom(PAGE_ZOOM_DEFAULT);
      return;
    }
    const horizontalBreathingRoom = viewportWidth >= 768 ? 96 : 48;
    updatePageZoom(((viewportWidth - horizontalBreathingRoom) / pageMetrics.width) * 100);
  }, [pageMetrics.width, updatePageZoom]);

  const handlePageWheelZoom = useCallback(
    (event: WheelEvent) => {
      if (!event.ctrlKey) {
        return;
      }
      const pageScroll = pageScrollRef.current;
      if (!pageScroll) {
        return;
      }

      event.preventDefault();
      const currentZoom = pageZoomRef.current;
      const nextZoom = normalizePageZoom(currentZoom - event.deltaY * PAGE_ZOOM_WHEEL_SENSITIVITY);
      if (nextZoom === currentZoom) {
        return;
      }

      const scrollRect = pageScroll.getBoundingClientRect();
      const pointerX = event.clientX - scrollRect.left;
      const pointerY = event.clientY - scrollRect.top;
      const currentScale = currentZoom / 100;
      const nextScale = nextZoom / 100;
      const worldX = (pageScroll.scrollLeft + pointerX) / currentScale;
      const worldY = (pageScroll.scrollTop + pointerY) / currentScale;

      updatePageZoom(nextZoom);
      window.requestAnimationFrame(() => {
        pageScroll.scrollLeft = worldX * nextScale - pointerX;
        pageScroll.scrollTop = worldY * nextScale - pointerY;
      });
    },
    [updatePageZoom]
  );

  useEffect(() => {
    const pageScroll = pageScrollRef.current;
    if (!pageScroll) {
      return;
    }

    pageScroll.addEventListener("wheel", handlePageWheelZoom, { capture: true, passive: false });
    return () => pageScroll.removeEventListener("wheel", handlePageWheelZoom, { capture: true });
  }, [handlePageWheelZoom]);

  const handleInsertBlankPage = useCallback(() => {
    if (!editor || readOnly) {
      return;
    }
    editor
      .chain()
      .focus()
      .insertContent([
        { type: "pageBreak" },
        { type: "paragraph" },
        { type: "pageBreak" },
        { type: "paragraph" },
      ])
      .run();
  }, [editor, readOnly]);

  const handleInsertCoverPage = useCallback(() => {
    if (!editor || readOnly) {
      return;
    }
    const coverTitle = document.title.trim() || "Unnamed lesson document";
    editor
      .chain()
      .focus("start")
      .insertContent([
        { type: "paragraph" },
        {
          type: "heading",
          attrs: { level: 1, textAlign: "center" },
          content: [{ type: "text", text: coverTitle }],
        },
        {
          type: "paragraph",
          attrs: { textAlign: "center" },
          content: [{ type: "text", text: "Lesson Notes" }],
        },
        {
          type: "paragraph",
          attrs: { textAlign: "center" },
          content: [{ type: "text", text: "Supplement the teaching objects, goals and usage scenarios here" }],
        },
        { type: "paragraph" },
        { type: "pageBreak" },
        { type: "paragraph" },
      ])
      .run();
  }, [document.title, editor, readOnly]);

  const handleInsertTableOfContents = useCallback(() => {
    if (!editor || readOnly) {
      return;
    }
    const headings: string[] = [];
    editor.state.doc.descendants((node) => {
      if (node.type.name === "heading") {
        const text = node.textContent.trim();
        if (text) {
          headings.push(text);
        }
      }
    });

    editor
      .chain()
      .focus("start")
      .insertContent([
        {
          type: "heading",
          attrs: { level: 2 },
          content: [{ type: "text", text: "Table of contents" }],
        },
        {
          type: "orderedList",
          content: (headings.length ? headings : ["After the title appears in the text, the table of contents page can be inserted again to automatically organize the structure."]).map(
            (heading) => ({
              type: "listItem",
              content: [
                {
                  type: "paragraph",
                  content: [{ type: "text", text: heading }],
                },
              ],
            })
          ),
        },
        { type: "paragraph" },
      ])
      .run();
  }, [editor, readOnly]);

  const handleInsertTextBox = useCallback(() => {
    if (!editor || readOnly) {
      return;
    }
    editor
      .chain()
      .focus()
      .insertContent({
        type: "blockquote",
        content: [
          {
            type: "paragraph",
            content: [{ type: "text", text: "Important note: Add definitions, reminders, or class notes here." }],
          },
        ],
      })
      .run();
  }, [editor, readOnly]);

  const handleInsertTable = useCallback(() => {
    if (!editor || readOnly) {
      return;
    }
    editor
      .chain()
      .focus()
      .insertTable({ rows: tableRows, cols: tableCols, withHeaderRow: tableHasHeaderRow })
      .run();
    setIsTableActive(true);
  }, [editor, readOnly, tableCols, tableHasHeaderRow, tableRows]);

  const handleInsertLink = useCallback(() => {
    if (!editor || readOnly) {
      return;
    }
    const selectedText = editor.state.doc
      .textBetween(editor.state.selection.from, editor.state.selection.to, " ")
      .trim();
    const hrefInput = window.prompt("Please enter the hyperlink address", "https://");
    const href = hrefInput?.trim() ?? "";
    if (!href) {
      return;
    }
    if (selectedText) {
      editor.chain().focus().extendMarkRange("link").setLink({ href }).run();
      return;
    }
    const labelInput = window.prompt("Please enter display text", "View profile");
    const label = labelInput?.trim() ?? "";
    if (!label) {
      return;
    }
    editor
      .chain()
      .focus()
      .insertContent({
        type: "text",
        text: label,
        marks: [{ type: "link", attrs: { href } }],
      })
      .run();
  }, [editor, readOnly]);

  const handleInsertHeaderFooter = useCallback(() => {
    if (readOnly) {
      return;
    }
    const nextHeader = window.prompt("Please enter header text, leave blank to clear", pageSettings.header_text);
    if (nextHeader === null) {
      return;
    }
    const nextFooter = window.prompt("Please enter footer text, leave blank to clear", pageSettings.footer_text);
    if (nextFooter === null) {
      return;
    }
    updatePageSettings({
      header_text: nextHeader.trim(),
      footer_text: nextFooter.trim(),
    });
  }, [pageSettings.footer_text, pageSettings.header_text, readOnly, updatePageSettings]);

  const handleInsertWatermark = useCallback(() => {
    if (readOnly) {
      return;
    }
    const nextWatermark = window.prompt("Please enter the watermark text, leave it blank to clear it", pageSettings.watermark_text || "internal lesson documents");
    if (nextWatermark === null) {
      return;
    }
    updatePageSettings({ watermark_text: nextWatermark.trim() });
  }, [pageSettings.watermark_text, readOnly, updatePageSettings]);

  const handleImageUpload = useCallback(
    (file: File) => {
      if (!editor || readOnly) {
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        const src = typeof reader.result === "string" ? reader.result : "";
        if (!src) {
          return;
        }
        editor.chain().focus().setImage({ src, alt: file.name }).run();
      };
      reader.readAsDataURL(file);
    },
    [editor, readOnly]
  );

  const tableInsertHint = `${tableRows} x ${tableCols}${tableHasHeaderRow ? "· Header" : ""}`;
  const tableInsertDisabled = !editor || readOnly;
  const tableEditDisabled = tableInsertDisabled || !isTableActive;

  const renderTableDimensionFields = (compact = true) => (
    <div
      className={clsx(
        "word-editor__table-dimensions flex items-center gap-1 rounded-lg border border-gray-200 bg-white text-gray-600",
        compact ? "h-9 px-2" : "h-[58px] px-2.5"
      )}
    >
      <Rows3 className="h-3.5 w-3.5 shrink-0" />
      <input
        type="number"
        min={TABLE_DIMENSION_MIN}
        max={TABLE_DIMENSION_MAX}
        value={tableRows}
        aria-label="Number of table rows"
        disabled={tableInsertDisabled}
        onChange={(event) => setTableRows(normalizeTableDimension(Number(event.target.value)))}
        className="w-9 border-0 bg-transparent text-center text-[12px] font-semibold outline-none disabled:cursor-not-allowed"
      />
      <span className="text-[10px] text-gray-300">x</span>
      <Columns3 className="h-3.5 w-3.5 shrink-0" />
      <input
        type="number"
        min={TABLE_DIMENSION_MIN}
        max={TABLE_DIMENSION_MAX}
        value={tableCols}
        aria-label="Number of table columns"
        disabled={tableInsertDisabled}
        onChange={(event) => setTableCols(normalizeTableDimension(Number(event.target.value)))}
        className="w-9 border-0 bg-transparent text-center text-[12px] font-semibold outline-none disabled:cursor-not-allowed"
      />
      <label
        title="Set the first row as header"
        className={clsx(
          "ml-1 flex items-center gap-1 border-l border-gray-100 pl-2 text-[11px] font-medium",
          tableInsertDisabled ? "cursor-not-allowed opacity-50" : "cursor-pointer"
        )}
      >
        <input
          type="checkbox"
          checked={tableHasHeaderRow}
          disabled={tableInsertDisabled}
          onChange={(event) => setTableHasHeaderRow(event.target.checked)}
          className="h-3.5 w-3.5 accent-black"
        />

        Header
      </label>
    </div>
  );

  const renderTableEditButtons = (compact = true) => {
    if (compact) {
      return (
        <div className="flex items-center gap-1 border-r border-gray-100 pr-4">
          <ToolbarButton
            title="Insert row above"
            disabled={tableEditDisabled}
            onClick={() => editor?.chain().focus().addRowBefore().run()}
          >
            <ChevronUp className="h-4 w-4" />
          </ToolbarButton>
          <ToolbarButton
            title="Insert row below"
            disabled={tableEditDisabled}
            onClick={() => editor?.chain().focus().addRowAfter().run()}
          >
            <ChevronDown className="h-4 w-4" />
          </ToolbarButton>
          <ToolbarButton
            title="Insert column left"
            disabled={tableEditDisabled}
            onClick={() => editor?.chain().focus().addColumnBefore().run()}
          >
            <ArrowLeft className="h-4 w-4" />
          </ToolbarButton>
          <ToolbarButton
            title="Insert column on the right"
            disabled={tableEditDisabled}
            onClick={() => editor?.chain().focus().addColumnAfter().run()}
          >
            <ArrowRight className="h-4 w-4" />
          </ToolbarButton>
          <ToolbarButton
            title="Merge cells"
            disabled={tableEditDisabled}
            onClick={() => editor?.chain().focus().mergeCells().run()}
          >
            <TableCellsMerge className="h-4 w-4" />
          </ToolbarButton>
          <ToolbarButton
            title="Split cells"
            disabled={tableEditDisabled}
            onClick={() => editor?.chain().focus().splitCell().run()}
          >
            <TableCellsSplit className="h-4 w-4" />
          </ToolbarButton>
          <ToolbarButton
            title="Delete table"
            disabled={tableEditDisabled}
            onClick={() => {
              onStructureRemovalIntent();
              editor?.chain().focus().deleteTable().run();
            }}
          >
            <Trash2 className="h-4 w-4" />
          </ToolbarButton>
        </div>
      );
    }

    return (
      <div className="flex items-center gap-2 border-r border-gray-100 pr-4">
        <RibbonActionButton
          title="Insert a line below"
          label="Add line"
          hint="Current form"
          icon={<Rows3 className="h-4 w-4" />}
          disabled={tableEditDisabled}
          onClick={() => editor?.chain().focus().addRowAfter().run()}
        />
        <RibbonActionButton
          title="Insert a column on the right"
          label="Gallie"
          hint="Current form"
          icon={<Columns3 className="h-4 w-4" />}
          disabled={tableEditDisabled}
          onClick={() => editor?.chain().focus().addColumnAfter().run()}
        />
        <RibbonActionButton
          title="Delete current row"
          label="Delete line"
          hint="Current form"
          icon={<Rows3 className="h-4 w-4" />}
          disabled={tableEditDisabled}
          onClick={() => editor?.chain().focus().deleteRow().run()}
        />
        <RibbonActionButton
          title="Delete current column"
          label="Delete column"
          hint="Current form"
          icon={<Columns3 className="h-4 w-4" />}
          disabled={tableEditDisabled}
          onClick={() => editor?.chain().focus().deleteColumn().run()}
        />
        <RibbonActionButton
          title="Merge cells"
          label="merge"
          hint="Select cells"
          icon={<TableCellsMerge className="h-4 w-4" />}
          disabled={tableEditDisabled}
          onClick={() => editor?.chain().focus().mergeCells().run()}
        />
        <RibbonActionButton
          title="Split cells"
          label="Split"
          hint="current cell"
          icon={<TableCellsSplit className="h-4 w-4" />}
          disabled={tableEditDisabled}
          onClick={() => editor?.chain().focus().splitCell().run()}
        />
        <RibbonActionButton
          title="Switch header row"
          label="header row"
          hint="Current form"
          icon={<PanelTop className="h-4 w-4" />}
          disabled={tableEditDisabled}
          onClick={() => editor?.chain().focus().toggleHeaderRow().run()}
        />
        <RibbonActionButton
          title="Delete table"
          label="Delete table"
          hint="Current form"
          icon={<Trash2 className="h-4 w-4" />}
          disabled={tableEditDisabled}
          onClick={() => {
            onStructureRemovalIntent();
            editor?.chain().focus().deleteTable().run();
          }}
        />
      </div>
    );
  };

  const renderHomeRibbon = () => (
    <>
      <div className="flex items-center gap-2 border-r border-gray-100 pr-4">
        <select
          disabled={!editor || readOnly}
          value={currentFontFamily}
          onChange={(event) => editor?.chain().focus().setFontFamily(event.target.value).run()}
          className="rounded-lg border border-gray-200 bg-white px-2.5 py-2 text-[12px] font-medium outline-none"
        >
          {FONT_FAMILY_OPTIONS.map((option) => (
            <option key={option.label} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <select
          disabled={!editor || readOnly}
          value={currentFontSize}
          onChange={(event) => editor?.chain().focus().setFontSize(`${event.target.value}px`).run()}
          className="rounded-lg border border-gray-200 bg-white px-2.5 py-2 text-[12px] font-medium outline-none"
        >
          {FONT_SIZE_OPTIONS.map((option) => (
            <option key={option.value} value={option.label}>
              {option.label}
            </option>
          ))}
        </select>
      </div>

      <div className="flex items-center gap-1 border-r border-gray-100 pr-4">
        <ToolbarButton
          title="Bold"
          active={editor?.isActive("bold")}
          disabled={!editor || readOnly}
          onClick={() => editor?.chain().focus().toggleBold().run()}
        >
          <Bold className="h-4 w-4" />
        </ToolbarButton>
        <ToolbarButton
          title="italics"
          active={editor?.isActive("italic")}
          disabled={!editor || readOnly}
          onClick={() => editor?.chain().focus().toggleItalic().run()}
        >
          <Italic className="h-4 w-4" />
        </ToolbarButton>
        <ToolbarButton
          title="Underline"
          active={editor?.isActive("underline")}
          disabled={!editor || readOnly}
          onClick={() => editor?.chain().focus().toggleUnderline().run()}
        >
          <Underline className="h-4 w-4" />
        </ToolbarButton>
        <ToolbarButton
          title="Highlight"
          active={editor?.isActive("highlight")}
          disabled={!editor || readOnly}
          onClick={() => editor?.chain().focus().toggleHighlight({ color: "#fef08a" }).run()}
        >
          <Highlighter className="h-4 w-4" />
        </ToolbarButton>
        <ToolbarButton
          title="text color"
          disabled={!editor || readOnly}
          onClick={() => editor?.chain().focus().setColor("#c2410c").run()}
        >
          <Type className="h-4 w-4" />
        </ToolbarButton>
      </div>

      <div className="flex items-center gap-1 border-r border-gray-100 pr-4">
        <ToolbarButton
          title="left aligned"
          active={editor?.isActive({ textAlign: "left" })}
          disabled={!editor || readOnly}
          onClick={() => editor?.chain().focus().setTextAlign("left").run()}
        >
          <AlignLeft className="h-4 w-4" />
        </ToolbarButton>
        <ToolbarButton
          title="center"
          active={editor?.isActive({ textAlign: "center" })}
          disabled={!editor || readOnly}
          onClick={() => editor?.chain().focus().setTextAlign("center").run()}
        >
          <AlignCenter className="h-4 w-4" />
        </ToolbarButton>
        <ToolbarButton
          title="Align right"
          active={editor?.isActive({ textAlign: "right" })}
          disabled={!editor || readOnly}
          onClick={() => editor?.chain().focus().setTextAlign("right").run()}
        >
          <AlignRight className="h-4 w-4" />
        </ToolbarButton>
        <ToolbarButton
          title="Quote"
          active={editor?.isActive("blockquote")}
          disabled={!editor || readOnly}
          onClick={() => editor?.chain().focus().toggleBlockquote().run()}
        >
          <Quote className="h-4 w-4" />
        </ToolbarButton>
      </div>

      <div className="flex items-center gap-1 border-r border-gray-100 pr-4">
        <ToolbarButton
          title="Bullets"
          active={editor?.isActive("bulletList")}
          disabled={!editor || readOnly}
          onClick={() => editor?.chain().focus().toggleBulletList().run()}
        >
          <List className="h-4 w-4" />
        </ToolbarButton>
        <ToolbarButton
          title="numbered list"
          active={editor?.isActive("orderedList")}
          disabled={!editor || readOnly}
          onClick={() => editor?.chain().focus().toggleOrderedList().run()}
        >
          <ListOrdered className="h-4 w-4" />
        </ToolbarButton>
      </div>

      <div className="flex items-center gap-2 border-r border-gray-100 pr-4">
        {renderTableDimensionFields()}
        <ToolbarButton
          title={`Insert ${tableInsertHint} table`}
          disabled={tableInsertDisabled}
          onClick={handleInsertTable}
        >
          <Table2 className="h-4 w-4" />
        </ToolbarButton>
      </div>

      {renderTableEditButtons()}

      <div className="flex items-center gap-1 border-r border-gray-100 pr-4">
        <ToolbarButton
          title="Cancel"
          disabled={!editor || readOnly}
          onClick={() => editor?.chain().focus().undo().run()}
        >
          <Undo2 className="h-4 w-4" />
        </ToolbarButton>
        <ToolbarButton
          title="Redo"
          disabled={!editor || readOnly}
          onClick={() => editor?.chain().focus().redo().run()}
        >
          <Redo2 className="h-4 w-4" />
        </ToolbarButton>
      </div>
    </>
  );

  const renderInsertRibbon = () => (
    <>
      <div className="flex items-center gap-2 border-r border-gray-100 pr-4">
        <RibbonActionButton
          title="insert blank page"
          label="blank page"
          hint="Pagination placeholder"
          icon={<FilePlus className="h-4 w-4" />}
          disabled={!editor || readOnly}
          onClick={handleInsertBlankPage}
        />
        <RibbonActionButton
          title="insert cover"
          label="cover"
          hint="Pinned template"
          icon={<LayoutTemplate className="h-4 w-4" />}
          disabled={!editor || readOnly}
          onClick={handleInsertCoverPage}
        />
        <RibbonActionButton
          title="Insert table of contents page"
          label="Contents page"
          hint="Generate by title"
          icon={<ClipboardList className="h-4 w-4" />}
          disabled={!editor || readOnly}
          onClick={handleInsertTableOfContents}
        />
      </div>

      <div className="flex items-center gap-2 border-r border-gray-100 pr-4">
        <RibbonActionButton
          title="Switch page number"
          label="page number"
          hint={pageSettings.show_page_number ? "Shown" : "Click to show"}
          icon={<Hash className="h-4 w-4" />}
          active={pageSettings.show_page_number}
          disabled={readOnly}
          onClick={() => updatePageSettings({ show_page_number: !pageSettings.show_page_number })}
        />
        <RibbonActionButton
          title="Set header and footer"
          label="header footer"
          hint="Edit copy"
          icon={<PanelTop className="h-4 w-4" />}
          active={Boolean(pageSettings.header_text || pageSettings.footer_text)}
          disabled={readOnly}
          onClick={handleInsertHeaderFooter}
        />
      </div>

      <div className="flex items-center gap-2 border-r border-gray-100 pr-4">
        <input
          ref={imageUploadRef}
          type="file"
          accept="image/*"
          className="hidden"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) {
              handleImageUpload(file);
            }
            event.currentTarget.value = "";
          }}
        />
        <RibbonActionButton
          title="Insert picture"
          label="picture"
          hint="Upload to lesson documents"
          icon={<ImagePlus className="h-4 w-4" />}
          disabled={!editor || readOnly}
          onClick={() => imageUploadRef.current?.click()}
        />
        {renderTableDimensionFields(false)}
        <RibbonActionButton
          title={`Insert ${tableInsertHint} table`}
          label="sheet"
          hint={tableInsertHint}
          icon={<Table2 className="h-4 w-4" />}
          disabled={tableInsertDisabled}
          onClick={handleInsertTable}
        />
        <RibbonActionButton
          title="Insert text box"
          label="text box"
          hint="Important side note"
          icon={<TextCursorInput className="h-4 w-4" />}
          disabled={!editor || readOnly}
          onClick={handleInsertTextBox}
        />
      </div>

      {renderTableEditButtons(false)}

      <div className="flex items-center gap-2 border-r border-gray-100 pr-4">
        <RibbonActionButton
          title="Insert hyperlink"
          label="hyperlink"
          hint="external information"
          icon={<LinkIcon className="h-4 w-4" />}
          disabled={!editor || readOnly}
          onClick={handleInsertLink}
        />
        <RibbonActionButton
          title="Insert watermark"
          label="watermark"
          hint="Page ID"
          icon={<Stamp className="h-4 w-4" />}
          active={Boolean(pageSettings.watermark_text)}
          disabled={readOnly}
          onClick={handleInsertWatermark}
        />
      </div>
    </>
  );

  const renderPageRibbon = () => (
    <>
      <div className="flex items-center gap-2 border-r border-gray-100 pr-4">
        {PAGE_MARGIN_OPTIONS.map((option) => (
          <RibbonActionButton
            key={option.value}
            title={`Page margins: ${option.label}`}
            label={option.label}
            hint="page margins"
            icon={<AlignHorizontalSpaceAround className="h-4 w-4" />}
            active={pageSettings.margin_preset === option.value}
            disabled={readOnly}
            onClick={() => updatePageSettings({ margin_preset: option.value })}
          />
        ))}
      </div>

      <div className="flex items-center gap-2 border-r border-gray-100 pr-4">
        <RibbonActionButton
          title="vertical layout"
          label="portrait"
          hint="paper orientation"
          icon={<RectangleVertical className="h-4 w-4" />}
          active={pageSettings.orientation === "portrait"}
          disabled={readOnly}
          onClick={() => updatePageSettings({ orientation: "portrait" })}
        />
        <RibbonActionButton
          title="Horizontal layout"
          label="Horizontal"
          hint="paper orientation"
          icon={<RectangleHorizontal className="h-4 w-4" />}
          active={pageSettings.orientation === "landscape"}
          disabled={readOnly}
          onClick={() => updatePageSettings({ orientation: "landscape" })}
        />
      </div>

      <div className="flex items-center gap-2 border-r border-gray-100 pr-4">
        {PAGE_SIZE_OPTIONS.map((option) => (
          <RibbonActionButton
            key={option.value}
            title={`Paper size: ${option.label}`}
            label={option.label}
            hint="paper size"
            icon={<Files className="h-4 w-4" />}
            active={pageSettings.page_size === option.value}
            disabled={readOnly}
            onClick={() => updatePageSettings({ page_size: option.value })}
          />
        ))}
      </div>

      <div className="flex items-center gap-2 border-r border-gray-100 pr-4">
        <RibbonActionButton
          title="single column layout"
          label="single column"
          hint="Column"
          icon={<FileText className="h-4 w-4" />}
          active={pageSettings.columns === 1}
          disabled={readOnly}
          onClick={() => updatePageSettings({ columns: 1 })}
        />
        <RibbonActionButton
          title="Double column layout"
          label="double column"
          hint="Column"
          icon={<Columns2 className="h-4 w-4" />}
          active={pageSettings.columns === 2}
          disabled={readOnly}
          onClick={() => updatePageSettings({ columns: 2 })}
        />
      </div>

      <div className="flex items-center gap-2 border-r border-gray-100 pr-4">
        <RibbonActionButton
          title="page border"
          label="page border"
          hint={pageSettings.page_border ? "Already turned on" : "Closed"}
          icon={<Frame className="h-4 w-4" />}
          active={pageSettings.page_border}
          disabled={readOnly}
          onClick={() => updatePageSettings({ page_border: !pageSettings.page_border })}
        />
        <RibbonActionButton
          title="Line number"
          label="Line number"
          hint={pageSettings.line_numbers ? "Shown" : "Click to show"}
          icon={<ListOrdered className="h-4 w-4" />}
          active={pageSettings.line_numbers}
          disabled={readOnly}
          onClick={() => updatePageSettings({ line_numbers: !pageSettings.line_numbers })}
        />
      </div>

      <div className="flex items-center gap-2 border-r border-gray-100 pr-4">
        {PAGE_BACKGROUND_OPTIONS.map((option) => (
          <RibbonActionButton
            key={option.value}
            title={`Page background: ${option.label}`}
            label={option.label}
            hint="background"
            icon={<PaintBucket className="h-4 w-4" />}
            active={pageSettings.background_style === option.value}
            disabled={readOnly}
            onClick={() => updatePageSettings({ background_style: option.value })}
          />
        ))}
      </div>
    </>
  );

  return (
    <div className="word-editor flex min-h-0 flex-1 flex-col overflow-hidden">
      <div
        className={clsx(
          "shrink-0 overflow-hidden transition-all duration-300",
          toolbarCollapsed ? "max-h-0 opacity-0" : "max-h-52 opacity-100"
        )}
        aria-hidden={toolbarCollapsed}
      >
        <div className={clsx("word-editor__toolbar border-b border-gray-200 bg-white", readOnly && "bg-gray-50")}>
          <div className="flex h-10 items-center border-b border-gray-100 px-6">
            <RibbonTabButton active={activeRibbonTab === "home"} onClick={() => setActiveRibbonTab("home")}>
              <PencilLine className="h-3.5 w-3.5" />

              HOME
            </RibbonTabButton>
            <RibbonTabButton active={activeRibbonTab === "insert"} onClick={() => setActiveRibbonTab("insert")}>
              <FilePlus className="h-3.5 w-3.5" />

              INSERT
            </RibbonTabButton>
            <RibbonTabButton active={activeRibbonTab === "page"} onClick={() => setActiveRibbonTab("page")}>
              <Files className="h-3.5 w-3.5" />

              Page(PAGE)
            </RibbonTabButton>
          </div>
          <div className="custom-scrollbar flex items-center gap-3 overflow-x-auto px-5 py-3 whitespace-nowrap">
            {activeRibbonTab === "home" ? renderHomeRibbon() : null}
            {activeRibbonTab === "insert" ? renderInsertRibbon() : null}
            {activeRibbonTab === "page" ? renderPageRibbon() : null}

            <div className="ml-auto flex items-center gap-2">
              <WordPageZoomControls
                value={pageZoom}
                onChange={updatePageZoom}
                onFitToWidth={fitPageToWidth}
              />
              <input
                ref={importRef}
                type="file"
                accept=".docx"
                className="hidden"
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) {
                    onImportDocx(file);
                  }
                  event.currentTarget.value = "";
                }}
              />
              <button
                type="button"
                onClick={() => importRef.current?.click()}
                className="inline-flex h-10 items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 text-[11px] font-bold uppercase tracking-wider text-gray-600 transition hover:border-gray-300"
              >
                <Upload className="h-4 w-4" />

                Import DOCX
              </button>
              <button
                type="button"
                onClick={onExportDocx}
                className="inline-flex h-10 items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 text-[11px] font-bold uppercase tracking-wider text-gray-600 transition hover:border-gray-300"
              >
                <Download className="h-4 w-4" />

                Export DOCX
              </button>
              <button
                type="button"
                onClick={onExportHtml}
                className="inline-flex h-10 items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 text-[11px] font-bold uppercase tracking-wider text-gray-600 transition hover:border-gray-300"
              >
                <FileText className="h-4 w-4" />

                Export HTML
              </button>
              <input
                ref={ridocImportRef}
                type="file"
                accept={RIDOC_FILE_ACCEPT}
                className="hidden"
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) {
                    onImportRidoc(file);
                  }
                  event.currentTarget.value = "";
                }}
              />
              <button
                type="button"
                onClick={onExportRidoc}
                className="inline-flex h-10 items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 text-[11px] font-bold uppercase tracking-wider text-gray-600 transition hover:border-gray-300"
              >
                <Download className="h-4 w-4" />

                Export course package
              </button>
            </div>
          </div>
        </div>
      </div>

      <div className="relative min-h-0 flex-1" data-board-viewport="true">
        <BoardDocumentOutline key={document.id} editor={editor} scrollContainerRef={pageScrollRef} />
        <div
          ref={pageScrollRef}
          data-board-scroll-container="true"
          className="word-editor__canvas h-full overflow-auto bg-[radial-gradient(circle_at_top,#f7f5ef,transparent_28%),linear-gradient(180deg,#f3f0e7_0%,#eef2f8_100%)]"
        >
          <div className="mx-auto flex w-max min-w-full justify-center px-6 py-10 md:px-10">
            <div
              className={clsx(
                "word-editor__page word-editor__page--zoomable relative flex shrink-0 flex-col overflow-hidden",
                !pageSettings.page_border && "word-editor__page--borderless",
                pageSettings.background_style === "warm" && "word-editor__page--warm",
                pageSettings.background_style === "grid" && "word-editor__page--grid",
                pageSettings.columns === 2 && "word-editor__page--columns-2",
                pageSettings.line_numbers && "word-editor__page--line-numbers"
              )}
              style={pageStyle}
            >
              {pageSettings.watermark_text ? (
                <div className="word-editor__watermark pointer-events-none select-none">{pageSettings.watermark_text}</div>
              ) : null}
              {pageSettings.header_text ? (
                <div className="word-editor__chrome word-editor__chrome--header" style={pageChromeStyle}>
                  <span>{pageSettings.header_text}</span>
                </div>
              ) : null}
              <div className="word-editor__title-row border-b border-[#ece4d9]" style={titleStyle}>
                <input
                  aria-label="Document title"
                  value={document.title}
                  disabled={readOnly}
                  onChange={(event) => onDocumentChange({ ...document, title: event.target.value })}
                  className="word-editor__title w-full border-0 bg-transparent text-[34px] font-semibold tracking-tight text-[#1a1a1a] outline-none placeholder:text-gray-300"
                  placeholder="Unnamed lesson document"
                />
              </div>
              <div className="flex-1" style={contentStyle}>
                {activeFormulaSelection && onFormulaInkSubmit ? (
                  <FormulaInkPopover
                    position={activeFormulaSelection.position}
                    sourceLatex={activeFormulaSelection.latex}
                    disabled={readOnly}
                    onReference={() => onFormulaReference?.(activeFormulaSelection)}
                    onGeometryReference={() => onFormulaGeometryReference?.(activeFormulaSelection)}
                    onSubmit={(payload) =>
                      onFormulaInkSubmit({
                        ...payload,
                        selection: activeFormulaSelection,
                      })
                    }
                  />
                ) : null}
                <EditorContent editor={editor} />
              </div>
              {pageSettings.footer_text || currentPageNumberLabel ? (
                <div className="word-editor__chrome word-editor__chrome--footer" style={pageChromeStyle}>
                  <span>{pageSettings.footer_text}</span>
                  <span>{currentPageNumberLabel}</span>
                </div>
              ) : null}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
