"use client";

import { useState } from "react";
import Image from "next/image";
import { sourceCloudApi } from "../../lib/source-cloud-api";
import type { SourceIngestionRecord } from "../../types";
import { SourceIndexStatus } from "./source-index-status";

export function SourceEnhancementControls({ packageId, source, disabled, onChanged, onError }: {
  packageId: string; source: SourceIngestionRecord; disabled?: boolean;
  onChanged: (source: SourceIngestionRecord) => void; onError: (message: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  if (source.metadata.source_backend !== "google") return null;
  const state = String(source.metadata.enhancement_status ?? (source.metadata.search_ready ? "ready" : "not_prepared"));
  const preparing = state === "queued" || state === "processing";
  const readable = source.metadata.original_read_ready === true;
  async function act() {
    setBusy(true);
    try {
      const updated = preparing
        ? await sourceCloudApi.cancelEnhancement(packageId, source.id)
        : await sourceCloudApi.prepareEnhancement(packageId, source.id, String(source.metadata.content_hash ?? ""));
      onChanged(updated);
    } catch (error) { onError(error instanceof Error ? error.message : "Enhanced search preparation failed."); }
    finally { setBusy(false); }
  }
  return <div className="mt-2 space-y-1" data-testid="source-enhancement-controls">
    <SourceIndexStatus packageId={packageId} source={source} onChanged={onChanged} onError={onError} />
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span>Enhanced search: {state === "ready" ? "Ready" : preparing ? "Preparing" : state === "failed" ? "Failed" : state === "interrupted" ? "Interrupted" : state === "cancelled" ? "Cancelled" : "Not prepared"}</span>
      <button type="button" onClick={() => void act()}
        disabled={state === "ready" || busy || !readable || Boolean(disabled && !preparing)}
        className={`inline-flex items-center gap-1.5 rounded border border-gray-200 px-2 py-1${state === "ready" ? "" : " disabled:opacity-40"}`}
        aria-label={`${state === "ready" ? "Enhanced search ready" : preparing ? "Cancel enhanced search preparation" : "Prepare enhanced search"}: ${source.title} (RAG · Google Cloud)`}>
        <Image src="/google-cloud-rag.webp" alt="" width={20} height={16} unoptimized
          className="h-4 w-5 shrink-0 object-contain" />
        <span>RAG · {state === "ready" ? "Ready" : busy ? "Working…" : preparing ? "Cancel preparation" : state === "failed" ? "Retry enhanced search" : "Prepare enhanced search"}</span>
      </button>
    </div>
    {typeof source.metadata.enhancement_error === "string" && source.metadata.enhancement_error &&
      <p className="text-xs text-amber-700">{source.metadata.enhancement_error}</p>}
  </div>;
}
