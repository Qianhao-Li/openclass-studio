"use client";

import { Eraser, LoaderCircle, Paperclip, X } from "lucide-react";
import { useState, type CSSProperties, type RefObject } from "react";

import { useFormulaInkCanvas } from "../../hooks/course-studio/use-formula-ink-canvas";

type ChatInkBoardProps = {
  panelRef: RefObject<HTMLDivElement | null>;
  position: CSSProperties;
  disabled: boolean;
  onClose: () => void;
  onSubmit: (imageDataUrl: string) => Promise<boolean>;
};

export function ChatInkBoard({ panelRef, position, disabled, onClose, onSubmit }: ChatInkBoardProps) {
  const [isSubmitting, setIsSubmitting] = useState(false);
  const { canvasRef, hasInk, clearCanvas, exportImage, canvasHandlers } = useFormulaInkCanvas();

  async function handleSubmit() {
    const imageDataUrl = exportImage();
    if (!imageDataUrl || disabled || isSubmitting) {
      return;
    }
    setIsSubmitting(true);
    try {
      const accepted = await onSubmit(imageDataUrl);
      if (accepted) {
        clearCanvas();
      }
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div
      ref={panelRef}
      role="dialog"
      aria-label="writing tablet"
      style={position}
      className="fixed z-[101] max-h-[calc(100vh-16px)] w-[min(560px,calc(100vw-16px))] overflow-y-auto rounded-2xl border border-gray-200 bg-white p-3 shadow-2xl"
    >
      <div className="flex items-center justify-between gap-3">
        <p className="text-[11px] font-bold uppercase tracking-widest text-gray-500">writing tablet</p>
        <button
          type="button"
          onClick={onClose}
          disabled={isSubmitting}
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-gray-400 transition hover:bg-gray-50 hover:text-gray-800 disabled:cursor-not-allowed disabled:opacity-50"
          aria-label="Close tablet"
          title="closure"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <canvas
        ref={canvasRef}
        aria-label="Handwriting input drawing board"
        className="mt-3 block h-[220px] w-full touch-none rounded-xl border border-gray-200 bg-white shadow-inner"
        {...canvasHandlers}
      />

      <div className="mt-3 flex items-center justify-between gap-2">
        <button
          type="button"
          onClick={clearCanvas}
          disabled={isSubmitting}
          className="inline-flex h-9 items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 text-[12px] font-semibold text-gray-600 transition hover:border-gray-300 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Eraser className="h-4 w-4" />

          Clear
        </button>
        <button
          type="button"
          disabled={!hasInk || disabled || isSubmitting}
          onClick={() => void handleSubmit()}
          className="inline-flex h-9 items-center gap-2 rounded-lg bg-gray-950 px-3 text-[12px] font-semibold text-white transition hover:bg-black disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isSubmitting ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Paperclip className="h-4 w-4" />}
          {isSubmitting ? "Adding" : "add to message"}
        </button>
      </div>
    </div>
  );
}
