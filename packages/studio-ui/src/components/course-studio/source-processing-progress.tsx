import { isManagedSource } from "../../lib/source-backend";
import { useEffect, useState } from "react";
import clsx from "clsx";
import { ChevronRight } from "lucide-react";

import { publicAgentActivityLabel, publicAgentActivityText } from "../../lib/agent-activity";
import { refinementActivity, refinementIsActive, refinementOwnsCurrentJob, refinementPhaseLabel } from "../../lib/source-refinement";
import type { AgentActivityEvent, SourceIngestionRecord } from "../../types";

type SourceProcessingProgressProps = {
  label: string;
  detail?: string;
  value?: number;
  /** ISO timestamp used to show elapsed wait time for indeterminate phases. */
  startedAt?: string;
  className?: string;
  activity?: AgentActivityEvent[];
};

type SourceProcessingState = {
  label: string;
  detail?: string;
  value?: number;
  startedAt?: string;
  activity?: AgentActivityEvent[];
};

const LEGACY_SOURCE_STATUS_PROGRESS: Partial<
  Record<SourceIngestionRecord["status"], SourceProcessingState>
> = {
  queued: { label: "Waiting for parsing to begin", value: 12 },
  fetching: { label: "Retrieving information", value: 32 },
  parsing: { label: "Parsing text", value: 58 },
  indexing: { label: "Building search index", value: 78 },
};

const DIRECTORY_SOURCE_STATUS_PROGRESS: Partial<
  Record<SourceIngestionRecord["status"], SourceProcessingState>
> = {
  queued: { label: "Waiting for directory creation", value: 12 },
  fetching: { label: "Retrieving information", value: 32 },
  parsing: { label: "Reading native file structure", value: 58 },
  indexing: { label: "Saving directory", value: 78 },
};

const LEGACY_JOB_PHASE_LABELS: Record<string, string> = {
  uploaded: "The file has been received and is being prepared for parsing",
  parsing: "Parsing text",
  reading_pages: "Reading text page by page",
  mapping_structure: "Recognizing table of contents and text structure",
  building_chunks: "Creating search fragment",
  extracting_visuals: "Retrieving charts and pictures",
  persisting: "Saving data index",
  transforming: "Generating import product",
  resolving_repository: "Resolving repository",
  resolving_commit: "Pinning repository commit",
  downloading_snapshot: "Downloading immutable repository snapshot",
  scanning_files: "Scanning repository files",
  reading_repository_files: "Reading selected repository files",
  planning_repository_reads: "Pi is selecting repository content",
  building_project_tree: "Building project tree",
};

const DIRECTORY_JOB_PHASE_LABELS: Record<string, string> = {
  uploaded: "The file has been received and the directory is being created",
  parsing: "Preparing directory extraction",
  reading_directory_metadata: "Reading directory metadata",
  locating_toc_pages: "Locating directory pages",
  mapping_directory_to_pages: "Binding directory and file ranges",
  scanning_heading_regions: "Checking page title areas",
  native_directory_extraction: "Reading native directory structure",
  native_pi_directory: "Native Pi is reading the directory",
  calibrating_pdf_p: "Calibrating printed pages and PDF pages",
  continuing_pdf_range_mapping: "Continuing until every chapter has a verified PDF range",
  validating_directory: "Verifying directory structure",
  sending_file_to_ai: "Sending file to directory model",
  waiting_for_directory_model: "Directory model is reading the file",
  materializing_directory: "Building chapter list",
  publishing_directory: "Saving directory",
  publishing_catalog: "Saving directory",
  catalog_ready: "Directory has been saved",
};

/** Phases that wait on an external model with no trustworthy percent. */
const DIRECTORY_INDETERMINATE_PHASES = new Set([
  "sending_file_to_ai",
  "waiting_for_directory_model",
  "native_pi_directory",
  "source_codex_investigation",
  "continuing_pdf_range_mapping",
]);

const DIRECTORY_PIPELINES = new Set([
  "native_directory_v1",
  "hybrid_directory_v1",
  "ai_directory_v1",
  "codex_directory_v1",
  "agent_catalog_v2",
  "agent_catalog_v3",
  "source_original_catalog_v1",
]);

