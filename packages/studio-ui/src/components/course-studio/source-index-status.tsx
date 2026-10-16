"use client";

import { useState } from "react";
import { sourceCloudApi } from "../../lib/source-cloud-api";
import type { SourceIngestionRecord } from "../../types";

export function SourceIndexStatus({ packageId, source, onChanged, onError }: {
  packageId: string; source: SourceIngestionRecord;
  onChanged: (source: SourceIngestionRecord) => void; onError: (message: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  if (source.metadata.source_processor !== "anythingllm") return null;
  const status = String(source.metadata.anythingllm_index_status ?? "not_prepared");
  const preparing = status === "queued" || status === "indexing";
  async function retry() {
    setBusy(true);
    try { onChanged(await sourceCloudApi.prepareIndex(packageId, source.id, String(source.metadata.content_hash))); }
    catch (error) { onError(error instanceof Error ? error.message : "Search index preparation failed."); }
    finally { setBusy(false); }
  }
  return <div className="space-y-1" data-testid="source-index-status">
    <div className="flex items-center gap-2 text-xs">
      <span>Search index: {status === "ready" ? "Ready" : preparing ? "Preparing" : status === "failed" ? "Failed" : "Not prepared"}</span>
      {!preparing && status !== "ready" && <button type="button" onClick={() => void retry()}
        disabled={busy || source.metadata.anythingllm_catalog_ready !== true}
        className="rounded border border-gray-200 px-2 py-1 disabled:opacity-40" aria-label={`Retry source search index: ${source.title}`}>
        {busy ? "Working…" : "Retry search index"}
      </button>}
    </div>
    {source.metadata.catalog_origin === "ai_organized" && <p className="text-[11px] text-gray-500">Content structure organized by AI.</p>}
    {typeof source.metadata.anythingllm_index_error === "string" && source.metadata.anythingllm_index_error &&
      <p className="text-xs text-amber-700">{source.metadata.anythingllm_index_error}</p>}
  </div>;
}
