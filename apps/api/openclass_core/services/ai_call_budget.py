from __future__ import annotations
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Literal
from openai.lib._pydantic import to_strict_json_schema
from pydantic import BaseModel
DEFAULT_REQUIREMENT_DEADLINE_SECONDS = 60.0
DEFAULT_REQUIREMENT_MAX_OUTPUT_TOKENS = 4096
ChatCompletionTokenParameter = Literal['max_tokens', 'max_completion_tokens']
AICallFailureKind = Literal['structured_parse_failed', 'deadline_exceeded', 'output_budget_exceeded', 'provider_unavailable', 'model_call_failed']

class AICallBudgetExceeded(TimeoutError):
    pass

class AIOutputBudgetExceeded(RuntimeError):
    pass

@dataclass
class AICallBudget:
    deadline_monotonic: float
    max_output_tokens: int
    max_output_chars: int
    failure_kind: AICallFailureKind | None = None
    failure_reason: str = ''

    @classmethod
    def for_requirement_refinement(cls) -> 'AICallBudget':
        deadline_seconds = _env_float('OPENCLASS_REQUIREMENT_AI_DEADLINE_SECONDS', DEFAULT_REQUIREMENT_DEADLINE_SECONDS, minimum=5.0)
        max_output_tokens = _env_int('OPENCLASS_REQUIREMENT_AI_MAX_OUTPUT_TOKENS', DEFAULT_REQUIREMENT_MAX_OUTPUT_TOKENS, minimum=512)
        return cls(deadline_monotonic=time.monotonic() + deadline_seconds, max_output_tokens=max_output_tokens, max_output_chars=max_output_tokens * 8)

    def remaining_seconds(self) -> float:
        remaining = self.deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise AICallBudgetExceeded('AI call deadline exceeded')
        return remaining

    def checkpoint(self) -> None:
        self.remaining_seconds()

    def validate_output(self, text: str) -> None:
        if len(text) > self.max_output_chars:
            raise AIOutputBudgetExceeded(f'AI output exceeded the {self.max_output_chars}-character budget')

    def record_failure(self, kind: AICallFailureKind, reason: str) -> None:
        self.failure_kind = kind
        self.failure_reason = reason
_current_ai_call_budget: ContextVar[AICallBudget | None] = ContextVar('current_ai_call_budget', default=None)

@contextmanager
def bind_ai_call_budget(budget: AICallBudget) -> Iterator[AICallBudget]:
    token = _current_ai_call_budget.set(budget)
    try:
        yield budget
    finally:
        _current_ai_call_budget.reset(token)

def current_ai_call_budget() -> AICallBudget | None:
    return _current_ai_call_budget.get()

def record_current_ai_call_failure(kind: AICallFailureKind, reason: str) -> None:
    budget = current_ai_call_budget()
    if budget is not None:
        budget.record_failure(kind, reason)

def budgeted_openai_client(client: Any) -> Any:
    budget = current_ai_call_budget()
    if budget is None:
        return client
    timeout = budget.remaining_seconds()
    with_options = getattr(client, 'with_options', None)
    if callable(with_options):
        return with_options(timeout=timeout, max_retries=0)
    return client

def budgeted_chat_completion_options(model: str, *, token_parameter: ChatCompletionTokenParameter | None=None) -> dict[str, Any]:
    budget = current_ai_call_budget()
    if budget is None:
        return {}
    budget.checkpoint()
    token_key = token_parameter or official_openai_token_parameter(model)
    return {token_key: budget.max_output_tokens, 'timeout': budget.remaining_seconds()}

def budgeted_responses_options() -> dict[str, Any]:
    budget = current_ai_call_budget()
    if budget is None:
        return {}
    budget.checkpoint()
    return {'max_output_tokens': budget.max_output_tokens, 'timeout': budget.remaining_seconds()}

def validate_ai_output_budget(text: str) -> None:
    budget = current_ai_call_budget()
    if budget is not None:
        budget.validate_output(text)

def ai_request_timeout(default: float) -> float:
    budget = current_ai_call_budget()
    return min(default, budget.remaining_seconds()) if budget is not None else default

def official_openai_token_parameter(model: str) -> ChatCompletionTokenParameter:
    return 'max_completion_tokens' if model.startswith(('gpt-', 'o1', 'o3', 'o4')) else 'max_tokens'

def strict_json_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Return the strict schema shape required by OpenAI Structured Outputs."""
    return to_strict_json_schema(schema)

def bounded_ai_log_text(value: Any, limit: int=12000) -> Any:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return f'{value[:limit]}\n...[truncated {len(value) - limit} chars]'

def _env_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.getenv(name)
    try:
        value = float(raw) if raw is not None else default
    except ValueError:
        value = default
    return max(minimum, value)

def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        value = default
    return max(minimum, value)
