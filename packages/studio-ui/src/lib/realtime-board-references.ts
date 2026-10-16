import type { RealtimeToolCallResponse, SelectionRef } from "../types";

export const MAX_REALTIME_BOARD_REFERENCES = 8;

function boardReferenceKey(selection: SelectionRef): string {
  const stableLocation = selection.segment_id || selection.text_hash;
  if (stableLocation) {
    return [selection.lesson_id, selection.document_id, stableLocation].join(":");
  }
  return [
    selection.lesson_id,
    selection.document_id,
    selection.heading_path?.join("/") ?? "",
    selection.before_text ?? "",
    selection.excerpt,
    selection.after_text ?? "",
  ].join(":");
}

export function addRealtimeBoardReference(
  current: SelectionRef[],
  selection: SelectionRef | null,
  lessonId: string
): SelectionRef[] {
  const lessonReferences = current.filter(
    (item) => item.kind === "board" && (!item.lesson_id || item.lesson_id === lessonId)
  );
  if (
    !selection ||
    selection.kind !== "board" ||
    (selection.lesson_id && selection.lesson_id !== lessonId) ||
    !selection.excerpt.trim()
  ) {
    return lessonReferences;
  }
  const nextKey = boardReferenceKey(selection);
  if (lessonReferences.some((item) => boardReferenceKey(item) === nextKey)) {
    return lessonReferences;
  }
  if (lessonReferences.length >= MAX_REALTIME_BOARD_REFERENCES) {
    return lessonReferences;
  }
  return [...lessonReferences, selection];
}

function referencePayload(result: RealtimeToolCallResponse, index: number) {
  const output = result.model_output;
  return {
    reference_index: index + 1,
    document_title: output.document_title,
    range_label: output.range_label,
    target: output.target,
    content: output.content,
    focus: output.focus,
  };
}

export function mergeRealtimeBoardReferenceResults(
  results: RealtimeToolCallResponse[]
): RealtimeToolCallResponse {
  if (results.length <= 1) {
    return results[0] ?? {
      status: "error",
      model_output: { status: "selection_missing", message: "There are no active board references." },
    };
  }
  const readable = results.filter(
    (result) => result.status === "ok" && result.model_output.status === "ok" && typeof result.model_output.content === "string"
  );
  const unresolved = results.flatMap((result, index) => {
    if (result.status === "ok" && result.model_output.status === "ok") {
      return [];
    }
    return [{
      reference_index: index + 1,
      status: result.model_output.status ?? result.status,
      message: result.model_output.message,
    }];
  });
  if (!readable.length) {
    return {
      status: "ok",
      model_output: {
        status: "not_found",
        reference_count: 0,
        unresolved_references: unresolved,
        message: "None of the accumulated board references could be resolved.",
      },
    };
  }
  return {
    status: "ok",
    model_output: {
      status: "ok",
      reference_count: readable.length,
      references: readable.map(referencePayload),
      unresolved_references: unresolved,
      instruction: "Use every resolved reference together. Keep their numbered identities distinct and do not replace an earlier reference with a later one.",
    },
    resolved_focus: readable[readable.length - 1].resolved_focus,
  };
}
