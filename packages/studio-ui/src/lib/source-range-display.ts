import type { SourceRange } from "../types";

const EPUB_SPINE_LABEL_RE = /^EPUB spine\s+(\d+)(?:-(\d+))?$/i;
const EPUB_ARCHIVE_MEMBER_RE = /(?:^|\/)[^/#]+\.(?:xhtml?|html?)(?:#.*)?$/i;

export function sourceRangeDisplayLabel(sourceRange?: SourceRange | null): string {
  if (!sourceRange) {
    return "";
  }
  const storedLabel = sourceRange.display_label.trim();
  if (sourceRange.kind === "pdf_pages" && !storedLabel) {
    const start = numericEndpoint(sourceRange.start);
    const end = numericEndpoint(sourceRange.end);
    if (start != null && end != null && start > 0 && end >= start) {
      return start === end ? `PDF p. ${start}` : `PDF pp. ${start}-${end}`;
    }
  }
  if (sourceRange.kind !== "epub_spine") {
    return storedLabel;
  }

  const numericStart = numericEndpoint(sourceRange.start);
  const numericEnd = numericEndpoint(sourceRange.end);
  if (numericStart != null) {
    const displayOffset = sourceRange.metadata.index_base === 0 ? 1 : 0;
    const displayStart = numericStart + displayOffset;
    const displayEnd = Math.max(displayStart, (numericEnd ?? numericStart) + displayOffset);
    return displayStart === displayEnd
      ? `EPUB location ${displayStart}`
      : `EPUB locations ${displayStart}-${displayEnd}`;
  }

  const storedSpineMatch = EPUB_SPINE_LABEL_RE.exec(storedLabel);
  if (storedSpineMatch) {
    const displayOffset = sourceRange.metadata.index_base === 0 ? 1 : 0;
    const displayStart = Number(storedSpineMatch[1]) + displayOffset;
    const displayEnd = Number(storedSpineMatch[2] ?? storedSpineMatch[1]) + displayOffset;
    return displayStart === displayEnd
      ? `EPUB location ${displayStart}`
      : `EPUB locations ${displayStart}-${displayEnd}`;
  }
  return storedLabel && !looksLikeEpubArchiveMember(storedLabel)
    ? storedLabel
    : "EPUB location";
}

export function sourceReferenceRangeDisplayLabel({
  pageRange,
  sourceLocator,
}: {
  pageRange?: string | null;
  sourceLocator?: string | null;
}): string {
  const storedLabel = String(pageRange ?? "").trim();
  if (!storedLabel || !String(sourceLocator ?? "").trim().toLowerCase().startsWith("epub:")) {
    return storedLabel;
  }
  if (looksLikeEpubArchiveMember(storedLabel)) {
    return "";
  }
  const storedSpineMatch = EPUB_SPINE_LABEL_RE.exec(storedLabel);
  if (!storedSpineMatch) {
    return storedLabel;
  }
  // Persisted `EPUB spine N` labels come from the catalog's zero-based spine
  // coordinates. Match the live SourceRange presentation so a refresh cannot
  // make the same chapter appear to move by one position.
  const displayStart = Number(storedSpineMatch[1]) + 1;
  const displayEnd = Number(storedSpineMatch[2] ?? storedSpineMatch[1]) + 1;
  return displayStart === displayEnd
    ? `EPUB location ${displayStart}`
    : `EPUB locations ${displayStart}-${displayEnd}`;
}

function numericEndpoint(value: SourceRange["start"]): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

function looksLikeEpubArchiveMember(value: string): boolean {
  return EPUB_ARCHIVE_MEMBER_RE.test(value.trim());
}
