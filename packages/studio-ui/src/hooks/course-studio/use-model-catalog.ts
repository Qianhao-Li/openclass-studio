"use client";

import { useEffect, useRef, useState } from "react";

import { listenForConnectionChanges } from "../../lib/connection-events";
import { useStudioHost } from "../../contexts/studio-host-context";
import { PROFILE_SETTINGS_CHANGED_EVENT, normalizeProfileSettings, type ProfileSettings } from "../../lib/profile-settings-state";
import { useStudioApi } from "../../contexts/studio-api-context";
import {
  FALLBACK_MODEL_CATALOG,
  MODEL_CREDENTIALS_CHANGED_EVENT,
  REALTIME_MODEL_STORAGE_KEY,
  TEXT_MODEL_STORAGE_KEY,
  catalogAwaitingCodexChoice,
  catalogForCodexConnection,
  findModelOption,
  modelAccessMethod,
  normalizeCourseStudioModelCatalog,
  optionToSelection,
  persistModelSelection,
  readStoredModelSelection,
  resolveModelSelection,
  selectionForModelOption,
} from "../../components/course-studio/model-catalog";
import type { AIModelCatalog, AIModelOption, AIModelSelection } from "../../types";
import type { ExternalConnection } from "../../types/external-connections";

export type CodexConnectionControls = {
  connections: ExternalConnection[];
  selectedId: string | null;
  busy: boolean;
  error: string;
  select: (connectionId: string) => Promise<void>;
};

function isCodexSubscription(selection: AIModelSelection | AIModelOption) {
  return selection.provider === "openai_codex" && modelAccessMethod(selection) === "chatgpt_subscription";
}

function prefersCodexSubscription(preference: string, stored: AIModelSelection | null) {
  if (preference === "auto") return Boolean(stored && isCodexSubscription(stored));
  const parts = preference.split(":");
  return parts[0] === "openai_codex" && (parts.length === 2 || parts[2] === "chatgpt_subscription");
}

function persistTextModelPreference(key: string, selection: AIModelSelection) {
  const { connection_id: _connectionId, ...preference } = selection;
  void _connectionId;
  persistModelSelection(key, preference);
}

