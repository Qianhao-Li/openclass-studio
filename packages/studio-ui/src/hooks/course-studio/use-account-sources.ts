"use client";

import { useCallback, useEffect, useRef, useState, type SetStateAction } from "react";
import { useWorkspacePrincipal } from "../../contexts/studio-host-context";
import { useStudioApi } from "../../contexts/studio-api-context";
import { useSourcePolling } from "./use-source-polling";
import { useSourceLibrarySync } from "./use-source-library-sync";
import type { SourceIngestionRecord } from "../../types";

const clients = new WeakMap<ReturnType<typeof useStudioApi>, Map<string, Promise<SourceIngestionRecord[]>>>();

function listSourcesOnce(api: ReturnType<typeof useStudioApi>, packageId: string, ownerId: string) {
  let requests = clients.get(api);
  if (!requests) {
    requests = new Map();
    clients.set(api, requests);
  }
  const key = `${ownerId}:${packageId}`;
  const pending = requests.get(key);
  if (pending) return pending;
  const request = api.listPackageSources(packageId).finally(() => {
    if (requests?.get(key) === request) requests.delete(key);
  });
  requests.set(key, request);
  return request;
}

export function useAccountSources(packageId: string, disabled: boolean, onError: (message: string) => void) {
  const api = useStudioApi();
  const user = useWorkspacePrincipal();
  const ownerId = user?.id ?? "";
  const scopeKey = `${ownerId}:${packageId}`;
  const [state, setState] = useState<{ scopeKey: string; sources: SourceIngestionRecord[] }>({ scopeKey, sources: [] });
  const [isLoading, setIsLoading] = useState(false);
  const busy = useRef(false);
  const sequence = useRef(0);
  const lifecycle = useRef(0);
  const sources = state.scopeKey === scopeKey ? state.sources : [];
  const setSources = useCallback((update: SetStateAction<SourceIngestionRecord[]>) => {
    setState((current) => ({ scopeKey, sources: typeof update === "function"
      ? update(current.scopeKey === scopeKey ? current.sources : []) : update }));
  }, [scopeKey]);

  useEffect(() => {
    busy.current = false;
    return () => { lifecycle.current += 1; sequence.current += 1; };
  }, [api, scopeKey]);

  const refreshSources = useCallback(async (): Promise<SourceIngestionRecord[] | null> => {
    if (disabled || !packageId || busy.current) return null;
    busy.current = true;
    const lifetime = lifecycle.current;
    setIsLoading(true);
    try {
      while (lifetime === lifecycle.current) {
        const requestSequence = sequence.current;
        const next = await listSourcesOnce(api, packageId, ownerId);
        if (lifetime !== lifecycle.current) return null;
        if (requestSequence !== sequence.current) continue;
        setSources(next);
        return next;
      }
      return null;
    } catch (error) {
      if (lifetime === lifecycle.current) onError(error instanceof Error ? error.message : "Failed to read data list");
      return null;
    } finally {
      if (lifetime === lifecycle.current) {
        busy.current = false;
        setIsLoading(false);
      }
    }
  }, [api, disabled, onError, ownerId, packageId, setSources]);

  const refreshAfterInvalidation = useCallback(async () => {
    sequence.current += 1;
    return refreshSources();
  }, [refreshSources]);
  const notify = useSourceLibrarySync(disabled, refreshAfterInvalidation);
  const libraryChanged = useCallback(() => { sequence.current += 1; notify(); }, [notify]);
  const refreshLibraryAfterChange = useCallback(async () => { libraryChanged(); await refreshSources(); }, [libraryChanged, refreshSources]);
  useSourcePolling({ disabled, sources, refreshSources });
  return { sources, setSources, isLoading, refreshSources, libraryChanged, refreshLibraryAfterChange };
}
