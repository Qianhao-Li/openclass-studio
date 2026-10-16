from __future__ import annotations
import base64
from html import escape
import re
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from openclass_core.models import UserView
from openclass_core.studio_dependencies import optional_current_user
from openclass_core.services import workspace_state
from openclass_core.services.ridoc_v3 import RidocV3Error, content_hash
router = APIRouter()

def validate_evidence(archive, package_id, lesson_id, evidence_id):
    if archive is None or archive.package.id != package_id or lesson_id not in archive.package.lessons or (not re.fullmatch('[a-f0-9]{64}', evidence_id)):
        raise HTTPException(404, 'Course evidence not found.')
    nodes = [node for node in archive.nodes.values() if node.lesson_id == lesson_id and evidence_id in node.evidence_ids]
    payload = archive.evidence.get(evidence_id)
    if not nodes or not isinstance(payload, dict) or content_hash(payload) != evidence_id:
        raise HTTPException(404, 'Course evidence not found.')
    return (archive, payload, nodes)

def load_evidence(package_id, lesson_id, evidence_id, user):
    store = workspace_state.get_ridoc_workspace_store()
    try:
        if user is not None and store.package_id_for_lesson(user.id, lesson_id) == package_id:
            archive = store.load_archive(user.id, package_id=package_id)
        else:
            public = store.load_public_archive(package_id)
            archive = public[0] if public else None
    except (RidocV3Error, FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, 'Course evidence not found.') from exc
    return validate_evidence(archive, package_id, lesson_id, evidence_id)

def present_evidence(loaded, format: str):
    archive, payload, nodes = loaded
    pages = []
    for item in payload.get('assets', []):
        digest = item.get('content_hash', '')
        data = archive.assets.get(digest)
        if data is None or content_hash(data) != digest or (not any((digest in node.resource_refs for node in nodes))):
            raise HTTPException(404, 'Course evidence asset not found.')
        mime = item.get('mime_type', 'image/png')
        if mime not in {'image/png', 'image/jpeg', 'image/webp', 'image/gif'}:
            raise HTTPException(404, 'Course evidence image type is unsupported.')
        pages.append({'page': item.get('page_number'), 'data_url': f'data:{mime};base64,' + base64.b64encode(data).decode()})
    view = {'source_title': str(payload.get('source_title', 'Source evidence')), 'text': str(payload.get('text', '')), 'pages': pages}
    headers = {'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'}
    if format == 'json':
        return JSONResponse(view, headers=headers)
    images = ''.join((f'''<figure><img src="{page['data_url']}" alt="Source page"><figcaption>{escape(str(page['page'] or ''))}</figcaption></figure>''' for page in pages))
    body = f"""<!doctype html><html lang="en" translate="no"><meta charset="utf-8"><meta name="google" content="notranslate"><meta name="viewport" content="width=device-width"><title>{escape(view['source_title'])}</title><style>body{{max-width:960px;margin:32px auto;padding:0 20px;font:16px/1.7 system-ui}}pre{{white-space:pre-wrap}}img{{max-width:100%;height:auto}}</style><h1>{escape(view['source_title'])}</h1><p>Source citation saved with this course</p><pre>{escape(view['text'])}</pre>{images}</html>"""
    headers['Content-Security-Policy'] = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
    return HTMLResponse(body, headers=headers)

@router.get('/api/packages/{package_id}/lessons/{lesson_id}/source-evidence/{evidence_id}')
def snapshot(package_id: str, lesson_id: str, evidence_id: str, format: str=Query('html', pattern='^(html|json)$'), user: UserView | None=Depends(optional_current_user)):
    return present_evidence(load_evidence(package_id, lesson_id, evidence_id, user), format)
