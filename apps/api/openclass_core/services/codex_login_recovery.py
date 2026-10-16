"""Project an owner's current platform authorization back to its login page."""
from __future__ import annotations
from datetime import datetime, timedelta
from typing import Any, Iterable
from openclass_core.models import CodexLoginStartResponse

def platform_login_to_resume(user_id: str, attempts: Iterable[Any], *, now: datetime, completed_retention_seconds: int) -> CodexLoginStartResponse | None:
    for attempt in attempts:
        if attempt.owner_user_id != user_id or attempt.purpose != 'platform':
            continue
        if attempt.status == 'pending':
            expires_at = attempt.expires_at
        elif attempt.status == 'succeeded' and attempt.completion_state != 'consumed' and attempt.completed_at:
            expires_at = attempt.completed_at + timedelta(seconds=completed_retention_seconds)
        else:
            continue
        if expires_at <= now:
            continue
        return CodexLoginStartResponse(login_id=attempt.login_id, verification_url=attempt.verification_url, user_code=attempt.user_code, expires_at=expires_at.isoformat(), connection_id=attempt.connection_id)
    return None
