"use client";

import { WordBoardEditor, type FormulaInkEditorSubmitPayload } from "./word-board-editor";
import type { SelectionPopoverPosition } from "./selection-utils";
import type { PendingHistoricalFork } from "./history-utils";
import type {
  BoardDocument,
  BoardFocusRef,
  CommitRecord,
  Lesson,
  SelectionRef,
} from "../../types";

type BoardEditorPanelProps = {
  historyPending?: boolean;
  readOnly?: boolean;
  activeLesson: Lesson;
  document: BoardDocument;
  pendingHistoricalFork?: PendingHistoricalFork | null;
  isPreviewMode: boolean;
  isDraftPreviewMode: boolean;
  isMergeMode: boolean;
  previewCommit: CommitRecord | null;
  toolbarCollapsed: boolean;
  transientTeachingFocus?: BoardFocusRef | null;
  onExitPreviewMode: () => void;
  onReturnToCurrentVersion: () => void;
  historicalForkReturnDisabled?: boolean;
  onDocumentChange: (document: BoardDocument) => void;
  onStructureRemovalIntent: () => void;
  onApplySelection: (selection: SelectionRef, position?: SelectionPopoverPosition | null) => void;
  onClearTransientSelection: () => void;
  onImportDocx: (file: File) => void;
  onExportDocx: () => void;
  onExportHtml: () => void;
  onImportRidoc: (file: File) => void;
  onExportRidoc: () => void;
  onReferenceFormula: (selection: SelectionRef) => void;
  onReferenceFormulaToGeometry: (selection: SelectionRef) => void;
  onFormulaInkSubmit: (payload: FormulaInkEditorSubmitPayload) => boolean;
};

function stringValue(value: unknown) {
  return typeof value === "string" ? value : "";
}

