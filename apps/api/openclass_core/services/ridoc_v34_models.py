"""RIDOC 3.4 independent state objects and explicit source identities."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class NodeRef(BaseModel):
    model_config = ConfigDict(extra='forbid')
    lesson_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    kind: Literal['M', 'C', 'E']
    status: Literal['live', 'deleted'] = 'live'

class DocumentState(BaseModel):
    model_config = ConfigDict(extra='forbid')
    lesson_id: str = Field(min_length=1)
    document_ref: str = Field(min_length=64, max_length=64)
    document_version: str = Field(min_length=1)
    structure_version: str = Field(min_length=1)
    versions: dict[str, str] = Field(default_factory=dict)
    resource_refs: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)

class DocumentOccurrence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    before_state_ref: str | None = Field(default=None, min_length=64, max_length=64)
    after_state_ref: str = Field(min_length=64, max_length=64)
    range_proof_ref: str | None = Field(default=None, min_length=64, max_length=64)
    input_trace_ref: str | None = Field(default=None, min_length=64, max_length=64)
    output_trace_ref: str | None = Field(default=None, min_length=64, max_length=64)
