"use client";

import { RotateCcw, ZoomIn, ZoomOut } from "lucide-react";
import { useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type WheelEvent } from "react";

import type { GeometryPoint, GeometryPrimitive, GeometryScene } from "../../types/geometry";

const SVG_WIDTH = 640;
const SVG_HEIGHT = 420;
const SVG_PADDING = 44;

type ProjectedPoint = GeometryPoint & {
  screenX: number;
  screenY: number;
  depth: number;
};

function finiteRange(minimum: number, maximum: number) {
  const span = maximum - minimum;
  return span > 0.0001 ? span : 1;
}

function rotatedPoint(point: GeometryPoint, yaw: number, pitch: number) {
  const yawRadians = (yaw * Math.PI) / 180;
  const pitchRadians = (pitch * Math.PI) / 180;
  const yawX = Math.cos(yawRadians) * point.x + Math.sin(yawRadians) * point.z;
  const yawZ = -Math.sin(yawRadians) * point.x + Math.cos(yawRadians) * point.z;
  return {
    x: yawX,
    y: Math.cos(pitchRadians) * point.y - Math.sin(pitchRadians) * yawZ,
    depth: Math.sin(pitchRadians) * point.y + Math.cos(pitchRadians) * yawZ,
  };
}

function projectScene(scene: GeometryScene, yaw: number, pitch: number, zoom: number) {
  const rotated = scene.points.map((point) =>
    scene.dimension === "3d" ? rotatedPoint(point, yaw, pitch) : { x: point.x, y: point.y, depth: point.z }
  );
  const naturalBounds = {
    minX: Math.min(...rotated.map((point) => point.x)),
    maxX: Math.max(...rotated.map((point) => point.x)),
    minY: Math.min(...rotated.map((point) => point.y)),
    maxY: Math.max(...rotated.map((point) => point.y)),
  };
  const bounds =
    scene.dimension === "2d"
      ? {
          minX: scene.viewport.x_min,
          maxX: scene.viewport.x_max,
          minY: scene.viewport.y_min,
          maxY: scene.viewport.y_max,
        }
      : naturalBounds;
  const centerX = (bounds.minX + bounds.maxX) / 2;
  const centerY = (bounds.minY + bounds.maxY) / 2;
  const scaleX = ((SVG_WIDTH - SVG_PADDING * 2) / finiteRange(bounds.minX, bounds.maxX)) * zoom;
  const scaleY = ((SVG_HEIGHT - SVG_PADDING * 2) / finiteRange(bounds.minY, bounds.maxY)) * zoom;
  const scale = Math.min(scaleX, scaleY);
  const points = new Map<string, ProjectedPoint>();
  scene.points.forEach((point, index) => {
    const projected = rotated[index];
    points.set(point.id, {
      ...point,
      screenX: SVG_WIDTH / 2 + (projected.x - centerX) * scale,
      screenY: SVG_HEIGHT / 2 - (projected.y - centerY) * scale,
      depth: projected.depth,
    });
  });
  return { points, scale, bounds };
}

function primitivePoints(primitive: GeometryPrimitive, points: Map<string, ProjectedPoint>) {
  return primitive.point_ids
    .map((pointId) => points.get(pointId))
    .filter((point): point is ProjectedPoint => Boolean(point));
}

function polylinePath(points: ProjectedPoint[], close = false) {
  if (!points.length) {
    return "";
  }
  const commands = points.map((point, index) => `${index ? "L" : "M"} ${point.screenX} ${point.screenY}`);
  return `${commands.join(" ")}${close ? " Z" : ""}`;
}

