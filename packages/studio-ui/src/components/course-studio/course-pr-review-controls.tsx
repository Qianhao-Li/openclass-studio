"use client";

import { useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import { CoursePrOrderEditor } from "./course-pr-order-editor";
import { CourseDocumentDiff } from "../course-document-diff";
import { useCoursePrReviewController, type CoursePrStudioBinding } from "../../hooks/course-studio/use-course-pr-review";
import type { CoursePrDecision } from "../../types/course-pr";

const button = "rounded-lg border border-stone-200 bg-white px-3 py-2 text-xs font-semibold disabled:opacity-40";
const decisionLabels: Record<CoursePrDecision, string> = { accept: "Accept", reject: "Reject", undecided: "Undecided" };

export function CoursePrReviewControls(props: CoursePrStudioBinding) {
  const controller = useCoursePrReviewController(props);
  const [expanded, setExpanded] = useState(true);
  const [showDiff, setShowDiff] = useState(false);
  if (!controller.context) return null;
  const { pr, review } = controller.context;
  const { comparison, decisions } = review;
  const disabled = controller.busy || Boolean(controller.disabledReason);
  const order = comparison.ordered_node_ids ?? comparison.diff_nodes.filter((node) => node.status !== "removed").map((node) => node.id);
  const groups = [...comparison.action_groups].sort((a, b) => Math.min(...a.node_ids.map((id) => order.indexOf(id)).filter((index) => index >= 0), Number.MAX_SAFE_INTEGER) - Math.min(...b.node_ids.map((id) => order.indexOf(id)).filter((index) => index >= 0), Number.MAX_SAFE_INTEGER));
  const blockers = comparison.blockers ?? [];
  const mergeReason = review.stale ? "The main branch or submitted PR version has changed. Compare again before merging." : controller.undecided ? `${controller.undecided} actions still need a decision.` : comparison.order_conflicts?.length && !comparison.order_confirmed ? "Confirm how the two branches should be ordered at this position." : blockers.length ? blockers.map((item) => item.reason).join("; ") : controller.disabledReason;
  const accepted = groups.filter((group) => decisions[group.id] === "accept").length;
  const rejected = groups.filter((group) => decisions[group.id] === "reject").length;

  return <section aria-label="Course PR review controls" className="shrink-0 border-b border-violet-200 bg-violet-50/70 px-4 py-3">
    <div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="text-sm font-semibold text-violet-950">Review PR: {pr.title}</h2><p className="mt-1 text-xs text-stone-600">Separate draft · Gold marks added actions; gray marks removed history nodes · Accepted {accepted} / Rejected {rejected} / Undecided {controller.undecided}</p></div><div className="flex flex-wrap gap-2"><button type="button" disabled={disabled} onClick={() => void controller.save()} className={button}>Save draft</button><button type="button" disabled={disabled || Boolean(mergeReason)} title={mergeReason ?? undefined} onClick={() => controller.setConfirming("merge")} className="rounded-lg bg-violet-700 px-3 py-2 text-xs font-semibold text-white disabled:opacity-40">{accepted === 0 && controller.undecided === 0 ? "Finish review (reject all)" : "Merge and publish"}</button><button type="button" disabled={disabled} onClick={() => controller.setConfirming("discard")} className={button}>Discard draft</button><button type="button" aria-label={expanded ? "Collapse PR review" : "Expand PR review"} onClick={() => setExpanded(!expanded)} className={button}>{expanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}</button></div></div>
    {controller.error ? <p role="alert" className="mt-2 text-sm text-rose-700">{controller.error}<button type="button" disabled={disabled} onClick={() => void controller.refresh()} className="ml-3 underline">Reload</button></p> : null}
    {controller.notice ? <p role="status" className="mt-2 text-xs text-emerald-800">{controller.notice}</p> : null}
    {mergeReason ? <p className="mt-2 text-xs text-stone-600">{mergeReason}</p> : null}
    {expanded ? <div className="mt-3 max-h-[35vh] overflow-auto rounded-xl border border-violet-100 bg-white p-3">
      <div className="mb-3 flex flex-wrap items-center gap-2"><button type="button" disabled={disabled || !groups.length} onClick={() => void controller.acceptAll()} className={button}>Accept all actions</button><button type="button" disabled={disabled || !order.length} onClick={() => void controller.order(order)} className={button}>Confirm current order</button><button type="button" onClick={() => setShowDiff(!showDiff)} className={button}>{showDiff ? "Hide document changes" : "View document changes"}</button><button type="button" disabled={disabled} onClick={() => void controller.rebase()} className={button}>Keep draft and compare main again</button><span className="text-xs text-stone-500">Complete actions move together; M→C stays intact</span></div>
      {comparison.order_conflicts?.length ? <p className="mb-3 rounded-lg bg-amber-50 p-3 text-xs text-amber-800">{comparison.order_confirmed ? "Learning order confirmed for both branches." : "Both branches add content at the same position. Confirm the order, or move complete actions up or down before merging."}</p> : null}
      <ol className="space-y-2">{groups.map((group) => <li key={group.id} data-pr-action-id={group.id} className={`flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2 ${group.kind === "remove" ? "border-stone-200 bg-stone-50" : "border-amber-200 bg-amber-50/50"}`}>
        <div className="min-w-0 flex-1"><p className="text-xs font-semibold">{group.title}</p><p className="mt-1 text-[11px] text-stone-500">{group.author === "reviewer" ? "Added by reviewer" : "Submitted by contributor"} · {group.kind === "remove" ? "Remove history action" : group.kind === "reorder" ? "Reorder" : "Add action"} · {group.node_ids.length} nodes{group.dependencies.length ? ` · Depends on ${group.dependencies.length} actions` : ""}</p></div>
        <div className="flex gap-1">{(["accept", "reject", "undecided"] as const).map((decision) => <button key={decision} type="button" disabled={disabled} aria-pressed={(decisions[group.id] ?? "undecided") === decision} onClick={() => void controller.decide(group.id, decision)} className={`rounded px-2 py-1 text-xs disabled:opacity-40 ${(decisions[group.id] ?? "undecided") === decision ? "bg-violet-700 text-white" : "border bg-white"}`}>{decisionLabels[decision]}</button>)}</div>
      </li>)}</ol>
      <CoursePrOrderEditor comparison={comparison} disabled={disabled} onChange={controller.order} />
      {showDiff ? <><CourseDocumentDiff before={comparison.document_diff.before_document ?? comparison.document_diff.before} after={comparison.document_diff.after_document ?? comparison.document_diff.after} beforeLabel="Common base document" afterLabel="Submitted and reviewed document" />{comparison.merged_document ? <CourseDocumentDiff before={comparison.document_diff.target_document ?? comparison.document_diff.target ?? comparison.document_diff.before} after={comparison.merged_document} beforeLabel="Current main-branch document" afterLabel="Proposed merge result" /> : <p className="mt-3 text-xs text-amber-800">Preview the merged document after action decisions and dependency conflicts are resolved.</p>}</> : null}
    </div> : null}
    {controller.confirming ? <div className="fixed inset-0 z-[120] flex items-center justify-center bg-black/30 p-4"><section role="dialog" aria-modal="true" aria-label={controller.confirming === "merge" ? "Complete course merge" : "Discard review draft"} className="w-full max-w-lg rounded-2xl bg-white p-6"><h3 className="font-semibold">{controller.confirming === "merge" ? accepted ? "Merge and save a new public version" : "Close PR without a new version" : "Discard this review draft"}</h3><p className="mt-3 text-sm leading-6">{controller.confirming === "merge" ? accepted ? `Accept ${accepted} complete actions and reject ${rejected} actions, saving a new version of the target course. Other lessons stay unchanged.` : `All ${rejected} actions are rejected. Finishing the review closes the PR without creating a new version.` : "Edits and action decisions in this draft will be discarded. The PR and original course are preserved."}</p>{controller.error ? <p role="alert" className="mt-3 text-sm text-rose-700">{controller.error}</p> : null}<div className="mt-5 flex justify-end gap-2"><button type="button" disabled={controller.busy} onClick={() => controller.setConfirming(null)} className={button}>Continue reviewing</button><button type="button" disabled={disabled} onClick={() => void controller.finish()} className="rounded-lg bg-violet-700 px-4 py-2 text-sm text-white disabled:opacity-40">{controller.busy ? "Saving…" : controller.confirming === "merge" ? accepted ? "Confirm merge and publish" : "Confirm closing PR" : "Confirm discard"}</button></div></section></div> : null}
  </section>;
}
