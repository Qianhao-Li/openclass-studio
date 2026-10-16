"use client";

import type { useHistoryStudioLeave } from "../../hooks/course-studio/use-history-studio-leave";
import type { CoursePackage } from "../../types";
import { historyReferenceLabel } from "../../lib/history-edit-labels";

const CHANGE_LABELS: Record<string, string> = {
  reorder_messages: "Reorder conversations", step_document: "Undo or redo board changes", delete_message: "Mark conversation for deletion", restore_message: "Restore conversation",
  step_document_actions: "Undo or redo document actions",
  create_branch: "Create branch", checkout_branch: "Switch branch", merge_branch: "Merge branches", delete_nodes: "Delete nodes", order: "Reorder", branch: "Branch changes",
  remove_document: "Board action pending deletion", restore_document: "Redo board action", remove_message: "Conversation pending deletion",
};
export function HistoryStudioLeaveDialog({ controller, course }: { controller: ReturnType<typeof useHistoryStudioLeave>; course: CoursePackage }) {
  const dialog = controller.dialog;
  if (!dialog) return null;
  const saving = dialog.intent === "save";
  const title = saving ? "Save History changes?" : "Keep these History changes?";
  const lessonTitles = Object.fromEntries(course.lessons.map((lesson) => [lesson.id, lesson.title]));
  const nodeTitles = Object.fromEntries(course.lessons.flatMap((lesson) => lesson.history_graph.commits.map((node) => [
    `${lesson.id}/${node.id}`, typeof node.metadata?.user_message === "string" ? node.metadata.user_message : node.label,
  ])));
  return <div className="fixed inset-0 z-[130] flex items-center justify-center bg-black/30 p-6">
    <section role="dialog" aria-modal="true" aria-label={title} className="max-h-[85vh] w-full max-w-2xl overflow-y-auto rounded-2xl bg-white p-6 shadow-2xl">
      <h3 className="text-base font-semibold">{title}</h3>
      {dialog.review ? <>
        <p className="mt-3 text-sm text-gray-600">Affects {dialog.review.affected_lesson_ids.length} lessons and {dialog.review.pending_removals.length} nodes pending deletion. Keeping the changes permanently deletes these nodes; other content stays unchanged.</p>
        {dialog.review.changes?.length ? <ul className="mt-3 space-y-1 text-sm">{dialog.review.changes.map((change, index) => <li key={index}>{lessonTitles[change.lesson_id] ?? change.lesson_id} · {CHANGE_LABELS[change.kind] ?? change.kind} {change.count} items</li>)}</ul> : null}
        {dialog.review.pending_removals.length ? <ul className="mt-3 space-y-1 text-xs">{dialog.review.pending_removals.map((item) => <li key={`${item.lesson_id}:${item.node_id}`}>{lessonTitles[item.lesson_id] ?? item.lesson_id} · {item.kind} · {nodeTitles[`${item.lesson_id}/${item.node_id}`] || item.node_id}</li>)}</ul> : null}
        {dialog.review.referrers.length ? <div className="mt-4 rounded-lg bg-amber-50 p-3 text-xs text-amber-900"><p className="font-semibold">Affected references and branches</p><ul className="mt-2 space-y-1">{dialog.review.referrers.map((ref, index) => <li key={index}>{lessonTitles[ref.lesson_id] ?? ref.lesson_id} · {ref.title ?? nodeTitles[`${ref.lesson_id}/${ref.node_id}`] ?? ref.node_id ?? ref.kind} · {ref.branch_name ?? ref.kind} · {historyReferenceLabel(ref.relation)}</li>)}</ul><p className="mt-2">References are preserved. Deleted targets show “Target deleted”.</p></div> : null}
        {saving ? <p className="mt-3 text-sm text-gray-600">All changes will be saved together. You will stay in this course.</p> : null}
      </> : <p className="mt-3 text-sm text-gray-500"><span>{dialog.busy ? "Processing these changes…" : "The review could not finish. Retry or cancel."}</span></p>}
      {dialog.message ? <p role="alert" className="mt-3 text-sm text-red-700">{dialog.message}</p> : null}
      <div className="mt-5 flex flex-wrap justify-end gap-3">
        {dialog.message ? <button disabled={dialog.busy} onClick={() => void controller.refresh()} className="text-sm">Review again</button> : null}
        <button disabled={dialog.busy && !(saving && dialog.preparing)} onClick={controller.cancel} className="rounded border px-3 py-2 text-sm">Cancel</button>
        {!saving ? <button disabled={dialog.busy} onClick={() => void controller.discard()} className="rounded border px-3 py-2 text-sm">No, discard changes</button> : null}
        <button disabled={dialog.busy || !dialog.review || Boolean(dialog.review.blocked_reason)} onClick={() => void controller.keep()} className="rounded bg-violet-600 px-3 py-2 text-sm text-white disabled:opacity-40">{saving ? "Save changes" : "Yes, keep changes"}</button>
      </div>
    </section>
  </div>;
}