function renderPrimitive(
  primitive: GeometryPrimitive,
  points: Map<string, ProjectedPoint>,
  scale: number,
  arrowMarkerId: string
) {
  const anchors = primitivePoints(primitive, points);
  const common = {
    stroke: primitive.color,
    strokeWidth: primitive.stroke_width,
    strokeDasharray: primitive.dashed ? "8 6" : undefined,
    opacity: primitive.opacity,
    fill: primitive.fill,
  };
  if (primitive.kind === "segment" && anchors.length >= 2) {
    return <line key={primitive.id} {...common} x1={anchors[0].screenX} y1={anchors[0].screenY} x2={anchors[1].screenX} y2={anchors[1].screenY} />;
  }
  if (primitive.kind === "vector" && anchors.length >= 2) {
    return (
      <line
        key={primitive.id}
        {...common}
        x1={anchors[0].screenX}
        y1={anchors[0].screenY}
        x2={anchors[1].screenX}
        y2={anchors[1].screenY}
        markerEnd={`url(#${arrowMarkerId})`}
      />
    );
  }
  if (primitive.kind === "line" && anchors.length >= 2) {
    const deltaX = anchors[1].screenX - anchors[0].screenX;
    const deltaY = anchors[1].screenY - anchors[0].screenY;
    const length = Math.hypot(deltaX, deltaY) || 1;
    const extension = 900;
    return (
      <line
        key={primitive.id}
        {...common}
        x1={anchors[0].screenX - (deltaX / length) * extension}
        y1={anchors[0].screenY - (deltaY / length) * extension}
        x2={anchors[1].screenX + (deltaX / length) * extension}
        y2={anchors[1].screenY + (deltaY / length) * extension}
      />
    );
  }
  if (["polyline", "polygon", "plane"].includes(primitive.kind) && anchors.length >= 3) {
    const closed = primitive.kind !== "polyline";
    return <path key={primitive.id} {...common} d={polylinePath(anchors, closed)} />;
  }
  if (["circle", "sphere"].includes(primitive.kind) && primitive.radius) {
    const center = points.get(primitive.center_id);
    if (center) {
      return (
        <circle
          key={primitive.id}
          {...common}
          cx={center.screenX}
          cy={center.screenY}
          r={primitive.radius * scale}
        />
      );
    }
  }
  if (primitive.kind === "ellipse" && primitive.radius && primitive.radius_y) {
    const center = points.get(primitive.center_id);
    if (center) {
      return (
        <ellipse
          key={primitive.id}
          {...common}
          cx={center.screenX}
          cy={center.screenY}
          rx={primitive.radius * scale}
          ry={primitive.radius_y * scale}
        />
      );
    }
  }
  if (primitive.kind === "point" && anchors[0]) {
    return <circle key={primitive.id} cx={anchors[0].screenX} cy={anchors[0].screenY} r="5" fill={primitive.color} opacity={primitive.opacity} />;
  }
  if (primitive.kind === "label" && anchors[0]) {
    return (
      <text key={primitive.id} x={anchors[0].screenX + 8} y={anchors[0].screenY - 8} className="fill-slate-700 text-[14px] font-semibold">
        {primitive.text || primitive.label}
      </text>
    );
  }
  return null;
}

