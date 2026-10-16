"use client";

import { useCallback, useEffect } from "react";
import { useWorkspacePrincipal } from "../../contexts/studio-host-context";

const CHANNEL = "openclass-source-library";

export function useSourceLibrarySync(disabled: boolean, refresh: () => Promise<unknown>) {
  const user = useWorkspacePrincipal();
  const ownerId = user?.id;
  const notifyLibraryChanged = useCallback(() => {
    if (!ownerId || typeof BroadcastChannel === "undefined") return;
    const channel = new BroadcastChannel(CHANNEL);
    channel.postMessage({ ownerId });
    channel.close();
  }, [ownerId]);

  useEffect(() => {
    if (disabled || !ownerId) return;
    const refreshVisible = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    const channel = typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel(CHANNEL);
    if (channel) channel.onmessage = (event: MessageEvent) => {
      if (event.data?.ownerId === ownerId) refreshVisible();
    };
    window.addEventListener("focus", refreshVisible);
    document.addEventListener("visibilitychange", refreshVisible);
    // Also discover changes from another device while this Sources panel is open.
    const timer = window.setInterval(refreshVisible, 30_000);
    return () => {
      channel?.close();
      window.clearInterval(timer);
      window.removeEventListener("focus", refreshVisible);
      document.removeEventListener("visibilitychange", refreshVisible);
    };
  }, [disabled, ownerId, refresh]);

  return notifyLibraryChanged;
}
