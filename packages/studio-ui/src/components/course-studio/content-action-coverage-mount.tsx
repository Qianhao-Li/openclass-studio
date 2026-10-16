"use client";

import { useMemo } from "react";
import type { DocumentActionNavigation } from "../../hooks/course-studio/use-document-action-navigation";
import type { Lesson, RidocHistoryPage } from "../../types";
import type { HistoryEditController } from "../../hooks/course-studio/use-history-edit-session";
import { contentActionCoverageView } from "../../lib/content-action-coverage-view";
import { ContentActionCoverage } from "./content-action-coverage";
import { historyNodeCommunityShareHref } from "./history-graph-utils";

/** Uses the same draft, operations, and leave boundary as the History list. */
export function ContentActionCoverageMount({ activeLesson, ridocHistory, readOnly, interactionLocked, historyOrder, navigation, canUseCommunity }: {
  activeLesson: Lesson; ridocHistory?: RidocHistoryPage | null; readOnly?: boolean; interactionLocked?: boolean;
  historyOrder?: HistoryEditController; navigation: DocumentActionNavigation;
  canUseCommunity: boolean;
}) {
  const lesson = historyOrder?.draftLesson ?? activeLesson;
  const history = historyOrder?.draftHistory ?? ridocHistory;
  const draftVersion = historyOrder?.active || historyOrder?.pending ? historyOrder.session?.version : undefined;
  const view = useMemo(() => {
    const view = contentActionCoverageView(lesson, history, draftVersion);
    return { ...view, actions: view.actions.map((action) => ({ ...action, display_spans: navigation.measuredSpans[action.id] ?? [] })) };
  }, [lesson, history, draftVersion, navigation.measuredSpans]);
  const previewCommitFor = (id: string) => {
    const commit = lesson.history_graph.commits.find((item) => item.id === id);
    if (!commit) return null;
    const snapshot = history?.replay_documents?.[id] ?? commit.snapshot;
    return { ...commit, snapshot };
  };
  const preview = async (id: string, fragmentId?: string) => {
    const commit = previewCommitFor(id);
    if (!commit) return false;
    await navigation.activate(commit, fragmentId, true);
    return true;
  };
  const step = async (direction: "undo" | "redo", ids: string[]) => {
    if (!historyOrder?.active || !ids.length) return;
    await historyOrder.stepActions(direction, ids);
  };
  return <div lang="en" translate="no">
    <ContentActionCoverage view={view} loading={!history || navigation.measuring} error={navigation.error || historyOrder?.message}
      disabledReason={historyOrder?.disabledReason ?? history?.document_history?.blocked_reason}
      editing={Boolean(historyOrder?.active)} readOnly={readOnly || !historyOrder?.enabled}
      busy={interactionLocked || Boolean(historyOrder && historyOrder.phase !== "idle")}
      selectedId={navigation.selectedId} onSelect={(id, fragmentId) => { void preview(id, fragmentId); }}
      onPreview={(id) => { void preview(id); }} onLocate={(id) => { void preview(id); }}
      canLocate={(id) => typeof previewCommitFor(id)?.snapshot.content_text === "string"}
      shareHref={(id) => {
        const savedCommit = activeLesson.history_graph.commits.find((commit) => commit.id === id);
        return savedCommit ? historyNodeCommunityShareHref(activeLesson, savedCommit) : null;
      }}
      shareDisabledReason={canUseCommunity ? null : "Sign in to share history actions."}
      onToggleEditing={() => { void (historyOrder?.active ? historyOrder.pause() : historyOrder?.begin()); }}
      onUndo={(ids) => { void step("undo", ids); }} onRedo={(id) => { void step("redo", [id]); }} />
  </div>;
}
