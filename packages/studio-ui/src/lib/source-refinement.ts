import type { SourceCatalogView, SourceIngestionRecord } from "../types";
import type { SourceCatalogRefinementView, SourceRefinementLocation } from "../types/source-refinement";

export function refinementIsActive(run: Pick<SourceCatalogRefinementView, "status"> | null | undefined) {
  return run?.status === "queued" || run?.status === "running";
}

export function refinementOwnsCurrentJob(source: SourceIngestionRecord) {
  const run = source.refinement, job = source.ingestion_job;
  return Boolean(run && job && (!run.job_id || job.id === run.job_id) &&
    job.phase_history?.at(-1)?.startsWith("pi_catalog_refinement"));
}

export function refinementActivity(source: SourceIngestionRecord) {
  if (!source.refinement?.job_id || source.ingestion_job?.id !== source.refinement.job_id) return [];
  return source.ingestion_job.agent_activity.filter(event => event.id.startsWith("refinement:") &&
    ["pi_process", "pi_tool_call", "pi_tool_result", "source_refinement_step", "source_refinement_stop"].includes(String(event.metadata.kind)));
}

export function refinementStatusLabel(run: Pick<SourceCatalogRefinementView, "status" | "stop_reason">) {
  const reasons: Record<string, string> = {
    completed: "Completed", completed_with_limits: "Completed with limitations",
    budget_exhausted: "Paused at the time limit", call_limit: "Paused at the model call limit",
    step_timeout: "A processing step timed out", no_progress: "Paused because no new progress was made",
    cancelled: "Stopped", interrupted: "Interrupted", version_changed: "The source or catalog version changed",
    model_timeout: "Model request timed out", tool_failed: "An original-reading tool failed",
    authentication_required: "Model authentication is required", unsupported_model: "The selected model cannot read this input",
    failed: "Could not complete this run",
  };
  if (!refinementIsActive(run) && reasons[run.stop_reason]) return reasons[run.stop_reason];
  return { queued: "Queued", running: "In progress", ready: "Completed", partial: "Saved progress; more work remains",
    failed: "Could not complete this run", cancelled: "Stopped", interrupted: "Interrupted" }[run.status];
}

export function refinementPhaseLabel(phase: string) {
  const labels: Record<string, string> = {
    prepare_original: "Verifying the original file", discover_directory: "Locating the original directory",
    inspect_directory_gap: "Checking missing directory entries", plan_directory_fragments: "Planning directory columns and fragments",
    transcribe_directory_fragment: "Reading a directory column or fragment", verify_directory_page: "Checking the full directory page",
    transcribe_directory_unit: "Reading original directory entries", review_directory_unit: "Rechecking original directory entries",
    review_seeded_directory_unit: "Checking the saved directory against its original page",
    reconcile_directory_unit: "Reconciling directory entries", calibrate_pdf: "Calibrating printed and PDF pages",
    review_page_labels: "Checking printed page labels", publish_bulk_mapping: "Saving verified chapter locations",
    map_chapter: "Checking a chapter boundary", waiting_retry: "Waiting to retry from saved progress",
    verify_chapter_scope: "Preparing an exact chapter reference",
    review_directory_heading_line: "Checking an original heading line",
  };
  return labels[phase] || "Pi is refining the directory";
}

export function refinementLocationLabel(range: SourceRefinementLocation | null | undefined) {
  if (!range || range.start === null) return "";
  const prefix = range.kind === "pdf_pages" ? "PDF p." : range.kind === "ppt_slides" ? "Slide" : "Position";
  return `${prefix} ${range.start}${range.end !== null && range.end !== range.start ? `–${range.end}` : ""}`;
}

export function mergePublishedCatalog(source: SourceIngestionRecord, catalog: SourceCatalogView): SourceIngestionRecord {
  const version = typeof source.metadata.catalog_version === "number" ? source.metadata.catalog_version : 0;
  if (catalog.catalog_version < version || source.metadata.content_hash && source.metadata.content_hash !== catalog.source_content_hash) return source;
  // Catalog summaries are cached snapshots; only source records own task and upload state.
  return { ...source, structure_status: catalog.status, structure_strategy: catalog.strategy,
    structure_has_verified_toc: catalog.has_verified_toc, structure_quality: catalog.quality,
    structure_error: catalog.error, structure_updated_at: catalog.catalog_updated_at,
    metadata: { ...source.metadata, catalog_version: catalog.catalog_version } };
}
