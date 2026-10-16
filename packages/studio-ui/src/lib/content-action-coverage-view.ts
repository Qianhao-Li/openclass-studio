import type { Lesson, RidocHistoryPage } from "../types";
import type { ContentActionView } from "../types/content-actions";

/** Geometry and permissions come from the complete server-side branch projection. */
export function contentActionCoverageView(
  lesson: Lesson, history: RidocHistoryPage | null | undefined, draftVersion?: number,
): ContentActionView {
  const branch = lesson.history_graph.current_branch;
  return {
    branch_name: branch,
    revision: history?.ridoc_revision ?? "",
    draft_version: draftVersion,
    // A limited node page is not a limited action graph. Never filter actions by it.
    actions: history?.current_branch === branch ? history.document_history?.actions ?? [] : [],
    document_length: Array.from(lesson.board_document.content_text ?? "").length,
  };
}
