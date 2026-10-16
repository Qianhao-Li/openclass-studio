"""Compact History responses without losing or fabricating document snapshots."""
from __future__ import annotations
from typing import Any
import gzip
from starlette.responses import JSONResponse, Response
from openclass_core.services.ridoc_v3 import content_hash

def compact_history_response(value: dict[str, Any], accept_encoding: str) -> Response:
    response = JSONResponse(value, headers={'Cache-Control': 'no-store', 'Vary': 'X-OpenClass-History-View, Accept-Encoding'})
    accepts_gzip = False
    if isinstance(accept_encoding, str):
        for item in accept_encoding.lower().split(','):
            parts = [part.strip() for part in item.split(';')]
            if parts[0] == 'gzip':
                try:
                    quality = next((float(part[2:]) for part in parts[1:] if part.startswith('q=')), 1.0)
                    accepts_gzip = 0 < quality <= 1
                except ValueError:
                    pass
    if accepts_gzip and len(response.body) >= 1024:
        body = gzip.compress(response.body, compresslevel=1, mtime=0)
        return Response(body, media_type='application/json', headers={'Cache-Control': 'no-store', 'Content-Encoding': 'gzip', 'Vary': 'X-OpenClass-History-View, Accept-Encoding'})
    return response

def compact_history_documents(value: dict[str, Any]) -> dict[str, Any]:
    """Intern only schema-defined BoardDocument fields, never user metadata."""
    documents: dict[str, Any] = {}

    def intern(document: dict[str, Any]) -> dict[str, str]:
        identity = content_hash(document)
        documents.setdefault(identity, document)
        return {'$history_document_ref': identity}
    package = value.get('draft_course_package')
    if package:
        for lesson in package['lessons']:
            lesson['board_document'] = intern(lesson['board_document'])
            graph = lesson['history_graph']
            for commit in graph['commits']:
                commit['snapshot'] = intern(commit['snapshot'])
            for branch in graph['branches'].values():
                branch['replay_documents'] = {key: intern(document) for key, document in branch.get('replay_documents', {}).items()}
    history = value.get('draft_history')
    if history:
        history['replay_documents'] = {key: intern(document) for key, document in history.get('replay_documents', {}).items()}
        for action in (history.get('document_history') or {}).get('actions', []):
            projection = action.get('range_projection') or {}
            if projection.get('before_document') is not None:
                projection['before_document'] = intern(projection['before_document'])
    return {**value, 'document_table': documents}

def clean_history_session(record: dict[str, Any]) -> dict[str, Any]:
    """The caller has validated the unchanged base and current RIDOC revision."""
    return {'id': record['session_id'], 'package_id': record['package_id'], 'lesson_id': record['lesson_id'], 'base_ridoc_revision': record['base_revision'], 'base_repository_version': record['base_repository_version'], 'version': record['version'], 'state': record['state'], 'dirty': False, 'pending': False, 'editing_enabled': bool(record.get('editing_enabled', True)), 'pending_removals': [], 'affected_lesson_ids': [], 'deleted_node_ids': [], 'stale': False, 'blocked_reason': None, 'operation_count': 0, 'reuse_base_revision': record['base_revision'], 'document_table': {}}
