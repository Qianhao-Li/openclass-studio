"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { useWorkspacePrincipal } from "../../contexts/studio-host-context";
import { ApiRequestError } from "../../lib/api";
import { listenForConnectionChanges } from "../../lib/connection-events";
import { getConnectedAccounts } from "../../lib/profile-api";
import { browseConnectedSources, getCourseConnectionScope, importConnectedSource, saveCourseConnectionScope, type CourseConnectionScope } from "../../lib/connected-sources-api";
import type { ConnectionResource, ConnectionResourcePage, ConnectionResourceQuery, ExternalConnection } from "../../types/external-connections";

const buttonClass = "rounded-md border border-gray-200 px-2.5 py-1.5 text-xs text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50";
const supportedProviders = new Set(["google", "microsoft", "github", "gitlab", "notion", "zotero"]);

// Chrome translation can replace a text node while a request is in flight.
function LiveText({ text }: { text: string }) { return <span key={text}>{text}</span>; }
function message(error: unknown) { return error instanceof Error ? error.message : "Connected Sources are temporarily unavailable."; }
function importable(item: ConnectionResource) {
  return Boolean(item.ref) && !item.temporary && !["calendar", "event", "calendars", "events", "drive_folder", "microsoft_folder", "repository", "repository_folder", "site_drives", "zotero_library", "data_source"].includes(item.kind);
}
function collections(account: ExternalConnection | undefined) {
  if (!account) return [];
  return Object.entries(account.capabilities).flatMap(([capability, grant]) => {
    if (!grant.granted || grant.enabled === false || capability === "calendar" || capability === "models") return [];
    const choices = capability === "sharepoint" ? [{ kind: "sites", label: "SharePoint sites" }]
      : capability === "files_personal" || capability === "files_work" ? [{ kind: "", label: capability === "files_personal" ? "Personal OneDrive files" : "Work OneDrive files" }, { kind: "drives", label: "OneDrive libraries" }]
      : capability === "drive_selected" || capability === "drive_all" ? [{ kind: "", label: capability === "drive_selected" ? "Selected Drive permission" : "Drive read permission" }]
      : account.provider === "zotero" ? [{ kind: "", label: "My library" }, { kind: "groups", label: "Shared libraries" }]
      : [{ kind: "", label: "Authorized resources" }];
    return choices.map((choice) => ({ ...choice, capability, key: `${capability}:${choice.kind}` }));
  });
}
function initialLocation(account: ExternalConnection | undefined): ConnectionResourceQuery {
  const choice = collections(account)[0];
  return choice ? { kind: choice.kind, capability: choice.capability } : {};
}

type Props = { packageId: string; onChanged: () => Promise<unknown> };

export function ConnectedSourcesControls({ packageId, onChanged }: Props) {
  const user = useWorkspacePrincipal();
  if (!user || user.isGuest || user.kind === "local") return null;
  return <ConnectedSourcesManager key={`${user.id}:${packageId}`} ownerId={user.id} packageId={packageId} onChanged={onChanged} />;
}

