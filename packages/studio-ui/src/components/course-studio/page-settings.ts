import type { DocumentPageSettings } from "../../types";

export const DEFAULT_PAGE_SETTINGS: DocumentPageSettings = {
  margin_preset: "normal",
  orientation: "portrait",
  page_size: "a4",
  columns: 1,
  page_border: true,
  background_style: "plain",
  watermark_text: "",
  line_numbers: false,
  show_page_number: false,
  header_text: "",
  footer_text: "",
};

export const PAGE_SIZE_OPTIONS = [
  { value: "a4", label: "A4", width: 860, height: 1216 },
  { value: "letter", label: "Letter", width: 884, height: 1142 },
  { value: "a3", label: "A3", width: 980, height: 1386 },
] as const;

export const PAGE_MARGIN_OPTIONS = [
  { value: "narrow", label: "narrow", paddingX: 42, paddingY: 54 },
  { value: "normal", label: "ordinary", paddingX: 56, paddingY: 68 },
  { value: "wide", label: "Width", paddingX: 74, paddingY: 86 },
] as const;

export const PAGE_BACKGROUND_OPTIONS = [
  { value: "plain", label: "pure white" },
  { value: "warm", label: "warm white" },
  { value: "grid", label: "grid paper" },
] as const;

export const PAGE_ZOOM_MIN = 50;
export const PAGE_ZOOM_MAX = 200;
export const PAGE_ZOOM_DEFAULT = 100;
export const PAGE_ZOOM_STEP = 10;
export const PAGE_ZOOM_SLIDER_STEP = 5;
export const PAGE_ZOOM_WHEEL_SENSITIVITY = 0.18;

export function normalizePageZoom(value: number) {
  if (!Number.isFinite(value)) {
    return PAGE_ZOOM_DEFAULT;
  }
  return Math.min(PAGE_ZOOM_MAX, Math.max(PAGE_ZOOM_MIN, Math.round(value)));
}

export function normalizePageSettings(settings?: Partial<DocumentPageSettings> | null): DocumentPageSettings {
  return {
    ...DEFAULT_PAGE_SETTINGS,
    ...(settings ?? {}),
  };
}

export function pagePreviewMetrics(settings: DocumentPageSettings) {
  const baseSize = PAGE_SIZE_OPTIONS.find((option) => option.value === settings.page_size) ?? PAGE_SIZE_OPTIONS[0];
  const margin = PAGE_MARGIN_OPTIONS.find((option) => option.value === settings.margin_preset) ?? PAGE_MARGIN_OPTIONS[1];
  const width = settings.orientation === "landscape" ? baseSize.height : baseSize.width;
  const height = settings.orientation === "landscape" ? baseSize.width : baseSize.height;
  return {
    width,
    height,
    paddingX: margin.paddingX,
    paddingY: margin.paddingY,
    contentMinHeight: Math.max(360, height - margin.paddingY * 2 - 190),
  };
}
