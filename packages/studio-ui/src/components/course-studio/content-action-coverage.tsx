"use client";

import { publicAgentActivityText } from "../../lib/agent-activity";

import { useId, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import { Eye, LocateFixed, Redo2, Share2, Undo2 } from "lucide-react";
import type { ContentAction, ContentActionView } from "../../types/content-actions";
import { COVERAGE_BASE_SCALE, coverageHitGroups, exposedUndoIds, layoutContentActions, visibleContentActions } from "./content-action-layout";

export interface ContentActionCoverageProps {
  view: ContentActionView | null;
  loading?: boolean;
  error?: string | null;
  disabledReason?: string | null;
  editing: boolean;
  readOnly?: boolean;
  busy?: boolean;
  selectedId?: string | null;
  onSelect: (id: string, fragmentId?: string) => void;
  onPreview: (id: string) => void;
  onLocate: (id: string) => void;
  canLocate?: (id: string) => boolean;
  shareHref?: (id: string) => string | null;
  shareDisabledReason?: string | null;
  onToggleEditing: () => void;
  onUndo: (ids: string[]) => void;
  onRedo: (id: string) => void;
}

const buttonBaseClass = "rounded-md border border-neutral-300 bg-white py-1.5 font-medium text-neutral-700 transition hover:bg-neutral-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-45 dark:border-neutral-600 dark:bg-neutral-900 dark:text-neutral-200 dark:hover:bg-neutral-800";
const buttonClass = `${buttonBaseClass} px-2.5 text-xs`;
const actionButtonClass = `${buttonBaseClass} inline-flex h-10 min-w-0 flex-1 flex-col items-center justify-center gap-0.5 whitespace-nowrap px-0.5 text-[10px] [&>svg]:h-3 [&>svg]:w-3`;
const actionColors = ["#bfdbfe", "#ddd6fe", "#a7f3d0", "#fecdd3", "#a5f3fc", "#fed7aa"];
const DEFAULT_COVERAGE_ZOOM = 1;

function actionColor(action: ContentAction): string {
  if (action.status === "withdrawn") return "#d4d4d4";
  if (action.is_new) return "#fcd34d";
  if (action.initial) return "#e5e7eb";
  return actionColors[Math.abs(action.sequence) % actionColors.length];
}

function positionLabel(action: ContentAction): string {
  if (action.display_spans !== undefined) {
    if (action.initial) return "Initial position";
    if (!action.display_spans.length) return "Range not determined";
    return action.display_spans.map((span) => `${(span.start * 100).toFixed(1)}%–${(span.end * 100).toFixed(1)}%`).join(", ");
  }
  if (action.initial || action.scope === "document") return "Whole document";
  if (!action.spans.length) return "No locatable range";
  return action.spans.map((span) => `${Math.round(span.start * 100)}%–${Math.round(span.end * 100)}%`).join(", ");
}

function blockedReason(action: ContentAction, labels: Map<string, string>): string {
  if (action.initial) return "The initial state C0 cannot be undone.";
  if (action.blocked_reason) return publicAgentActivityText(action.blocked_reason);
  if (action.blockers.length) return `Resolve these dependent actions first: ${action.blockers.map((id) => labels.get(id) ?? id).join(", ")}.`;
  return action.status === "withdrawn" ? "This action cannot be redone." : "This action cannot be undone.";
}

export function ContentActionCoverage({
  view, loading = false, error, disabledReason, editing, readOnly = false, busy = false,
  selectedId, onSelect, onPreview, onLocate, canLocate, shareHref, shareDisabledReason, onToggleEditing, onUndo, onRedo,
}: ContentActionCoverageProps) {
  const headingId = useId();
  const detailId = useId();
  const [showAll, setShowAll] = useState(false);
  const [zoom, setZoom] = useState(DEFAULT_COVERAGE_ZOOM);
  const actions = useMemo(() => visibleContentActions(view?.actions ?? [], showAll), [view, showAll]);
  const layout = useMemo(() => layoutContentActions(actions, zoom), [actions, zoom]);
  const labels = useMemo(() => new Map((view?.actions ?? []).map((action) => [action.id, action.label])), [view]);
  const selected = view?.actions.find((action) => action.id === selectedId);
  const undoIds = exposedUndoIds(actions);
  const changeCount = view?.actions.filter((action) => !action.initial).length ?? 0;
  const disabled = busy || loading || !view;
  const mutationDisabled = disabled || Boolean(disabledReason);
  const selectedBlocked = selected && !selected.initial && (selected.status === "withdrawn" ? !selected.can_redo : !selected.can_undo);
  const selectedLocatable = Boolean(selected?.range_projection?.fragments.some((f) => f.precision === "exact") && (canLocate?.(selected.id) ?? true));
  const selectedShareHref = selected ? shareHref?.(selected.id) : null;
  const unavailableReason = busy ? "The current operation is still running. Try again shortly." : loading || !view ? "Loading document actions. Try again shortly." : null;
  const selectedShareDisabledReason = shareDisabledReason ?? unavailableReason ?? (!selectedShareHref ? "This action is not saved yet. Keep the History draft before sharing." : null);
  const editDisabledReason = unavailableReason ?? disabledReason ?? (!editing ? "Enter edit mode first." : null);
  const selectAction = (id: string) => {
    if (!actions.some((action) => action.id === id)) setShowAll(true);
    onSelect(id);
  };
  const linkedActions = (ids: string[]) => ids.length ? ids.map((id, index) => <span key={id}>
    {index > 0 ? ", " : null}<button type="button" className="text-blue-700 underline underline-offset-2 dark:text-blue-300" onClick={() => selectAction(id)}>{labels.get(id) ?? id}</button>
  </span>) : <span>None</span>;

  return (
    <section data-testid="content-action-coverage" aria-labelledby={headingId} aria-busy={loading || busy} className="min-w-0 overflow-hidden rounded-xl border border-neutral-200 bg-neutral-50 text-neutral-900 dark:border-neutral-700 dark:bg-neutral-950 dark:text-neutral-100">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-neutral-200 p-3 dark:border-neutral-700">
        <div className="min-w-0">
          <h3 id={headingId} className="text-sm font-semibold"><span>Document action coverage</span></h3>
          <p className="mt-1 text-xs text-neutral-500 dark:text-neutral-400"><span>Vertical position follows the document; horizontal levels show dependencies. Matching colors belong to one complete action. Independent later actions can be kept.</span></p>
        </div>
        {!readOnly && <button type="button" className={buttonClass} aria-label={editing ? "Exit document action edit mode" : "Enter document action edit mode"} disabled={disabled || (!editing && Boolean(disabledReason))} title={disabledReason ?? undefined} aria-pressed={editing} onClick={onToggleEditing}><span>{editing ? "Exit edit mode" : "Edit mode"}</span></button>}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
        <div className="flex flex-wrap items-center gap-2 text-xs text-neutral-600 dark:text-neutral-300">
          <span>{view ? `Branch ${view.branch_name}` : "Document action"}</span>
          <span>{showAll ? `All ${changeCount} actions` : `Latest ${Math.min(changeCount, 20)} actions`}</span>
          {changeCount > 20 && <button type="button" className={buttonClass} onClick={() => setShowAll((value) => !value)}><span>{showAll ? "Show latest 20" : `Show all ${changeCount}`}</span></button>}
        </div>
        <div className="flex items-center gap-1" role="group" aria-label="Coverage zoom">
          <button type="button" className={buttonClass} disabled={zoom <= 0.75} onClick={() => setZoom((value) => Math.max(0.75, value - 0.25))} aria-label="Zoom out coverage"><span>−</span></button>
          <button type="button" className={buttonClass} onClick={() => setZoom(DEFAULT_COVERAGE_ZOOM)} aria-label="Reset coverage zoom"><span>{Math.round(zoom * 100)}%</span></button>
          <button type="button" className={buttonClass} disabled={zoom >= 2} onClick={() => setZoom((value) => Math.min(2, value + 0.25))} aria-label="Zoom in coverage"><span>＋</span></button>
        </div>
      </div>

      {loading && <p role="status" className="px-3 pb-2 text-xs text-neutral-500"><span>Loading document actions…</span></p>}
      {error && <p role="alert" className="px-3 pb-2 text-xs text-red-600 dark:text-red-400"><span>{error}</span></p>}
      {disabledReason && disabledReason !== error && <p role="status" className="px-3 pb-2 text-xs text-amber-700 dark:text-amber-300"><span>{disabledReason}</span></p>}
      {!loading && !error && !actions.length && <p className="p-6 text-center text-sm text-neutral-500 dark:text-neutral-400"><span>No document actions yet.</span></p>}

      {!!actions.length && <div tabIndex={0} role="group" aria-label="Document action ranges" className="max-h-[640px] overflow-auto border-y border-neutral-200 bg-white focus-visible:outline-2 focus-visible:outline-blue-500 dark:border-neutral-700 dark:bg-neutral-900">
        <div className="relative" style={{ width: layout.width, height: layout.height }}>
          <svg className="pointer-events-none absolute inset-0 h-full w-full" aria-hidden="true">
            {[0, 0.25, 0.5, 0.75, 1].map((position) => <g key={position}>
              <line x1={54} x2={layout.width - 16} y1={layout.documentTop + position * layout.documentHeight} y2={layout.documentTop + position * layout.documentHeight} stroke="currentColor" className="text-neutral-100 dark:text-neutral-800" />
              <text x={8} y={layout.documentTop + position * layout.documentHeight + 4} fontSize={10} fill="currentColor" className="text-neutral-500 dark:text-neutral-400">{`${position * 100}%`}</text>
            </g>)}
            {layout.edges.map((edge) => <path key={edge.id} d={edge.path} fill="none" stroke="currentColor" strokeWidth={selectedId === edge.fromId || selectedId === edge.toId ? 2 : 1} strokeDasharray="4 3" className={selectedId === edge.fromId || selectedId === edge.toId ? "text-blue-500" : "text-neutral-400 dark:text-neutral-500"} />)}
          </svg>

          {layout.ranks.map((rank) => <div key={rank.rank} className="absolute top-3 whitespace-nowrap border-b border-neutral-200 pb-1 text-center text-[10px] text-neutral-500 dark:border-neutral-700 dark:text-neutral-400" style={{ left: rank.x, width: rank.width, fontSize: 10 * Math.min(zoom, 1) }}><span>{`Dependency level ${rank.rank}`}</span></div>)}
          {layout.nodes.map(({ action, x, width, spans, anchorY }) => {
            const color = actionColor(action);
            const leaf = !action.initial && action.status === "active" && action.can_undo;
            const selectedNode = selectedId === action.id;
            const title = `${action.label} · ${positionLabel(action)}${action.is_new ? " · New draft" : ""}${action.status === "withdrawn" ? " · Withdrawn" : ""}${leaf ? " · Available to undo" : ""}`;
            const blockStyle: CSSProperties = {
              backgroundColor: color,
              borderColor: selectedNode ? "#2563eb" : action.is_new ? "#b45309" : leaf ? "#0f766e" : "#737373",
              borderWidth: selectedNode || leaf ? 2 : 1,
              boxShadow: action.is_new ? "0 0 12px rgb(245 158 11 / .45)" : leaf ? "0 0 9px rgb(20 184 166 / .25)" : undefined,
            };
            const hitGroups = coverageHitGroups(spans);
            return <div key={action.id}>
              {spans.length > 1 && <div aria-hidden="true" className="absolute w-0 border-l-2 border-dashed" style={{ left: x + width / 2, top: spans[0].top, height: spans.at(-1)!.top + spans.at(-1)!.height - spans[0].top, borderColor: action.status === "withdrawn" ? "#a3a3a3" : color }} />}
              {hitGroups.map((group, index) => {
                const before = Math.min(6, Math.max(0, (group.top - (index ? hitGroups[index - 1].top + hitGroups[index - 1].height : layout.documentTop)) / 2));
                const after = Math.min(6, Math.max(0, ((hitGroups[index + 1]?.top ?? layout.documentTop + layout.documentHeight) - group.top - group.height) / 2));
                return <button key={`${group.top}:${index}`} type="button" data-coverage-action-id={action.id} data-action-status={action.status} data-can-undo={action.can_undo}
                  title={title} aria-label={`${title}, fragment group ${index + 1}`} aria-pressed={selectedNode} aria-describedby={selectedNode ? detailId : undefined}
                  className="absolute bg-transparent text-left text-[11px] font-medium text-neutral-900 hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500"
                  style={{ left: x, top: group.top - before, width, height: group.height + before + after }}
                  onClick={(event) => {
                    const y = event.clientY - event.currentTarget.getBoundingClientRect().top + group.top - before;
                    const distance = (part: typeof group.parts[number]) => Math.max(part.top - y, y - part.top - part.height, 0);
                    const closest = event.detail ? group.parts.reduce((best, part) => distance(part) < distance(best) ? part : best) : group.parts[0];
                    onSelect(action.id, closest.fragment_id);
                  }}>
                  {group.parts.map((span, partIndex) => <span key={partIndex} data-coverage-span="true" aria-hidden="true" className="pointer-events-none absolute inset-x-0 rounded border"
                    style={{ top: span.top - group.top + before, height: span.height, ...blockStyle, ...(span.height < 4 ? { borderWidth: 0, outline: `1px solid ${blockStyle.borderColor}` } : {}) }} />)}
                  {index === 0 && <span className="absolute left-1 z-10 max-w-[calc(100%-8px)] truncate rounded px-0.5 leading-4" style={{ top: before + (group.parts[0].height < 16 ? -16 : 2), backgroundColor: color, fontSize: 11 * COVERAGE_BASE_SCALE * zoom }}><span>{action.label}</span>{leaf && <span aria-hidden="true"> ↟</span>}{action.is_new && <span> New</span>}</span>}
                </button>;
              })}
              {!spans.length && <button type="button" data-coverage-action-id={action.id} data-action-status={action.status} data-can-undo={action.can_undo} className="absolute truncate rounded border border-dashed px-1 py-1 text-xs text-neutral-900" style={{ left: x, top: anchorY, width, backgroundColor: color }} title={title} aria-pressed={selectedNode} onClick={() => onSelect(action.id)}><span>{`${action.label} · Range unknown`}</span></button>}
            </div>;
          })}
        </div>
      </div>}

      <div className="flex flex-wrap gap-x-3 gap-y-1 px-3 py-2 text-[11px] text-neutral-500 dark:text-neutral-400" aria-label="Coverage legend">
        <span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-teal-500" />Available to undo</span>
        <span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-neutral-400" />Withdrawn / pending removal</span>
        <span><span className="mr-1 inline-block h-2 w-2 rounded-sm bg-amber-400" />New draft</span>
        <span>Dashed lines across columns show dependencies; within a column they link parts of the same action.</span>
      </div>

      <div id={detailId} className="border-t border-neutral-200 p-3 text-xs dark:border-neutral-700" aria-live="polite">
        {selected ? <>
          <div className="min-w-0">
            <p className="font-semibold"><span>{selected.label}</span><span className="ml-2 font-normal text-neutral-500 dark:text-neutral-400">{selected.initial ? "Initial state" : selected.status === "withdrawn" ? "Withdrawn" : selected.is_new ? "New draft" : "Document action"}</span></p>
            {selected.range_projection && <p className="mt-1 text-neutral-600 dark:text-neutral-300">Actual changes: {selected.range_projection.fragments.filter((fragment) => fragment.precision === "exact").length} fragments{selected.range_projection.fragments.some((fragment) => fragment.precision === "unknown" || fragment.reference_precision === "unknown") ? " · Some ranges cannot be located precisely" : ""}</p>}
            {selected.summary && <p className="mt-1 whitespace-pre-wrap break-words text-neutral-600 dark:text-neutral-300"><span>{selected.range_projection ? "Undo check: " : ""}{selected.summary}</span></p>}
          </div>
          <div role="group" aria-label="Selected action controls" className="mt-2 flex items-center gap-1 overflow-x-auto py-1">
            <button type="button" className={actionButtonClass} aria-label="Preview action" disabled={disabled} title={unavailableReason ?? "Preview the document after this action"} onClick={() => onPreview(selected.id)}><Eye aria-hidden="true" className="h-3.5 w-3.5" /><span>Preview</span></button>
            <button type="button" className={actionButtonClass} aria-label="Locate in document" disabled={disabled || !selectedLocatable} onClick={() => onLocate(selected.id)} title={unavailableReason ?? (!selectedLocatable ? "This action has no verified range in the preview document" : "Locate the changed range in the action preview")}><LocateFixed aria-hidden="true" className="h-3.5 w-3.5" /><span>Locate</span></button>
            {selectedShareHref && !selectedShareDisabledReason ? <a className={actionButtonClass} href={selectedShareHref} target="_blank" rel="noreferrer" aria-label="Share this action" title="Share this saved history action in the community"><Share2 aria-hidden="true" className="h-3.5 w-3.5" /><span>Share</span></a>
              : <button type="button" className={actionButtonClass} aria-label="Share this action" disabled title={selectedShareDisabledReason ?? undefined}><Share2 aria-hidden="true" className="h-3.5 w-3.5" /><span>Share</span></button>}
            {!readOnly && !selected.initial && <>
              <button type="button" className={actionButtonClass} aria-label="Apply this action" disabled={Boolean(editDisabledReason) || selected.status !== "withdrawn" || !selected.can_redo} title={editDisabledReason ?? (selected.status === "active" ? "This action is already applied to the current document." : selected.can_redo ? "Reapply this action to the History draft" : blockedReason(selected, labels))} onClick={() => onRedo(selected.id)}><Redo2 aria-hidden="true" className="h-3.5 w-3.5" /><span>Apply</span></button>
              <button type="button" className={actionButtonClass} aria-label="Undo this action" disabled={Boolean(editDisabledReason) || selected.status !== "active" || !selected.can_undo} title={editDisabledReason ?? (selected.status === "withdrawn" ? "This action was undone. Use Apply to restore it." : selected.can_undo ? "Undo only this action" : blockedReason(selected, labels))} onClick={() => onUndo([selected.id])}><Undo2 aria-hidden="true" className="h-3.5 w-3.5" /><span>Undo</span></button>
            </>}
          </div>
          <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-neutral-600 dark:text-neutral-300">
            <dt><span>Changed range</span></dt><dd><span>{positionLabel(selected)}</span></dd>
            <dt><span>Dependencies</span></dt><dd className="break-words">{linkedActions(selected.dependencies)}</dd>
            <dt><span>Blocking actions</span></dt><dd className="break-words">{linkedActions(selected.blockers)}</dd>
          </dl>
          {selected.changes?.length ? <div className="mt-3">
            <p className="font-medium"><span>Changes</span></p>
            <ul className="mt-1 max-h-48 space-y-2 overflow-auto rounded border border-neutral-200 p-2 dark:border-neutral-700">
              {selected.changes.map((change, index) => <li key={`${change.block_id}:${change.start}:${index}`} className="whitespace-pre-wrap break-words">
                {change.deleted === change.inserted ? <span className="text-neutral-600 dark:text-neutral-300">Formatting or attribute changes: {change.inserted.replaceAll("\uFFFC", "[object]") || "content here"}</span> : <>
                  {change.deleted ? <del className="mr-1 bg-red-50 text-red-800 dark:bg-red-950 dark:text-red-200">{change.deleted.replaceAll("\uFFFC", "[object]")}</del> : null}
                  {change.inserted ? <ins className="bg-emerald-50 text-emerald-800 no-underline dark:bg-emerald-950 dark:text-emerald-200">{change.inserted.replaceAll("\uFFFC", "[object]")}</ins> : null}
                </>}
              </li>)}
            </ul>
          </div> : null}
          {!selectedLocatable && <p className="mt-2 text-neutral-500 dark:text-neutral-400"><span>This action has no verified range in the preview document.</span></p>}
          {(selectedBlocked || selected.initial) && <p className="mt-2 text-amber-700 dark:text-amber-300"><span>{blockedReason(selected, labels)}</span></p>}
          {!readOnly && editing && !selected.initial && <p className="mt-3 text-neutral-500 dark:text-neutral-400"><span>Changes are previewed in the History edit draft.</span></p>}
        </> : <p className="text-neutral-500 dark:text-neutral-400"><span>Select an action to see its document range and available operations.</span></p>}
      </div>

      {!readOnly && editing && <div className="flex flex-wrap items-center gap-2 border-t border-neutral-200 px-3 py-2 dark:border-neutral-700">
        <button type="button" className={buttonClass} disabled={mutationDisabled || !undoIds.length} title={disabledReason ?? (undoIds.length ? "Undo the actions currently available in the graph. Newly exposed actions are kept." : "No visible actions are currently available to undo")} onClick={() => onUndo(undoIds)}><span>{`Undo available actions (${undoIds.length})`}</span></button>
        <p className="text-[11px] text-neutral-500 dark:text-neutral-400"><span>{undoIds.length ? "Only visible actions currently available to undo are included." : "No actions are available to undo. Select an action to see why."}</span></p>
      </div>}
    </section>
  );
}
