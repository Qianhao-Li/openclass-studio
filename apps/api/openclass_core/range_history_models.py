"""Read-only projections of verified document changes, shared by History views."""
from typing import Literal
from pydantic import BaseModel, Field

class ContentActionSpan(BaseModel):
    start: float = Field(ge=0, le=1)
    end: float = Field(ge=0, le=1)

class ContentActionView(BaseModel):
    id: str
    label: str
    sequence: int
    initial: bool = False
    rank: int = 0
    spans: list[ContentActionSpan] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    can_undo: bool = False
    can_redo: bool = False
    status: Literal['active', 'withdrawn'] = 'active'
    scope: Literal['ranges', 'document'] = 'ranges'
    summary: str = ''
    blocked_reason: str | None = None
    changes: list[dict] = Field(default_factory=list)
