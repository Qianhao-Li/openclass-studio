export const CONNECTIONS_CHANGED_EVENT = "openclass:connections-changed";
const CONNECTIONS_STORAGE_KEY = "openclass.connections.changed";

export function announceConnectionsChanged() {
  window.dispatchEvent(new Event(CONNECTIONS_CHANGED_EVENT));
  window.dispatchEvent(new Event("openclass:model-credentials-changed"));
  try { window.localStorage.setItem(CONNECTIONS_STORAGE_KEY, String(Date.now())); } catch { /* Focus refresh remains available without storage. */ }
}

export function listenForConnectionChanges(refresh: () => void) {
  const storage = (event: StorageEvent) => { if (event.key === CONNECTIONS_STORAGE_KEY) refresh(); };
  window.addEventListener(CONNECTIONS_CHANGED_EVENT, refresh);
  window.addEventListener("focus", refresh);
  window.addEventListener("storage", storage);
  return () => {
    window.removeEventListener(CONNECTIONS_CHANGED_EVENT, refresh);
    window.removeEventListener("focus", refresh);
    window.removeEventListener("storage", storage);
  };
}
