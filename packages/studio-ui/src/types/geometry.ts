import type { AIModelSelection, ChatAttachmentRef, SelectionRef } from ".";

export type GeometryDimension = "2d" | "3d";

export type GeometryPrimitiveKind =
  | "point"
  | "segment"
  | "line"
  | "polyline"
  | "polygon"
  | "circle"
  | "ellipse"
  | "vector"
  | "plane"
  | "sphere"
  | "label";

export interface GeometryPoint {
  id: string;
  label: string;
  x: number;
  y: number;
  z: number;
  color: string;
  hidden: boolean;
}

export interface GeometryPrimitive {
  id: string;
  kind: GeometryPrimitiveKind;
  label: string;
  point_ids: string[];
  center_id: string;
  radius: number | null;
  radius_y: number | null;
  text: string;
  color: string;
  fill: string;
  opacity: number;
  stroke_width: number;
  dashed: boolean;
}

export interface GeometryScene {
  version: "1.0";
  title: string;
  summary: string;
  dimension: GeometryDimension;
  show_axes: boolean;
  show_grid: boolean;
  viewport: {
    x_min: number;
    x_max: number;
    y_min: number;
    y_max: number;
  };
  points: GeometryPoint[];
  primitives: GeometryPrimitive[];
  steps: string[];
  source_excerpt: string;
}

export interface GeometryGenerationPayload {
  selection: SelectionRef;
  instructions?: string;
  attachments?: ChatAttachmentRef[];
  text_model?: AIModelSelection | null;
}
