"use client";

import clsx from "clsx";
import { Bot, GitMerge, RefreshCw, Save, Square, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { LessonMergeOrderEditor } from "./lesson-merge-order-editor";

import type {
  LessonMergeConflictView,
  LessonMergeResolution,
  LessonMergeSessionView,
} from "../../types";

type LessonMergePanelProps = {
  session: LessonMergeSessionView;
  isDraftDirty: boolean;
  isAIProposing: boolean;
  onResolveConflict: (
    conflictId: string,
    resolution: LessonMergeResolution,
    customValue?: unknown
  ) => void | Promise<void>;
  onProposeWithAI: () => void | Promise<void>;
  onCancelAI: () => void;
  onRecompute: () => void | Promise<void>;
  onAbandon: () => void | Promise<void>;
  onSubmit: () => void | Promise<void>;
  onOrderChange: (nodeIds: string[]) => void;
};

function shortCommit(value: string) {
  return value.slice(0, 10);
}

function conflictKindLabel(conflict: LessonMergeConflictView) {
  if (conflict.kind === "board") {
    return "board block";
  }
  if (conflict.kind === "learning_requirement") {
    return "learning needs";
  }
  if (conflict.kind === "board_task") {
    return "Current board content tasks";
  }
  return "Data citation";
}

function ConflictCard({
  conflict,
  onResolve,
}: {
  conflict: LessonMergeConflictView;
  onResolve: (
    resolution: LessonMergeResolution,
    customValue?: unknown
  ) => void | Promise<void>;
}) {
  const [customText, setCustomText] = useState(() =>
    JSON.stringify(conflict.custom_value ?? conflict.target_value, null, 2)
  );
  function applyCustom() {
    let value: unknown = customText;
    try {
      value = JSON.parse(customText) as unknown;
    } catch {
      // Plain text is a valid custom board-block resolution.
    }
    void onResolve("custom", value);
  }

  const options: Array<{ value: LessonMergeResolution; label: string }> = [
    { value: "target", label: "current" },
    { value: "source", label: "source" },
    { value: "both", label: "both" },
    { value: "clear", label: "Clear" },
  ];

  return (
    <article className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[10px] font-bold uppercase tracking-widest text-gray-400">
            {conflictKindLabel(conflict)} · {conflict.path}
          </p>
          <p className="mt-1 text-sm font-semibold leading-6 text-gray-900">{conflict.title}</p>
        </div>
        <span
          className={clsx(
            "shrink-0 rounded-full px-2 py-1 text-[10px] font-semibold",
            conflict.resolved ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-700"
          )}
        >
          {conflict.resolved ? "Resolved" : "Pending"}
        </span>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2">
        {options.map((option) => (
          <button
            key={option.value}
            type="button"
            onClick={() => void onResolve(option.value)}
            className={clsx(
              "rounded-lg border px-3 py-2 text-xs font-semibold transition",
              conflict.resolution === option.value
                ? "border-black bg-black text-white"
                : "border-gray-200 bg-white text-gray-600 hover:border-gray-400"
            )}
          >
            {option.label}
          </button>
        ))}
      </div>
      <details className="mt-3 rounded-lg bg-gray-50 p-3">
        <summary className="cursor-pointer text-xs font-semibold text-gray-600">Custom resolution</summary>
        <textarea
          value={customText}
          onChange={(event) => setCustomText(event.target.value)}
          rows={6}
          className="mt-3 w-full resize-y rounded-lg border border-gray-200 bg-white p-2 font-mono text-[11px] leading-5 outline-none focus:border-black"
        />
        <button
          type="button"
          onClick={applyCustom}
          className="mt-2 rounded-lg border border-gray-300 bg-white px-3 py-2 text-xs font-semibold text-gray-700"
        >

          Apply custom value
        </button>
      </details>
    </article>
  );
}

export function LessonMergePanel({
  session,
  isDraftDirty,
  isAIProposing,
  onResolveConflict,
  onProposeWithAI,
  onCancelAI,
  onRecompute,
  onAbandon,
  onSubmit,
  onOrderChange,
}: LessonMergePanelProps) {
  const unresolvedCount = session.conflicts.filter((conflict) => !conflict.resolved).length;
  const latestActivity = session.agent_activity[session.agent_activity.length - 1];
  const stale = session.status === "stale";
  const [pendingAction, setPendingAction] = useState(false);
  const panelRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    panelRef.current?.parentElement?.scrollTo({ top: 0 });
  }, [session.id]);
  const blockers = session.event_plan?.blockers ?? [];
  const blockedReason = stale
    ? "The branch changed. Recalculate the merge before committing."
    : blockers.length
      ? blockers.map((blocker) => `${blocker.node_id}: ${blocker.reason}`).join("; ")
      : unresolvedCount ? "Resolve the merge conflicts before committing." : null;
  async function runAction(action: () => void | Promise<void>) {
    if (pendingAction) return;
    setPendingAction(true);
    try { await action(); } finally { setPendingAction(false); }
  }

  return (
    <div ref={panelRef} className="space-y-5">
      <section className="rounded-xl border border-blue-200 bg-blue-50 p-4">
        <div className="flex items-center gap-2 text-blue-800">
          <GitMerge className="h-4 w-4" />
          <p className="text-xs font-bold uppercase tracking-wider">Studio Merge Mode</p>
        </div>
        <p className="mt-3 text-sm font-semibold text-gray-950">
          {session.source_branch_name} → {session.target_branch_name}
        </p>
        <dl className="mt-3 space-y-1 text-[11px] leading-5 text-gray-600">
          <div className="flex justify-between gap-3"><dt>common ancestor</dt><dd className="font-mono">{shortCommit(session.base_commit_id)}</dd></div>
          <div className="flex justify-between gap-3"><dt>current head</dt><dd className="font-mono">{shortCommit(session.target_head_commit_id)}</dd></div>
          <div className="flex justify-between gap-3"><dt>Sourcehead</dt><dd className="font-mono">{shortCommit(session.source_head_commit_id)}</dd></div>
        </dl>
        <p className="mt-3 text-[11px] text-gray-500">
          {isDraftDirty ? "Merge draft has unsaved changes." : `Draft saved · v${session.version}`}
        </p>
      </section>

      {stale ? (
        <section className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">

          The branch head has changed and the old draft is still retained but cannot be submitted.
          <button
            type="button"
            onClick={() => void runAction(onRecompute)}
            disabled={pendingAction}
            className="mt-3 inline-flex items-center gap-2 rounded-lg bg-amber-900 px-3 py-2 text-xs font-semibold text-white"
          >
            <RefreshCw className="h-3.5 w-3.5" />

            Recalculate based on the latest head
          </button>
        </section>
      ) : null}

      {session.event_plan ? (
        <>
          <LessonMergeOrderEditor key={session.id} plan={session.event_plan} disabled={stale || pendingAction} onOrderChange={onOrderChange} />
          {blockers.length ? (
            <section role="alert" className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-xs leading-6 text-amber-900">
              <h3 className="font-semibold">Document changes block this merge</h3>
              <ul className="mt-2 list-disc space-y-1 pl-4">
                {blockers.map((blocker, index) => <li key={`${blocker.node_id}:${index}`}><strong>{session.event_plan?.nodes.find((node) => node.id === blocker.node_id)?.title ?? blocker.node_id}</strong>: {blocker.reason}{blocker.object_id ? ` (${blocker.object_id})` : ""}</li>)}
              </ul>
              <p className="mt-2">The merge can be committed only when every document change can be replayed safely.</p>
            </section>
          ) : <p className="rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-xs leading-5 text-emerald-800">All document changes replay successfully in their default order. The board shows the resulting document.</p>}
        </>
      ) : <section>
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="text-[10px] font-bold uppercase tracking-widest text-gray-400">merge conflicts</p>
            <p className="mt-1 text-xs font-semibold text-gray-700">
              {unresolvedCount ? `${unresolvedCount} unresolved` : "All conflicts resolved"}
            </p>
          </div>
          <button
            type="button"
            onClick={() => (isAIProposing ? onCancelAI() : void onProposeWithAI())}
            disabled={!unresolvedCount && !isAIProposing}
            className="inline-flex items-center gap-2 rounded-lg bg-violet-600 px-3 py-2 text-xs font-semibold text-white disabled:opacity-40"
          >
            {isAIProposing ? <Square className="h-3.5 w-3.5 fill-current" /> : <Bot className="h-3.5 w-3.5" />}
            {isAIProposing ? "Cancel AI" : "AI merge"}
          </button>
        </div>
        {latestActivity ? (
          <p className="mt-3 rounded-lg bg-violet-50 px-3 py-2 text-[11px] text-violet-700">
            {latestActivity.label}
          </p>
        ) : null}
        <div className="mt-4 space-y-3">
          {session.conflicts.map((conflict) => (
            <ConflictCard
              key={`${conflict.id}:${conflict.resolution}`}
              conflict={conflict}
              onResolve={(resolution, customValue) =>
                onResolveConflict(conflict.id, resolution, customValue)
              }
            />
          ))}
          {!session.conflicts.length ? (
            <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-800">

              A deterministic merge has produced a conflict-free draft.
            </div>
          ) : null}
        </div>
      </section>}

      {session.draft_runtime.invalidated_teaching_state ? (
        <p className="rounded-xl border border-gray-200 bg-gray-50 p-4 text-xs leading-6 text-gray-600">

          The old teaching progress and board content positions have expired and have been retained in the audit; the next teaching will be rebuilt based on the merged board content.
        </p>
      ) : null}

      <div className="grid grid-cols-2 gap-2 border-t border-gray-200 pt-5">
        <button
          type="button"
          onClick={() => void runAction(onAbandon)}
          disabled={pendingAction}
          className="inline-flex items-center justify-center gap-2 rounded-lg border border-gray-300 bg-white px-3 py-2 text-xs font-semibold text-gray-700"
        >
          <Trash2 className="h-3.5 w-3.5" />

          abandon draft
        </button>
        <button
          type="button"
          onClick={() => void runAction(onSubmit)}
          disabled={Boolean(blockedReason) || isAIProposing || pendingAction}
          title={blockedReason ?? undefined}
          className="inline-flex items-center justify-center gap-2 rounded-lg bg-black px-3 py-2 text-xs font-semibold text-white disabled:cursor-not-allowed disabled:opacity-40"
        >
          <Save className="h-3.5 w-3.5" />

          Commit merge
        </button>
      </div>
      {blockedReason ? <p className="text-[11px] leading-5 text-amber-800">{blockedReason}</p> : null}
    </div>
  );
}
