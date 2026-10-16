/** Share simultaneous reads only. No persisted or stale permission/content cache. */
const reads = new Map<string, Promise<unknown>>();
let sessionGeneration = 0;

export function getReadRequestSessionGeneration() {
  return sessionGeneration;
}

export function resetReadRequests() {
  sessionGeneration += 1;
  reads.clear();
}

export function simultaneousRead<T>(key: string, read: () => Promise<T>): Promise<T> {
  const scopedKey = `${sessionGeneration}:${key}`;
  const existing = reads.get(scopedKey);
  if (existing) return existing as Promise<T>;
  const pending = read();
  reads.set(scopedKey, pending);
  void pending.then(
    () => { if (reads.get(scopedKey) === pending) reads.delete(scopedKey); },
    () => { if (reads.get(scopedKey) === pending) reads.delete(scopedKey); },
  );
  return pending;
}
