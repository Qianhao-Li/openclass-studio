"use client";

import { useEffect, useRef } from "react";
import type { RidocNodeDeletionPreview } from "../../types";

export function HistoryNodeDeletionDialog({ preview, busy, message, disabledReason, onCancel, onConfirm }: {
  preview: RidocNodeDeletionPreview | null;
  busy: boolean;
  message: string | null;
  disabledReason: string | null;
  onCancel: () => void;
  onConfirm: () => void | Promise<void>;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (preview && dialog && !dialog.open) dialog.showModal();
    if (!preview && dialog?.open) dialog.close();
  }, [preview]);
  const blocker = preview?.blocked_reason || disabledReason;
  return (
    <dialog ref={dialogRef} aria-labelledby="history-deletion-title"
      onCancel={(event) => { event.preventDefault(); if (!busy) onCancel(); }}
      className="fixed m-auto max-h-[85vh] w-[min(560px,calc(100vw-32px))] overflow-y-auto rounded-xl border border-gray-200 bg-white p-6 shadow-xl backdrop:bg-black/35">
      {preview ? <>
        <h2 id="history-deletion-title" className="text-base font-semibold">Delete this conversation turn</h2>
        <p className="mt-3 text-sm leading-6 text-gray-700">Delete this question and answer while keeping later conversations and board changes. All branches using this node will skip it. This cannot be undone.</p>
        <div className="mt-4 rounded-lg bg-gray-50 p-4 text-sm">
          <p className="font-medium text-gray-600">User message</p>
          <p className="mt-2 whitespace-pre-wrap break-words">{preview.user_message}</p>
          <details className="mt-4">
            <summary className="cursor-pointer font-medium text-gray-600">View the full AI response</summary>
            <p className="mt-2 whitespace-pre-wrap break-words">{preview.assistant_message}</p>
          </details>
        </div>
        <p className="mt-4 break-words text-sm text-gray-600">Affected branches: {preview.affected_branches.join(", ") || "None"}</p>
        {preview.retained_document_node_ids.length > 0 ?
          <p className="mt-2 text-sm text-gray-600">Keep board changes from this turn: {preview.retained_document_node_ids.length} changes; their source will show “Original conversation deleted”.</p> : null}
        {message ? <p role="alert" className="mt-4 text-sm text-amber-800">{message}</p> : null}
        {blocker ? <p className="mt-3 text-sm text-gray-600">{blocker}</p> : null}
        <div className="mt-6 flex justify-end gap-3">
          <button type="button" autoFocus disabled={busy} onClick={onCancel}
            className="rounded-lg border border-gray-300 px-4 py-2 text-sm disabled:opacity-40">Cancel</button>
          <button type="button" disabled={busy || Boolean(blocker)} onClick={() => void onConfirm()}
            className="rounded-lg bg-red-600 px-4 py-2 text-sm text-white disabled:opacity-40">
            {busy ? "Deleting…" : "Confirm deletion"}
          </button>
        </div>
      </> : null}
    </dialog>
  );
}