function stringArrayValue(value: unknown) {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function nullableNumberValue(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function boardFocusFromMetadata(value: unknown): BoardFocusRef | null {
  if (!value || typeof value !== "object") {
    return null;
  }
  const raw = value as Record<string, unknown>;
  if (raw.source !== "board") {
    return null;
  }
  const headingPath = stringArrayValue(raw.heading_path);
  const excerpt = stringValue(raw.excerpt).trim();
  const displayLabel = stringValue(raw.display_label).trim();
  if (!excerpt && !displayLabel && !headingPath.length) {
    return null;
  }
  return {
    source: "board",
    lesson_id: stringValue(raw.lesson_id) || null,
    document_id: stringValue(raw.document_id) || null,
    segment_id: stringValue(raw.segment_id) || null,
    kind: typeof raw.kind === "string" ? (raw.kind as BoardFocusRef["kind"]) : null,
    heading_path: headingPath,
    excerpt,
    before_text: stringValue(raw.before_text),
    after_text: stringValue(raw.after_text),
    text_hash: stringValue(raw.text_hash) || null,
    excerpt_hash: stringValue(raw.excerpt_hash) || null,
    confidence: typeof raw.confidence === "number" ? raw.confidence : 0,
    reason: stringValue(raw.reason),
    display_label: displayLabel,
    order_start: nullableNumberValue(raw.order_start),
    order_end: nullableNumberValue(raw.order_end),
  };
}

function currentHeadCommit(lesson: Lesson) {
  const branch = lesson.history_graph.branches[lesson.history_graph.current_branch];
  const commitId = branch?.head_commit_id ?? lesson.history_graph.commits[lesson.history_graph.commits.length - 1]?.id;
  return lesson.history_graph.commits.find((commit) => commit.id === commitId) ?? null;
}

function commitTeachingFocus(commit: CommitRecord | null) {
  if (!commit?.metadata || typeof commit.metadata !== "object") {
    return null;
  }
  const metadata = commit.metadata;
  const assistantMessage = stringValue(metadata.assistant_message).trim();
  const hasDirective = Boolean(metadata.board_explanation_directive && typeof metadata.board_explanation_directive === "object");
  const isTeachingCommit = assistantMessage && hasDirective;
  return isTeachingCommit ? boardFocusFromMetadata(metadata.resolved_focus) : null;
}

function currentTeachingFocus(lesson: Lesson, previewCommit: CommitRecord | null) {
  return commitTeachingFocus(previewCommit ?? currentHeadCommit(lesson));
}

export function BoardEditorPanel({
  historyPending = false,
  readOnly = false,
  activeLesson,
  document,
  pendingHistoricalFork = null,
  isPreviewMode,
  isDraftPreviewMode,
  isMergeMode,
  previewCommit,
  toolbarCollapsed,
  transientTeachingFocus,
  onExitPreviewMode,
  onReturnToCurrentVersion,
  historicalForkReturnDisabled = false,
  onDocumentChange,
  onStructureRemovalIntent,
  onApplySelection,
  onClearTransientSelection,
  onImportDocx,
  onExportDocx,
  onExportHtml,
  onImportRidoc,
  onExportRidoc,
  onReferenceFormula,
  onReferenceFormulaToGeometry,
  onFormulaInkSubmit,
}: BoardEditorPanelProps) {
  const teachingFocus = isMergeMode
    ? null
    : transientTeachingFocus ?? currentTeachingFocus(activeLesson, previewCommit);
  const sourceCommitId = pendingHistoricalFork?.continuationNodeId ?? currentHeadCommit(activeLesson)?.id ?? null;

  return (
    <section className="relative z-10 flex min-w-0 flex-col overflow-hidden bg-white shadow-[0_0_20px_rgba(0,0,0,0.02)]">
      {historyPending ? <p data-testid="history-pending-banner" className="shrink-0 border-b border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-900">History has pending changes. New conversations and board edits are paused. Keep or discard these changes when leaving Studio.</p> : null}
      {pendingHistoricalFork ? (
        <div
          data-testid="historical-fork-banner"
          className="shrink-0 border-b border-violet-200 bg-violet-50 px-5 py-3 text-sm text-violet-700"
        >
          Preparing a new branch from: {pendingHistoricalFork.sourceLabel}
          <button
            type="button"
            className="ml-3 rounded-md border border-violet-200 bg-white px-3 py-1 text-[11px] font-bold uppercase tracking-wider text-violet-700 hover:bg-violet-100 disabled:cursor-wait disabled:opacity-45"
            onClick={onReturnToCurrentVersion}
            disabled={historicalForkReturnDisabled}
          >
            Return to current version
          </button>
        </div>
      ) : null}
      {isPreviewMode ? (
        <div className="shrink-0 border-b border-violet-200 bg-violet-50 px-5 py-3 text-sm text-violet-700">

          Previewing historical snapshots:{previewCommit?.label}
          <button
            type="button"
            className="ml-3 rounded-md border border-violet-200 bg-white px-3 py-1 text-[11px] font-bold uppercase tracking-wider text-violet-700"
            onClick={onExitPreviewMode}
          >

            Return to current version
          </button>
        </div>
      ) : null}
      {!isPreviewMode && isDraftPreviewMode ? (
        <div className="shrink-0 border-b border-amber-200 bg-amber-50 px-5 py-3 text-sm text-amber-800">

          Showing live board progress. Tool snapshots are saved immediately; draft-only output is finalized when generation completes.
          <button
            type="button"
            className="ml-3 rounded-md border border-amber-200 bg-white px-3 py-1 text-[11px] font-bold uppercase tracking-wider text-amber-800"
            onClick={onExitPreviewMode}
          >

            Return to current version
          </button>
        </div>
      ) : null}
      {isMergeMode ? (
        <div className="shrink-0 border-b border-blue-200 bg-blue-50 px-5 py-3 text-sm text-blue-800">

          {readOnly
            ? "Previewing the merged document. Document changes are replayed in their default order."
            : "Editing merge draft. Changes are saved to the merge session until you commit the merge."}
        </div>
      ) : null}

      <WordBoardEditor
        key={pendingHistoricalFork ? `historical-fork:${pendingHistoricalFork.startedAt}` : "current-board-editor"}
        document={document}
        readOnly={readOnly || isPreviewMode || isDraftPreviewMode}
        teachingFocus={teachingFocus}
        toolbarCollapsed={toolbarCollapsed}
        onDocumentChange={onDocumentChange}
        onStructureRemovalIntent={onStructureRemovalIntent}
        onSelectionChange={(payload) => {
          if (!payload) {
            onClearTransientSelection();
            return;
          }
          onApplySelection(
            {
              kind: "board",
              location_kind: payload.locationKind,
              lesson_id: activeLesson.id,
              source_commit_id: sourceCommitId,
              document_id: payload.documentId,
              excerpt: payload.excerpt,
              before_text: payload.beforeText,
              after_text: payload.afterText,
            },
            payload.position
          );
        }}
        onImportDocx={onImportDocx}
        onExportDocx={onExportDocx}
        onExportHtml={onExportHtml}
        onImportRidoc={onImportRidoc}
        onExportRidoc={onExportRidoc}
        onFormulaReference={(selection) =>
          onReferenceFormula({
            kind: "board",
            location_kind: selection.locationKind,
            lesson_id: activeLesson.id,
            source_commit_id: sourceCommitId,
            document_id: selection.documentId,
            excerpt: selection.excerpt,
            before_text: selection.beforeText,
            after_text: selection.afterText,
          })
        }
        onFormulaGeometryReference={(selection) =>
          onReferenceFormulaToGeometry({
            kind: "board",
            location_kind: selection.locationKind,
            lesson_id: activeLesson.id,
            source_commit_id: sourceCommitId,
            document_id: selection.documentId,
            excerpt: selection.excerpt,
            before_text: selection.beforeText,
            after_text: selection.afterText,
          })
        }
        onFormulaInkSubmit={(payload) => {
          const accepted = onFormulaInkSubmit(payload);
          if (!accepted) {
            return false;
          }
          onApplySelection(
            {
              kind: "board",
              location_kind: payload.selection.locationKind,
              lesson_id: activeLesson.id,
              source_commit_id: sourceCommitId,
              document_id: payload.selection.documentId,
              excerpt: payload.selection.excerpt,
              before_text: payload.selection.beforeText,
              after_text: payload.selection.afterText,
            },
            null
          );
          return true;
        }}
      />
    </section>
  );
}
