import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";

type ResizablePanelWidthOptions = {
  storageKey: string;
  defaultWidth: number;
  minWidth: number;
  maxWidth: number;
  dragDirection?: "grow-left" | "grow-right";
  keyboardStep?: number;
  label?: string;
};

function clampPanelWidth(width: number, minWidth: number, maxWidth: number) {
  return Math.min(maxWidth, Math.max(minWidth, Math.round(width)));
}

export function useResizablePanelWidth({
  storageKey,
  defaultWidth,
  minWidth,
  maxWidth,
  dragDirection = "grow-right",
  keyboardStep = 24,
  label = "Adjust panel width",
}: ResizablePanelWidthOptions) {
  const [width, setWidth] = useState(() => {
    const fallbackWidth = clampPanelWidth(defaultWidth, minWidth, maxWidth);
    if (typeof window === "undefined") {
      return fallbackWidth;
    }
    const storedWidth = window.localStorage.getItem(storageKey);
    const parsedWidth = storedWidth ? Number.parseInt(storedWidth, 10) : Number.NaN;
    return Number.isFinite(parsedWidth) ? clampPanelWidth(parsedWidth, minWidth, maxWidth) : fallbackWidth;
  });
  const [isResizing, setIsResizing] = useState(false);
  const widthRef = useRef(width);
  const dragStartRef = useRef<{ x: number; width: number } | null>(null);

  const clampWidth = useCallback(
    (nextWidth: number) => clampPanelWidth(nextWidth, minWidth, maxWidth),
    [maxWidth, minWidth]
  );

  const updateWidth = useCallback(
    (nextWidth: number | ((currentWidth: number) => number)) => {
      setWidth((currentWidth) => {
        const resolvedWidth =
          typeof nextWidth === "function" ? nextWidth(currentWidth) : nextWidth;
        const clampedWidth = clampWidth(resolvedWidth);
        widthRef.current = clampedWidth;
        return clampedWidth;
      });
    },
    [clampWidth]
  );

  useEffect(() => {
    window.localStorage.setItem(storageKey, String(width));
  }, [storageKey, width]);

  useEffect(() => {
    widthRef.current = width;
  }, [width]);

  useEffect(() => {
    if (!isResizing) {
      return;
    }

    function handlePointerMove(event: globalThis.PointerEvent) {
      const start = dragStartRef.current;
      if (!start) {
        return;
      }
      const dragDelta = dragDirection === "grow-left" ? start.x - event.clientX : event.clientX - start.x;
      updateWidth(start.width + dragDelta);
    }

    function handlePointerUp() {
      dragStartRef.current = null;
      setIsResizing(false);
    }

    const previousCursor = document.body.style.cursor;
    const previousUserSelect = document.body.style.userSelect;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp, { once: true });

    return () => {
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousUserSelect;
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
    };
  }, [dragDirection, isResizing, updateWidth]);

  function handlePointerDown(event: PointerEvent<HTMLElement>) {
    if (event.button !== 0) {
      return;
    }
    event.preventDefault();
    dragStartRef.current = { x: event.clientX, width: widthRef.current };
    setIsResizing(true);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      updateWidth((currentWidth) =>
        currentWidth + (dragDirection === "grow-left" ? keyboardStep : -keyboardStep)
      );
    }
    if (event.key === "ArrowRight") {
      event.preventDefault();
      updateWidth((currentWidth) =>
        currentWidth + (dragDirection === "grow-left" ? -keyboardStep : keyboardStep)
      );
    }
    if (event.key === "Home") {
      event.preventDefault();
      updateWidth(minWidth);
    }
    if (event.key === "End") {
      event.preventDefault();
      updateWidth(maxWidth);
    }
  }

  return {
    width,
    isResizing,
    dragHandleProps: {
      role: "separator",
      tabIndex: 0,
      "aria-orientation": "vertical" as const,
      "aria-valuemin": minWidth,
      "aria-valuemax": maxWidth,
      "aria-valuenow": width,
      "aria-label": label,
      title: label,
      onPointerDown: handlePointerDown,
      onKeyDown: handleKeyDown,
    },
  };
}