export function GeometrySceneViewer({ scene }: { scene: GeometryScene }) {
  const [yaw, setYaw] = useState(32);
  const [pitch, setPitch] = useState(-20);
  const [zoom, setZoom] = useState(1);
  const dragStartRef = useRef<{ x: number; y: number; yaw: number; pitch: number } | null>(null);
  const projection = useMemo(() => projectScene(scene, yaw, pitch, zoom), [pitch, scene, yaw, zoom]);
  const arrowMarkerId = `geometry-arrow-${scene.title.replace(/[^a-z0-9]/gi, "").slice(0, 12) || "scene"}`;

  function handlePointerDown(event: ReactPointerEvent<SVGSVGElement>) {
    if (scene.dimension !== "3d") {
      return;
    }
    event.currentTarget.setPointerCapture(event.pointerId);
    dragStartRef.current = { x: event.clientX, y: event.clientY, yaw, pitch };
  }

  function handlePointerMove(event: ReactPointerEvent<SVGSVGElement>) {
    const start = dragStartRef.current;
    if (!start || scene.dimension !== "3d") {
      return;
    }
    setYaw(start.yaw + (event.clientX - start.x) * 0.45);
    setPitch(Math.max(-85, Math.min(85, start.pitch - (event.clientY - start.y) * 0.35)));
  }

  function handlePointerUp(event: ReactPointerEvent<SVGSVGElement>) {
    dragStartRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }

  function handleWheel(event: WheelEvent<SVGSVGElement>) {
    event.preventDefault();
    setZoom((current) => Math.max(0.55, Math.min(2.5, current * (event.deltaY > 0 ? 0.92 : 1.08))));
  }

  const xAxisY = projection.points.size
    ? SVG_HEIGHT / 2 - ((0 - (projection.bounds.minY + projection.bounds.maxY) / 2) * projection.scale)
    : SVG_HEIGHT / 2;
  const yAxisX = projection.points.size
    ? SVG_WIDTH / 2 + ((0 - (projection.bounds.minX + projection.bounds.maxX) / 2) * projection.scale)
    : SVG_WIDTH / 2;

  return (
    <div className="overflow-hidden rounded-2xl border border-slate-200 bg-slate-950 shadow-inner" data-geometry-scene>
      <div className="flex items-center justify-between border-b border-white/10 px-3 py-2 text-white">
        <span className="text-[10px] font-bold uppercase tracking-[0.18em] text-slate-300">
          {scene.dimension === "3d" ? "3D · Drag to rotate" : "2D · Wheel zoom"}
        </span>
        <div className="flex items-center gap-1">
          <button type="button" onClick={() => setZoom((value) => Math.min(2.5, value * 1.15))} className="rounded-md p-1.5 text-slate-300 hover:bg-white/10 hover:text-white" aria-label="Enlarge the graph">
            <ZoomIn className="h-3.5 w-3.5" />
          </button>
          <button type="button" onClick={() => setZoom((value) => Math.max(0.55, value / 1.15))} className="rounded-md p-1.5 text-slate-300 hover:bg-white/10 hover:text-white" aria-label="Shrink graphics">
            <ZoomOut className="h-3.5 w-3.5" />
          </button>
          <button type="button" onClick={() => { setZoom(1); setYaw(32); setPitch(-20); }} className="rounded-md p-1.5 text-slate-300 hover:bg-white/10 hover:text-white" aria-label="Reset graphics perspective">
            <RotateCcw className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
      <svg
        viewBox={`0 0 ${SVG_WIDTH} ${SVG_HEIGHT}`}
        className="block aspect-[16/10] w-full touch-none select-none bg-[radial-gradient(circle_at_top,#1e293b,#020617_70%)]"
        role="img"
        aria-label={`${scene.title} interactive graphic`}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerCancel={handlePointerUp}
        onWheel={handleWheel}
      >
        <defs>
          <marker id={arrowMarkerId} markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth">
            <path d="M0,0 L0,6 L9,3 z" fill="#38bdf8" />
          </marker>
          <pattern id={`${arrowMarkerId}-grid`} width="32" height="32" patternUnits="userSpaceOnUse">
            <path d="M 32 0 L 0 0 0 32" fill="none" stroke="rgba(148,163,184,0.12)" strokeWidth="1" />
          </pattern>
        </defs>
        {scene.show_grid ? <rect width={SVG_WIDTH} height={SVG_HEIGHT} fill={`url(#${arrowMarkerId}-grid)`} /> : null}
        {scene.show_axes && scene.dimension === "2d" ? (
          <g stroke="rgba(203,213,225,0.42)" strokeWidth="1.5">
            <line x1="0" y1={xAxisY} x2={SVG_WIDTH} y2={xAxisY} />
            <line x1={yAxisX} y1="0" x2={yAxisX} y2={SVG_HEIGHT} />
          </g>
        ) : null}
        {[...scene.primitives]
          .sort((first, second) => {
            const firstDepth = primitivePoints(first, projection.points).reduce((sum, point) => sum + point.depth, 0);
            const secondDepth = primitivePoints(second, projection.points).reduce((sum, point) => sum + point.depth, 0);
            return firstDepth - secondDepth;
          })
          .map((primitive) => renderPrimitive(primitive, projection.points, projection.scale, arrowMarkerId))}
        {[...projection.points.values()].filter((point) => !point.hidden).map((point) => (
          <g key={point.id}>
            <circle cx={point.screenX} cy={point.screenY} r="4.5" fill={point.color || "#f8fafc"} stroke="#fff" strokeWidth="1.5" />
            {point.label ? (
              <text x={point.screenX + 8} y={point.screenY - 8} fill="#f8fafc" className="text-[14px] font-semibold drop-shadow">
                {point.label}
              </text>
            ) : null}
          </g>
        ))}
      </svg>
    </div>
  );
}
