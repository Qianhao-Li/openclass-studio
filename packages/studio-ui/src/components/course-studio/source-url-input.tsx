"use client";

import { Globe2 } from "lucide-react";
import { useEffect, useState } from "react";

import { useWorkspacePrincipal } from "../../contexts/studio-host-context";
import { listenForConnectionChanges } from "../../lib/connection-events";
import { getConnectedAccounts } from "../../lib/profile-api";
import type { ExternalConnection } from "../../types/external-connections";

const GITHUB_REPOSITORY_URL = /^https?:\/\/(?:www\.)?github\.com\/[^/]+\/[^/?#]+/i;

type AccountState = { ownerId: string; accounts: ExternalConnection[]; error: string };

export function SourceUrlInput({ value, onChange, disabled, onSubmit }: {
  value: string;
  onChange: (value: string) => void;
  disabled: boolean;
  onSubmit: (connectionId?: string) => void;
}) {
  const user = useWorkspacePrincipal();
  const ownerId = user?.isGuest ? "" : user?.id ?? "";
  const githubUrl = GITHUB_REPOSITORY_URL.test(value.trim());
  const [state, setState] = useState<AccountState>({ ownerId: "", accounts: [], error: "" });
  const [selectedId, setSelectedId] = useState("");

  useEffect(() => {
    if (!githubUrl || !ownerId) return;
    let active = true;
    let sequence = 0;
    const refresh = () => {
      const request = ++sequence;
      void getConnectedAccounts().then((accounts) => {
      if (!active || request !== sequence) return;
      if (accounts.some((account) => account.owner_user_id !== ownerId)) throw new Error("Your OpenClass account changed. Reload this page.");
      setState({ ownerId, accounts: accounts.filter((account) => account.provider === "github" && account.status === "connected"
        && account.capabilities.repositories?.granted === true && account.capabilities.repositories?.enabled !== false), error: "" });
    }).catch(() => {
      if (active && request === sequence) setState({ ownerId, accounts: [], error: "GitHub accounts could not be listed. The server will verify access before import." });
    });
    };
    refresh();
    const stopListening = listenForConnectionChanges(refresh);
    return () => { active = false; stopListening(); };
  }, [githubUrl, ownerId]);

  const loading = githubUrl && Boolean(ownerId) && state.ownerId !== ownerId;
  const accounts = githubUrl && state.ownerId === ownerId ? state.accounts : [];
  const chosenId = accounts.some((account) => account.id === selectedId) ? selectedId : "";
  const needsChoice = accounts.length > 1 && !chosenId;

  return <div>
    <label className="mt-3 block text-[11px] font-bold uppercase tracking-widest text-gray-500">URL</label>
    <div className="mt-2 flex gap-2">
      <input value={value} onChange={(event) => onChange(event.target.value)} placeholder="https://"
        className="h-9 min-w-0 flex-1 rounded-md border border-gray-200 px-3 text-sm outline-none transition focus:border-black"
        disabled={disabled} />
      <button type="button" onClick={() => onSubmit(githubUrl && chosenId ? chosenId : undefined)}
        disabled={!value.trim() || disabled || loading || needsChoice}
        className="flex h-9 w-9 items-center justify-center rounded-md bg-black text-white transition hover:bg-gray-800 disabled:cursor-not-allowed disabled:opacity-50"
        title="Import URL" aria-label="Import URL"><Globe2 className="h-4 w-4" /></button>
    </div>
    {githubUrl && accounts.length > 1 ? <label className="mt-2 block text-xs text-gray-600">GitHub account for this repository
      <select aria-label="GitHub account for repository URL" className="mt-1 block h-9 w-full rounded-md border border-gray-200 bg-white px-2 text-sm"
        value={chosenId} onChange={(event) => setSelectedId(event.target.value)} disabled={disabled}>
        <option value="">Choose an account</option>
        {accounts.map((account) => <option key={account.id} value={account.id}>{account.display_name} · {account.id.slice(-6)}</option>)}
      </select>
    </label> : null}
    {loading ? <p className="mt-2 text-xs text-gray-500">Checking connected GitHub accounts…</p> : null}
    {githubUrl && state.ownerId === ownerId && state.error ? <p className="mt-2 text-xs text-amber-700">{state.error}</p> : null}
  </div>;
}
