"""Restore source citation controls solely from the authoritative course."""
from __future__ import annotations
from openclass_core.models import SourceCitation
from openclass_core.services.ridoc_v3 import RidocV3Error, content_hash

def source_citations_for_node(archive, node) -> list[SourceCitation]:
    try:
        citations = [SourceCitation.model_validate(raw) for raw in node.decision_metadata.get('source_citations', [])]
    except (TypeError, ValueError) as exc:
        raise RidocV3Error('The course source citation metadata is invalid') from exc
    restored = []
    for citation in citations:
        if not citation.evidence_ref:
            restored.append(citation)
            continue
        payload = archive.evidence.get(citation.evidence_ref)
        if citation.evidence_ref not in node.evidence_ids or not isinstance(payload, dict) or content_hash(payload) != citation.evidence_ref:
            raise RidocV3Error('The course source citation has no valid node-bound snapshot')
        fields = {'citation_id': citation.evidence_id, 'source_id': citation.source_id, 'source_content_hash': citation.source_content_hash, 'source_document_id': citation.source_document_id, 'page_start': citation.page_start, 'page_end': citation.page_end}
        if any((payload.get(key) != value for key, value in fields.items())):
            raise RidocV3Error('The course source citation identity disagrees with its snapshot')
        for asset in payload.get('assets', []):
            digest = asset.get('content_hash', '')
            raw = archive.assets.get(digest)
            if raw is None or digest not in node.resource_refs or content_hash(raw) != digest:
                raise RidocV3Error('The course source citation image has no valid node-bound asset')
        restored.append(citation.model_copy(update={'snapshot_url': f'/api/packages/{archive.package.id}/lessons/{node.lesson_id}/source-evidence/{citation.evidence_ref}'}))
    return restored
