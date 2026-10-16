"use client";

import { publicAgentActivityText } from "../../lib/agent-activity";

import { RefreshCw } from "lucide-react";
import { useEffect } from "react";

import { useWorkspacePrincipal, useStudioHost } from "../../contexts/studio-host-context";
import { useSourceCloudSetting } from "../../hooks/course-studio/use-source-cloud-setting";
import { sourceCloudApi } from "../../lib/source-cloud-api";

type SourceCloudControlsProps = {
  disabled?: boolean;
  onSearchPreferenceChange?: (enabled: boolean | null) => void;
};

export function SourceCloudControls({ disabled, onSearchPreferenceChange }: SourceCloudControlsProps) {
  const user = useWorkspacePrincipal();
  if (!user || disabled) return null;
  // Account changes unmount both request owners before the next identity loads.
  return <section key={`${user.id}:${user.kind}`} className="mt-3 space-y-3 border-t border-gray-100 pt-3" aria-label="Source search settings">
    <AutoSourceRetrievalControl onPreferenceChange={onSearchPreferenceChange} />
  </section>;
}

function SettingError({ message, label, onRetry }: { message: string; label: string; onRetry: () => Promise<void> }) {
  if (!message) return null;
  return <div role="alert" className="flex items-start justify-between gap-2 text-xs text-red-700">
    <span>{publicAgentActivityText(message)}</span>
    <button type="button" onClick={() => void onRetry()} aria-label={label} className="shrink-0"><RefreshCw className="h-3.5 w-3.5" /></button>
  </div>;
}

function AutoSourceRetrievalControl({ onPreferenceChange }: { onPreferenceChange?: (enabled: boolean | null) => void }) {
  const host = useStudioHost();
  const { value: preference, error, busy, refresh, run } = useSourceCloudSetting(sourceCloudApi.preference, "Could not load source settings.");
  useEffect(() => { onPreferenceChange?.(preference?.enabled ?? null); }, [onPreferenceChange, preference]);
  return <div className="space-y-2">
    <div className="flex items-center justify-between gap-3">
      <div>
        <p className="text-xs font-semibold text-gray-800">Automatic source search</p>
        <p id="auto-source-search-description" className="mt-1 text-[11px] leading-5 text-gray-500">Allow AI to find relevant material in your Sources during teaching. Explicit file and chapter references remain available when this is off.</p>
      </div>
      <button type="button" role="switch" aria-label="Automatic source search" aria-describedby="auto-source-search-description"
        aria-checked={Boolean(preference?.enabled)} data-testid="auto-source-retrieval-toggle"
        disabled={!preference || busy} onClick={() => {
          if (preference) void run(() => sourceCloudApi.setPreference(!preference.enabled, preference.revision));
        }}
        className={`relative h-5 w-9 shrink-0 rounded-full transition-colors disabled:opacity-50 ${preference?.enabled ? "bg-gray-900" : "bg-gray-300"}`}>
        <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform dark:bg-white ${preference?.enabled ? "left-0.5 translate-x-4" : "left-0.5"}`} />
      </button>
    </div>
    <p className="text-[11px] leading-5 text-gray-400">{host.mode === "local" ? "Original sources and their verified reading data stay on this device." : "Uploads prepare ordinary reading and a complete catalog automatically. Prepare enhanced search separately for each file. Only Sources files and their derived content use Google storage."}</p>
    <SettingError message={error} label="Retry source settings" onRetry={refresh} />
  </div>;
}
