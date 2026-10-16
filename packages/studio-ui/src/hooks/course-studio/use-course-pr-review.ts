"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useCoursePrReview } from "../../contexts/course-pr-review-context";
import { coursePrApi } from "../../lib/course-pr-api";
import { api } from "../../lib/api";
import { repositoryHref } from "../../lib/course-routes";
import type { CoursePackage } from "../../types";
import type { CoursePrDecision, CoursePrReview } from "../../types/course-pr";
import type { AutoSaveReason } from "./use-board-draft";

export type CoursePrStudioBinding = {
  coursePackage: CoursePackage;
  busyReason: string | null;
  historyPending: boolean;
  prepareHistory: () => Promise<boolean>;
  flush: (reason: AutoSaveReason) => Promise<boolean>;
  apply: (course: CoursePackage) => unknown;
};

export function useCoursePrReviewController(binding: CoursePrStudioBinding) {
  const context = useCoursePrReview();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirming, setConfirming] = useState<"merge" | "discard" | null>(null);
  const requestSequence = useRef(0);
  const mutationBusy = useRef(false);
  const { review, replaceReview } = context ?? {};
  const reviewId = review?.id;

  const refresh = useCallback(async () => {
    if (!reviewId || !replaceReview) return null;
    const sequence = ++requestSequence.current;
    const next = await coursePrApi.review(reviewId);
    if (sequence === requestSequence.current) replaceReview(next);
    return next;
  }, [reviewId, replaceReview]);

  useEffect(() => {
    if (!reviewId || mutationBusy.current) return;
    let active = true;
    void refresh().catch((failure) => { if (active) setError(failure instanceof Error ? failure.message : "Could not refresh the review draft."); });
    return () => { active = false; requestSequence.current += 1; };
  }, [reviewId, binding.coursePackage.ridoc_revision, refresh]);

  useEffect(() => {
    if (!reviewId) return;
    const focused = () => { if (!mutationBusy.current) void refresh().catch((failure) => setError(failure instanceof Error ? failure.message : "Could not refresh the review status.")); };
    window.addEventListener("focus", focused);
    return () => window.removeEventListener("focus", focused);
  }, [reviewId, refresh]);

  async function prepare() {
    if (binding.busyReason) throw new Error(binding.busyReason);
    if (binding.historyPending && !(await binding.prepareHistory())) throw new Error("Resolve pending History changes first.");
    if (!(await binding.flush("manual"))) throw new Error("The board has not been saved. Your review draft is preserved.");
    const current = await refresh();
    if (!current) throw new Error("The review session is unavailable.");
    return current;
  }

  async function run(action: (current: CoursePrReview) => Promise<CoursePrReview | string | void>, message: string) {
    if (mutationBusy.current) return;
    mutationBusy.current = true; setBusy(true); setError(""); setNotice("");
    context?.setOperationBusy(true);
    try {
      const current = await prepare();
      const next = await action(current);
      if (next && typeof next !== "string") { replaceReview?.(next); binding.apply(next.course_package); }
      setNotice(typeof next === "string" ? next : message);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "The operation did not complete. Your draft is preserved.");
      await refresh().catch(() => undefined);
    } finally { mutationBusy.current = false; setBusy(false); context?.setOperationBusy(false); }
  }

  function decide(id: string, decision: CoursePrDecision) {
    return run((current) => coursePrApi.decide(current, { ...current.decisions, [id]: decision }), "Action decisions saved to the review draft.");
  }
  function acceptAll() {
    return run((current) => coursePrApi.decide(current, Object.fromEntries(current.comparison.action_groups.map((group) => [group.id, "accept" as const]))), "All actions accepted. Check the merge preview.");
  }
  function order(orderedIds: string[]) {
    return run((current) => coursePrApi.decide(current, current.decisions, orderedIds), "Learning order confirmed and saved to the draft.");
  }
  function save() { return run((current) => coursePrApi.save(current), "Draft saved. The original project and published version are unchanged."); }
  function rebase() { return run(async (current) => { await coursePrApi.rebase(current); window.location.reload(); }, "Review changes preserved and compared with the main branch again."); }
  function finish() {
    const action = confirming;
    if (!action || !context) return;
    return run(async (current) => {
      if (action === "merge") {
        const undecided = current.comparison.action_groups.filter((group) => !current.decisions[group.id] || current.decisions[group.id] === "undecided");
        if (undecided.length) throw new Error(`${undecided.length} actions still need a decision. Review them first.`);
        const result = await coursePrApi.merge(current);
        const repositoryId = result.repository_id ?? context.pr.target_repository_id;
        const href = await api.getRepositoryProject(repositoryId).then((project) => repositoryHref(project),
          () => `/repositories/project/${encodeURIComponent(repositoryId)}`);
        window.location.assign(href);
        return result.status === "closed" ? "All changes rejected. The PR was closed without creating a new version." : "Merge complete. The new public version is saved.";
      } else {
        await coursePrApi.discard(current);
        window.location.assign(`/course-prs/${encodeURIComponent(context.pr.id)}`);
      }
    }, action === "merge" ? "Merged and saved a new version." : "Review draft discarded.");
  }
  const undecided = review?.comparison.action_groups.filter((group) => !review.decisions[group.id] || review.decisions[group.id] === "undecided").length ?? 0;
  return { context, busy, error, notice, confirming, setConfirming, decide, acceptAll, order, save, rebase, finish,
    refresh: () => run((current) => Promise.resolve(current), "Review status reloaded."),
    undecided, disabledReason: binding.busyReason };
}
