import type {
  SelectionRef,
  SourceCatalogView,
  SourceChapter,
  SourceIngestionRecord,
  RepositoryMapNode,
  RepositoryMapView,
  SourceRange,
} from "../../types";
import { sourceRangeDisplayLabel } from "../../lib/source-range-display";

export function createOpenNotebookSourceSelection(source: SourceIngestionRecord): SelectionRef {
  return {
    kind: "source",
    excerpt: `《${source.title}》`,
    source_ingestion_id: source.id,
    source_title: source.title,
    source_uri: source.source_uri,
    source_scope_kind: "source",
    source_content_hash: metadataContentHash(source),
  };
}

export const createWholeSourceSelection = createOpenNotebookSourceSelection;

export function createRepositoryNodeSelection(
  source: SourceIngestionRecord,
  map: RepositoryMapView,
  node: RepositoryMapNode
): SelectionRef {
  const location = node.path || node.title;
  return {
    kind: "source",
    excerpt: `《${source.title}》 · ${node.title} · ${map.snapshot.resolved_commit_sha.slice(0, 12)}`,
    heading_path: [node.title],
    source_ingestion_id: source.id,
    source_title: source.title,
    source_uri: source.source_uri,
    source_chapter_id: node.id,
    source_chapter_title: node.title,
    source_locator: location,
    source_scope_kind: "repository_node",
    source_repository_node_id: node.id,
    source_repository_tree_kind: node.tree_kind,
    source_content_hash: map.snapshot.manifest_hash,
  };
}

export function createSourceChapterSelection(
  source: SourceIngestionRecord,
  chapter: SourceChapter,
  catalog?: SourceCatalogView | null
): SelectionRef {
  const chapterLabel = sourceChapterLabel(chapter);
  const path = chapter.path.length ? chapter.path.join(" > ") : chapterLabel;
  const sourceRange = sourceChapterRange(chapter);
  const rangeLabel = sourceRangeDisplayLabel(sourceRange) || sourceChapterPageRange(chapter);
  const pdfStart = sourceRange?.kind === "pdf_pages" && typeof sourceRange.start === "number"
    ? sourceRange.start
    : chapter.page_start;
  // The explicit range is inclusive; the legacy selection field is exclusive.
  const pdfEnd = sourceRange?.kind === "pdf_pages" && typeof sourceRange.end === "number"
    ? sourceRange.end + 1
    : chapter.page_end ?? undefined;
  return {
    kind: "source",
    excerpt: [`《${source.title}》`, path, rangeLabel].filter(Boolean).join(" · "),
    heading_path: chapter.path,
    source_ingestion_id: source.id,
    source_title: source.title,
    source_uri: source.source_uri,
    source_chapter_id: chapter.id,
    source_chapter_number: chapter.number,
    source_chapter_title: chapter.title,
    source_page_range: rangeLabel,
    source_locator: chapter.source_locator,
    source_page_start: pdfStart,
    source_page_end: pdfEnd,
    source_scope_kind: "chapter",
    source_range: sourceRange,
    catalog_version: chapter.catalog_version ?? catalog?.catalog_version ?? null,
    source_content_hash: chapter.source_content_hash || catalog?.source_content_hash || "",
  };
}

export function sourceChapterRange(chapter: SourceChapter): SourceRange | null {
  if (chapter.range) {
    return chapter.range;
  }
  if (chapter.page_start == null) {
    return null;
  }
  const inclusiveEnd = Math.max(
    chapter.page_start,
    (chapter.page_end ?? chapter.page_start + 1) - 1
  );
  return {
    kind: "pdf_pages",
    start: chapter.page_start,
    end: inclusiveEnd,
    container: "",
    start_anchor: "",
    end_anchor: "",
    path: chapter.path,
    display_label:
      inclusiveEnd === chapter.page_start
        ? `p. ${chapter.page_start}`
        : `pp. ${chapter.page_start}-${inclusiveEnd}`,
    end_inclusive: true,
    metadata: { compatibility_source: "legacy_exclusive_page_end" },
  };
}

export function sourceChapterLabel(chapter: SourceChapter) {
  const title = chapter.title.trim();
  if (
    !chapter.number ||
    title.startsWith(chapter.number) ||
    /^第\s*[0-9一二三四五六七八九十百零〇两]+\s*[章节篇部]/.test(title)
  ) {
    return title;
  }
  return `${chapter.number} ${title}`.trim();
}

function sourceChapterPageRange(chapter: SourceChapter) {
  const authoritativeLabel = sourceRangeDisplayLabel(chapter.range);
  if (authoritativeLabel) {
    return authoritativeLabel;
  }
  if (chapter.page_start == null) {
    return "";
  }
  const displayEnd = Math.max(chapter.page_start, (chapter.page_end ?? chapter.page_start + 1) - 1);
  if (displayEnd === chapter.page_start) {
    return `p. ${chapter.page_start}`;
  }
  return `pp. ${chapter.page_start}-${displayEnd}`;
}

function metadataContentHash(source: SourceIngestionRecord) {
  const value = source.metadata?.content_hash ?? source.metadata?.source_content_hash;
  return typeof value === "string" ? value.trim() : "";
}
