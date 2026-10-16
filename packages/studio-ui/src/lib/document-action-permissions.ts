import type { DocumentHistoryState } from "../types/document-history";

export function canStepDocumentAction(
  state: DocumentHistoryState | undefined, nodeId: string, direction: "undo" | "redo",
): boolean {
  if (!state || state.blocked_reason) return false;
  if (state.actions) {
    const action = state.actions.find((item) => item.id === nodeId);
    return Boolean(action && !action.initial && (direction === "undo" ? action.can_undo : action.can_redo));
  }
  return state[direction === "undo" ? "undo_node_id" : "redo_node_id"] === nodeId;
}

export function documentActionBlockedReason(state: DocumentHistoryState | undefined, nodeId: string) {
  return state?.blocked_reason ?? state?.actions?.find((item) => item.id === nodeId)?.blocked_reason ?? null;
}
