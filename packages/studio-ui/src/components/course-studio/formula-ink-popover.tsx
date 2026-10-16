"use client";

import { Box, Eraser, PenLine, RefreshCcw, Send, TextQuote, X } from "lucide-react";
import { useState } from "react";

import type { SelectionPopoverPosition } from "./selection-utils";
import { useFormulaInkCanvas } from "../../hooks/course-studio/use-formula-ink-canvas";
import type { FormulaInkAction } from "../../types";

export type FormulaInkSubmitPayload = {
  action: FormulaInkAction;
  imageDataUrl: string;
  sourceLatex: string;
};

type FormulaInkPopoverProps = {
  position: SelectionPopoverPosition | null;
  sourceLatex: string;
  disabled?: boolean;
  onReference: () => void;
  onGeometryReference: () => void;
  onSubmit: (payload: FormulaInkSubmitPayload) => boolean;
};

export function FormulaInkPopover({
  position,
  sourceLatex,
  disabled = false,
  onReference,
  onGeometryReference,
  onSubmit,
}: FormulaInkPopoverProps) {
  const [expandedSourceLatex, setExpandedSourceLatex] = useState<string | null>(null);
  const { canvasRef, hasInk, clearCanvas, exportImage, canvasHandlers } = useFormulaInkCanvas();
  const isExpanded = expandedSourceLatex === sourceLatex;

  if (!position || !sourceLatex.trim()) {
    return null;
  }

  function handleSubmit(action: FormulaInkAction) {
    const imageDataUrl = exportImage();
    if (!imageDataUrl) {
      return;
    }
    const accepted = onSubmit({
      action,
      imageDataUrl,
      sourceLatex,
    });
    if (!accepted) {
      return;
    }
    setExpandedSourceLatex(null);
    clearCanvas();
  }

  if (!isExpanded) {
    return (
      <div
        className="fixed z-[95] flex -translate-x-1/2 items-center overflow-hidden rounded-xl border border-gray-200 bg-white shadow-lg"
        style={{ left: position.left, top: position.top }}
        onMouseDown={(event) => event.preventDefault()}
      >
        <button
          type="button"
          disabled={disabled}
          onClick={() => {
            clearCanvas();
            setExpandedSourceLatex(sourceLatex);
          }}
          className="inline-flex h-10 items-center gap-2 px-3.5 text-[13px] font-medium text-gray-800 transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
          title="Handwritten editing formula"
        >
          <PenLine className="h-4 w-4" />

          Edit formula
        </button>
        <span className="h-5 w-px bg-gray-200" aria-hidden="true" />
        <button
          type="button"
          disabled={disabled}
          onClick={onReference}
          className="inline-flex h-10 items-center gap-2 px-3.5 text-[13px] font-medium text-gray-800 transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
          title="Reference the formula to the input box"
        >
          <TextQuote className="h-4 w-4" />

          Quote
        </button>
        <span className="h-5 w-px bg-gray-200" aria-hidden="true" />
        <button
          type="button"
          disabled={disabled}
          onClick={onGeometryReference}
          className="inline-flex h-10 items-center gap-2 px-3.5 text-[13px] font-medium text-gray-800 transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
          title="Reference formulas to graph generation"
        >
          <Box className="h-4 w-4" />

          Reference to graphics
        </button>
      </div>
    );
  }

  return (
    <div
      className="fixed z-[96] w-[min(560px,calc(100vw-32px))] -translate-x-1/2 rounded-2xl border border-gray-200 bg-white p-3 shadow-2xl"
      style={{ left: position.left, top: position.top }}
      onMouseDown={(event) => event.preventDefault()}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[11px] font-bold uppercase tracking-widest text-gray-500">Handwritten formula</p>
          <p className="mt-1 truncate rounded-lg bg-gray-50 px-2 py-1 font-mono text-[12px] text-gray-700">
            {sourceLatex}
          </p>
        </div>
        <button
          type="button"
          onClick={() => {
            setExpandedSourceLatex(null);
            clearCanvas();
          }}
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-gray-400 transition hover:bg-gray-50 hover:text-gray-800"
          aria-label="Close the handwritten formula panel"
          title="closure"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <canvas
        ref={canvasRef}
        aria-label="Handwritten formula drawing board"
        className="mt-3 block h-[220px] w-full touch-none rounded-xl border border-gray-200 bg-white shadow-inner"
        {...canvasHandlers}
      />

      <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
        <button
          type="button"
          onClick={clearCanvas}
          className="inline-flex h-9 items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 text-[12px] font-semibold text-gray-600 transition hover:border-gray-300"
        >
          <Eraser className="h-4 w-4" />

          Clear
        </button>
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={!hasInk || disabled}
            onClick={() => handleSubmit("reference")}
            className="inline-flex h-9 items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 text-[12px] font-semibold text-gray-700 transition hover:border-gray-300 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Send className="h-4 w-4" />

            Quote
          </button>
          <button
            type="button"
            disabled={!hasInk || disabled}
            onClick={() => handleSubmit("replace")}
            className="inline-flex h-9 items-center gap-2 rounded-lg bg-gray-950 px-3 text-[12px] font-semibold text-white transition hover:bg-black disabled:cursor-not-allowed disabled:opacity-50"
          >
            <RefreshCcw className="h-4 w-4" />

            Change
          </button>
        </div>
      </div>
    </div>
  );
}
