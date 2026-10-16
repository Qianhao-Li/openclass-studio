export type SourceRefinementMode = "continue" | "reverify";
export type SourceRefinementStatus = "queued" | "running" | "ready" | "partial" | "failed" | "cancelled" | "interrupted";

export type SourceRefinementRequest = {
  mode: SourceRefinementMode;
  requestId: string;
  targetChapterId?: string;
};

export interface SourceRefinementLocation {
  kind: string;
  start: number | null;
  end: number | null;
}

export interface SourceCatalogRefinementView {
  request_id: string;
  job_id?: string | null;
  mode: SourceRefinementMode | null;
  target_chapter_id?: string | null;
  status: SourceRefinementStatus;
  phase: string;
  created_at: string | null;
  started_at: string | null;
  updated_at: string | null;
  deadline_at: string | null;
  finished_at: string | null;
  current_step: { id: string; stage: string; source_range: SourceRefinementLocation | null;
    fragment_id: string; attempt: number; started_at: string | null; deadline_at: string | null } | null;
  last_checkpoint: { stage: string; source_range: SourceRefinementLocation | null;
    fragment_id: string; saved_at: string | null; kind: string } | null;
  stop_reason: string;
  model_calls: number;
  budget_seconds: number | null;
  max_model_calls: number | null;
  completed_units: number;
  total_units: number;
  discovery_complete: boolean;
  location_summary: Partial<Record<"verified" | "position_only" | "unresolved" | "outside_uploaded_file" | "total", number>>;
}
