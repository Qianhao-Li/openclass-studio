"""Let one workflow own output repair without nesting provider repair loops."""
from __future__ import annotations
import json
from contextlib import contextmanager
from contextvars import ContextVar
from pydantic import ValidationError
_repair_allowed: ContextVar[bool] = ContextVar('structured_output_repair_allowed', default=True)

@contextmanager
def single_attempt_structured_output():
    token = _repair_allowed.set(False)
    try:
        yield
    finally:
        _repair_allowed.reset(token)

def structured_output_repair_allowed() -> bool:
    return _repair_allowed.get()

class StructuredOutputValidationError(RuntimeError):
    """A model output failed validation; payload contains no original output."""

    def __init__(self, feedback: dict[str, object]):
        self.feedback = dict(feedback)
        super().__init__('The model returned an invalid structured output.')

    @classmethod
    def from_validation(cls, error: Exception, schema):
        feedback: dict[str, object] = {'issue': 'invalid_structured_output'}
        if isinstance(error, ValidationError):
            detail = error.errors(include_input=False, include_context=False, include_url=False)[0]
            known_fields: set[str] = set()

            def visit(value):
                if isinstance(value, dict):
                    properties = value.get('properties')
                    if isinstance(properties, dict):
                        known_fields.update(properties)
                    for child in value.values():
                        visit(child)
                elif isinstance(value, list):
                    for child in value:
                        visit(child)
            visit(schema.model_json_schema())
            feedback.update(error_type=detail['type'], field=[part if isinstance(part, int) or part in known_fields else '<item>' for part in detail.get('loc', ())[:4]])
        elif isinstance(error, json.JSONDecodeError):
            feedback['issue'] = 'invalid_json'
        return cls(feedback)
