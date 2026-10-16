import clsx from "clsx";
import { KeyRound, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

import { MODEL_CREDENTIALS_CHANGED_EVENT } from "./model-catalog";
import { useStudioApi } from "../../contexts/studio-api-context";
import type { AIProviderCredentialStatus } from "../../types";

type Props = {
  provider: string;
  onProviderChange: (provider: string) => void;
};

export function PersonalApiProviderForm({ provider, onProviderChange }: Props) {
  const api = useStudioApi();
  const [credentials, setCredentials] = useState<AIProviderCredentialStatus[]>([]);
  const [loading, setLoading] = useState(true);
  const [apiKey, setApiKey] = useState("");
  const [configuration, setConfiguration] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<"save" | "remove" | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [draftProvider, setDraftProvider] = useState(provider);
  if (draftProvider !== provider) {
    setDraftProvider(provider);
    setApiKey("");
    setConfiguration({});
    setMessage("");
    setError("");
  }
  const credential = credentials.find((item) => item.provider === provider);
  const fields = credential?.configuration_fields ?? [];
  const canSave = credential?.manageable && apiKey.trim() &&
    (credential.configured || fields.every((field) => configuration[field.name]?.trim()));

  useEffect(() => {
    let cancelled = false;
    api.getModelCredentials().then((statuses) => {
      if (!cancelled) {
        setCredentials(statuses);
        setLoading(false);
      }
    }).catch((cause: unknown) => {
      if (!cancelled) {
        setError(cause instanceof Error ? cause.message : "Unable to load model providers.");
        setLoading(false);
      }
    });
    return () => { cancelled = true; };
  }, [api]);

  function changeProvider(nextProvider: string) {
    setApiKey("");
    setConfiguration({});
    setMessage("");
    setError("");
    onProviderChange(nextProvider);
  }

  function updateStatus(status: AIProviderCredentialStatus) {
    setCredentials((current) => current.map((item) => item.provider === status.provider ? status : item));
    window.dispatchEvent(new Event(MODEL_CREDENTIALS_CHANGED_EVENT));
  }

  async function saveKey() {
    if (!credential || !canSave || busy) return;
    setBusy("save");
    setMessage("");
    setError("");
    try {
      const values = Object.fromEntries(Object.entries(configuration).filter(([, value]) => value.trim()));
      const status = await api.saveModelCredential(provider, apiKey.trim(), values);
      updateStatus(status);
      setApiKey("");
      setConfiguration({});
      setMessage(`${status.label} API key saved.`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to save API key.");
    } finally {
      setBusy(null);
    }
  }

  async function removeKey() {
    if (!credential?.manageable || busy) return;
    setBusy("remove");
    setMessage("");
    setError("");
    try {
      const status = await api.deleteModelCredential(provider);
      updateStatus(status);
      setApiKey("");
      setConfiguration({});
      setMessage(`${status.label} API key removed.`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to remove API key.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="mt-4 border-t border-gray-100 pt-4">
      <div className="flex items-center gap-2">
        <KeyRound className="h-4 w-4 text-gray-500" />
        <h4 className="text-sm font-semibold text-gray-950">Personal API Key</h4>
      </div>
      <p className="mt-1 text-xs leading-5 text-gray-500">
        Choose a model provider, then save your API key. Keys are private to your account and never shown here.
      </p>
      <form className="mt-3 rounded-lg border border-gray-200 bg-gray-50 p-3" onSubmit={(event) => { event.preventDefault(); void saveKey(); }}>
        <label htmlFor="personal-api-provider" className="block text-xs font-semibold text-gray-700">Model provider</label>
        <select
          id="personal-api-provider"
          value={provider}
          disabled={loading || !!busy || !credentials.length}
          onChange={(event) => changeProvider(event.target.value)}
          className="mt-1.5 h-10 w-full rounded-lg border border-gray-200 bg-white px-3 text-sm outline-none focus:border-gray-500 disabled:opacity-50"
        >
          <option value="" disabled>{loading ? "Loading providers…" : "Select a provider"}</option>
          {credentials.map((item) => (
            <option key={item.provider} value={item.provider}>{item.label}{item.configured ? " · Key saved" : ""}</option>
          ))}
        </select>
        {credential ? (
          <>
            <div className="mt-3 flex items-center justify-between gap-2">
              <label htmlFor="personal-provider-api-key" className="text-xs font-semibold text-gray-700">
                {credential.provider === "amazon-bedrock" ? "Bearer token" : "API key"}
              </label>
              <span className={clsx("text-xs", credential.configured ? "text-emerald-700" : "text-gray-500")}>
                {credential.configured ? "Key saved" : "Not configured"}
              </span>
            </div>
            <input
              id="personal-provider-api-key"
              aria-label={`${credential.label} API Key`}
              type="password"
              autoComplete="off"
              spellCheck={false}
              disabled={!credential.manageable || !!busy}
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
              placeholder={credential.configured ? "Enter a new key to replace" : `Enter ${credential.label} API key`}
              className="mt-1.5 h-10 w-full rounded-lg border border-gray-200 bg-white px-3 text-sm outline-none focus:border-gray-500 disabled:bg-gray-100"
            />
            {fields.map((field) => (
              <label key={field.name} className="mt-3 block text-xs font-semibold text-gray-700">
                {field.label}
                <input
                  type="text"
                  autoComplete="off"
                  spellCheck={false}
                  disabled={!credential.manageable || !!busy}
                  value={configuration[field.name] ?? ""}
                  onChange={(event) => setConfiguration((current) => ({ ...current, [field.name]: event.target.value }))}
                  placeholder={credential.configured ? "Leave blank to keep saved setting" : field.placeholder}
                  className="mt-1.5 h-10 w-full rounded-lg border border-gray-200 bg-white px-3 text-sm font-normal outline-none focus:border-gray-500 disabled:bg-gray-100"
                />
              </label>
            ))}
            <div className="mt-3 flex items-center gap-2">
              <button type="submit" disabled={!canSave || !!busy} className="rounded-lg bg-gray-950 px-3 py-2 text-xs font-semibold text-white hover:bg-gray-800 disabled:cursor-not-allowed disabled:opacity-40">
                {busy === "save" ? "Saving…" : credential.configured ? "Replace key" : "Save API key"}
              </button>
              {credential.configured ? (
                <button type="button" disabled={!credential.manageable || !!busy} onClick={() => void removeKey()} className="inline-flex items-center gap-1 rounded-lg px-3 py-2 text-xs font-semibold text-red-600 hover:bg-red-50 disabled:opacity-40">
                  <Trash2 className="h-3.5 w-3.5" />{busy === "remove" ? "Removing…" : "Remove key"}
                </button>
              ) : null}
            </div>
            {!credential.manageable ? <p className="mt-2 text-xs text-amber-700">Sign in to save your personal API key.</p> : null}
          </>
        ) : !loading && !error ? <p className="mt-3 text-xs text-gray-500">Select a provider to enter its API key.</p> : null}
      </form>
      {message ? <p role="status" className="mt-3 text-xs leading-5 text-emerald-700">{message}</p> : null}
      {error ? <p role="alert" className="mt-3 text-xs leading-5 text-red-700">{error}</p> : null}
    </div>
  );
}
