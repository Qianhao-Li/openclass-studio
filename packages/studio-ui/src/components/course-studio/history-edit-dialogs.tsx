"use client";

import { LessonMergeOrderEditor } from "./lesson-merge-order-editor";
import type { HistoryEditController } from "../../hooks/course-studio/use-history-edit-session";
import { historyReferenceLabel } from "../../lib/history-edit-labels";

export function HistoryEditDialogs({ controller }: { controller: HistoryEditController }) {
  const impact = controller.impactDialog;
  const targets = impact?.targets?.length ? impact.targets : impact ? [{ lesson_id: impact.lesson_id, node_id: impact.node_id, kind: impact.kind }] : [];
  const merge = controller.mergeDraft;
  if (!impact && !merge) return null;
  const busy = controller.phase !== "idle";
  return <div className="fixed inset-0 z-[110] flex items-center justify-center bg-black/30 p-6">
    <section role="dialog" aria-modal="true" aria-label={merge ? "Merge branches in the edit draft" : "Review affected nodes"} className="max-h-[85vh] w-full max-w-4xl overflow-y-auto rounded-2xl bg-white p-5 shadow-2xl">
      {impact ? <>
        <h3 className="font-semibold">Review affected nodes</h3>
        <p className="mt-2 text-sm text-gray-600">{targets.length} nodes selected. Confirm to mark them for deletion while keeping their positions. Review and save the changes with Save or when leaving Studio.</p>
        <ul className="mt-4 space-y-2 text-sm">{targets.map((target) => {
          const node = controller.draftHistory?.nodes.find((item) => item.id === target.node_id);
          return <li key={`${target.lesson_id}:${target.node_id}`}>
            <p className="whitespace-pre-wrap">{node?.user_message || node?.title || target.node_id}</p>
            {node?.assistant_message ? <p className="mt-1 whitespace-pre-wrap text-gray-600">{node.assistant_message}</p> : null}
          </li>;
        })}</ul>
        {impact.referrers.length ? <div className="mt-4 rounded-lg bg-amber-50 p-3 text-xs"><p className="font-semibold">Affected references and branches</p><ul className="mt-2 space-y-1">{impact.referrers.map((ref, index) => <li key={index}>{ref.title ?? ref.node_id ?? ref.lesson_id} · {ref.branch_name ?? ref.kind} · {historyReferenceLabel(ref.relation)}</li>)}</ul></div> : null}
        <div className="mt-5 flex justify-end gap-3">
          <button disabled={busy} onClick={controller.cancelImpact}>Cancel</button>
          <button disabled={busy || controller.stale} onClick={() => void controller.confirmImpact()} className="rounded bg-red-600 px-3 py-2 text-white">Mark for deletion</button>
        </div>
      </> : merge ? <>
        <h3 className="mb-4 font-semibold">Merge {merge.source} → {merge.target}</h3>
        <LessonMergeOrderEditor plan={merge.plan} disabled={busy || controller.stale} onOrderChange={controller.changeMergeOrder} />
        {merge.plan.blockers.map((blocker, index) => <p key={index} className="text-sm text-amber-800">{blocker.reason}</p>)}
        <div className="mt-5 flex justify-end gap-3">
          <button disabled={busy} onClick={controller.cancelMerge}>Cancel merge</button>
          <button disabled={busy || controller.stale || Boolean(merge.plan.blockers.length)} onClick={() => void controller.confirmMerge()} className="rounded bg-violet-600 px-3 py-2 text-white">Apply to edit draft</button>
        </div>
      </> : null}
      {controller.message ? <p role="alert" className="mt-3 text-sm text-red-700">{controller.message}</p> : null}
    </section>
  </div>;
}
