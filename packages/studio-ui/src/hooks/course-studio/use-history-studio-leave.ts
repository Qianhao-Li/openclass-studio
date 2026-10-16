"use client";

import { useEffect, useEffectEvent, useRef, useState } from "react";
import type { HistoryEditReview } from "../../types/history-edit";

type Options = {
  scope: string;
  pending: boolean;
  warnOnUnload?: boolean;
  interceptBrowser?: boolean;
  prepare: (intent: "leave" | "save", isCurrent: () => boolean) => Promise<HistoryEditReview | null>;
  commit: (review: HistoryEditReview) => Promise<boolean>;
  discard: () => Promise<boolean>;
};
/** Explicit Save and Studio leave share the same version-bound review and commit. */
export function useHistoryStudioLeave(options: Options) {
  const [dialog, setDialog] = useState<{ scope: string; intent: "leave" | "save"; review: HistoryEditReview | null; busy: boolean; preparing: boolean; message: string | null } | null>(null);
  const pendingRequest = useRef<{ scope: string; promise: Promise<boolean>; resolve: (ok: boolean) => void } | null>(null);
  const reviewGeneration = useRef(0);
  const latest = useRef(options);
  useEffect(() => { latest.current = options; });
  useEffect(() => () => { reviewGeneration.current += 1; pendingRequest.current?.resolve(false); pendingRequest.current = null; }, [options.scope]);
  function finish(ok: boolean) {
    reviewGeneration.current += 1;
    pendingRequest.current?.resolve(ok); pendingRequest.current = null; setDialog(null);
  }
  async function prepare(intent: "leave" | "save") {
    const generation = ++reviewGeneration.current;
    const isCurrent = () => generation === reviewGeneration.current && latest.current.scope === options.scope;
    if (intent === "save" || options.warnOnUnload || dialog) setDialog({ scope: options.scope, intent, review: null, busy: true, preparing: true, message: null });
    try {
      const review = await options.prepare(intent, isCurrent);
      if (!isCurrent()) return;
      if (!review?.dirty) { finish(true); return; }
      setDialog({ scope: options.scope, intent, review, busy: false, preparing: false, message: review.blocked_reason ?? null });
    } catch (error) {
      if (isCurrent()) setDialog({ scope: options.scope, intent, review: null, busy: false, preparing: false, message: error instanceof Error ? error.message : "Could not review changes. Please try again." });
    }
  }
  function requestReview(intent: "leave" | "save"): Promise<boolean> {
    if (pendingRequest.current?.scope === options.scope) return pendingRequest.current.promise;
    let resolve!: (ok: boolean) => void;
    const promise = new Promise<boolean>((value) => { resolve = value; });
    pendingRequest.current = { scope: options.scope, promise, resolve };
    void prepare(intent); return promise;
  }
  const requestLeave = () => requestReview("leave");
  async function answer(keep: boolean) {
    const current = dialog;
    if (!current || current.busy || current.scope !== options.scope || (keep && !current.review)) return;
    const generation = reviewGeneration.current;
    const isCurrent = () => generation === reviewGeneration.current && latest.current.scope === options.scope;
    setDialog({ ...current, busy: true, preparing: false });
    try {
      const ok = keep ? await options.commit(current.review!) : await options.discard();
      if (!isCurrent()) return;
      if (ok) finish(true);
      else setDialog({ ...current, busy: false, message: "The operation could not finish. Your draft is preserved. Retry, review again, or cancel." });
    } catch (error) {
      if (isCurrent()) setDialog({ ...current, busy: false,
        message: `Could not complete the operation: ${error instanceof Error ? error.message : "Please try again."}` });
    }
  }
  const leaveFromBrowser = useEffectEvent(requestLeave);
  useEffect(() => {
    if (!options.pending || options.interceptBrowser === false) return;
    type NavigationSurface = EventTarget & { currentEntry?: { index: number }; traverseTo: (key: string) => { finished: Promise<unknown> } };
    type TraversalEvent = Event & { navigationType?: string; destination?: { key: string; url: string }; hashChange?: boolean };
    const navigation = (window as Window & { navigation?: NavigationSurface }).navigation;
    const navigationIndex = () => (window as Window & { navigation?: { currentEntry?: { index: number } } }).navigation?.currentEntry?.index;
    const key = "__openclassHistoryPosition";
    let sourcePosition = navigationIndex() ?? (history.state?.[key] as number | undefined) ?? 0;
    let sourceHref = location.href;
    let sourceState = history.state;
    let restoring: (() => void) | null = null;
    let approvedTraversal = false;
    let approvedNavigation = false;
    const originalPush = history.pushState;
    const originalReplace = history.replaceState;
    const mark = (state: unknown, position: number) => ({ ...(state && typeof state === "object" ? state : {}), [key]: position });
    const push: History["pushState"] = function(state, unused, url) {
      originalPush.call(history, mark(state, sourcePosition + 1), unused, url);
      sourcePosition = navigationIndex() ?? sourcePosition + 1; sourceHref = location.href; sourceState = history.state;
    };
    const replace: History["replaceState"] = function(state, unused, url) {
      originalReplace.call(history, mark(state, sourcePosition), unused, url);
      sourceHref = location.href; sourceState = history.state;
    };
    // Preserve Next's router state while tracking direction on browsers without Navigation API.
    originalReplace.call(history, mark(sourceState, sourcePosition), "", sourceHref);
    sourceState = history.state; history.pushState = push; history.replaceState = replace;
    const click = (event: MouseEvent) => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const link = (event.target as Element | null)?.closest?.("a[href]") as HTMLAnchorElement | null;
      if (!link || link.target === "_blank" || link.hasAttribute("download")) return;
      const target = new URL(link.href, window.location.href);
      if (target.origin === location.origin && target.pathname === location.pathname) return;
      event.preventDefault(); event.stopPropagation();
      void leaveFromBrowser().then((ok) => { if (ok) location.assign(link.href); });
    };
    const unload = (event: BeforeUnloadEvent) => { if (latest.current.warnOnUnload ?? latest.current.pending) { event.preventDefault(); event.returnValue = ""; } };
    const navigate = (event: Event) => {
      const traversal = event as TraversalEvent;
      if (approvedNavigation) { approvedNavigation = false; return; }
      if (!latest.current.pending || traversal.navigationType !== "traverse" || !traversal.cancelable || !traversal.destination) return;
      if (new URL(traversal.destination.url).pathname === new URL(sourceHref).pathname) return;
      event.preventDefault();
      const destinationKey = traversal.destination.key;
      void leaveFromBrowser().then((ok) => {
        if (ok && navigation) { approvedNavigation = true; approvedTraversal = true; void navigation.traverseTo(destinationKey).finished.catch(() => undefined); }
      });
    };
    const pop = (event: PopStateEvent) => {
      if (approvedTraversal || !latest.current.pending) { approvedTraversal = false; return; }
      if (restoring) { const restored = restoring; restoring = null; restored(); return; }
      event.stopImmediatePropagation();
      const targetHref = location.href;
      const targetPosition = navigationIndex() ?? event.state?.[key] as number | undefined;
      const delta = typeof targetPosition === "number" ? targetPosition - sourcePosition : null;
      if (delta && Number.isFinite(delta)) {
        const restored = new Promise<void>((resolve) => { restoring = resolve; });
        history.go(-delta);
        void restored.then(() => leaveFromBrowser()).then((ok) => { if (ok) { approvedTraversal = true; history.go(delta); } });
      } else {
        // Unknown pre-app entry: restore this view and replay its URL only after an explicit answer.
        originalReplace.call(history, sourceState, "", sourceHref);
        void leaveFromBrowser().then((ok) => { if (ok) location.assign(targetHref); });
      }
    };
    navigation?.addEventListener("navigate", navigate);
    document.addEventListener("click", click, true); window.addEventListener("beforeunload", unload); window.addEventListener("popstate", pop, true);
    return () => {
      if (history.pushState === push) history.pushState = originalPush;
      if (history.replaceState === replace) history.replaceState = originalReplace;
      restoring?.();
      navigation?.removeEventListener("navigate", navigate);
      document.removeEventListener("click", click, true); window.removeEventListener("beforeunload", unload); window.removeEventListener("popstate", pop, true);
    };
  }, [options.pending, options.interceptBrowser]);
  return { requestLeave, requestSave: () => requestReview("save"), dialog: dialog?.scope === options.scope ? dialog : null,
    keep: () => answer(true), discard: () => answer(false), cancel: () => finish(false), refresh: () => prepare(dialog?.intent ?? "leave") };
}
