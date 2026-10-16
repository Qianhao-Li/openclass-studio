"""Public identities and capabilities shared by Studio hosts."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class WorkspacePrincipal(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str = Field(min_length=1, max_length=128)
    kind: Literal['local', 'platform']
    capabilities: frozenset[str] = frozenset()

    def require(self, capability: str) -> None:
        from fastapi import HTTPException
        if capability not in self.capabilities:
            raise HTTPException(403, f'This workspace does not allow {capability}.')

class StudioCapabilities(BaseModel):
    courses: bool = True
    personal_models: bool = True
    platform_connected: bool = False
    cloud_enhancement: bool = False

class StudioBootstrap(BaseModel):
    api_version: str = '1'
    mode: Literal['local', 'platform']
    principal: WorkspacePrincipal
    capabilities: StudioCapabilities = Field(default_factory=StudioCapabilities)
