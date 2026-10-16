from __future__ import annotations
import json
from typing import Any

def json_object(text: str) -> dict[str, Any]:
    normalized = text.strip()
    if normalized.startswith('```'):
        lines = normalized.splitlines()
        if lines and lines[0].startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        normalized = '\n'.join(lines).strip()
    try:
        value = json.loads(normalized)
    except json.JSONDecodeError:
        start = normalized.find('{')
        end = normalized.rfind('}')
        if start < 0 or end <= start:
            raise
        value = json.loads(normalized[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError('Structured response must be a JSON object')
    return value

def validation_issues(error: Exception) -> list[dict[str, str]]:
    errors_method = getattr(error, 'errors', None)
    if callable(errors_method):
        try:
            raw_issues = errors_method(include_input=False, include_url=False)
        except TypeError:
            raw_issues = errors_method()
        issues: list[dict[str, str]] = []
        for raw_issue in raw_issues[:12]:
            location = raw_issue.get('loc', ())
            path = '.'.join((str(part) for part in location))
            issues.append({'path': path or '$', 'type': str(raw_issue.get('type', 'validation_error'))[:120], 'message': str(raw_issue.get('msg', 'Invalid value'))[:240]})
        if issues:
            return issues
    if isinstance(error, json.JSONDecodeError):
        return [{'path': '$', 'type': 'json_decode_error', 'message': f'{error.msg} at line {error.lineno}, column {error.colno}'[:240]}]
    return [{'path': '$', 'type': type(error).__name__, 'message': str(error)[:240] or 'Structured response validation failed'}]

def validation_repair_prompt(error: Exception) -> str:
    issues = json.dumps(validation_issues(error), ensure_ascii=False, separators=(',', ':'))
    return f"The previous JSON object did not validate against the supplied schema. Return one corrected JSON object and no prose. Preserve the answer's meaning, but fix every validation issue below. Do not return null for a non-nullable field; provide a schema-valid value or omit the field when the schema allows it. Do not invent new semantic claims.\nValidation issues: {issues}"
