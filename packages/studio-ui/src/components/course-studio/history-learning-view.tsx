"use client";

import { useMemo, useState } from "react";
import { buildLearningHistoryGraph, type HistoryGraphLayout } from "./history-graph-layout";
import type { BranchRef } from "../../types";
import { hasCustomHistoryOrder } from "../../lib/history-order-view";

export function useHistoryLearningView(layout: HistoryGraphLayout, branch: BranchRef | undefined, branchFocus: string) {
  const [showSourceGraph, setShowSourceGraph] = useState(false);
  const [forcedBranch, setForcedBranch] = useState<string | null>(null);
  const hasLearningOrder = Boolean(branch?.learning_node_ids?.length && (branch.learning_order_merged || forcedBranch === branch.name || hasCustomHistoryOrder(branch.learning_node_ids, [...layout.nodes].reverse().map((node) => node.id))));
  const showingLearningOrder = hasLearningOrder && !showSourceGraph &&
    (branchFocus === "all" || branchFocus === branch?.name);
  const displayedLayout = useMemo(
    () => showingLearningOrder ? buildLearningHistoryGraph(layout, branch!.learning_node_ids!) : layout,
    [branch, layout, showingLearningOrder]
  );
  return {
    displayedLayout, hasLearningOrder, showingLearningOrder,
    toggleLearningView: () => setShowSourceGraph(showingLearningOrder),
    showLearningView: () => { setShowSourceGraph(false); setForcedBranch(branch?.name ?? null); },
  };
}

export function HistoryLearningViewToggle({ showingLearningOrder, onClick }: {
  showingLearningOrder: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-label={showingLearningOrder ? "Show source graph" : "Show learning order"}
      aria-pressed={showingLearningOrder}
      onClick={onClick}
      className="h-8 rounded-lg border border-blue-200 bg-blue-50 px-2.5 text-[11px] font-medium text-blue-700"
    >
      {showingLearningOrder ? "Source graph" : "Learning order"}
    </button>
  );
}
