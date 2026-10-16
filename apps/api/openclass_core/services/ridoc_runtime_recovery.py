"""Reconcile interrupted turns at startup without changing course outcomes."""
from __future__ import annotations
import json
import os
from typing import Any
from uuid import uuid4
from openclass_core.models import now_iso
_PROCESS_NONCE = uuid4().hex
_PROCESS_KEY = '_runtime_process_id'

def runtime_process_id() -> str:
    return f'{os.getpid()}:{_PROCESS_NONCE}'

def owned_runtime_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, _PROCESS_KEY: runtime_process_id()}

def recover_interrupted_runtime_turns(store: Any) -> int:
    """Run before API startup completes, under the single-writer store contract.

    Previous-process turns (including legacy rows without process ownership)
    cannot resume their in-memory workflows. Retain their runtime evidence as
    interrupted; never promote partial results or edit the authoritative RIDOC.
    Repeated calls preserve turns started by this process, regardless of age.
    """
    recovered = 0
    with store._connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        rows = connection.execute("SELECT owner_user_id, package_id, lesson_id, turn_id, payload_json FROM ridoc_runtime_turns WHERE state='pending'").fetchall()
        for row in rows:
            try:
                payload = json.loads(row['payload_json'])
            except (TypeError, ValueError):
                payload = None
            if isinstance(payload, dict) and payload.get(_PROCESS_KEY) == runtime_process_id():
                continue
            stamp = now_iso()
            connection.execute("UPDATE ridoc_runtime_turns SET state='interrupted', error=?, updated_at=? WHERE owner_user_id=? AND package_id=? AND lesson_id=? AND turn_id=? AND state='pending'", ('API process restarted before the turn completed', stamp, row['owner_user_id'], row['package_id'], row['lesson_id'], row['turn_id']))
            connection.execute('INSERT INTO ridoc_runtime_audit(id, owner_user_id, package_id, lesson_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)', (f'audit_{uuid4().hex}', row['owner_user_id'], row['package_id'], row['lesson_id'], 'turn_interrupted', json.dumps({'turn_id': row['turn_id'], 'reason': 'api_process_restarted'}), stamp))
            recovered += 1
    return recovered
