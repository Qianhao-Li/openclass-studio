import clsx from "clsx";
import { Maximize2, Minus, Plus } from "lucide-react";
import type { CSSProperties, ReactNode } from "react";

import {
  PAGE_ZOOM_DEFAULT,
  PAGE_ZOOM_MAX,
  PAGE_ZOOM_MIN,
  PAGE_ZOOM_SLIDER_STEP,
  PAGE_ZOOM_STEP,
} from "./page-settings";

export function ToolbarButton({
  active,
  disabled,
  title,
  onClick,
  children,
}: {
  active?: boolean;
  disabled?: boolean;
  title: string;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      aria-pressed={active}
      onClick={onClick}
      disabled={disabled}
      className={clsx(
        "flex h-9 w-9 items-center justify-center rounded-lg border text-gray-600 transition",
        active
          ? "border-black bg-black text-white"
          : "border-transparent hover:border-gray-200 hover:bg-white",
        disabled && "cursor-not-allowed opacity-40"
      )}
    >
      {children}
    </button>
  );
}

export function RibbonTabButton({
  active,
  children,
  onClick,
}: {
  active: boolean;
  children: ReactNode;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={clsx(
        "mr-4 flex h-full items-center gap-1.5 border-b-2 px-2 text-[10px] font-bold uppercase tracking-widest transition-colors",
        active ? "border-black text-black" : "border-transparent text-gray-400 hover:text-black"
      )}
    >
      {children}
    </button>
  );
}

export function RibbonActionButton({
  title,
  label,
  hint,
  icon,
  active,
  disabled,
  onClick,
}: {
  title: string;
  label: string;
  hint?: string;
  icon?: ReactNode;
  active?: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      title={title}
      aria-pressed={active}
      onClick={onClick}
      disabled={disabled}
      className={clsx(
        "flex min-w-[86px] flex-col items-start rounded-lg border px-2.5 py-2 text-left transition",
        active
          ? "border-black bg-black text-white shadow-sm"
          : "border-gray-200 bg-white text-gray-700 hover:border-gray-300 hover:bg-gray-50",
        disabled && "cursor-not-allowed opacity-40"
      )}
    >
      <span className="flex items-center gap-2">
        {icon ? (
          <span
            className={clsx(
              "flex h-7 w-7 shrink-0 items-center justify-center rounded-md",
              active ? "bg-white/15 text-white" : "bg-gray-100 text-gray-700"
            )}
          >
            {icon}
          </span>
        ) : null}
        <span className="text-[12px] font-semibold">{label}</span>
      </span>
      {hint ? <span className={clsx("mt-1 text-[10px]", active ? "text-white/70" : "text-gray-400")}>{hint}</span> : null}
    </button>
  );
}

export function WordPageZoomControls({
  value,
  onChange,
  onFitToWidth,
}: {
  value: number;
  onChange: (value: number) => void;
  onFitToWidth: () => void;
}) {
  const zoomProgress = ((value - PAGE_ZOOM_MIN) / (PAGE_ZOOM_MAX - PAGE_ZOOM_MIN)) * 100;

  return (
    <div className="word-editor__zoom-controls flex h-10 items-center gap-1 rounded-full border border-gray-200 bg-gradient-to-b from-white to-gray-50 px-1.5 text-gray-600 shadow-[0_1px_3px_rgba(15,23,42,0.08)]">
      <button
        type="button"
        title="Adapt to page width"
        aria-label="Adapt to page width"
        onClick={onFitToWidth}
        className="flex h-7 w-7 items-center justify-center rounded-full transition hover:bg-white hover:text-black hover:shadow-sm"
      >
        <Maximize2 className="h-3.5 w-3.5" />
      </button>
      <button
        type="button"
        title="Reset zoom to 100%"
        onClick={() => onChange(PAGE_ZOOM_DEFAULT)}
        className="mx-0.5 flex h-7 min-w-14 items-center justify-center rounded-full border border-gray-200 bg-white px-2 text-[12px] font-semibold tabular-nums text-gray-800 shadow-[inset_0_1px_0_rgba(255,255,255,0.75)] transition hover:border-gray-300 hover:text-black"
      >
        <span className="notranslate" translate="no">
          {value}%
        </span>
      </button>
      <button
        type="button"
        title="Reduce page"
        aria-label="Reduce page"
        disabled={value <= PAGE_ZOOM_MIN}
        onClick={() => onChange(value - PAGE_ZOOM_STEP)}
        className="flex h-7 w-7 items-center justify-center rounded-full transition hover:bg-white hover:text-black hover:shadow-sm disabled:cursor-not-allowed disabled:opacity-35 disabled:hover:bg-transparent disabled:hover:shadow-none"
      >
        <Minus className="h-3.5 w-3.5" />
      </button>
      <input
        type="range"
        min={PAGE_ZOOM_MIN}
        max={PAGE_ZOOM_MAX}
        step={PAGE_ZOOM_SLIDER_STEP}
        value={value}
        aria-label="Page zoom"
        onChange={(event) => onChange(Number(event.target.value))}
        className="word-editor__zoom-range h-5 w-28 sm:w-32"
        style={{ "--word-zoom-progress": `${zoomProgress}%` } as CSSProperties}
      />
      <button
        type="button"
        title="Enlarge page"
        aria-label="Enlarge page"
        disabled={value >= PAGE_ZOOM_MAX}
        onClick={() => onChange(value + PAGE_ZOOM_STEP)}
        className="flex h-7 w-7 items-center justify-center rounded-full transition hover:bg-white hover:text-black hover:shadow-sm disabled:cursor-not-allowed disabled:opacity-35 disabled:hover:bg-transparent disabled:hover:shadow-none"
      >
        <Plus className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
