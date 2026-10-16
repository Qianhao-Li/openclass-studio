"use client";

import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { LocalPlatformControls } from "./local-platform-controls";
import { LocalAccountMenu } from "./local-account-menu";
import { useRoutePanel } from "../hooks/use-route-panel";
import { api } from "../lib/api";
import { MODEL_CREDENTIALS_CHANGED_EVENT } from "./course-studio/model-catalog";
import type { AIProviderCredentialStatus, CoursePackage, CodexLoginStartResponse } from "../types";
import type { WorkspaceSummary } from "../types/workspace-summary";

export function LocalStudioControls({ onNavigate }: { onNavigate?: (href: string) => Promise<void> } = {}) {
  const params = useSearchParams();
  const [panel, setPanel] = useRoutePanel<"files" | "models" | null>(params.get("files") === "1" ? "files" : null);
  const [workspace, setWorkspace] = useState<WorkspaceSummary | null>(null);
  const [providers, setProviders] = useState<AIProviderCredentialStatus[]>([]);
  const [provider, setProvider] = useState("");
  const [key, setKey] = useState("");
  const [config, setConfig] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [modelLogin, setModelLogin] = useState<CodexLoginStartResponse | null>(null);
  const [loginMessage, setLoginMessage] = useState("");
  const upload = useRef<HTMLInputElement | null>(null);
  const selected = providers.find(item => item.provider === provider);

  useEffect(() => {
    if (!panel) return;
    let disposed = false;
    const load = panel === "files"
      ? api.getWorkspaceSummary().then(value => { if (!disposed) { setWorkspace(value); setError(""); } })
      : api.getModelCredentials().then(value => { if (!disposed) { setProviders(value); setProvider(current => current || value[0]?.provider || ""); setError(""); } });
    void load.catch(cause => { if (!disposed) setError(cause instanceof Error ? cause.message : "Unable to load workspace"); });
    return () => { disposed = true; setKey(""); };
  }, [panel]);
  useEffect(() => {
    if (!modelLogin || panel !== "models") return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const value = await api.getCodexLoginStatus(modelLogin!.login_id);
        if (disposed) return;
        if (value.status === "succeeded") {
          setLoginMessage("ChatGPT account connected."); setModelLogin(null);
          window.dispatchEvent(new Event(MODEL_CREDENTIALS_CHANGED_EVENT));
        } else if (value.status !== "pending") { setError(value.error || `Connection ${value.status}.`); setModelLogin(null); }
        else timer = setTimeout(poll, 2500);
      } catch (cause) { if (!disposed) setError(cause instanceof Error ? cause.message : "Unable to check account connection"); }
    }
    void poll();
    return () => { disposed = true; clearTimeout(timer); };
  }, [modelLogin, panel]);

  async function navigate(href: string) {
    if (onNavigate) await onNavigate(href); else window.location.assign(href);
  }
  async function openCourse(course: Pick<CoursePackage, "active_lesson_id">) {
    if (!course.active_lesson_id) throw new Error("This course has no active page.");
    await navigate(`/studio?lesson=${encodeURIComponent(course.active_lesson_id)}`);
    setPanel(null);
  }
  async function run(action: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true); setError("");
    try { await action(); } catch (cause) { setError(cause instanceof Error ? cause.message : "The operation could not be completed"); }
    finally { setBusy(false); }
  }
  async function saveKey() {
    await api.saveModelCredential(provider, key, config);
    setKey("");
    setProviders(await api.getModelCredentials());
    window.dispatchEvent(new Event(MODEL_CREDENTIALS_CHANGED_EVENT));
  }
  return <>
    <LocalPlatformControls onNavigate={onNavigate} renderTrigger={({ onOpen, connected }) => <LocalAccountMenu
      connected={connected}
      onCourses={() => setPanel("files")}
      onModels={() => setPanel("models")}
      onPlatform={onOpen}
    />} />
    {panel ? <div className="fixed inset-0 z-[300] flex items-center justify-center bg-black/30 p-6" role="presentation">
      <section role="dialog" aria-modal="true" aria-label={panel === "files" ? "Local courses" : "Personal models"} className="max-h-[85vh] w-full max-w-xl overflow-y-auto rounded-2xl bg-white p-6 shadow-xl">
        <header className="mb-5 flex items-center justify-between"><h2 className="text-lg font-semibold">{panel === "files" ? "Courses on this device" : "Your model accounts and API keys"}</h2><button aria-label="Close dialog" onClick={() => { setPanel(null); setKey(""); }}>✕</button></header>
        {error ? <p role="alert" className="mb-4 text-sm text-red-700">{error}</p> : null}
        {panel === "files" ? <>
          <div className="mb-5 flex gap-3"><button disabled={busy} className="rounded-lg bg-stone-900 px-4 py-2 text-white" onClick={() => void run(async () => openCourse(await api.generateLesson({ startBlank: true })))}>New course</button>
            <button disabled={busy} className="rounded-lg border px-4 py-2" onClick={() => upload.current?.click()}>Import RIDOC</button>
            <input ref={upload} type="file" accept=".ridoc" className="hidden" aria-label="Import RIDOC file" onChange={event => { const file = event.target.files?.[0]; if (file) void run(async () => openCourse(await api.importRidoc(file))); event.target.value = ""; }} />
          </div>
          <div className="space-y-2">{workspace?.packages.flatMap(course => course.lessons.map(lesson => <button key={`${course.id}:${lesson.id}`} disabled={busy} className="block w-full rounded-xl border p-3 text-left hover:bg-stone-50" onClick={() => void run(() => navigate(`/studio?lesson=${encodeURIComponent(lesson.id)}`))}>{lesson.title}</button>))}</div>
          {workspace?.packages.length === 0 ? <p className="text-sm text-stone-500">Create a course or open a RIDOC file to get started.</p> : null}
        </> : <>
          <p className="mb-4 text-sm text-stone-600">Personal model credentials stay on this device. Cloud AI requires an internet connection.</p>
          <div className="mb-5 rounded-xl border p-3">
            <button disabled={busy || Boolean(modelLogin)} className="rounded-lg border px-3 py-2" onClick={() => void run(async () => { setLoginMessage(""); setModelLogin(await api.startCodexConnectionLogin()); })}>Connect your ChatGPT account</button>
            {modelLogin ? <div className="mt-3 space-y-2 text-sm"><a href={modelLogin.verification_url} target="_blank" rel="noreferrer" className="underline">Open account authorization</a><p>Authorization code: <strong>{modelLogin.user_code}</strong></p><button onClick={() => void run(async () => { await api.cancelCodexLogin(modelLogin.login_id); setModelLogin(null); })}>Cancel connection</button></div> : null}
            {loginMessage ? <p role="status" className="mt-2 text-sm">{loginMessage}</p> : null}
          </div>
          <label className="block text-sm">Provider<select aria-label="Model provider" value={provider} onChange={event => { setProvider(event.target.value); setKey(""); setConfig({}); }} className="mt-1 block w-full rounded-lg border p-2">{providers.map(item => <option key={item.provider} value={item.provider}>{item.label}{item.configured ? " · Connected" : ""}</option>)}</select></label>
          <label className="mt-4 block text-sm">API key<input aria-label="Personal API key" type="password" autoComplete="off" value={key} onChange={event => setKey(event.target.value)} className="mt-1 block w-full rounded-lg border p-2" /></label>
          {selected?.configuration_fields?.map(field => <label key={field.name} className="mt-4 block text-sm">{field.label}<input value={config[field.name] || ""} onChange={event => setConfig(value => ({ ...value, [field.name]: event.target.value }))} placeholder={field.placeholder} className="mt-1 block w-full rounded-lg border p-2" /></label>)}
          <div className="mt-5 flex gap-3"><button disabled={busy || !provider || !key.trim()} className="rounded-lg bg-stone-900 px-4 py-2 text-white disabled:opacity-40" onClick={() => void run(saveKey)}>Save API key</button>
            {selected?.configured ? <button disabled={busy} className="rounded-lg border px-4 py-2" onClick={() => void run(async () => { await api.deleteModelCredential(provider); setProviders(await api.getModelCredentials()); window.dispatchEvent(new Event(MODEL_CREDENTIALS_CHANGED_EVENT)); })}>Remove key</button> : null}</div>
        </>}
      </section>
    </div> : null}
  </>;
}
