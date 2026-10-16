import { isManagedSource } from "../../lib/source-backend";
import type { SourceCatalogView, SourceChapter, SourceIngestionRecord } from "../../types";

export function hasVerifiedChapterRange(chapter: SourceChapter, catalog: SourceCatalogView): boolean {
  const range = chapter.range;
  const state = chapter.metadata?.location_state;
  if (!range || chapter.anchor_status !== "verified" || state === "outside_uploaded_file" ||
      state === "position_only" || state === "unresolved") return false;
  if (range.kind === "pdf_pages" && (typeof range.start !== "number" || !Number.isInteger(range.start) || range.start < 1 ||
      typeof range.end !== "number" || !Number.isInteger(range.end) || range.end < range.start)) return false;
  if (chapter.source_content_hash && chapter.source_content_hash !== catalog.source_content_hash ||
      chapter.catalog_version != null && chapter.catalog_version !== catalog.catalog_version) return false;
  const mappedCatalogs = ["native_directory_v1", "hybrid_directory_v1", "ai_directory_v1", "codex_directory_v1",
    "agent_catalog_v2", "agent_catalog_v3", "anythingllm_direct_catalog_v1", "pi_catalog_refinement_v1"];
  return ![catalog.strategy, catalog.catalog_schema_version, catalog.task_contract].some(
    value => value != null && mappedCatalogs.includes(value)) || chapter.mapping_status === "verified";
}

export function hasCompleteCitableCatalog(catalog: SourceCatalogView | null | undefined): boolean {
  if (!catalog || catalog.status !== "ready" || catalog.catalog_version < 1 || !catalog.source_content_hash ||
      catalog.directory_status !== "complete" || catalog.directory_gaps?.length || catalog.unresolved_node_count ||
      catalog.chapter_count < 1 || catalog.chapters.length !== catalog.chapter_count ||
      catalog.verified_chapter_count !== catalog.chapter_count) return false;
  return catalog.chapters.every((chapter) => chapter.mapping_status === "verified" &&
    chapter.anchor_status === "verified" && chapter.range != null &&
    chapter.source_content_hash === catalog.source_content_hash && chapter.catalog_version === catalog.catalog_version);
}

export function hasCompleteMappedCatalog(catalog: SourceCatalogView | null | undefined): boolean {
  if (!catalog || catalog.status !== "ready" || catalog.catalog_version < 1 || !catalog.source_content_hash ||
      catalog.directory_status !== "complete" || catalog.directory_gaps?.length ||
      catalog.chapter_count < 1 || catalog.chapters.length !== catalog.chapter_count) return false;
  const mapped = catalog.chapters.filter(chapter => hasVerifiedChapterRange(chapter, catalog));
  const outside = catalog.chapters.filter(chapter => chapter.metadata?.location_state === "outside_uploaded_file" && chapter.range == null);
  // Older API summaries count every non-citable row, including explained outside entries.
  return Number.isInteger(catalog.unresolved_node_count) && catalog.unresolved_node_count >= 0 &&
    catalog.unresolved_node_count <= outside.length && mapped.length === catalog.verified_chapter_count && catalog.chapters.every(chapter =>
    chapter.source_content_hash === catalog.source_content_hash && chapter.catalog_version === catalog.catalog_version &&
    (hasVerifiedChapterRange(chapter, catalog) ||
      chapter.metadata?.location_state === "outside_uploaded_file" && chapter.range == null));
}

export function sourceCatalogPresentation(source: SourceIngestionRecord, catalog?: SourceCatalogView | null) {
  const complete = hasCompleteMappedCatalog(catalog);
  const google = isManagedSource(source);
  const failed = !complete && google && (catalog?.status === "failed" ||
    ["failed", "partial", "unavailable"].includes(String(source.metadata.catalog_status)));
  const nativeTextReady = source.status === "ready" && (source.metadata.native_text_ready === true ||
    ["ready", "visual"].includes(String(source.metadata.basic_read_status)));
  return { complete, failed,
    label: google && source.status === "ready" && !complete ? (nativeTextReady ? "Questions available" : failed ? "Catalog failed" : "Processing catalog") : null,
    message: complete ? "The complete catalog is available. Entries outside the uploaded file are marked separately."
      : nativeTextReady ? (failed ? "Questions about verified text are available. Catalog verification failed; retry to finish the catalog."
      : "Questions about verified text are available. The complete, citable catalog is still being prepared.")
      : failed ? "Catalog verification did not finish. Retry processing to create a complete, citable catalog."
      : "Verifying the complete catalog and its page locations." };
}
