import type { SourceCatalogView, SourceIngestionRecord } from "../../types";

function positions(value: unknown, hash: unknown): string {
  if (!Array.isArray(value)) return "";
  const pages = new Set<number>(), others = new Set<string>();
  for (const item of value) {
    if (!item || typeof item !== "object" || item.content_hash && item.content_hash !== hash) continue;
    const range = item.locator ?? item.range;
    if (!range) continue;
    if (range.kind === "pdf_pages" && Number.isInteger(range.start) && Number.isInteger(range.end)) {
      for (let page = range.start; page <= range.end; page++) pages.add(page);
    } else if (range.kind) others.add(`${range.kind}: ${range.container || range.start || "original navigation"}`);
  }
  const sorted = [...pages].sort((a,b) => a-b), spans: string[] = [];
  for (let index=0; index<sorted.length; index++) {
    const first=sorted[index]; let last=first;
    while (sorted[index+1] === last+1) last=sorted[++index];
    spans.push(first === last ? String(first) : `${first}–${last}`);
  }
  return [...(spans.length ? [`PDF p. ${spans.join(", ")}`] : []), ...others].join("; ");
}

export function AnythingLLMDirectorySummary({ catalog, source }: { catalog: SourceCatalogView; source?: SourceIngestionRecord }) {
  const metadata = source?.metadata ?? {};
  const evidence = positions(metadata.catalog_directory_evidence, catalog.source_content_hash);
  const candidates = positions(metadata.catalog_directory_candidates, catalog.source_content_hash);
  const coverage = positions(metadata.catalog_read_ranges, catalog.source_content_hash);
  const refined = catalog.task_contract === "pi_catalog_refinement_v1" || catalog.catalog_schema_version === "pi_catalog_refinement_v1";
  const rawTranscription = metadata.catalog_transcription_progress;
  const transcription = rawTranscription && typeof rawTranscription === "object"
    ? rawTranscription as Record<string, unknown> : null;
  const completedUnits = typeof transcription?.completed_units === "number" ? transcription.completed_units : null;
  const totalUnits = typeof transcription?.total_units === "number" ? transcription.total_units : null;
  return <div className="mt-2 rounded-md border border-blue-100 bg-white/80 p-2 text-[11px] leading-4 text-gray-600" data-testid="anythingllm-directory-result">
    <p className="font-semibold text-gray-800">Saved directory · {catalog.chapter_count} entries · Version {catalog.catalog_version}</p>
    <p className="mt-1">Chapter locations are shown when available.</p>
    <div className="mt-1 space-y-1" data-testid="source-directory-provenance">
      <p>{refined ? "Refined by Pi Agent" : "Initial directory by AnythingLLM"}</p>
      {!source?.refinement && typeof metadata.catalog_refinement_status === "string" && <p>Pi refinement: {metadata.catalog_refinement_status}</p>}
      {completedUnits !== null && totalUnits !== null && <p>
        Directory transcription: {completedUnits}/{totalUnits} units{transcription?.complete === true ? " · Original directory transcribed; chapter locations tracked separately" : ""}
      </p>}
      <p>{evidence ? `Directory read from: ${evidence}` : metadata.catalog_directory_origin === "native_navigation" ? "Directory read from: original native navigation" : "Directory position: pending confirmation"}</p>
      {candidates && <p>Candidate directory positions: {candidates}</p>}
      {coverage && <p>Read coverage: {coverage}</p>}
      {metadata.catalog_origin === "ai_organized" && <p>AI-organized content structure</p>}
      {metadata.catalog_search_engine === "google" && <p>Located using this file’s Google enhanced index</p>}
      {typeof metadata.catalog_search_warning === "string" && metadata.catalog_search_warning && <p>{metadata.catalog_search_warning}</p>}
    </div>
  </div>;
}
