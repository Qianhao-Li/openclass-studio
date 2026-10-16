/** Operation identities survive a page refresh; credentials never enter browser storage. */
const memory = new Map<string, { operation_id: string }>();
export function pendingSyncOperation<T extends { operation_id: string }>(key: string, create: () => T): T {
  const cached = memory.get(key);
  if (cached) return cached as T;
  try {
    const stored = localStorage.getItem(key);
    if (stored) {
      const value = JSON.parse(stored) as T;
      if (typeof value.operation_id === "string" && /^[a-zA-Z0-9_-]{16,128}$/.test(value.operation_id)) { memory.set(key, value); return value; }
    }
  } catch { /* The in-memory request still retains its operation identity. */ }
  const operation = create();
  memory.set(key, operation);
  try { localStorage.setItem(key, JSON.stringify(operation)); } catch { /* Private browsing may disable storage. */ }
  return operation;
}

export function completeSyncOperation(key: string) {
  memory.delete(key);
  try { localStorage.removeItem(key); } catch { /* A completed transfer remains recorded by the local backend. */ }
}
