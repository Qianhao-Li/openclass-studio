"use client";

import { useRef, useState } from "react";

import { useStudioHost } from "../../contexts/studio-host-context";
import { StudioEmptyState } from "./studio-navigation";
import { useInterfaceLanguage } from "../../contexts/interface-language-context";
import { useStudioApi } from "../../contexts/studio-api-context";

export function StudioWorkspaceEntry({ empty, error, onReload }: {
  empty: boolean;
  error: string | null;
  onReload: () => void;
}) {
  const api = useStudioApi();
  const host = useStudioHost();
  const { texts } = useInterfaceLanguage();
  const [creating, setCreating] = useState(false);
  const [creationError, setCreationError] = useState<string | null>(null);
  const inFlight = useRef(false);

  async function createFirstPage() {
    if (!empty || inFlight.current) return;
    inFlight.current = true;
    setCreating(true);
    setCreationError(null);
    try {
      await api.generateLesson({ startBlank: true });
      // Reload the authoritative package separately: a failed read must never
      // retry a successful creation and leave the guest with duplicate courses.
      onReload();
    } catch (cause) {
      setCreationError(cause instanceof Error ? cause.message : "Failed to create course");
      inFlight.current = false;
      setCreating(false);
    }
  }

  const visibleError = creationError ?? error;
  return <main className="flex min-h-screen flex-col bg-[#f8f6f0] text-[#1a1a1a]">
    <header className="flex h-12 shrink-0 items-center justify-between border-b border-gray-200 bg-white px-4">
      <span className="text-sm font-semibold">OpenClass Studio</span>
      {host.accountMenu}
    </header>
    {visibleError ? <div role="alert" className="mx-6 mt-6 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
      {visibleError}
    </div> : null}
    {empty ? <StudioEmptyState texts={texts.studio} creating={creating} onCreate={() => void createFirstPage()} /> :
      <section className="flex flex-1 flex-col items-center justify-center gap-5 p-6">
        <p className="text-sm text-gray-500">{texts.studio.packageMissing}</p>
        <button type="button" onClick={onReload} className="rounded-full bg-stone-950 px-5 py-3 text-sm font-medium text-white hover:bg-stone-800">
          Retry
        </button>
      </section>}
  </main>;
}
