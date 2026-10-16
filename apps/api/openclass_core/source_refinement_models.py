"""Safe, read-only progress for one source catalog run."""
from typing import Literal
from pydantic import BaseModel, Field

class SourceRefinementRange(BaseModel):
    kind: str
    start: int | None = None
    end: int | None = None

class SourceRefinementStep(BaseModel):
    id: str = ''
    stage: str = ''
    source_range: SourceRefinementRange | None = None
    fragment_id: str = ''
    attempt: int = 0
    started_at: str | None = None
    deadline_at: str | None = None

class SourceRefinementCheckpoint(BaseModel):
    stage: str = ''
    source_range: SourceRefinementRange | None = None
    fragment_id: str = ''
    saved_at: str | None = None
    kind: str = ''

class SourceCatalogRefinementView(BaseModel):
    request_id: str
    job_id: str | None = None
    mode: Literal['continue', 'reverify'] | None = None
    target_chapter_id: str | None = None
    status: Literal['queued', 'running', 'ready', 'partial', 'failed', 'cancelled', 'interrupted']
    phase: str = ''
    created_at: str | None = None
    started_at: str | None = None
    updated_at: str | None = None
    deadline_at: str | None = None
    finished_at: str | None = None
    current_step: SourceRefinementStep | None = None
    last_checkpoint: SourceRefinementCheckpoint | None = None
    stop_reason: str = ''
    model_calls: int = 0
    budget_seconds: int | None = None
    max_model_calls: int | None = None
    completed_units: int = 0
    total_units: int = 0
    discovery_complete: bool = False
    location_summary: dict[str, int] = Field(default_factory=dict)
