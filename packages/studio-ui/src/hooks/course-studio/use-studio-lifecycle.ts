"use client";

import { useEffect, useEffectEvent, type RefObject } from "react";

export function useStudioLifecycle({ chatInputRef, chatInput, selectionExcerpt, mergeActive, openMergeHistory }: {
  chatInputRef: RefObject<HTMLTextAreaElement | null>;
  chatInput: string;
  selectionExcerpt?: string;
  mergeActive: boolean;
  openMergeHistory: () => void;
}) {
  function adjustComposerHeight() {
    const input = chatInputRef.current;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 120)}px`;
  }
  const resize = useEffectEvent(adjustComposerHeight);
  useEffect(() => { resize(); }, [chatInput, selectionExcerpt]);

  const showMergeHistory = useEffectEvent(openMergeHistory);
  useEffect(() => {
    if (!mergeActive) return;
    const frame = window.requestAnimationFrame(() => showMergeHistory());
    return () => window.cancelAnimationFrame(frame);
  }, [mergeActive]);

  return { adjustComposerHeight };
}
