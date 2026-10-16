"use client";

import Link from "next/link";
import { useState } from "react";
import { GitPullRequest, X } from "lucide-react";
import { useStudioApi, useStudioContext } from "../contexts/studio-api-context";
import { coursePrApi } from "../lib/course-pr-api";
import type { Lesson } from "../types";
import type { CoursePrSource, CoursePrTarget } from "../types/course-pr";

export function CoursePrSubmit({ lesson, disabledReason, prepare }: { lesson: Lesson; disabledReason?: string | null; prepare?: () => Promise<boolean> }) {
  const context = useStudioContext();
  const api = useStudioApi();
  const [open, setOpen] = useState(false);
  const [address, setAddress] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [resolved, setResolved] = useState<{ source: CoursePrSource; target: CoursePrTarget } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [createdId, setCreatedId] = useState("");
  if (context.kind === "temporary" || context.kind === "review" || context.kind === "version") return null;
  const repositoryId = context.kind === "repository" ? context.repositoryId : undefined;

  async function resolve() {
    if (disabledReason) { setError(disabledReason); return; }
    setBusy(true); setError(""); setResolved(null);
    try {
      if (prepare && !(await prepare())) throw new Error("Save the board and finish or discard the History draft before submitting a PR.");
      const current = await api.getCoursePackage(repositoryId);
      if (!current.ridoc_revision) throw new Error("Save this course before submitting a PR.");
      const source: CoursePrSource = { target_address: address.trim(), source_repository_id: repositoryId,
        source_package_id: repositoryId ? undefined : current.id, source_lesson_id: lesson.id, source_branch: lesson.history_graph.current_branch,
        expected_source_revision: current.ridoc_revision, target_branch: "main" };
      const target = await coursePrApi.resolve(source);
      setResolved({ source, target });
    } catch (failure) { setError(failure instanceof Error ? failure.message : "Could not resolve the target course."); }
    finally { setBusy(false); }
  }
  async function submit() {
    if (!resolved) return;
    setBusy(true); setError("");
    try {
      const result = await coursePrApi.create({ ...resolved.source, title: title.trim(), description: description.trim() });
      setCreatedId(result.id);
    } catch (failure) { setError(failure instanceof Error ? failure.message : "The PR was not submitted. Please try again."); }
    finally { setBusy(false); }
  }
  return <section aria-label="Course PR submission" className="mb-4 rounded-xl border border-neutral-200 bg-neutral-50 p-3 dark:border-neutral-700 dark:bg-neutral-950">
    <form className="flex flex-wrap items-end gap-2" onSubmit={(event) => {
      event.preventDefault();
      if (busy || disabledReason || !address.trim()) return;
      setOpen(true); setCreatedId(""); setTitle((current) => current || `Improve ${lesson.title}`);
      void resolve();
    }}>
      <label className="min-w-[180px] flex-1 text-xs font-medium text-neutral-700 dark:text-neutral-200"><span>Target project address</span><input value={address} inputMode="url" disabled={busy} onChange={(event) => { setAddress(event.target.value); setResolved(null); setCreatedId(""); setError(""); }} placeholder="/author/course or a full course URL" className="mt-1 w-full rounded-lg border border-neutral-300 bg-white px-3 py-2 text-sm text-neutral-900 outline-none focus:border-violet-500 focus:ring-2 focus:ring-violet-200 disabled:opacity-50 dark:border-neutral-600 dark:bg-neutral-900 dark:text-neutral-100" /></label>
      <button type="submit" disabled={busy || Boolean(disabledReason) || !address.trim()} title={disabledReason ?? undefined} className="inline-flex shrink-0 items-center gap-2 rounded-lg border border-violet-200 bg-white px-3 py-2 text-xs font-semibold text-violet-800 disabled:opacity-40 dark:border-violet-800 dark:bg-neutral-900 dark:text-violet-300"><GitPullRequest className="h-4 w-4" /><span>Submit course PR</span></button>
    </form>
    <p className="mt-1 text-xs text-stone-500">Use the target repository’s course URL. Submissions require the same course or a verified Fork.</p>
    {disabledReason ? <p className="mt-1 text-xs text-stone-500">{disabledReason}</p> : null}
    {open ? <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/30 p-4"><section role="dialog" aria-modal="true" aria-label="Submit course PR" className="max-h-[85vh] w-full max-w-xl overflow-auto rounded-2xl bg-white p-6 shadow-xl">
      <header className="mb-4 flex items-center justify-between"><h2 className="font-semibold">Submit course PR</h2><button type="button" disabled={busy} aria-label="Close PR submission" onClick={() => setOpen(false)}><X className="h-5 w-5" /></button></header>
      <p className="mb-4 text-sm text-stone-600">Submit all changes from {lesson.title}. Other lessons in the target course will stay unchanged.</p>
      {createdId ? <p role="status" className="rounded-lg bg-emerald-50 p-4 text-sm text-emerald-800">PR submitted.<Link className="ml-2 underline" href={`/course-prs/${encodeURIComponent(createdId)}`}>View PR</Link></p> : <>
        <label className="block text-sm">Related target course address<input autoFocus value={address} disabled={busy} onChange={(event) => { setAddress(event.target.value); setResolved(null); }} placeholder="/author/course or a full course URL" className="mt-1 w-full rounded-lg border px-3 py-2" /></label>
        <button type="button" disabled={busy || !address.trim()} onClick={() => void resolve()} className="mt-3 rounded-lg border px-3 py-2 text-sm disabled:opacity-40">{busy && !resolved ? "Checking…" : "Check target and shared origin"}</button>
        {resolved ? <div className="mt-4 rounded-xl border border-violet-200 bg-violet-50 p-4 text-sm"><p className="font-semibold">Target: {resolved.target.target_repository.title}</p><p className="break-all text-violet-800">{resolved.target.target_repository.canonical_path}</p><p className="mt-1 text-xs text-stone-600">Submit to main · Shared origin verified</p></div> : null}
        <label className="mt-4 block text-sm">Title<input value={title} onChange={(event) => setTitle(event.target.value)} className="mt-1 w-full rounded-lg border px-3 py-2" /></label>
        <label className="mt-4 block text-sm">Description of changes<textarea rows={3} value={description} onChange={(event) => setDescription(event.target.value)} className="mt-1 w-full rounded-lg border px-3 py-2" /></label>
        <button type="button" disabled={busy || !resolved || !title.trim()} onClick={() => void submit()} className="mt-5 rounded-lg bg-violet-700 px-4 py-2 text-sm font-semibold text-white disabled:opacity-40">{busy && resolved ? "Submitting…" : "Submit PR"}</button>
      </>}
      {error ? <p role="alert" className="mt-3 text-sm text-rose-700">{error}</p> : null}
    </section></div> : null}
  </section>;
}