function ConnectedSourcesManager({ ownerId, packageId, onChanged }: Props & { ownerId: string }) {
  const [accounts, setAccounts] = useState<ExternalConnection[]>([]);
  const [scope, setScope] = useState<CourseConnectionScope | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [connectionId, setConnectionId] = useState("");
  const [location, setLocation] = useState<ConnectionResourceQuery>({});
  const [history, setHistory] = useState<ConnectionResourceQuery[]>([]);
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<ConnectionResource[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const sequence = useRef(0);

  const refresh = useCallback(async () => {
    const request = ++sequence.current;
    setBusy("loading"); setError("");
    try {
      const [nextAccounts, nextScope] = await Promise.all([getConnectedAccounts(), getCourseConnectionScope(packageId)]);
      if (request !== sequence.current) return;
      if (nextAccounts.some((entry) => entry.owner_user_id !== ownerId)) throw new Error("Your OpenClass account changed. Reload this course.");
      const available = nextAccounts.filter((entry) => supportedProviders.has(entry.provider));
      setAccounts(available);
      setScope(nextScope);
      setSelected(nextScope.scope.connection_ids);
      const nextId = nextScope.scope.connection_ids.find((id) => available.some((entry) => entry.id === id && entry.status === "connected" && collections(entry).length)) ?? "";
      setConnectionId(nextId);
      setLocation(initialLocation(available.find((entry) => entry.id === nextId)));
      setItems([]); setCursor(null); setLoaded(false);
    } catch (failure) { if (request === sequence.current) setError(message(failure)); }
    finally { if (request === sequence.current) setBusy(""); }
  }, [ownerId, packageId]);

  useEffect(() => {
    const timer = window.setTimeout(() => { void refresh(); }, 0);
    const stopListening = listenForConnectionChanges(() => void refresh());
    return () => { sequence.current += 1; window.clearTimeout(timer); stopListening(); };
  }, [refresh]);

  async function saveScope() {
    if (!scope || busy) return;
    const request = ++sequence.current;
    setBusy("saving"); setError(""); setNotice("");
    try {
      const saved = await saveCourseConnectionScope(packageId, selected, scope.revision);
      if (request !== sequence.current) return;
      setScope(saved); setSelected(saved.scope.connection_ids);
      const nextId = saved.scope.connection_ids.includes(connectionId) ? connectionId : saved.scope.connection_ids[0] ?? "";
      setConnectionId(nextId);
      setLocation(initialLocation(accounts.find((entry) => entry.id === nextId)));
      setItems([]); setCursor(null); setLoaded(false);
      setNotice("Course access saved. Choose a resource to add to Sources.");
    } catch (failure) {
      if (request === sequence.current) setError(`${message(failure)}${failure instanceof ApiRequestError && failure.status === 409 ? " Reload course access before trying again." : ""}`);
    } finally { if (request === sequence.current) setBusy(""); }
  }

  async function browse(next = false, target = location, search = query) {
    if (!connectionId) return;
    const request = ++sequence.current;
    setBusy("browsing"); setError(""); setNotice("");
    try {
      const page: ConnectionResourcePage = await browseConnectedSources(connectionId, { ...target, query: search, ...(next && cursor ? { cursor } : {}) });
      if (request !== sequence.current) return;
      if (page.connection_id !== connectionId || page.evidence_ready !== false) throw new Error("The selected account changed. Refresh connected accounts.");
      setItems((current) => next ? [...current, ...page.items] : page.items);
      setCursor(page.next_cursor); setLoaded(true);
    } catch (failure) { if (request === sequence.current) setError(message(failure)); }
    finally { if (request === sequence.current) setBusy(""); }
  }

  async function importItem(item: ConnectionResource) {
    if (!scope || !connectionId || !item.ref || busy || !importable(item)) return;
    const request = ++sequence.current;
    setBusy(`import:${item.resource_id}`); setError(""); setNotice("");
    try {
      const ref = { ...item.ref, ...(location.capability ? { capability: location.capability } : {}), package_id: packageId, scope_revision: scope.revision, purpose: "use" };
      const result = await importConnectedSource(packageId, connectionId, ref);
      if (request !== sequence.current) return;
      setNotice(result.reused ? `“${item.title}” is already in Sources at this version.` : `“${item.title}” was added to Sources (${result.status}). Follow processing below; reference it when ready.`);
      await onChanged();
    } catch (failure) { if (request === sequence.current) setError(message(failure)); }
    finally { if (request === sequence.current) setBusy(""); }
  }

  function chooseAccount(id: string) {
    sequence.current += 1;
    setConnectionId(id); setLocation(initialLocation(accounts.find((entry) => entry.id === id))); setHistory([]); setQuery(""); setItems([]); setCursor(null); setLoaded(false); setBusy(""); setError("");
  }

  function chooseCollection(key: string) {
    sequence.current += 1;
    const choice = collections(accounts.find((entry) => entry.id === connectionId)).find((entry) => entry.key === key);
    setLocation(choice ? { kind: choice.kind, capability: choice.capability } : {}); setHistory([]); setQuery(""); setItems([]); setCursor(null); setLoaded(false); setBusy(""); setError("");
  }

  function open(item: ConnectionResource) {
    if (!item.browse) return;
    const target: ConnectionResourceQuery = { kind: item.browse.kind, resource_id: item.browse.resource_id, capability: item.browse.capability ?? location.capability };
    setHistory((current) => [...current, { ...location, query }]);
    setLocation(target); setQuery(item.browse.query ?? "");
    void browse(false, target, item.browse.query ?? "");
  }

  function back() {
    const target = history.at(-1);
    if (!target) return;
    setHistory((current) => current.slice(0, -1));
    setLocation(target); setQuery(target.query ?? "");
    void browse(false, target, target.query ?? "");
  }

  const active = accounts.filter((entry) => entry.status === "connected" && collections(entry).length > 0 && scope?.scope.connection_ids.includes(entry.id));
  const availableCollections = collections(active.find((entry) => entry.id === connectionId));
  const scopeChanged = Boolean(scope && (selected.length !== scope.scope.connection_ids.length || selected.some((id) => !scope.scope.connection_ids.includes(id))));
  return <section className="mt-3 space-y-2 border-t border-gray-100 pt-3 text-xs" aria-label="Connected account sources" data-testid="connected-sources-controls">
    <div className="flex items-center justify-between gap-2"><h3 className="font-semibold text-gray-800">Add from connected accounts</h3><Link href="/settings/connections" className="text-blue-700 underline">Manage accounts</Link></div>
    <p className="text-[11px] leading-5 text-gray-500">Choose an account and add a resource to Sources. Once processing is ready, use the existing source or chapter reference button to ask a question. Browsing only lists names.</p>
    {busy === "loading" ? <p key="loading" className="text-gray-500"><span>Loading connected accounts…</span></p> : null}
    {scope && !accounts.length ? <p key="empty" className="text-gray-500"><span>No available source accounts. Connect one in Settings.</span></p> : null}
    {accounts.length ? <div className="space-y-1">{accounts.map((entry) => <label key={entry.id} className="flex items-center gap-2"><input type="checkbox" checked={selected.includes(entry.id)} disabled={Boolean(busy) || (entry.status !== "connected" && !selected.includes(entry.id))} onChange={() => setSelected((current) => current.includes(entry.id) ? current.filter((id) => id !== entry.id) : [...current, entry.id])} /><span translate="no"><LiveText text={`${entry.display_name} · ${entry.provider}${entry.status !== "connected" ? ` · ${entry.status === "paused" ? "Paused" : "Reconnect required"}` : ""}`} /></span></label>)}</div> : null}
    {scope ? <button type="button" className={buttonClass} disabled={Boolean(busy) || !scopeChanged} onClick={() => void saveScope()}><LiveText text={busy === "saving" ? "Saving…" : "Save course access"} /></button> : null}
    {active.length ? <div className="space-y-2 border-t border-gray-100 pt-2">
      <label className="block font-medium text-gray-700">Browse account<select aria-label="Browse account" className="mt-1 block w-full rounded border border-gray-200 bg-white p-1.5" value={connectionId} disabled={Boolean(busy)} onChange={(event) => chooseAccount(event.target.value)}>{active.map((entry) => <option key={entry.id} value={entry.id}>{entry.display_name} · {entry.provider}</option>)}</select></label>
      {availableCollections.length > 1 ? <label className="block font-medium text-gray-700">Resource collection<select aria-label="Resource collection" className="mt-1 block w-full rounded border border-gray-200 bg-white p-1.5" value={`${location.capability ?? ""}:${location.kind ?? ""}`} disabled={Boolean(busy)} onChange={(event) => chooseCollection(event.target.value)}>{availableCollections.map((entry) => <option key={entry.key} value={entry.key}>{entry.label}</option>)}</select></label> : null}
      {history.length ? <button type="button" className={buttonClass} disabled={Boolean(busy)} onClick={back}>Back</button> : null}
      <div className="flex gap-2"><input aria-label="Search connected resources" className="min-w-0 flex-1 rounded border border-gray-200 p-1.5" value={query} disabled={Boolean(busy)} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); void browse(false, location); } }} placeholder="Search or browse resources" /><button type="button" className={buttonClass} disabled={Boolean(busy) || !connectionId} onClick={() => void browse(false, location)}><LiveText text={busy === "browsing" ? "Browsing…" : "Browse"} /></button></div>
      {loaded && !items.length ? <p key="no-resources" className="text-gray-500"><span>No authorized resources found. Choose resources in Settings or search another term.</span></p> : null}
      <div className="max-h-64 space-y-1 overflow-y-auto">{items.map((item, index) => <div key={`${item.kind}:${item.resource_id}:${index}`} className="flex items-center justify-between gap-2 rounded border border-gray-100 p-2"><span className="min-w-0 break-words" translate="no"><LiveText text={item.title || item.resource_id} /><span className="block text-[10px] text-gray-500">{item.kind}</span></span><span className="flex shrink-0 gap-1">{item.browse ? <button type="button" className={buttonClass} disabled={Boolean(busy)} onClick={() => open(item)} aria-label={`Open ${item.title}`}>Open</button> : null}{importable(item) ? <button type="button" className={buttonClass} disabled={Boolean(busy)} onClick={() => void importItem(item)} aria-label={`Add ${item.title} to Sources`}><LiveText text={busy === `import:${item.resource_id}` ? "Adding…" : "Add to Sources"} /></button> : null}</span></div>)}</div>
      {cursor ? <button type="button" className={buttonClass} disabled={Boolean(busy)} onClick={() => void browse(true)}>Load more</button> : null}
    </div> : null}
    {notice ? <p key={`notice:${notice}`} role="status" className="text-emerald-700"><LiveText text={notice} /></p> : null}
    {error ? <div key={`error:${error}`} role="alert" className="space-y-1 text-red-700"><LiveText text={error} /><button type="button" className={buttonClass} disabled={Boolean(busy)} onClick={() => void refresh()}>Reload connected accounts</button></div> : null}
  </section>;
}
