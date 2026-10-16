"use client";

import { useEffect } from "react";

import type { SourceIngestionRecord } from "../../types";
import { refinementIsActive, refinementOwnsCurrentJob } from "../../lib/source-refinement";

const ACTIVE_SOURCE_STATUSES = new Set<string>([
  "queued",
  "fetching",
  "parsing",
  "indexing",
]);
const SOURCE_POLL_DELAYS_MS = [1000, 2000, 5000, 10000] as const;

type UseSourcePollingOptions = {
  disabled: boolean;
  sources: SourceIngestionRecord[];
  refreshSources: () => Promise<SourceIngestionRecord[] | null | undefined>;
};

export function useSourcePolling({ disabled, sources, refreshSources }: UseSourcePollingOptions) {
  const fingerprint = activeSourceFingerprint(sources);

  useEffect(() => {
    if (disabled || !fingerprint) {
      return;
    }
    let cancelled = false;
    let attempt = 0;
    let timerId: number | null = null;

    const schedule = () => {
      const delay = SOURCE_POLL_DELAYS_MS[Math.min(attempt, SOURCE_POLL_DELAYS_MS.length - 1)];
      timerId = window.setTimeout(() => {
        void refreshSources().then((nextSources) => {
          if (cancelled) {
            return;
          }
          if (!nextSources || activeSourceFingerprint(nextSources) === fingerprint) {
            attempt += 1;
            schedule();
          }
        });
      }, delay);
    };
    schedule();
    return () => {
      cancelled = true;
      if (timerId !== null) {
        window.clearTimeout(timerId);
      }
    };
  }, [disabled, fingerprint, refreshSources]);
}

export function sourceNeedsRefresh(source: SourceIngestionRecord) {
  if (["queued", "indexing"].includes(String(source.metadata.anythingllm_index_status))) return true;
  return sourceHasActiveProcessing(source);
}

export function sourceHasActiveProcessing(source: SourceIngestionRecord) {
  if (refinementIsActive(source.refinement)) return true;
  if (source.metadata.processing_mode === "ordinary" && (
    (!source.refinement && ["pending", "running"].includes(String(source.metadata.catalog_status))) ||
    ["pending", "parsing"].includes(String(source.metadata.basic_read_status)) ||
    ["queued", "processing"].includes(String(source.metadata.enhancement_status)))) return true;
  const finishedPiJob = source.refinement && !refinementIsActive(source.refinement) &&
    refinementOwnsCurrentJob(source);
  if (finishedPiJob) return false;
  if (ACTIVE_SOURCE_STATUSES.has(source.status)) {
    return true;
  }
  return Boolean(source.ingestion_job && ACTIVE_SOURCE_STATUSES.has(source.ingestion_job.status)) ||
    (typeof source.metadata.update_status === "string" && ACTIVE_SOURCE_STATUSES.has(source.metadata.update_status));
}

export function activeSourceFingerprint(sources: SourceIngestionRecord[]) {
  return sources
    .filter(sourceNeedsRefresh)
    .map((source) =>
      [
        source.id,
        source.refinement?.request_id ?? "",
        source.refinement?.status ?? "",
        source.refinement?.updated_at ?? "",
        source.refinement?.phase ?? "",
        source.status,
        source.ingestion_job?.id ?? "",
        source.ingestion_job?.status ?? "",
        source.ingestion_job?.progress ?? "",
        source.ingestion_job?.phase_history?.at(-1) ?? "",
        source.ingestion_job?.heartbeat_at ?? "",
        source.ingestion_job?.updated_at ?? "",
        source.metadata.update_status ?? "",
        source.metadata.pending_source_version ?? "",
        source.metadata.catalog_version ?? "",
        source.metadata.catalog_status ?? "",
        source.metadata.basic_read_status ?? "",
        source.metadata.enhancement_status ?? "",
        source.metadata.anythingllm_index_status ?? "",
        source.updated_at,
      ].join(":")
    )
    .sort()
    .join("|");
}
