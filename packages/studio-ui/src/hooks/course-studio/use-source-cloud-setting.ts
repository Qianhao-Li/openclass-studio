"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/** One independently retryable setting; unmounting invalidates its pending results. */
export function useSourceCloudSetting<T>(read: () => Promise<T>, failureMessage: string) {
  const [value, setValue] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const writing = useRef(false);
  const sequence = useRef(0);

  const refresh = useCallback(async () => {
    if (writing.current) return;
    const seq = ++sequence.current;
    try {
      const result = await read();
      if (seq !== sequence.current) return;
      setValue(result);
      setError("");
    } catch (error) {
      if (seq === sequence.current) setError(error instanceof Error ? error.message : failureMessage);
    }
  }, [read, failureMessage]);

  useEffect(() => {
    let active = true;
    void Promise.resolve().then(() => { if (active) void refresh(); });
    const onFocus = () => { void refresh(); };
    window.addEventListener("focus", onFocus);
    return () => {
      active = false;
      sequence.current += 1;
      window.removeEventListener("focus", onFocus);
    };
  }, [refresh]);

  async function run(task: (isCurrent: () => boolean) => Promise<T | void>) {
    if (writing.current) return;
    writing.current = true;
    const seq = ++sequence.current;
    const isCurrent = () => seq === sequence.current;
    setBusy(true);
    setError("");
    try {
      const result = await task(isCurrent);
      if (isCurrent() && result !== undefined) setValue(result);
    } catch (error) {
      if (isCurrent()) setError(error instanceof Error ? error.message : failureMessage);
    } finally {
      if (isCurrent()) {
        writing.current = false;
        setBusy(false);
      }
    }
  }

  return { value, error, busy, refresh, run };
}
