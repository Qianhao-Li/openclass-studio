"use client";

import { useCallback, useState } from "react";
import {
  EMPTY_MERGE_SELECTION,
  selectMergeMessage,
  type MergeNodeSelection,
} from "../../lib/merge-event-order";

export function useMergeNodeSelection() {
  const [selection, setSelection] = useState<MergeNodeSelection>(EMPTY_MERGE_SELECTION);
  const select = useCallback((
    nodeId: string,
    scope: string,
    scopeIds: readonly string[],
    options: { range?: boolean; toggle?: boolean } = {},
  ) => setSelection((current) => selectMergeMessage(current, nodeId, scope, scopeIds, options)), []);
  const clear = useCallback(() => setSelection(EMPTY_MERGE_SELECTION), []);
  return { selectedIds: selection.ids, select, clear };
}
