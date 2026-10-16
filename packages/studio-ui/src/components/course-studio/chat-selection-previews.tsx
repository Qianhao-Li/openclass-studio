"use client";

import { useId, useState } from "react";
import clsx from "clsx";
import { ChevronDown, ChevronUp, TextQuote } from "lucide-react";
import type { SelectionRef } from "../../types";

function selectionPreviewLabel(selection: SelectionRef): string {
  if (selection.kind === "source") return "Referenced material sections";
  return selection.kind === "board" ? "Selected lesson documents" : "Quoted dialogue";
}

export function selectionPreviewText(excerpt: string): string {
  return excerpt.replace(/\s+/g, " ").trim();
}

function selectionPreviewKey(selection: SelectionRef, index: number): string {
  return [selection.kind, selection.source_ingestion_id, selection.source_chapter_id,
    selection.source_page_start, selection.source_page_end, selection.document_id,
    selection.segment_id, selection.text_hash, index].join(":");
}

function ChatSelectionPreview({ selection }: { selection: SelectionRef }) {
  const [isExpanded, setIsExpanded] = useState(false);
  const excerpt = selectionPreviewText(selection.excerpt);
  if (!excerpt) return null;
  return (
    <div data-testid="chat-selection-preview" className="rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-gray-700">
      <div className="flex items-start gap-2">
        <TextQuote aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0 text-gray-400" />
        <div className="min-w-0">
          <p className="text-[11px] font-semibold text-gray-500">{selectionPreviewLabel(selection)}</p>
          <p className={clsx("mt-1 whitespace-pre-wrap break-words pr-1 text-[12px] leading-5",
            isExpanded ? "custom-scrollbar max-h-40 overflow-y-auto" : "max-h-10 overflow-hidden")}>
            “{excerpt}”
          </p>
          <button type="button" aria-expanded={isExpanded} onClick={() => setIsExpanded((current) => !current)}
            className="mt-2 inline-flex h-6 items-center gap-1 rounded-md px-1.5 text-[11px] font-semibold text-gray-500 transition hover:bg-white hover:text-gray-900">
            {isExpanded ? <ChevronUp aria-hidden="true" className="h-3.5 w-3.5" /> : <ChevronDown aria-hidden="true" className="h-3.5 w-3.5" />}
            {isExpanded ? "close" : "Expand"}
          </button>
        </div>
      </div>
    </div>
  );
}

/** Display facts already present on these refs; excerpts never define scope. */
export function sourceSelectionOverview(selections: readonly SelectionRef[]) {
  const titles = [...new Set(selections.map((item) => item.source_title?.trim()).filter((title): title is string => Boolean(title)))];
  const title = titles.slice(0, 2).join(" · ") + (titles.length > 2 ? ` (+${titles.length - 2} more)` : "");
  const first = selections[0];
  const oneSource = Boolean(first.source_ingestion_id) && selections.every((item) =>
    item.source_ingestion_id === first.source_ingestion_id
    && (item.source_content_hash || "") === (first.source_content_hash || "")
    && (item.catalog_version ?? null) === (first.catalog_version ?? null));
  const pages = selections.map((item) => {
    const range = item.source_range;
    const start = range ? range.start : item.source_page_start;
    const end = range ? range.end : item.source_page_end;
    if ((range && range.kind !== "pdf_pages") || typeof start !== "number" || typeof end !== "number" || end < start) {
      return { start: null, end: null };
    }
    // Native scopes own their endpoint convention. Old chapter selections use
    // an exclusive source_page_end, including the one-page compatibility form.
    const exclusive = range && (range as { end_inclusive?: boolean }).end_inclusive === false;
    return { start, end: range ? end - (exclusive ? 1 : 0) : Math.max(start, end - 1) };
  });
  if (!oneSource || !pages.every(({ start, end }) => typeof start === "number" && Number.isSafeInteger(start)
      && typeof end === "number" && Number.isSafeInteger(end) && start > 0 && end >= start)) return { title, range: "" };
  const start = Math.min(...pages.map((item) => item.start as number));
  const end = Math.max(...pages.map((item) => item.end as number));
  const unit = selections.every((item) => item.source_range?.kind === "pdf_pages") ? "PDF pages" : "pages";
  return { title, range: `${unit} ${start === end ? start : `${start}–${end}`}` };
}

function ChatSourceSelectionGroup({ selections }: { selections: readonly SelectionRef[] }) {
  const [isExpanded, setIsExpanded] = useState(false);
  const detailsId = useId();
  const overview = sourceSelectionOverview(selections);
  return (
    <div data-testid="chat-source-selection-group" className="overflow-hidden rounded-lg border border-gray-200 bg-gray-50 text-gray-700">
      <button type="button" data-testid="chat-source-selection-toggle" aria-expanded={isExpanded} aria-controls={detailsId}
        aria-label={`${isExpanded ? "Collapse" : "Expand"} ${selections.length} referenced material sections`}
        onClick={() => setIsExpanded((current) => !current)}
        className="flex w-full items-start gap-2 px-3 py-2 text-left transition hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-blue-500">
        <TextQuote aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0 text-gray-400" />
        <span className="min-w-0 flex-1">
          <span className="block text-[11px] font-semibold text-gray-500">Referenced material sections</span>
          {overview.title ? <span className="mt-1 line-clamp-2 break-words text-[12px] leading-5">{overview.title}</span> : null}
          <span className="mt-1 block text-[11px] text-gray-500">
            {overview.range ? `${overview.range} · ` : ""}{selections.length} references
          </span>
        </span>
        {isExpanded ? <ChevronUp aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0" /> : <ChevronDown aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 shrink-0" />}
      </button>
      <div id={detailsId} hidden={!isExpanded} data-testid="chat-source-selection-details" className="space-y-2 border-t border-gray-200 p-2">
        {selections.map((selection, index) => <ChatSelectionPreview key={selectionPreviewKey(selection, index)} selection={selection} />)}
      </div>
    </div>
  );
}

export function ChatSelectionPreviews({ selections }: { selections: readonly SelectionRef[] }) {
  // This component is mounted inside exactly one chat message. Preserve card
  // order and non-source cards, including any selected board or quoted dialogue.
  const runs: SelectionRef[][] = [];
  for (const selection of selections) {
    if (!selectionPreviewText(selection.excerpt)) continue;
    const previous = runs[runs.length - 1];
    if (selection.kind === "source" && previous?.[0].kind === "source") previous.push(selection);
    else runs.push([selection]);
  }
  return <>{runs.map((run, index) => run.length > 1 && run[0].kind === "source"
    ? <ChatSourceSelectionGroup key={`group:${selectionPreviewKey(run[0], index)}`} selections={run} />
    : <ChatSelectionPreview key={selectionPreviewKey(run[0], index)} selection={run[0]} />)}</>;
}
