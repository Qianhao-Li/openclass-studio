"""Request-local course storage. Never substitute the learner's account identity."""
from __future__ import annotations
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class LearningSessionScope:
    session_id: str
    owner_user_id: str
    ridoc_store: Any
    runtime_store: Any
    board_asset_store: Any
learning_session_scope: ContextVar[LearningSessionScope | None] = ContextVar('openclass_learning_session', default=None)
