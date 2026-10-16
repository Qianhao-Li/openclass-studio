import { useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";

type InkPoint = {
  x: number;
  y: number;
};

const DEFAULT_CANVAS_WIDTH = 520;
const DEFAULT_CANVAS_HEIGHT = 220;

function pointFromPointerEvent(canvas: HTMLCanvasElement, event: ReactPointerEvent<HTMLCanvasElement>): InkPoint {
  const rect = canvas.getBoundingClientRect();
  return {
    x: ((event.clientX - rect.left) / Math.max(rect.width, 1)) * DEFAULT_CANVAS_WIDTH,
    y: ((event.clientY - rect.top) / Math.max(rect.height, 1)) * DEFAULT_CANVAS_HEIGHT,
  };
}

function prepareCanvas(canvas: HTMLCanvasElement) {
  const ratio = typeof window === "undefined" ? 1 : window.devicePixelRatio || 1;
  const width = Math.round(DEFAULT_CANVAS_WIDTH * ratio);
  const height = Math.round(DEFAULT_CANVAS_HEIGHT * ratio);
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  canvas.style.width = "100%";
  canvas.style.height = `${DEFAULT_CANVAS_HEIGHT}px`;
  const context = canvas.getContext("2d");
  if (!context) {
    return null;
  }
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.lineCap = "round";
  context.lineJoin = "round";
  context.lineWidth = 4;
  context.strokeStyle = "#111827";
  context.fillStyle = "#ffffff";
  return context;
}

function drawPoint(context: CanvasRenderingContext2D, point: InkPoint) {
  context.beginPath();
  context.arc(point.x, point.y, 1.8, 0, Math.PI * 2);
  context.fillStyle = "#111827";
  context.fill();
}

function drawLine(context: CanvasRenderingContext2D, from: InkPoint, to: InkPoint) {
  context.beginPath();
  context.moveTo(from.x, from.y);
  context.lineTo(to.x, to.y);
  context.stroke();
}

export function useFormulaInkCanvas() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const drawingRef = useRef(false);
  const lastPointRef = useRef<InkPoint | null>(null);
  const [hasInk, setHasInk] = useState(false);

  const clearCanvas = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) {
      setHasInk(false);
      return;
    }
    const context = prepareCanvas(canvas);
    if (!context) {
      setHasInk(false);
      return;
    }
    context.fillStyle = "#ffffff";
    context.fillRect(0, 0, DEFAULT_CANVAS_WIDTH, DEFAULT_CANVAS_HEIGHT);
    drawingRef.current = false;
    lastPointRef.current = null;
    setHasInk(false);
  }, []);

  useEffect(() => {
    clearCanvas();
  }, [clearCanvas]);

  const handlePointerDown = useCallback((event: ReactPointerEvent<HTMLCanvasElement>) => {
    if (event.button !== 0 && event.pointerType === "mouse") {
      return;
    }
    const canvas = canvasRef.current;
    if (!canvas) {
      return;
    }
    const context = prepareCanvas(canvas);
    if (!context) {
      return;
    }
    const point = pointFromPointerEvent(canvas, event);
    canvas.setPointerCapture(event.pointerId);
    drawingRef.current = true;
    lastPointRef.current = point;
    drawPoint(context, point);
    setHasInk(true);
  }, []);

  const handlePointerMove = useCallback((event: ReactPointerEvent<HTMLCanvasElement>) => {
    if (!drawingRef.current) {
      return;
    }
    const canvas = canvasRef.current;
    const previous = lastPointRef.current;
    if (!canvas || !previous) {
      return;
    }
    const context = prepareCanvas(canvas);
    if (!context) {
      return;
    }
    const point = pointFromPointerEvent(canvas, event);
    drawLine(context, previous, point);
    lastPointRef.current = point;
    setHasInk(true);
  }, []);

  const stopDrawing = useCallback((event: ReactPointerEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (canvas?.hasPointerCapture(event.pointerId)) {
      canvas.releasePointerCapture(event.pointerId);
    }
    drawingRef.current = false;
    lastPointRef.current = null;
  }, []);

  const exportImage = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas || !hasInk) {
      return null;
    }
    return canvas.toDataURL("image/png");
  }, [hasInk]);

  return {
    canvasRef,
    hasInk,
    clearCanvas,
    exportImage,
    canvasHandlers: {
      onPointerDown: handlePointerDown,
      onPointerMove: handlePointerMove,
      onPointerUp: stopDrawing,
      onPointerCancel: stopDrawing,
      onPointerLeave: stopDrawing,
    },
  };
}
