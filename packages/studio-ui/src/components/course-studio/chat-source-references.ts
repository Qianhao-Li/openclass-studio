import type { CommitRecord, SelectionRef, SourceCitation } from "../../types";

export function sourceCitationDisplayNumber(citation: SourceCitation, legacyIndex: number): number {
  return typeof citation.citation_number === "number" && Number.isInteger(citation.citation_number) && citation.citation_number > 0
    ? citation.citation_number : legacyIndex + 1;
}

export function sourceCitationsFromMetadata(value: unknown): SourceCitation[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.flatMap((item) => {
    if (!item || typeof item !== "object") {
      return [];
    }
    const citation = item as Partial<SourceCitation>;
    if (
      typeof citation.evidence_id !== "string" ||
      typeof citation.source_id !== "string" ||
      typeof citation.source_title !== "string" ||
      typeof citation.excerpt !== "string" ||
      typeof citation.source_content_hash !== "string" ||
      typeof citation.parser_run_id !== "string"
    ) {
      return [];
    }
    return [{
      evidence_id: citation.evidence_id,
      citation_number: typeof citation.citation_number === "number" && Number.isInteger(citation.citation_number) && citation.citation_number > 0
        ? citation.citation_number : undefined,
      evidence_ref: typeof citation.evidence_ref === "string" ? citation.evidence_ref : undefined,
      source_document_id: typeof citation.source_document_id === "string" ? citation.source_document_id : undefined,
      snapshot_url: typeof citation.snapshot_url === "string" ? citation.snapshot_url : undefined,
      source_id: citation.source_id,
      source_title: citation.source_title,
      section_path: Array.isArray(citation.section_path)
        ? citation.section_path.filter((part): part is string => typeof part === "string")
        : [],
      page_start: typeof citation.page_start === "number" ? citation.page_start : null,
      page_end: typeof citation.page_end === "number" ? citation.page_end : null,
      excerpt: citation.excerpt,
      chunk_ids: Array.isArray(citation.chunk_ids)
        ? citation.chunk_ids.filter((chunk): chunk is string => typeof chunk === "string")
        : [],
      block_ids: Array.isArray(citation.block_ids)
        ? citation.block_ids.filter((block): block is string => typeof block === "string")
        : undefined,
      bbox: Array.isArray(citation.bbox)
        ? citation.bbox.filter((coordinate): coordinate is number => typeof coordinate === "number")
        : [],
      source_content_hash: citation.source_content_hash,
      parser_run_id: citation.parser_run_id,
    }];
  });
}

function sourceSelectionsFromFrozenRequirement(value: unknown): SelectionRef[] {
  if (!value || typeof value !== "object") {
    return [];
  }
  const grounding = (value as Record<string, unknown>).source_grounding;
  if (!grounding || typeof grounding !== "object") {
    return [];
  }
  const references = (grounding as Record<string, unknown>).confirmed_references;
  if (!Array.isArray(references)) {
    return [];
  }
  return references.flatMap((item) => {
    if (!item || typeof item !== "object") {
      return [];
    }
    const reference = item as Record<string, unknown>;
    const sourceTitle = typeof reference.source_title === "string" ? reference.source_title.trim() : "";
    const chapterTitle = typeof reference.chapter_title === "string" ? reference.chapter_title.trim() : "";
    const pageRange = typeof reference.page_range === "string" ? reference.page_range.trim() : "";
    const excerpt = [sourceTitle, chapterTitle, pageRange].filter(Boolean).join(" · ");
    if (!excerpt) {
      return [];
    }
    return [{
      kind: "source" as const,
      excerpt,
      heading_path: Array.isArray(reference.section_path)
        ? reference.section_path.filter((part): part is string => typeof part === "string")
        : [],
      source_ingestion_id: typeof reference.source_ingestion_id === "string" ? reference.source_ingestion_id : null,
      source_title: sourceTitle,
      source_chapter_id: typeof reference.source_chapter_id === "string" ? reference.source_chapter_id : null,
      source_chapter_number: typeof reference.chapter_number === "string" ? reference.chapter_number : "",
      source_chapter_title: chapterTitle,
      source_page_range: pageRange,
      source_page_start: typeof reference.page_start === "number" ? reference.page_start : null,
      source_page_end: typeof reference.page_end === "number" ? reference.page_end : null,
      source_scope_kind: reference.scope_kind === "page_range" ? "page_range" : "chapter",
      source_content_hash: typeof reference.content_hash === "string" ? reference.content_hash : "",
    }];
  });
}

function sourceSelectionsFromQueryScope(commit: CommitRecord): SelectionRef[] {
  const value = commit.metadata?.source_query_scope;
  if (!value || typeof value !== "object") {
    return [];
  }
  const scope = value as Record<string, unknown>;
  if (scope.mode === "all_ready_sources") {
    return [{ kind: "source", excerpt: "All available sources in this course", source_scope_kind: "source" }];
  }
  if (!Array.isArray(scope.refs)) {
    return [];
  }
  const citations = sourceCitationsFromMetadata(commit.metadata?.source_citations);
  const usedCitationCounts = new Map<string, number>();
  return scope.refs.flatMap((item) => {
    if (!item || typeof item !== "object") {
      return [];
    }
    const reference = item as Record<string, unknown>;
    const sourceId = typeof reference.source_ingestion_id === "string" ? reference.source_ingestion_id : "";
    if (!sourceId) {
      return [];
    }
    const sourceCitations = citations.filter((candidate) => candidate.source_id === sourceId);
    const usedCitationCount = usedCitationCounts.get(sourceId) ?? 0;
    const citation = sourceCitations[usedCitationCount] ?? sourceCitations[0];
    usedCitationCounts.set(sourceId, usedCitationCount + 1);
    const sourceTitle = citation?.source_title.trim() ?? "";
    const headingPath = citation?.section_path ?? [];
    const chapterTitle = headingPath.at(-1) ?? "";
    const pageStart = typeof reference.page_start === "number" ? reference.page_start : citation?.page_start ?? null;
    const pageEnd = typeof reference.page_end === "number" ? reference.page_end : citation?.page_end ?? null;
    const pageRange = pageStart
      ? pageEnd && pageEnd !== pageStart
        ? `Pages ${pageStart}–${pageEnd}`
        : `Page ${pageStart}`
      : "";
    const fallbackLabel = scope.mode === "page_range" ? "Referenced source pages" : "Referenced source chapter";
    return [{
      kind: "source" as const,
      excerpt: [sourceTitle, headingPath.join(" · "), pageRange].filter(Boolean).join(" · ") || fallbackLabel,
      heading_path: headingPath,
      source_ingestion_id: sourceId,
      source_title: sourceTitle,
      source_chapter_id: typeof reference.source_chapter_id === "string" ? reference.source_chapter_id : null,
      source_chapter_title: chapterTitle,
      source_page_range: pageRange,
      source_page_start: pageStart,
      source_page_end: pageEnd,
      source_scope_kind: scope.mode === "page_range" ? "page_range" : scope.mode === "chapter" ? "chapter" : "source",
      source_content_hash: typeof reference.source_content_hash === "string" ? reference.source_content_hash : "",
    }];
  });
}

export function sourceSelectionsFromCommit(commit: CommitRecord): SelectionRef[] {
  const frozenSelections = sourceSelectionsFromFrozenRequirement(commit.metadata?.frozen_requirement_payload);
  return frozenSelections.length ? frozenSelections : sourceSelectionsFromQueryScope(commit);
}