export function isDirectoryCatalogSource(source: SourceIngestionRecord) {
  return (
    DIRECTORY_PIPELINES.has(source.structure_strategy ?? "") ||
    DIRECTORY_PIPELINES.has(source.ingestion_job?.adapter ?? "") ||
    DIRECTORY_PIPELINES.has(String(source.metadata.catalog_pipeline ?? ""))
  );
}

function activityValue(value: unknown): string {
  if (typeof value === "string") {
    return value.trim();
  }
  if (value === undefined || value === null) {
    return "";
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function sourceActivityLabel(event: AgentActivityEvent): string {
  const label = publicAgentActivityLabel(event.label);
  if (!/(继续核对|持续完善)/.test(label)) {
    return label;
  }
  return event.status === "pending" || event.status === "running"
    ? "Checking catalog boundaries"
    : "Check catalog boundaries";
}

function activitySections(event: AgentActivityEvent): Array<{ label: string; value: string; mono?: boolean }> {
  const sections: Array<{ label: string; value: string; mono?: boolean }> = [];
  const seen = new Set<string>();
  const append = (label: string, raw: unknown, mono = false) => {
    const value = activityValue(raw);
    if (!value || seen.has(value)) {
      return;
    }
    seen.add(value);
    sections.push({ label, value: mono ? value : publicAgentActivityText(value), mono });
  };
  append("Command", event.metadata.command, true);
  append("Arguments", event.metadata.arguments, true);
  append("Output", event.metadata.output ?? event.metadata.result, true);
  append("Error output", event.metadata.stderr, true);
  append("Details", event.metadata.detail);
  append("Query", event.metadata.query, true);
  return sections;
}

function activityTime(value: string): string {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) {
    return "";
  }
  return date.toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function SourceCodexActivity({
  events,
  className,
  title = "Source agent live activity",
  expandedByDefault = false,
}: {
  events: AgentActivityEvent[];
  className?: string;
  title?: string;
  expandedByDefault?: boolean;
}) {
  if (!events.length) {
    return null;
  }
  const visibleEvents = [...events].sort((left, right) =>
    left.created_at.localeCompare(right.created_at)
  );
  const latestEvent = visibleEvents.at(-1);
  return (
    <details
      open={expandedByDefault || undefined}
      className={clsx("group mt-2 rounded-md border border-gray-200 bg-gray-50/80 px-2.5 py-2", className)}
      aria-live="polite"
    >
      <summary className="flex cursor-pointer list-none items-center gap-1.5 text-[10px] text-gray-500 marker:content-none [&::-webkit-details-marker]:hidden">
        <ChevronRight className="h-3 w-3 shrink-0 transition-transform group-open:rotate-90" aria-hidden="true" />
        <span className="shrink-0 font-semibold tracking-wide">{title}</span>
        <span className="shrink-0 text-gray-400">· {visibleEvents.length} events</span>
        {latestEvent ? <span className="truncate text-gray-400">· {sourceActivityLabel(latestEvent)}</span> : null}
      </summary>
      <div className="custom-scrollbar mt-2 max-h-[32rem] space-y-2 overflow-auto pr-1">
        {visibleEvents.map((event) => {
          const sections = activitySections(event);
          const isActive = event.status === "pending" || event.status === "running";
          const isCancelled = event.status === "cancelled";
          const isFailed = event.status === "failed" || event.status === "blocked";
          return (
            <div key={event.id} className="min-w-0 rounded border border-gray-200 bg-white px-2 py-1.5 text-[11px] leading-4 text-gray-600">
              <div className="flex items-start gap-1.5">
                <span
                  className={clsx(
                    "mt-1 h-1.5 w-1.5 shrink-0 rounded-full",
                    isActive && "animate-pulse bg-emerald-500",
                    isCancelled && "bg-amber-500",
                    isFailed && "bg-rose-500",
                    !isActive && !isCancelled && !isFailed && "bg-gray-300"
                  )}
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-start justify-between gap-2">
                    <span className="break-words font-medium text-gray-700">{sourceActivityLabel(event)}</span>
                    <span className="shrink-0 font-mono text-[9px] tabular-nums text-gray-400">{activityTime(event.created_at)}</span>
                  </div>
                  {typeof event.metadata.tool === "string" && event.metadata.tool ? (
                    <p className="mt-0.5 font-mono text-[9px] text-blue-600">tool: {event.metadata.tool}</p>
                  ) : null}
                </div>
              </div>
              {sections.length ? (
                <div className="mt-1.5 space-y-1.5">
                  {sections.map((section) => (
                    <div key={`${event.id}:${section.label}`}>
                      <p className="mb-0.5 text-[9px] font-semibold text-gray-400">{section.label}</p>
                      <pre
                        className={clsx(
                          "custom-scrollbar max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-gray-50 px-2 py-1.5 text-[10px] leading-4 text-gray-700",
                          section.mono ? "font-mono" : "font-sans"
                        )}
                      >
                        {section.value}
                      </pre>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </details>
  );
}

function formatFileSize(sizeBytes: number): string | null {
  if (!Number.isFinite(sizeBytes) || sizeBytes <= 0) {
    return null;
  }
  if (sizeBytes >= 1024 * 1024) {
    return `${(sizeBytes / (1024 * 1024)).toFixed(sizeBytes >= 10 * 1024 * 1024 ? 0 : 1)} MB`;
  }
  if (sizeBytes >= 1024) {
    return `${Math.max(1, Math.round(sizeBytes / 1024))} KB`;
  }
  return `${sizeBytes} B`;
}

function formatElapsed(startedAt: string, nowMs: number): string | null {
  const startedMs = Date.parse(startedAt);
  if (!Number.isFinite(startedMs)) {
    return null;
  }
  const elapsedSec = Math.max(0, Math.floor((nowMs - startedMs) / 1000));
  if (elapsedSec < 5) {
    return null;
  }
  const minutes = Math.floor(elapsedSec / 60);
  const seconds = elapsedSec % 60;
  if (minutes <= 0) {
    return `Waiting ${seconds}s`;
  }
  return `Waiting ${minutes}m ${seconds.toString().padStart(2, "0")}s`;
}

function directoryAiWaitDetail(source: SourceIngestionRecord): string {
  const sizeLabel = formatFileSize(source.size_bytes);
  const model =
    typeof source.metadata.catalog_model_selection === "object" &&
    source.metadata.catalog_model_selection &&
    "model" in source.metadata.catalog_model_selection
      ? String((source.metadata.catalog_model_selection as { model?: unknown }).model ?? "")
      : typeof source.metadata.catalog_model === "string"
        ? source.metadata.catalog_model
        : "";
  const parts = [
    sizeLabel ? `${sizeLabel} file` : null,
    model ? `Model ${model}` : null,
    "The model reads the file to build its catalog; large files can take several minutes",
  ].filter(Boolean);
  return parts.join(" · ");
}

function directoryRangeMappingDetail(events: AgentActivityEvent[]): string | null {
  for (const event of [...events].reverse()) {
    const sourceProgress = event.metadata.source_progress;
    if (!sourceProgress || typeof sourceProgress !== "object") {
      continue;
    }
    const progress = sourceProgress as Record<string, unknown>;
    if (
      progress.unit !== "ranges" ||
      typeof progress.completed !== "number" ||
      typeof progress.total !== "number"
    ) {
      continue;
    }
    const remaining =
      typeof progress.remaining === "number"
        ? progress.remaining
        : Math.max(0, progress.total - progress.completed);
    return `Mapped ${progress.completed}/${progress.total} entries; ${remaining} remaining`;
  }
  return null;
}

export function SourceProcessingProgress({
  label,
  detail,
  value,
  startedAt,
  className,
  activity = [],
}: SourceProcessingProgressProps) {
  const isDeterminate = typeof value === "number";
  const normalizedValue = isDeterminate ? Math.max(0, Math.min(100, Math.round(value))) : undefined;
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    if (isDeterminate || !startedAt) {
      return;
    }
    const timerId = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timerId);
  }, [isDeterminate, startedAt]);

  const displayLabel = publicAgentActivityLabel(label);
  const displayDetail = detail ? publicAgentActivityText(detail) : undefined;
  const elapsedLabel = !isDeterminate && startedAt ? formatElapsed(startedAt, nowMs) : null;

  return (
    <div className={clsx("min-w-0", className)} aria-live="polite">
      <div className="mb-1.5 flex items-center justify-between gap-3 text-[11px] font-medium text-gray-500">
        <span className="truncate">{displayLabel}</span>
        {isDeterminate ? (
          <span className="shrink-0 tabular-nums text-emerald-700">{normalizedValue}%</span>
        ) : (
          <span className="inline-flex shrink-0 items-center gap-1 tabular-nums text-emerald-700">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500" aria-hidden="true" />
            {elapsedLabel ?? "In progress"}
          </span>
        )}
      </div>
      <div
        role="progressbar"
        aria-label={displayLabel}
        aria-valuemin={isDeterminate ? 0 : undefined}
        aria-valuemax={isDeterminate ? 100 : undefined}
        aria-valuenow={normalizedValue}
        aria-valuetext={
          isDeterminate ? undefined : `${displayLabel}${elapsedLabel ? `, ${elapsedLabel}` : ", in progress"}`
        }
        className="h-1.5 w-full overflow-hidden rounded-full bg-gray-100"
      >
        <div
          className={clsx(
            "h-full rounded-full bg-emerald-500 transition-[width] duration-500 ease-out",
            !isDeterminate && "source-processing-progress__indeterminate"
          )}
          style={isDeterminate ? { width: `${normalizedValue}%` } : undefined}
        />
      </div>
      {displayDetail ? <p className="mt-1.5 text-[10px] leading-4 text-gray-500">{displayDetail}</p> : null}
      <SourceCodexActivity events={activity} />
    </div>
  );
}

export function getSourceProcessingState(source: SourceIngestionRecord): SourceProcessingState | null {
  if (source.refinement) {
    if (refinementIsActive(source.refinement)) return {
      label: source.refinement.status === "queued" ? "Pi catalog refinement queued" : refinementPhaseLabel(source.refinement.phase),
      detail: "Saved directory entries remain available while Pi checks the original.",
      startedAt: source.refinement.started_at ?? undefined,
      activity: refinementActivity(source),
    };
    if ((!source.ingestion_job || refinementOwnsCurrentJob(source)) &&
        !["queued", "processing"].includes(String(source.metadata.enhancement_status)) &&
        !["pending", "parsing"].includes(String(source.metadata.basic_read_status))) return null;
  }
  if (source.metadata.processing_mode === "ordinary") {
    const metadata = source.metadata;
    const phase = source.ingestion_job?.phase_history.at(-1);
    const enhanced = ["queued", "processing"].includes(String(metadata.enhancement_status));
    const reading = ["pending", "parsing"].includes(String(metadata.basic_read_status));
    const catalog = ["pending", "running"].includes(String(metadata.catalog_status));
    if (!enhanced && !reading && !catalog) return null;
    return {
      label: enhanced ? "Preparing enhanced search" : reading ? "Parsing original content"
        : phase === "basic_catalog_verifying" ? "Verifying all chapter ranges" : "Generating the complete catalog",
      detail: enhanced ? "Ordinary reading and the existing catalog remain available."
        : reading ? "Extracting native text and preserving source positions."
          : "Returned directory entries appear as processing continues.",
      value: source.ingestion_job?.progress,
      startedAt: source.ingestion_job?.created_at ?? source.created_at,
      activity: source.ingestion_job?.agent_activity ?? [],
    };
  }
  const isDirectoryCatalog = isDirectoryCatalogSource(source);
  const statusProgress = isDirectoryCatalog
    ? DIRECTORY_SOURCE_STATUS_PROGRESS
    : LEGACY_SOURCE_STATUS_PROGRESS;
  const phaseLabels = isDirectoryCatalog
    ? DIRECTORY_JOB_PHASE_LABELS
    : LEGACY_JOB_PHASE_LABELS;
  const job = source.ingestion_job;
  if (job && ACTIVE_JOB_STATUSES.has(job.status)) {
    const phase = job.phase_history.at(-1) ?? "";
    if (isManagedSource(source)) {
      const reading = phase === "catalog_generation" || phase === "reading_pdf_page_images" ||
        (source.metadata.catalog_mode === "manual" && job.status === "parsing");
      const verified = source.structure_quality?.verified_chapter_count ?? 0;
      const counts = [
        typeof source.metadata.original_pages_read === "number" ? `Original pages read: ${source.metadata.original_pages_read}` : "",
        typeof source.metadata.model_call_count === "number" ? `Model calls: ${source.metadata.model_call_count}` : "",
      ].filter(Boolean).join(" · ");
      return {
        label: phase === "catalog_queued" ? "Catalog reading queued"
          : reading ? "AI is reading original pages to build the catalog"
            : phase === "uploaded" ? "Original uploaded; waiting for cloud parsing"
              : (source.metadata.source_backend === "local" ? "Preparing local source reading" : "Google is parsing and indexing the source"),
        detail: reading ? counts || "Located original pages are being checked for complete chapter rows and page boundaries."
          : phase === "catalog_queued" ? (verified > 0 || source.metadata.catalog_ready === true
            ? "Verified chapters remain available while the catalog is updated." : "The chapter list will appear after original-page verification.")
            : "Waiting for cloud results. This operation does not report a page completion percentage.",
        startedAt: job.created_at,
        activity: job.agent_activity ?? [],
      };
    }
    const reversedActivity = [...job.agent_activity].reverse();
    if (
      isDirectoryCatalog &&
      (phase === "reading_pdf_page_images" || phase === "source_codex_scanning_pages")
    ) {
      const pageProgress = reversedActivity
        .map((event) => event.metadata.source_progress)
        .find((value) => value && typeof value === "object" &&
          (value as Record<string, unknown>).phase === "source_codex_scanning_pages") as
          Record<string, unknown> | undefined;
      return {
        label: typeof pageProgress?.label === "string"
          ? pageProgress.label : "AI is reading original PDF page images",
        detail: typeof pageProgress?.detail === "string"
          ? pageProgress.detail : "Original page images are sent directly to AI without OCR or text extraction",
        value: undefined,
        startedAt: job.created_at,
        activity: job.agent_activity ?? [],
      };
    }
    const latestConcreteActivity = reversedActivity.find(
      (event) =>
        typeof event.metadata.tool === "string" &&
        event.metadata.tool.length > 0 &&
        event.metadata.kind !== "sourceCatalogProgress"
    );
    const latestActivity =
      latestConcreteActivity ??
      reversedActivity.find((event) => event.status === "running") ??
      job.agent_activity.at(-1);
    const rangeMappingDetail = directoryRangeMappingDetail(job.agent_activity);
    const isIndeterminate =
      isDirectoryCatalog &&
      (DIRECTORY_INDETERMINATE_PHASES.has(phase) ||
        (phase === "parsing" && job.progress >= 50 && job.progress < 84));
    return {
      label:
        latestConcreteActivity?.label ??
        phaseLabels[phase] ??
        statusProgress[job.status]?.label ??
        (isDirectoryCatalog ? "Creating directory" : "Processing data"),
      detail:
        rangeMappingDetail ||
        latestActivity?.label ||
        (isIndeterminate ? directoryAiWaitDetail(source) : statusProgress[job.status]?.detail),
      value: isIndeterminate ? undefined : job.progress,
      // Prefer job start for wait elapsed; updated_at moves on heartbeats.
      startedAt: isIndeterminate
        ? job.created_at
        : job.heartbeat_at || job.updated_at || job.created_at,
      activity: job.agent_activity ?? [],
    };
  }
  const ingestionState = statusProgress[source.status];
  if (ingestionState) {
    if (isManagedSource(source)) {
      return { label: "Preparing source processing", detail: "Waiting for a confirmed processing stage.", startedAt: source.updated_at, activity: [] };
    }
    return { ...ingestionState, activity: [] };
  }
  if (source.status !== "ready") {
    return null;
  }
  if (isManagedSource(source) &&
      (source.structure_status === "pending" || source.structure_status === "building")) {
    return {
      label: source.structure_status === "pending" ? "Waiting for catalog reading" : "Verifying chapter locations",
      detail: "The catalog will update after original content and chapter boundaries are verified.",
      startedAt: source.updated_at,
      activity: [],
    };
  }
  if (source.structure_status === "pending") {
    return {
      label: isDirectoryCatalog ? "Preparing to create directory" : "Preparing structure index",
      value: 88,
      activity: [],
    };
  }
  if (source.structure_status === "building") {
    return {
      label: isDirectoryCatalog ? "Verifying directory range" : "Binding table of contents and text",
      value: 94,
      activity: [],
    };
  }
  return null;
}

const ACTIVE_JOB_STATUSES = new Set<SourceIngestionRecord["status"]>([
  "queued",
  "fetching",
  "parsing",
  "indexing",
]);
