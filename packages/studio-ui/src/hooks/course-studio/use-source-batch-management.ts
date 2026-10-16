"use client";

import { useState } from "react";

import { useStudioApi } from "../../contexts/studio-api-context";

type UseSourceBatchManagementOptions = {
  packageId: string;
  sourceIds: string[];
  disabled: boolean;
  onRemoved: (sourceIds: string[]) => void;
  onError: (message: string) => void;
};

export function useSourceBatchManagement({
  packageId,
  sourceIds,
  disabled,
  onRemoved,
  onError,
}: UseSourceBatchManagementOptions) {
  const api = useStudioApi();
  const [isActive, setIsActive] = useState(false);
  const [isRemoving, setIsRemoving] = useState(false);
  const [storedSelectedSourceIds, setSelectedSourceIds] = useState<Set<string>>(new Set());
  const availableSourceIds = new Set(sourceIds);
  const selectedSourceIds = new Set(
    [...storedSelectedSourceIds].filter((sourceId) => availableSourceIds.has(sourceId))
  );

  const selectedCount = selectedSourceIds.size;
  const allSelected = sourceIds.length > 0 && selectedCount === sourceIds.length;

  function start() {
    if (!disabled && sourceIds.length > 0) {
      setIsActive(true);
    }
  }

  function cancel() {
    if (isRemoving) {
      return;
    }
    setIsActive(false);
    setSelectedSourceIds(new Set());
  }

  function clear() {
    if (!isRemoving) {
      setSelectedSourceIds(new Set());
    }
  }

  function toggle(sourceId: string) {
    if (disabled || isRemoving) {
      return;
    }
    setSelectedSourceIds((current) => {
      const next = new Set(current);
      if (next.has(sourceId)) {
        next.delete(sourceId);
      } else {
        next.add(sourceId);
      }
      return next;
    });
  }

  function toggleAll() {
    if (disabled || isRemoving) {
      return;
    }
    setSelectedSourceIds(allSelected ? new Set() : new Set(sourceIds));
  }

  async function removeSelected() {
    const selectedIds = sourceIds.filter((sourceId) => selectedSourceIds.has(sourceId));
    if (disabled || isRemoving || selectedIds.length === 0) {
      return;
    }
    if (!window.confirm(`Delete the ${selectedIds.length} selected sources? They will no longer be searchable or referenceable in this course.`)) {
      return;
    }

    setIsRemoving(true);
    try {
      const result = await api.bulkDeletePackageSources(packageId, selectedIds);
      const removedIds = result.removed.map((source) => source.id);
      const failedIds = result.failed.map((item) => item.id);
      const failureMessages = result.failed
        .map((item) => item.detail.trim())
        .filter((message, index, all) => Boolean(message) && all.indexOf(message) === index);
      if (removedIds.length > 0) {
        onRemoved(removedIds);
      }
      setSelectedSourceIds(new Set(failedIds));
      if (failedIds.length === 0) {
        setIsActive(false);
      } else {
        const detail = failureMessages[0] ? ` (${failureMessages[0]})` : "";
        onError(
          `${removedIds.length} sources removed; ${failedIds.length} could not be removed. Please retry.${detail}`
        );
      }
    } catch (error) {
      onError(error instanceof Error ? error.message : "Data removal failed");
    } finally {
      setIsRemoving(false);
    }
  }

  return {
    isActive,
    isRemoving,
    selectedSourceIds,
    selectedCount,
    allSelected,
    start,
    cancel,
    clear,
    toggle,
    toggleAll,
    removeSelected,
  };
}
