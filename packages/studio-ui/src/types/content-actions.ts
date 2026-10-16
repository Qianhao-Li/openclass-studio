/** Normalized positions in the server's stable coverage coordinate space. */
export interface ContentActionSpan {
  start: number;
  end: number;
  fragment_id?: string;
}

export interface DocumentRangeAnchor {
  path: number[];
  kind: "node" | "text" | "gap";
  start: number;
  end: number;
}

export interface DocumentRangeFragment {
  id: string;
  kind: "initial" | "insert" | "replace" | "delete";
  precision: "exact" | "unknown";
  reason?: string | null;
  before: DocumentRangeAnchor[];
  after: DocumentRangeAnchor[];
  before_marker?: DocumentRangeAnchor;
  after_marker?: DocumentRangeAnchor;
  reference: DocumentRangeAnchor[];
  reference_precision: "exact" | "unknown";
}

export interface DocumentRangeProjection {
  version: 1;
  offset_unit: "unicode_scalar_inline_atom";
  reference_document_ref: string;
  before_document_ref: string | null;
  after_document_ref: string;
  before_document?: import(".").BoardDocument | null;
  fragments: DocumentRangeFragment[];
}

export interface ContentActionChange {
  block_id: string;
  start: number;
  end: number;
  deleted: string;
  inserted: string;
}

/** A document action; dependencies are authoritative and never inferred from overlap. */
export interface ContentAction {
  id: string;
  label: string;
  sequence: number;
  initial: boolean;
  rank: number;
  spans: ContentActionSpan[];
  /** Unicode character coordinates normalized to the action's preview snapshot. */
  locate_spans?: ContentActionSpan[];
  changes?: ContentActionChange[];
  dependencies: string[];
  blockers: string[];
  can_undo: boolean;
  can_redo: boolean;
  status: "active" | "withdrawn";
  is_new?: boolean;
  scope: "ranges" | "document";
  range_projection?: DocumentRangeProjection;
  /** Actual measured ranges in the common reference document. */
  display_spans?: ContentActionSpan[];
  summary?: string;
  blocked_reason?: string | null;
}

export interface ContentActionView {
  branch_name: string;
  revision: string;
  draft_version?: number;
  actions: ContentAction[];
  document_length: number;
}
