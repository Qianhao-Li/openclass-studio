"use client";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "next/navigation";
import { api } from "../lib/api";
import type { CoursePackage } from "../types";
import { studioPlatform, type PlatformConnection, type PlatformGrant } from "../lib/studio-platform-api";
import { MODEL_CREDENTIALS_CHANGED_EVENT } from "./course-studio/model-catalog";
import { useWorkspacePrincipal } from "../contexts/studio-host-context";
import { pendingSyncOperation, completeSyncOperation } from "../lib/studio-sync-operation";
import type { PullCourseRequest } from "../lib/studio-platform-api";
import { useRoutePanel } from "../hooks/use-route-panel";

export function LocalPlatformControls({ onNavigate, renderTrigger }: {
  onNavigate?: (href: string) => Promise<void>;
  renderTrigger?: (props: { onOpen: () => void; connected: boolean }) => ReactNode;
}) {
  const params = useSearchParams();
  const principal = useWorkspacePrincipal();
  const [open, setOpen] = useRoutePanel(params.get("platform") === "1");
  const [status, setStatus] = useState<PlatformConnection | null>(null);
  const [grant, setGrant] = useState<PlatformGrant | null>(null);
  const [transfer, setTransfer] = useState(false);
  const [course, setCourse] = useState<CoursePackage | null>(null);
  const [remote, setRemote] = useState<{ id: string; title: string; revision: string }[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [target, setTarget] = useState("");
  const [prTitle, setPrTitle] = useState("");
  const operation = useRef<{ revision: string; id: string } | null>(null);
  const lessonId = params.get("lesson");
  const refreshModels = () => window.dispatchEvent(new Event(MODEL_CREDENTIALS_CHANGED_EVENT));
  useEffect(() => {
    let active = true;
    void studioPlatform.status().then(value => { if (active) setStatus(value); }).catch(() => {});
    return () => { active = false; };
  }, []);
  useEffect(() => {
    if (!open) return;
    let active = true;
    void Promise.all([studioPlatform.status(), api.getCoursePackage(null, lessonId).catch(() => null)]).then(([value, local]) => {
      if (!active) return;
      setStatus(value); setCourse(local);
      if (value.connected && value.scopes?.includes("repositories.read")) void studioPlatform.courses().then(rows => { if (active) setRemote(rows); }).catch(error => { if (active) setMessage(error.message); });
    }).catch(error => { if (active) setMessage(error.message); });
    return () => { active = false; };
  }, [open, lessonId]);
  useEffect(() => {
    if (!grant || !open) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const result = await studioPlatform.poll(grant!.attempt_id);
        if (!active) return;
        if (result.status === "connected") { setGrant(null); setStatus(await studioPlatform.status()); setMessage("OpenClass connected. Choose a model marked Platform quota to use it."); refreshModels(); }
        else if (result.status === "pending") timer = setTimeout(poll, (result.interval || 5) * 1000);
        else { setGrant(null); setMessage(`Connection ${result.status}.`); }
      } catch (error) { if (active) setMessage(error instanceof Error ? error.message : "Unable to check platform connection."); }
    }
    timer = setTimeout(poll, grant.interval * 1000);
    return () => { active = false; clearTimeout(timer); };
  }, [grant, open]);
  async function run(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setMessage("");
    try { await action(); } catch (error) { setMessage(error instanceof Error ? error.message : "The platform operation failed."); }
    finally { setBusy(false); }
  }
  async function push() {
    if (!course?.ridoc_revision) throw new Error("Save this course before Push.");
    const storageKey = `openclass:sync:${principal?.id}:${status?.origin}:${status?.account_id}:push:${course.id}:${course.ridoc_revision}`;
    if (operation.current?.revision !== course.ridoc_revision) operation.current = { revision: course.ridoc_revision, id: pendingSyncOperation(storageKey, () => ({ operation_id: crypto.randomUUID() })).operation_id };
    const receipt = await studioPlatform.push(course, operation.current.id);
    completeSyncOperation(storageKey); operation.current = null;
    setRemote(await studioPlatform.courses());
    setMessage(`Pushed course revision ${receipt.ridoc_revision.slice(0, 12)}.`);
  }
  return <>{renderTrigger ? renderTrigger({ onOpen: () => setOpen(true), connected: status?.connected === true })
    : <button className="rounded-lg border px-3 py-1.5" onClick={() => setOpen(true)}>OpenClass connection</button>}
    {open ? <div className="fixed inset-0 z-[310] flex items-center justify-center bg-black/30 p-6"><section role="dialog" aria-modal="true" aria-label="OpenClass connection" className="max-h-[85vh] w-full max-w-xl space-y-4 overflow-auto rounded-2xl bg-white p-6 shadow-xl">
      <header className="flex items-center justify-between"><h2 className="text-lg font-semibold">OpenClass connection</h2><button aria-label="Close platform connection" onClick={() => setOpen(false)}>✕</button></header>
      <p className="text-sm text-stone-600">Local courses stay on this device. Connecting allows the permissions you approve; uploading a course requires Push.</p>
      <p className="text-sm">Platform: {status?.origin || "https://open-classes.com"}</p>
      {status?.connected ? <>
        <p className="text-sm">Connected account: {status.account_id}</p>
        <button disabled={busy} className="rounded-lg border px-3 py-2" onClick={() => void run(async () => { const result = await studioPlatform.disconnect(); setStatus(await studioPlatform.status()); setRemote([]); refreshModels(); setMessage(result.message || "Disconnected. Local courses and personal model accounts remain available."); })}>Disconnect platform</button>
        {status.scopes?.includes("repositories.write") ? <div className="space-y-3 rounded-xl border p-4">
          <p className="text-sm">Saved course: {course?.title || "No active course"}</p>
          <button disabled={busy || !course?.ridoc_revision} className="rounded-lg bg-stone-900 px-3 py-2 text-white" onClick={() => void run(push)}>Push saved course</button>
          <div className="space-y-2">{remote.map(row => <div className="flex items-center justify-between gap-3 rounded-lg border p-2" key={row.id}><span className="text-sm">{row.title}</span><button disabled={busy} className="rounded border px-2 py-1 text-sm" onClick={() => void run(async () => {
            const association = course ? await studioPlatform.association(course.id) : null;
            const storageKey = `openclass:sync:${principal?.id}:${status?.origin}:${status?.account_id}:pull:${row.id}:${row.revision}`;
            const request = pendingSyncOperation<PullCourseRequest>(storageKey, () => ({ operation_id: crypto.randomUUID(), revision: row.revision,
              ...(association?.repository_id === row.id && course?.ridoc_revision ? { package_id: course.id, expected_ridoc_revision: course.ridoc_revision } : {}) }));
            const pulled = await studioPlatform.pull(row.id, request);
            completeSyncOperation(storageKey);
            setOpen(false); if (onNavigate) await onNavigate(`/studio?lesson=${encodeURIComponent(pulled.active_lesson_id || "")}`);
          })}>Pull</button></div>)}</div>
          <input className="w-full rounded-lg border p-2 text-sm" aria-label="PR target course address" placeholder="Target course address" value={target} onChange={event => setTarget(event.target.value)} />
          <input className="w-full rounded-lg border p-2 text-sm" aria-label="PR title" placeholder="Pull request title" value={prTitle} onChange={event => setPrTitle(event.target.value)} />
          <button disabled={busy || !course?.active_lesson_id || !target || !prTitle} className="rounded-lg border px-3 py-2" onClick={() => void run(async () => {
            if (!course?.active_lesson_id || !course.ridoc_revision) return;
            const result = await studioPlatform.submitPR({ package_id: course.id, source_lesson_id: course.active_lesson_id, expected_ridoc_revision: course.ridoc_revision, target_address: target, title: prTitle });
            setMessage(`Pull request submitted: ${result.id}`);
          })}>Submit course PR</button>
        </div> : <p className="text-sm">This connection permits model usage. Reconnect with course exchange enabled to Push, Pull or submit PRs.</p>}
      </> : <><label className="flex gap-2 text-sm"><input type="checkbox" checked={transfer} onChange={event => setTransfer(event.target.checked)} />Allow course download, Push and PR submission</label>
        <button disabled={busy || Boolean(grant)} className="rounded-lg bg-stone-900 px-3 py-2 text-white" onClick={() => void run(async () => setGrant(await studioPlatform.connect(transfer ? ["models.invoke", "repositories.read", "repositories.write", "course_prs.write"] : ["models.invoke"])))}>Connect OpenClass account</button>
        {grant ? <div className="space-y-2 text-sm"><p>Authorization code: <strong className="font-mono">{grant.user_code}</strong></p><a className="underline" href={grant.verification_uri_complete} target="_blank" rel="noreferrer">Open platform authorization</a></div> : null}
      </>}
      {message ? <p role="status" className="text-sm">{message}</p> : null}
    </section></div> : null}</>;
}
