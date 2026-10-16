"use client";

import { useEffect, useEffectEvent, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useStudioContext } from "../../contexts/studio-api-context";
import { useStudioHost } from "../../contexts/studio-host-context";
import { learningSessions } from "../../lib/learning-sessions";
import { ApiRequestError } from "../../lib/api";
import type { AutoSaveReason } from "./use-board-draft";

type Options = {
  revision?: string | null;
  documentDirty: boolean;
  chatBusy: boolean;
  mergeActive: boolean;
  historyDirty?: boolean;
  prepareLeave?: () => Promise<boolean>;
  flushAutoSave: (reason: AutoSaveReason) => Promise<boolean>;
  flushBeacon: (reason: AutoSaveReason) => void;
  flushMerge: () => Promise<boolean>;
  stopChat: () => void | Promise<unknown>;
};

export function useStudioNavigation(options: Options) {
  const host = useStudioHost();
  const context = useStudioContext();
  const sessionId = context.kind === "temporary" ? context.sessionId : null;
  const router = useRouter();
  const searchParams = useSearchParams();
  const pathname = usePathname();
  const requestedReturn = searchParams.get("returnTo");
  const returnTo = host.mode === "local" ? host.homeHref : requestedReturn?.startsWith("/") && !requestedReturn.startsWith("//") ? requestedReturn : host.homeHref;
  const home = context.kind === "repository" ? pathname.replace(/\/studio$/, "") : context.kind === "review" ? `/course-prs/${encodeURIComponent(context.prId)}` : context.kind === "version" ? `/courses/versions/${encodeURIComponent(context.repositoryId)}` : returnTo;
  const [hasChanges, setHasChanges] = useState(false);
  const [destination, setDestination] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reviewingHistory, setReviewingHistory] = useState(false);
  const [error, setError] = useState("");
  const ended = useRef(false);
  const sequence = useRef(0);
  const leaving = useRef(false);
  const pendingSaveRevision = useRef<string | null>(null);
  const dirty = hasChanges || options.documentDirty || options.chatBusy || options.mergeActive || options.historyDirty;

  const flushBeacon = useEffectEvent(() => { if (!options.historyDirty) options.flushBeacon("pagehide"); });
  const protectUnload = useEffectEvent((event: BeforeUnloadEvent) => {
    if (!ended.current && dirty) { event.preventDefault(); event.returnValue = ""; }
  });

  useEffect(() => {
    if (sessionId) {
      const handler = (event: BeforeUnloadEvent) => protectUnload(event);
      window.addEventListener("beforeunload", handler);
      return () => window.removeEventListener("beforeunload", handler);
    }
    const handler = () => flushBeacon();
    window.addEventListener("beforeunload", handler);
    window.addEventListener("pagehide", handler);
    return () => {
      flushBeacon();
      window.removeEventListener("beforeunload", handler);
      window.removeEventListener("pagehide", handler);
    };
  }, [sessionId]);

  useEffect(() => {
    if (!sessionId) return;
    const current = ++sequence.current;
    void learningSessions.get(sessionId).then((session) => {
      if (current === sequence.current) setHasChanges(session.has_changes);
    }).catch((failure) => { if (!ended.current) setError(failure instanceof Error ? failure.message : "Unable to load temporary course"); });
    return () => { sequence.current += 1; };
  }, [sessionId, options.revision]);

  useEffect(() => {
    if (!sessionId) return;
    const touch = () => {
      if (!ended.current && document.visibilityState === "visible") {
        void learningSessions.touch(sessionId).catch((failure) => setError(failure instanceof Error ? failure.message : "Unable to extend this session"));
      }
    };
    touch();
    const timer = window.setInterval(touch, 60_000);
    document.addEventListener("visibilitychange", touch);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", touch); };
  }, [sessionId]);

  function navigate(target: string) {
    ended.current = true;
    if (sessionId) {
      try {
        for (const key of Object.keys(localStorage)) {
          if (key.startsWith(`openclass.learning-draft:${sessionId}:`)) localStorage.removeItem(key);
        }
      } catch { /* Storage denial must not block an acknowledged save/discard. */ }
      window.location.assign(target);
    } else router.push(target);
  }

  async function requestLeave(target = home) {
    if (leaving.current || ended.current) return;
    leaving.current = true;
    setError("");
    try {
      if (!sessionId) {
        if (options.prepareLeave && !(await options.prepareLeave())) return;
        if (options.mergeActive && !(await options.flushMerge())) return;
        if (await options.flushAutoSave("return-home")) navigate(target);
        return;
      }
      if (options.chatBusy || options.mergeActive || options.historyDirty) { setDestination(target); return; }
      if (!(await options.flushAutoSave("return-home"))) { setDestination(target); setError("Your document has not finished saving. Retry before leaving."); return; }
      const session = await learningSessions.get(sessionId);
      setHasChanges(session.has_changes);
      if (session.has_changes) setDestination(target);
      else { await learningSessions.discard(sessionId); navigate(target); }
    } catch (failure) {
      setDestination(target);
      setError(failure instanceof Error ? failure.message : "Unable to leave this session");
    } finally { leaving.current = false; }
  }

  const interceptLeave = useEffectEvent((target: string) => { void requestLeave(target); });
  useEffect(() => {
    if (!sessionId) return;
    const click = (event: MouseEvent) => {
      const link = event.target instanceof Element ? event.target.closest<HTMLAnchorElement>("a[href]") : null;
      if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || link.target === "_blank" || link.hasAttribute("download")) return;
      const url = new URL(link.href, location.href);
      if (url.origin !== location.origin || (url.pathname === location.pathname && url.search === location.search)) return;
      event.preventDefault(); event.stopImmediatePropagation();
      interceptLeave(url.pathname + url.search + url.hash);
    };
    // A same-page checkpoint lets Back be confirmed before Next unmounts the workbench.
    if (history.state?.learningSession !== sessionId) history.pushState({ ...history.state, learningSession: sessionId }, "", location.href);
    let restoring = false;
    const pop = (event: PopStateEvent) => {
      if (ended.current) return;
      event.stopImmediatePropagation();
      if (restoring) { restoring = false; return; }
      restoring = true;
      history.go(1);
      interceptLeave(returnTo);
    };
    document.addEventListener("click", click, true);
    window.addEventListener("popstate", pop, true);
    return () => { document.removeEventListener("click", click, true); window.removeEventListener("popstate", pop, true); };
  }, [sessionId, returnTo]);

  async function finish(save: boolean) {
    if (!sessionId || !destination || busy) return;
    setBusy(true); setError("");
    try {
      if (save) {
        if (options.chatBusy || options.mergeActive) return;
        if (!pendingSaveRevision.current) {
          if (options.prepareLeave) {
            setReviewingHistory(true);
            try { if (!(await options.prepareLeave())) return; }
            finally { setReviewingHistory(false); }
          }
          if (!(await options.flushAutoSave("return-home"))) throw new Error("The document could not be saved. Your temporary session is still available.");
          pendingSaveRevision.current = (await learningSessions.get(sessionId)).revision;
        }
        await learningSessions.save(sessionId, pendingSaveRevision.current);
      } else {
        await learningSessions.discard(sessionId);
        void options.stopChat();
      }
      navigate(destination);
    } catch (failure) {
      if (failure instanceof ApiRequestError && failure.status === 409) pendingSaveRevision.current = null;
      setError(failure instanceof Error ? failure.message : "Unable to save this course");
    }
    finally { setBusy(false); }
  }

  return {
    temporary: Boolean(sessionId), promptOpen: destination !== null && !reviewingHistory, busy, error,
    blocked: options.chatBusy ? "Wait for the current reply, or stop generation before saving." : options.mergeActive ? "Submit or abandon the current merge before saving." : "",
    returnHome: () => requestLeave(home), requestLeave,
    saveAndExit: () => finish(true), discardAndExit: () => finish(false),
    continueLearning: () => { setDestination(null); setError(""); },
    stopChat: options.stopChat, chatBusy: options.chatBusy,
  };
}
