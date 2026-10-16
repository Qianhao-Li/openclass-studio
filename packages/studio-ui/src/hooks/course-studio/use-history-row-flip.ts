"use client";

import { useCallback, useLayoutEffect, useRef } from "react";

/** FLIP: https://aerotwist.com/blog/flip-your-animations/
 * Capture the current painted position before cancelling an unfinished animation,
 * then invert only stationary rows after React commits the next ordering.
 */
export function useHistoryRowFlip(orderKey: string, enabled: boolean, draggedIds: readonly string[]) {
  const elements = useRef(new Map<string, HTMLElement>());
  const previous = useRef(new Map<string, DOMRect>());
  const animations = useRef(new Map<string, Animation>());
  const register = useCallback((id: string, element: HTMLElement | null) => {
    if (element) elements.current.set(id, element);
    else { animations.current.get(id)?.cancel(); animations.current.delete(id); elements.current.delete(id); }
  }, []);
  const capture = useCallback(() => {
    const positions = new Map<string, DOMRect>();
    elements.current.forEach((element, id) => positions.set(id, element.getBoundingClientRect()));
    animations.current.forEach((animation, id) => { animation.cancel(); const element = elements.current.get(id); if (element) delete element.dataset.historyRowAnimating; });
    animations.current.clear();
    previous.current = positions;
  }, []);
  const draggedKey = draggedIds.join("\u0000");
  useLayoutEffect(() => {
    if (!enabled) {
      animations.current.forEach((animation, id) => { animation.cancel(); const element = elements.current.get(id); if (element) delete element.dataset.historyRowAnimating; });
      animations.current.clear();
    }
    const moving = new Set(draggedKey.split("\u0000"));
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const next = new Map<string, DOMRect>();
    elements.current.forEach((element, id) => {
      const rect = element.getBoundingClientRect();
      next.set(id, rect);
      const before = previous.current.get(id);
      if (!enabled || reduced || moving.has(id) || !before) return;
      const delta = before.top - rect.top;
      if (Math.abs(delta) < 0.5) return;
      const animation = element.animate([{ transform: `translateY(${delta}px)` }, { transform: "translateY(0)" }], {
        duration: 200, easing: "cubic-bezier(0.2, 0, 0, 1)",
      });
      animations.current.set(id, animation);
      element.dataset.historyRowAnimating = "true";
      void animation.finished.then(() => {
        if (animations.current.get(id) === animation) { animations.current.delete(id); delete element.dataset.historyRowAnimating; }
      }).catch(() => { if (animations.current.get(id) === animation) delete element.dataset.historyRowAnimating; });
    });
    previous.current = next;
  }, [orderKey, enabled, draggedKey]);
  useLayoutEffect(() => {
    const running = animations.current;
    return () => { running.forEach((animation) => animation.cancel()); running.clear(); };
  }, []);
  return { register, capture };
}
