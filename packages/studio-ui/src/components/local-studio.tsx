"use client";

import { useEffect, useMemo, useState } from "react";
import { CourseStudio } from "./course-studio";
import { LocalStudioControls } from "./local-studio-controls";
import { StudioHostProvider, type StudioHost } from "../contexts/studio-host-context";
import { configureLocalStudioTransport } from "../lib/studio-transport";
import { localStudio, type LocalStudioBootstrap } from "../lib/local-studio-api";
import { api } from "../lib/api";
import { normalizeProfileSettings } from "../lib/profile-settings-state";

export function LocalStudio({ launchToken, apiOrigin }: { launchToken: string; apiOrigin: string }) {
  const [bootstrap, setBootstrap] = useState<LocalStudioBootstrap | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let disposed = false;
    async function connect() {
      try {
        configureLocalStudioTransport(apiOrigin);
        const response = await fetch(`${apiOrigin}/api/workbench/v1/session`, {
          method: "POST", credentials: "include", cache: "no-store",
          headers: { "X-OpenClass-Launch-Token": launchToken },
        });
        if (!response.ok) throw new Error("Unable to open this local workspace. Check that the Studio launcher is running.");
        const result = await response.json() as LocalStudioBootstrap;
        if (result.mode !== "local" || result.principal.kind !== "local") throw new Error("The server is not a local Studio workspace.");
        if (!disposed) { setBootstrap(result); setError(""); }
      } catch (failure) { if (!disposed) setError(failure instanceof Error ? failure.message : "Unable to start Studio"); }
    }
    void connect();
    return () => { disposed = true; };
  }, [launchToken, apiOrigin, attempt]);
  const host = useMemo<StudioHost>(() => ({
    mode: "local", homeHref: "/studio?files=1", client: api,
    capabilities: { speech: false, realtime: false, cloudSources: false, platformCollaboration: false },
    principal: bootstrap?.principal,
    getModelPreferences: async () => {
      const value = await localStudio.preferences();
      return { principalId: value.principal_id, settings: normalizeProfileSettings(value.settings) };
    },
    accountMenu: <LocalStudioControls />,
    renderAccountMenu: (navigation) => <LocalStudioControls onNavigate={navigation.requestLeave} />,
  }), [bootstrap]);
  if (!bootstrap) return <main className="flex min-h-screen flex-col items-center justify-center gap-4 bg-white p-8 text-stone-800">
    <p>{error || "Opening local workspace…"}</p>
    {error ? <button className="rounded-lg bg-stone-900 px-4 py-2 text-white" onClick={() => setAttempt(value => value + 1)}>Retry</button> : null}
  </main>;
  return <StudioHostProvider host={host}><CourseStudio /></StudioHostProvider>;
}