export function useModelCatalog() {
  const api = useStudioApi();
  const host = useStudioHost();
  const userId = host.principal?.id ?? null;
  const textStorageKey = `${TEXT_MODEL_STORAGE_KEY}:${userId ?? "anonymous"}`;
  const realtimeStorageKey = `${REALTIME_MODEL_STORAGE_KEY}:${userId ?? "anonymous"}`;
  const preferenceRef = useRef({ text: "", realtime: "", codex: null as string | null });
  const explicitConnectionRef = useRef<{ ownerId: string | null; id: string } | null>(null);
  const explicitTextSelectionRef = useRef<{ ownerId: string | null; selection: AIModelSelection } | null>(null);
  const lastResolvedConnectionRef = useRef<{ ownerId: string | null; id: string } | null>(null);
  const baseCatalogRef = useRef<AIModelCatalog>(FALLBACK_MODEL_CATALOG);
  const [catalogOwnerId, setCatalogOwnerId] = useState<string | null>(null);
  const catalogRequestRef = useRef(0);
  const [codexConnections, setCodexConnections] = useState<ExternalConnection[]>([]);
  const [selectedCodexConnectionId, setSelectedCodexConnectionId] = useState<string | null>(null);
  const [codexConnectionBusy, setCodexConnectionBusy] = useState(false);
  const [codexConnectionError, setCodexConnectionError] = useState("");
  const [modelCatalog, setModelCatalog] = useState<AIModelCatalog>(() =>
    normalizeCourseStudioModelCatalog(FALLBACK_MODEL_CATALOG)
  );
  const [selectedTextModel, setSelectedTextModel] = useState<AIModelSelection>(FALLBACK_MODEL_CATALOG.defaults.text);
  const [selectedRealtimeModel, setSelectedRealtimeModel] = useState<AIModelSelection>(
    FALLBACK_MODEL_CATALOG.defaults.realtime
  );
  const [openModelMenu, setOpenModelMenu] = useState<"text" | "realtime" | null>(null);

  useEffect(() => {
    let disposed = false;
    if (explicitConnectionRef.current?.ownerId !== userId) explicitConnectionRef.current = null;
    if (explicitTextSelectionRef.current?.ownerId !== userId) explicitTextSelectionRef.current = null;
    if (lastResolvedConnectionRef.current?.ownerId !== userId) lastResolvedConnectionRef.current = null;
    let catalog = normalizeCourseStudioModelCatalog(FALLBACK_MODEL_CATALOG);
    function applyPreferences(settings: ProfileSettings, force = false, connectionId: string | null = null, accountChoiceRequired = false) {
      function resolvePreferred(options: AIModelOption[], preferred: string, key: string, fallback: AIModelSelection) {
        const saved = readStoredModelSelection(key);
        // Model preference may survive a reload; account authorization does not.
        const stored = saved ? { ...saved, connection_id: isCodexSubscription(saved) ? connectionId : null } : null;
        const matches = options.filter((item) => item.enabled && (
          `${item.provider}:${item.model}` === preferred ||
          `${item.provider}:${item.model}:${modelAccessMethod(item)}` === preferred
        ));
        const option = matches.find((item) => stored?.provider === item.provider && stored.model === item.model && modelAccessMethod(stored) === modelAccessMethod(item) && (stored.connection_id ?? null) === (item.connection_id ?? null)) ?? matches[0];
        const matchesPreference = option && stored?.provider === option.provider && stored.model === option.model && modelAccessMethod(stored) === modelAccessMethod(option) && (stored.connection_id ?? null) === (option.connection_id ?? null);
        return resolveModelSelection(options, option && !matchesPreference ? optionToSelection(option) : stored, fallback);
      }
      if (force || settings.preferredTextModel !== preferenceRef.current.text || settings.preferred_codex_connection_id !== preferenceRef.current.codex) {
        const explicit = explicitTextSelectionRef.current?.ownerId === userId ? explicitTextSelectionRef.current.selection : null;
        if (explicit) {
          const option = findModelOption(catalog.text, explicit);
          setSelectedTextModel(option?.enabled ? selectionForModelOption(option, explicit) : explicit);
          if (!option?.enabled && (!isCodexSubscription(explicit) || connectionId)) setCodexConnectionError("The selected model is unavailable. Choose a model to continue.");
        } else {
        const chosen = resolvePreferred(catalog.text, settings.preferredTextModel, textStorageKey, catalog.defaults.text);
        const saved = readStoredModelSelection(textStorageKey);
        const preferred = catalog.text.find((item) => `${item.provider}:${item.model}:${modelAccessMethod(item)}` === settings.preferredTextModel || `${item.provider}:${item.model}` === settings.preferredTextModel);
        const requested = preferred ?? (prefersCodexSubscription(settings.preferredTextModel, saved) ? saved && isCodexSubscription(saved) ? saved : FALLBACK_MODEL_CATALOG.defaults.text : saved)
          ?? (accountChoiceRequired ? FALLBACK_MODEL_CATALOG.defaults.text : catalog.defaults.text);
        setSelectedTextModel(accountChoiceRequired && !connectionId && isCodexSubscription(requested)
          ? { ...requested, connection_id: null } : chosen);
        }
      }
      if (force || settings.preferredRealtimeModel !== preferenceRef.current.realtime) {
        setSelectedRealtimeModel(resolvePreferred(catalog.realtime, settings.preferredRealtimeModel, realtimeStorageKey, catalog.defaults.realtime));
      }
      preferenceRef.current = { text: settings.preferredTextModel, realtime: settings.preferredRealtimeModel, codex: settings.preferred_codex_connection_id };
    }
    async function loadModelCatalog() {
      const sequence = ++catalogRequestRef.current;
      const preferences = host.getModelPreferences?.() ?? Promise.resolve({ principalId: userId, settings: {} });
      const [baseResult, accountsResult, settingsResult] = await Promise.allSettled([api.getAIModels(), api.getCodexConnections(), preferences]);
      if (disposed || sequence !== catalogRequestRef.current) return;
      const base = normalizeCourseStudioModelCatalog(baseResult.status === "fulfilled" ? baseResult.value : FALLBACK_MODEL_CATALOG);
      baseCatalogRef.current = base;
      const available = accountsResult.status === "fulfilled"
        ? accountsResult.value.filter((entry) => entry.status === "connected" && entry.capabilities.models?.granted && entry.capabilities.models?.enabled !== false)
        : [];
      if ((accountsResult.status === "fulfilled" && accountsResult.value.some((entry) => entry.owner_user_id !== userId))
          || (settingsResult.status === "fulfilled" && settingsResult.value.principalId !== userId)) {
        setCodexConnections([]);
        setSelectedCodexConnectionId(null);
        setCodexConnectionError("Your OpenClass account changed. Reload this page.");
        setModelCatalog(catalogAwaitingCodexChoice(FALLBACK_MODEL_CATALOG));
        setSelectedTextModel({ ...FALLBACK_MODEL_CATALOG.defaults.text, connection_id: null });
        setCatalogOwnerId(userId);
        setCodexConnectionBusy(false);
        return;
      }
      setCodexConnections(available);
      const explicitId = explicitConnectionRef.current?.ownerId === userId ? explicitConnectionRef.current.id : null;
      const preferencesAvailable = settingsResult.status === "fulfilled" && settingsResult.value.principalId === userId;
      if (!preferencesAvailable && !explicitId && !host.principal?.isGuest) {
        setSelectedCodexConnectionId(null);
        setCodexConnectionError("Account preferences are unavailable. Reload or explicitly choose a ChatGPT account to continue.");
        setModelCatalog(catalogAwaitingCodexChoice(base));
        setSelectedTextModel({ ...FALLBACK_MODEL_CATALOG.defaults.text, connection_id: null });
        setCatalogOwnerId(userId);
        setCodexConnectionBusy(false);
        return;
      }
      const settings = normalizeProfileSettings(preferencesAvailable ? settingsResult.value.settings : null);
      const requestedId = explicitId ?? settings.preferred_codex_connection_id;
      const invalidSelection = Boolean(requestedId && !available.some((entry) => entry.id === requestedId));
      const previousId = lastResolvedConnectionRef.current?.ownerId === userId ? lastResolvedConnectionRef.current.id : null;
      const lostAutomaticSelection = !requestedId && Boolean(previousId && !available.some((entry) => entry.id === previousId));
      const subscriptionPreferred = prefersCodexSubscription(settings.preferredTextModel, readStoredModelSelection(textStorageKey));
      let connectionId = lostAutomaticSelection ? null : requestedId
        ? available.find((entry) => entry.id === requestedId)?.id ?? null
        : available.length === 1 ? available[0].id : null;
      const accountChoiceRequired = Boolean(requestedId) || available.length > 0 || lostAutomaticSelection || subscriptionPreferred;
      setSelectedCodexConnectionId(connectionId);
      setCodexConnectionError(invalidSelection || lostAutomaticSelection
        ? explicitId || lostAutomaticSelection ? "The selected ChatGPT account is unavailable. Choose another account to continue."
          : "The default ChatGPT account is unavailable. Choose an account or update your default to continue."
        : !available.length && subscriptionPreferred ? "No available ChatGPT account. Connect or select an account to continue." : "");
      catalog = accountChoiceRequired && !connectionId ? catalogAwaitingCodexChoice(base) : base;
      if (connectionId) {
        lastResolvedConnectionRef.current = { ownerId: userId, id: connectionId };
        try {
          const scoped = normalizeCourseStudioModelCatalog(await api.getCodexConnectionModels(connectionId));
          if (disposed || sequence !== catalogRequestRef.current) return;
          if (!scoped.text.some((option) => isCodexSubscription(option) && option.enabled)) throw new Error("No subscription models are available for this account. Reconnect or choose another account.");
          catalog = catalogForCodexConnection(base, scoped, connectionId);
          setCodexConnectionError("");
        } catch (error) {
          if (disposed || sequence !== catalogRequestRef.current) return;
          connectionId = null;
          setSelectedCodexConnectionId(null);
          catalog = catalogAwaitingCodexChoice(base);
          setCodexConnectionError(error instanceof Error ? error.message : "Could not load this ChatGPT account's models.");
        }
      }
      setCodexConnectionBusy(false);
      setCatalogOwnerId(userId);
      setModelCatalog(catalog);
      applyPreferences(settings, true, connectionId, accountChoiceRequired);
    }
    function onSettingsChanged() { void loadModelCatalog(); }
    void loadModelCatalog();
    const stopListening = listenForConnectionChanges(loadModelCatalog);
    window.addEventListener(MODEL_CREDENTIALS_CHANGED_EVENT, loadModelCatalog);
    window.addEventListener(PROFILE_SETTINGS_CHANGED_EVENT, onSettingsChanged);
    return () => {
      disposed = true;
      stopListening();
      catalogRequestRef.current += 1;
      window.removeEventListener(MODEL_CREDENTIALS_CHANGED_EVENT, loadModelCatalog);
      window.removeEventListener(PROFILE_SETTINGS_CHANGED_EVENT, onSettingsChanged);
    };
  }, [api, userId, host, textStorageKey, realtimeStorageKey]);

  async function selectCodexConnection(connectionId: string) {
    if (catalogOwnerId !== userId) return;
    const selected = codexConnections.find((entry) => entry.id === connectionId);
    if (!selected) return;
    const request = ++catalogRequestRef.current;
    explicitConnectionRef.current = { ownerId: userId, id: connectionId };
    const requestedModel = { ...(isCodexSubscription(selectedTextModel) ? selectedTextModel : FALLBACK_MODEL_CATALOG.defaults.text), connection_id: connectionId };
    explicitTextSelectionRef.current = { ownerId: userId, selection: requestedModel };
    setSelectedCodexConnectionId(connectionId);
    setCodexConnectionBusy(true);
    setCodexConnectionError("");
    setModelCatalog(catalogAwaitingCodexChoice(baseCatalogRef.current));
    setSelectedTextModel(requestedModel);
    try {
      const scoped = normalizeCourseStudioModelCatalog(await api.getCodexConnectionModels(connectionId));
      if (request !== catalogRequestRef.current) return;
      if (!scoped.text.some((option) => isCodexSubscription(option) && option.enabled)) throw new Error("No subscription models are available for this account. Reconnect or choose another account.");
      const nextCatalog = catalogForCodexConnection(baseCatalogRef.current, scoped, connectionId);
      lastResolvedConnectionRef.current = { ownerId: userId, id: connectionId };
      setModelCatalog(nextCatalog);
      const option = nextCatalog.text.find((item) => item.connection_id === connectionId && item.model === selectedTextModel.model && item.enabled)
        ?? nextCatalog.text.find((item) => item.connection_id === connectionId && item.enabled);
      if (option) {
        const next = option.model === selectedTextModel.model && option.provider === selectedTextModel.provider
          ? selectionForModelOption(option, { ...selectedTextModel, connection_id: connectionId }) : optionToSelection(option);
        setSelectedTextModel(next);
        explicitTextSelectionRef.current = { ownerId: userId, selection: next };
        persistTextModelPreference(textStorageKey, next);
      }
    } catch (error) {
      if (request === catalogRequestRef.current) setCodexConnectionError(error instanceof Error ? error.message : "Could not load this ChatGPT account's models.");
    } finally { if (request === catalogRequestRef.current) setCodexConnectionBusy(false); }
  }

  const visibleCatalog = catalogOwnerId === userId ? modelCatalog : normalizeCourseStudioModelCatalog(FALLBACK_MODEL_CATALOG);
  const selectedTextOption = findModelOption(visibleCatalog.text, selectedTextModel);
  const selectedRealtimeOption = findModelOption(visibleCatalog.realtime, selectedRealtimeModel);
  const selectedRealtimeTransport =
    selectedRealtimeOption?.transport ??
    (selectedRealtimeModel.provider === "openai" ? "openai_webrtc" : "gemini_live_websocket");

  function selectTextModel(selection: AIModelSelection) {
    const option = findModelOption(visibleCatalog.text, selection);
    if (!option?.enabled) {
      return;
    }
    const nextSelection = selectionForModelOption(option, selection);
    explicitTextSelectionRef.current = { ownerId: userId, selection: nextSelection };
    if (isCodexSubscription(nextSelection) && nextSelection.connection_id) explicitConnectionRef.current = { ownerId: userId, id: nextSelection.connection_id };
    setSelectedTextModel(nextSelection);
    persistTextModelPreference(textStorageKey, nextSelection);
    setOpenModelMenu(null);
  }

  function selectRealtimeModel(option: AIModelOption) {
    if (!option.enabled) {
      return;
    }
    const nextSelection = optionToSelection(option);
    setSelectedRealtimeModel(nextSelection);
    persistModelSelection(realtimeStorageKey, nextSelection);
    setOpenModelMenu(null);
  }

  return {
    modelCatalog: visibleCatalog,
    selectedTextModel,
    selectedRealtimeModel,
    selectedTextOption,
    selectedRealtimeOption,
    selectedRealtimeTransport,
    codexConnectionControls: {
      connections: catalogOwnerId === userId ? codexConnections : [],
      selectedId: catalogOwnerId === userId ? selectedCodexConnectionId : null,
      busy: codexConnectionBusy,
      error: codexConnectionError,
      select: selectCodexConnection,
    } satisfies CodexConnectionControls,
    openModelMenu,
    setOpenModelMenu,
    selectTextModel,
    selectRealtimeModel,
  };
}
