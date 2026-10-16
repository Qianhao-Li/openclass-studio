import { isManagedSource } from "../../lib/source-backend";
import type { SelectionRef, SourceIngestionRecord, SourceQueryScope } from "../../types";
import type { LessonComposerState } from "./history-utils";

export function sourceSupportsOriginalReference(source: SourceIngestionRecord): boolean {
  const metadata = source.metadata;
  return isManagedSource(source) && source.status === "ready" &&
    (metadata.catalog_ready === true || metadata.native_text_ready === true ||
      ["ready", "visual"].includes(String(metadata.basic_read_status))) &&
    typeof metadata.content_hash === "string" && Boolean(metadata.content_hash.trim()) &&
    (metadata.original_read_ready === true ||
      (Array.isArray(metadata.capabilities) && metadata.capabilities.includes("read_original")));
}

function validSourceSelection(selection: SelectionRef) {
  return (
    selection.kind === "source" &&
    Boolean(selection.source_ingestion_id?.trim()) &&
    Boolean(selection.source_content_hash?.trim())
  );
}

function selectionKey(selection: SelectionRef) {
  return [
    selection.source_ingestion_id,
    selection.source_scope_kind,
    selection.source_repository_node_id,
    selection.source_chapter_id,
    selection.source_page_start,
    selection.source_page_end,
  ].join(":");
}

export function appendSourceQuerySelection(
  current: SelectionRef[],
  next: SelectionRef
): SelectionRef[] {
  if (!validSourceSelection(next)) {
    return current;
  }
  const nextKind = next.source_scope_kind ?? (next.source_chapter_id ? "chapter" : "source");
  const compatible = current.filter(
    (item) => validSourceSelection(item) && (item.source_scope_kind ?? (item.source_chapter_id ? "chapter" : "source")) === nextKind
  );
  const withoutSameSourceScope = compatible.filter((item) => {
    if (selectionKey(item) === selectionKey(next)) {
      return false;
    }
    return !(nextKind === "source" && item.source_ingestion_id === next.source_ingestion_id);
  });
  return [...withoutSameSourceScope, next];
}

export function applySourceReferenceToComposerState(
  current: LessonComposerState,
  sourceReference: SelectionRef
): LessonComposerState {
  if (sourceReference.source_scope_kind === "repository_node") {
    return {
      ...current,
      composerMode: "ask",
      includeSelectionInPrompt: true,
      composerSelection: sourceReference,
      composerSelections: [sourceReference],
      sourceQuerySelections: [],
      sourceQueryAllReady: false,
    };
  }
  return {
    ...current,
    composerMode: "ask",
    includeSelectionInPrompt: true,
    composerSelection: null,
    composerSelections: [],
    sourceQuerySelections: appendSourceQuerySelection(
      current.sourceQueryAllReady ? [] : current.sourceQuerySelections,
      sourceReference
    ),
    sourceQueryAllReady: false,
  };
}

export function sourceQueryScopeFromComposer(
  selections: SelectionRef[],
  allReadySources: boolean
): SourceQueryScope | null {
  if (allReadySources) {
    return { mode: "all_ready_sources", refs: [] };
  }
  const valid = selections.filter(validSourceSelection);
  if (!valid.length) {
    return null;
  }
  if (valid.some((selection) => selection.source_scope_kind === "repository_node")) {
    // Repository nodes are frozen through the full SelectionRef contract.
    // Sending them as a generic source query scope discards the node identity
    // and is rejected because repository selections also carry a node ID.
    return null;
  }
  const kinds = new Set(
    valid.map((selection) => selection.source_scope_kind ?? (selection.source_chapter_id ? "chapter" : "source"))
  );
  if (kinds.size !== 1) {
    return null;
  }
  const kind = [...kinds][0];
  const refs = valid.map((selection) => {
    // Chapter SelectionRef retains a legacy exclusive page_end. SourceQueryRef
    // uses inclusive pages, so take PDF bounds from the canonical catalog range.
    const pdfRange = kind === "chapter" && selection.source_range?.kind === "pdf_pages" ? selection.source_range : null;
    const pageStart = pdfRange?.start ?? selection.source_page_start;
    const pageEnd = pdfRange?.end ?? selection.source_page_end;
    return {
      source_ingestion_id: selection.source_ingestion_id!,
      source_content_hash: selection.source_content_hash!,
      ...(selection.source_chapter_id ? { source_chapter_id: selection.source_chapter_id } : {}),
      ...(selection.source_chapter_id && typeof selection.catalog_version === "number" && selection.catalog_version > 0
        ? { catalog_version: selection.catalog_version } : {}),
      ...(typeof pageStart === "number" ? { page_start: pageStart } : {}),
      ...(typeof pageEnd === "number" ? { page_end: pageEnd } : {}),
    };
  });
  if (kind === "page_range") {
    return { mode: "page_range", refs };
  }
  if (kind === "chapter") {
    return { mode: "chapter", refs };
  }
  return { mode: refs.length === 1 ? "source" : "sources", refs };
}

export function sourceQuerySelectionLabel(selection: SelectionRef) {
  if (selection.source_scope_kind === "repository_node") {
    return selection.source_chapter_title || selection.source_locator || selection.excerpt;
  }
  if (selection.source_scope_kind === "chapter") {
    return selection.source_chapter_title || selection.excerpt;
  }
  if (selection.source_scope_kind === "page_range") {
    return `${selection.source_title || "material"} · ${selection.source_page_range || "Specify page segment"}`;
  }
  return selection.source_title || selection.excerpt;
}
