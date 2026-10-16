"use client";

import { useEffect, useRef, useState } from "react";
import { applyConnectedSourceUpdate, checkConnectedSourceUpdate, type ConnectedSourceUpdate } from "../../lib/connected-sources-api";
import type { SourceIngestionRecord } from "../../types";

const buttonClass = "rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-700 disabled:cursor-not-allowed disabled:opacity-50";

export function ConnectedSourceUpdateControls({ packageId, source, onChanged }: { packageId: string; source: SourceIngestionRecord; onChanged: () => Promise<unknown> }) {
  const origin = source.metadata.origin;
  if (!origin || typeof origin !== "object" || (origin as Record<string, unknown>).origin !== "external_connection" || !source.metadata.external_resource_locator) return null;
  const hash = typeof source.metadata.content_hash === "string" ? source.metadata.content_hash : "";
  const version = typeof source.metadata.source_version === "string" ? source.metadata.source_version : "";
  if (!hash || !version) return <p className="text-xs text-amber-700"><span>The saved source version is unavailable. Updates cannot be checked for this copy.</span></p>;
  return <UpdateActions key={`${source.id}:${hash}:${version}`} packageId={packageId} source={source} hash={hash} version={version} onChanged={onChanged} />;
}

function UpdateActions({ packageId, source, hash, version, onChanged }: { packageId: string; source: SourceIngestionRecord; hash: string; version: string; onChanged: () => Promise<unknown> }) {
  const [update, setUpdate] = useState<ConnectedSourceUpdate | null>(null);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const sequence = useRef(0);
  useEffect(() => () => { sequence.current += 1; }, []);
  async function run(apply: boolean) {
    if (busy || (apply && !update)) return;
    const request = ++sequence.current;
    setBusy(apply ? "applying" : "checking"); setError(""); setNotice("");
    try {
      if (apply && update) {
        const result = await applyConnectedSourceUpdate(packageId, update);
        if (request !== sequence.current) return;
        setUpdate(null);
        setNotice(result.reused ? "This source already has the checked version." : "Update added. Follow processing in Sources; the previous version remains available until it is ready.");
        await onChanged();
      } else {
        const result = await checkConnectedSourceUpdate(packageId, source.id, hash, version);
        if (request !== sequence.current) return;
        setUpdate(result);
        setNotice(result.changed ? "A newer source snapshot is available. Apply it to update this Source." : "This source matches the latest accessible version.");
      }
    } catch (failure) {
      if (request !== sequence.current) return;
      setUpdate(null);
      setError(failure instanceof Error ? failure.message : "Could not check this source. Reconnect the account or try again.");
    } finally { if (request === sequence.current) setBusy(""); }
  }
  return <div className="space-y-1 rounded border border-gray-100 bg-gray-50 p-2" aria-label={`External source updates ${source.title}`}>
    <div className="flex flex-wrap gap-2"><button type="button" className={buttonClass} disabled={Boolean(busy) || source.status !== "ready"} onClick={() => void run(false)}><span key={busy === "checking" ? "checking" : "check"}>{busy === "checking" ? "Checking…" : "Check for updates"}</span></button>{update?.changed ? <button type="button" className={buttonClass} disabled={Boolean(busy)} onClick={() => void run(true)}><span key={busy === "applying" ? "applying" : "apply"}>{busy === "applying" ? "Adding update…" : "Apply source update"}</span></button> : null}</div>
    {source.status !== "ready" ? <p className="text-xs text-gray-500"><span>Finish source processing before checking another update.</span></p> : null}
    {notice ? <p key={notice} role="status" className="text-xs text-emerald-700"><span>{notice}</span></p> : null}
    {error ? <p key={error} role="alert" className="text-xs text-red-700"><span>{error}</span></p> : null}
  </div>;
}
