"""Reject incomplete source reads before a learning board is frozen or written."""
from __future__ import annotations
import json
from collections.abc import Iterable
from openclass_core.models import RetrievalEvidence

def require_complete_board_evidence(evidence: Iterable[RetrievalEvidence]) -> None:
    """Check the terminal receipt for each source/version/range, retaining holes.

    An early cursor is superseded by completion of the same authenticated range.
    Different sources, ranges, and explicit missing units cannot supersede it.
    Older original-file adapters have no paginated coverage metadata; their own
    range/manifest validation remains authoritative.
    """
    latest: dict[str, dict] = {}
    missing: list[dict] = []
    seen: set[str] = set()
    for item in evidence:
        for entry in item.metadata.get('source_coverage', []):
            if not isinstance(entry, dict):
                raise ValueError('Source reading coverage is invalid; the board was not generated.')
            signature = json.dumps(entry, sort_keys=True, ensure_ascii=False)
            if signature in seen:
                continue
            seen.add(signature)
            if entry.get('missing') or entry.get('reason'):
                missing.append(entry)
            key = json.dumps([entry.get('source_id') or item.source_ingestion_id, entry.get('source_version') or item.metadata.get('source_version'), entry.get('source_content_hash') or item.metadata.get('source_content_hash'), entry.get('read_range')], sort_keys=True, ensure_ascii=False)
            latest[key] = entry
    missing.extend((entry for entry in latest.values() if entry.get('complete') is not True))
    if missing:
        details = [{key: entry[key] for key in ('read_range', 'missing', 'reason', 'remaining_ranges') if entry.get(key) is not None} for entry in missing]
        raise ValueError('The selected source range has not been read completely; no board was generated. Unresolved ranges: ' + json.dumps(details, ensure_ascii=False, separators=(',', ':')))
