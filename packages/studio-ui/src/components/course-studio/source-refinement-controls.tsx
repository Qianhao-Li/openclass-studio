"use client";

import { useEffect, useRef, useState } from "react";
import { useStudioApi } from "../../contexts/studio-api-context";
import { ApiRequestError } from "../../lib/api";
import { publicAgentActivityText } from "../../lib/agent-activity";
import { refinementActivity, refinementIsActive, refinementLocationLabel, refinementPhaseLabel, refinementStatusLabel } from "../../lib/source-refinement";
import { sourceHasActiveProcessing } from "../../hooks/course-studio/use-source-polling";
import { SourceCodexActivity } from "./source-processing-progress";
import { hasCompleteMappedCatalog } from "./source-catalog-publication";
import type { AIModelSelection, SourceCatalogView, SourceIngestionRecord } from "../../types";
import type { SourceRefinementMode } from "../../types/source-refinement";

function elapsed(start: string | null, end: number) {
  const beginning = start ? Date.parse(start) : NaN;
  if (!Number.isFinite(beginning) || !Number.isFinite(end)) return "Timing unavailable";
  const seconds = Math.max(0, Math.floor((end - beginning) / 1000));
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

export function SourceRefinementRun({ source }: { source: SourceIngestionRecord }) {
  const run = source.refinement;
  const active = refinementIsActive(run);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  if (!run) return null;
  const finalTime = run.finished_at || run.updated_at;
  const location = refinementLocationLabel(run.current_step?.source_range);
  const checkpoint = run.last_checkpoint;
  const counts = run.location_summary;
  const activity = refinementActivity(source);
  return <div data-testid="source-refinement-run" className="mt-2 rounded border border-blue-100 bg-blue-50/40 p-2 text-[11px] leading-5 text-gray-600" aria-live="polite">
    <p className="font-medium text-gray-800">Pi Agent · {refinementStatusLabel(run)}</p>
    {active && <p>{refinementPhaseLabel(run.phase)}{location ? ` · ${location}` : ""}
      {run.current_step?.fragment_id ? ` · Fragment ${run.current_step.fragment_id}` : ""}</p>}
    <p>This run: {elapsed(run.started_at, active ? now : finalTime ? Date.parse(finalTime) : NaN)}
      {active && run.budget_seconds ? ` · Automatic continuation up to ${Math.round(run.budget_seconds / 60)} minutes` : ""}</p>
    {run.total_units > 0 && <p>Directory units checked: {run.completed_units}/{run.total_units}
      {!run.discovery_complete ? " · Directory discovery continues" : ""}</p>}
    {counts.total !== undefined && <p>{counts.verified ?? 0}/{counts.total} ready to cite
      {counts.position_only ? ` · ${counts.position_only} awaiting page ranges` : ""}
      {counts.outside_uploaded_file ? ` · ${counts.outside_uploaded_file} outside the uploaded file` : ""}
      {counts.unresolved ? ` · ${counts.unresolved} awaiting locations` : ""}</p>}
    {checkpoint && <p>Saved progress: {refinementPhaseLabel(checkpoint.stage)}
      {refinementLocationLabel(checkpoint.source_range) ? ` · ${refinementLocationLabel(checkpoint.source_range)}` : ""}</p>}
    {!active && run.status !== "ready" && <p>Saved directory entries and checkpoints remain available. Continue to finish the remaining work.</p>}
    {activity.length ? <SourceCodexActivity events={activity} title="Pi execution activity" /> :
      <p className="text-gray-500">{active ? "Execution activity has not arrived yet." : "No execution activity is available for this run."}</p>}
  </div>;
}

export function SourceRefinementActions({ packageId, source, catalog, catalogModel, onCatalogUpdate, onRefresh, onStarted }: {
  packageId: string;
  source: SourceIngestionRecord;
  catalog: SourceCatalogView | null;
  catalogModel: AIModelSelection | null;
  onCatalogUpdate: (catalog: SourceCatalogView) => void;
  onRefresh: () => Promise<void>;
  onStarted: () => void;
}) {
  const api = useStudioApi();
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState<{ id: string; priorId: string | undefined } | null>(null);
  const request = useRef<{ id: string; mode: SourceRefinementMode } | null>(null);
  const refresh = useRef(onRefresh);
  useEffect(() => { refresh.current = onRefresh; }, [onRefresh]);
  const acknowledged = source.refinement?.request_id === pending?.id || Boolean(pending && source.refinement && source.refinement.request_id !== pending.priorId);
  const waiting = Boolean(pending && !acknowledged);
  const active = sourceHasActiveProcessing(source);
  const disabled = submitting || waiting || active || !catalog;
  const reason = active ? (refinementIsActive(source.refinement) ? "Pi is refining this catalog" : "This source has another processing task")
    : waiting ? "Checking the submitted task" : !catalog ? "Loading the current catalog" : "";

  useEffect(() => {
    if (!waiting || submitting) return;
    let attempts = 0;
    const timer = window.setInterval(() => {
      void refresh.current();
      if (++attempts >= 3) window.clearInterval(timer);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [submitting, waiting]);

  async function start(mode: SourceRefinementMode) {
    if (disabled || !catalog) return;
    if (!request.current || request.current.mode !== mode) request.current = { id: crypto.randomUUID(), mode };
    const identity = request.current;
    setSubmitting(true);
    setMessage("");
    try {
      const next = await api.rebuildPackageSourceCatalog(packageId, source.id, catalogModel, catalog,
        { mode, requestId: identity.id });
      const completeAvailable = mode === "continue" && source.refinement?.status === "ready" &&
        next.catalog_version === catalog.catalog_version && hasCompleteMappedCatalog(next);
      setPending(completeAvailable ? null : { id: identity.id, priorId: source.refinement?.request_id });
      if (completeAvailable) setMessage("The existing complete catalog is available.");
      request.current = null;
      onCatalogUpdate(next);
      onStarted();
      await onRefresh();
    } catch (error) {
      setMessage(error instanceof Error ? publicAgentActivityText(error.message) : "Catalog refinement could not start.");
      if (error instanceof ApiRequestError && error.status === 409) {
        request.current = null;
        await onRefresh();
      }
    } finally {
      setSubmitting(false);
    }
  }

  return <div className="w-full text-[11px]">
    <div className="flex flex-wrap items-center justify-end gap-2">
      <button type="button" disabled={disabled} onClick={() => void start("continue")}
        aria-label={`Continue refining catalog ${source.title}`} title={reason || "Continue from saved directory work"}
        className="rounded border border-blue-200 bg-white px-2 py-1 text-blue-700 disabled:cursor-not-allowed disabled:opacity-50">
        {submitting ? "Starting…" : "Continue refining"}
      </button>
      <details className="relative">
        <summary className="cursor-pointer text-gray-500">More catalog actions</summary>
        <button type="button" disabled={disabled} onClick={() => void start("reverify")}
          aria-label={`Reverify entire catalog ${source.title}`} title={reason || "Recheck the original directory and all existing locations"}
          className="mt-1 rounded border border-gray-200 bg-white px-2 py-1 text-gray-700 disabled:cursor-not-allowed disabled:opacity-50">
          Reverify entire catalog
        </button>
      </details>
    </div>
    {reason && <p className="mt-1 text-gray-500">{reason}.</p>}
    {waiting && <button type="button" className="text-blue-700 underline" onClick={() => void onRefresh()}>Refresh task status</button>}
    {message && <p role="status" data-testid="source-refinement-message" className="mt-1 text-amber-700">{message}</p>}
  </div>;
}
