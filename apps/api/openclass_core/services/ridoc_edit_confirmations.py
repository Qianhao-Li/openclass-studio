"""Revision-bound impact acknowledgements and final review capabilities."""
from __future__ import annotations
import json
import secrets
from typing import Any
from openclass_core.services.ridoc_edit_session_store import ensure_tables
from openclass_core.services.ridoc_v3 import RidocDocumentNode, RidocMessageNode, RidocV3ConflictError, RidocV3Error, content_hash

def reference_rows(archive, lesson_id: str, node_id: str) -> list[dict[str, Any]]:
    from openclass_core.services.ridoc_v34 import reference_impact
    rows = []
    for item in reference_impact(archive, lesson_id, node_id):
        referrer = item.get('node_id') or item.get('referrer_id')
        kind = item.get('kind') or item.get('referrer_kind') or 'node'
        row = {'lesson_id': item.get('lesson_id', lesson_id), 'kind': kind, 'relation': item.get('relation') or item.get('role') or ', '.join(item.get('roles', [])) or 'reference'}
        if referrer and kind not in {'branch', 'merge'}:
            row['node_id'] = referrer
        if item.get('branch_name'):
            row['branch_name'] = item['branch_name']
        if item.get('title'):
            row['title'] = item['title']
        rows.append(row)
    return rows

def bound_payload(record: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    return {'session_id': record['session_id'], 'base_revision': record['base_revision'], 'base_repository_version': record['base_repository_version'], 'draft_version': record['version'], 'snapshot_revision': record['base_revision'], 'operations_hash': content_hash(json.loads(record['operations_json'])), **payload}

def issue_token(store: Any, record: dict[str, Any], purpose: str, payload: dict[str, Any]) -> str:
    token = secrets.token_urlsafe(32)
    with store._connect() as connection:
        ensure_tables(connection)
        connection.execute('INSERT INTO ridoc_edit_confirmations VALUES (?,?,?,?,?,?,?)', (content_hash(token.encode()), record['owner_id'], record['scope_id'], record['session_id'], record['version'], purpose, content_hash(bound_payload(record, payload))))
    return token

def require_token(store: Any, record: dict[str, Any], purpose: str, payload: dict[str, Any], token: str | None) -> None:
    if not token:
        raise RidocV3Error('Review the impact and explicitly confirm the changes first')
    with store._connect() as connection:
        row = connection.execute('SELECT * FROM ridoc_edit_confirmations WHERE token_hash=? AND owner_id=? AND scope_id=? AND session_id=?', (content_hash(token.encode()), record['owner_id'], record['scope_id'], record['session_id'])).fetchone()
    if row is None or row['draft_version'] != record['version'] or row['purpose'] != purpose or (row['payload_hash'] != content_hash(bound_payload(record, payload))):
        raise RidocV3ConflictError('The confirmation details have changed. Review them again before confirming')

def removal_impact(archive, lesson_id: str, node_id: str) -> dict[str, Any]:
    node = archive.nodes.get(f'{lesson_id}/{node_id}')
    if not isinstance(node, (RidocMessageNode, RidocDocumentNode)) or (isinstance(node, RidocDocumentNode) and node.initial):
        raise RidocV3Error('Only complete M nodes or non-initial C nodes can be marked')
    referrers = reference_rows(archive, lesson_id, node_id)
    return {'lesson_id': lesson_id, 'node_id': node_id, 'kind': node.kind, 'referrers': referrers, 'requires_confirmation': bool(referrers)}

def removal_set_impact(archive, lesson_id: str, node_ids: list[str]) -> dict[str, Any]:
    if not node_ids or len(set(node_ids)) != len(node_ids):
        raise RidocV3Error('Select document changes without duplicates')
    impacts = [removal_impact(archive, lesson_id, identity) for identity in sorted(node_ids)]
    if any((item['kind'] != 'C' for item in impacts)):
        raise RidocV3Error('Batch range operations can only process document C changes')
    selected = set(node_ids)
    referrers = []
    for item in impacts:
        for referrer in item['referrers']:
            if referrer.get('lesson_id') == lesson_id and referrer.get('node_id') in selected:
                continue
            if referrer not in referrers:
                referrers.append(referrer)
    return {**impacts[0], 'targets': [{key: item[key] for key in ('lesson_id', 'node_id', 'kind')} for item in impacts], 'referrers': referrers, 'requires_confirmation': bool(referrers)}

def removal_set_purpose(lesson_id: str, node_ids: list[str]) -> str:
    return f'impact-set:{lesson_id}:{content_hash(sorted(node_ids))}'

def review_payload(record, display, removed, affected, changes, changed) -> dict[str, Any]:
    pending = [{key: item[key] for key in ('lesson_id', 'node_id', 'kind')} for item in removed]
    pending_ids = {(item['lesson_id'], item['node_id']) for item in pending}
    referrers = []
    for item in pending:
        for row in reference_rows(display, item['lesson_id'], item['node_id']):
            if row.get('node_id') and (row['lesson_id'], row['node_id']) in pending_ids:
                continue
            if row not in referrers:
                referrers.append(row)
    return {'pending_removals': pending, 'referrers': referrers, 'changes': changes, 'affected_lesson_ids': affected, 'dirty': changed, 'stale': False, 'blocked_reason': None, 'version': record['version'], 'base_ridoc_revision': record['base_revision']}
