"""Frozen model capabilities for runtime tools, never provider rediscovery mid-turn."""
from __future__ import annotations

def freeze_image_capability(selection, owner_user_id, lease, *, supported: bool) -> dict:
    lease.check()
    return {'owner_user_id': owner_user_id, 'provider': selection.provider, 'model': selection.model, 'access_method': selection.access_method or 'chatgpt_subscription', 'connection_id': lease.connection_id, 'generation': lease.generation, 'supports_image_inputs': supported is True}

def frozen_image_capability(selection, owner_user_id, snapshot) -> bool:
    from openclass_core.services.codex_connections import resolve_codex_lease
    if not isinstance(snapshot, dict) or selection.provider != 'openai_codex':
        return False
    if not selection.connection_id or selection.access_method == 'platform_credits':
        return False
    lease = resolve_codex_lease(owner_user_id, selection.connection_id)
    if lease is None:
        return False
    expected = {'owner_user_id': owner_user_id, 'provider': selection.provider, 'model': selection.model, 'access_method': selection.access_method or 'chatgpt_subscription', 'connection_id': lease.connection_id, 'generation': lease.generation}
    return all((snapshot.get(key) == value for key, value in expected.items())) and snapshot.get('supports_image_inputs') is True
