"""Retain displaced TOC rows as fallible clues for one original-unit recheck."""
from collections import Counter
from openclass_core.models import SourceRange
from openclass_core.services.google_sources.config import GoogleSourceError

def row_clues(nodes):
    return [{'title': row['title'], 'level': row.get('level', 1), 'printed_page_label': row.get('printed_page_label', '')} for node in nodes for row in [node.model_dump() if hasattr(node, 'model_dump') else node]]

def snapshot_clues(task):
    """Recover location-bound leads from the previous catalog, never body text."""
    from openclass_core.services.source_catalog_transcription import unit_key
    result = {}
    content_hash, source_version = (task.get('content_hash'), task.get('source_version'))
    if not isinstance(content_hash, str) or not content_hash or (not isinstance(source_version, str)) or (not source_version):
        return result
    seed = task.get('seed')
    for row in seed if isinstance(seed, list) else []:
        if not isinstance(row, dict) or row.get('source_content_hash') != content_hash or (not isinstance(row.get('title'), str)) or (not row['title'].strip()) or (type(row.get('level')) is not int) or (row['level'] < 1):
            continue
        metadata = row.get('metadata', {})
        if not isinstance(metadata, dict):
            continue
        proofs = metadata.get('directory_evidence', [])
        proofs = [proofs] if isinstance(proofs, dict) else proofs if isinstance(proofs, list) else []
        located = set()
        for proof in proofs:
            if not isinstance(proof, dict):
                continue
            value = proof.get('locator', {})
            if not isinstance(value, dict) or proof.get('content_hash') != content_hash or proof.get('source_version') != source_version or (value.get('kind') != 'pdf_pages') or (type(value.get('start')) is not int) or (type(value.get('end')) is not int) or (value['start'] < 1) or (value['start'] != value.get('end')):
                continue
            try:
                key = unit_key(value)
            except (ValueError, TypeError):
                continue
            if key in located:
                continue
            located.add(key)
            result.setdefault(key, []).append({'title': row['title'], 'level': row['level'], 'printed_page_label': str(metadata.get('printed_page', ''))})
    return result

def _position(row):
    return (''.join(row['title'].split()), row.get('level', 1), row.get('printed_page_label', '').strip())

def record_changes(task, source_range, previous, observed, read_ids, *, complete, reconciled=False):
    from openclass_core.services.source_catalog_transcription import unit_key
    key = unit_key(source_range)
    archive = dict(task.get('directory_clue_archive', {}))
    pending = dict(task.get('directory_reconciliation', {}))
    original = list(archive.get(key, []))
    archived, occurrences = (Counter((_position(row) for row in original)), Counter())
    for row in row_clues(previous):
        position = _position(row)
        occurrences[position] += 1
        if occurrences[position] > archived[position]:
            original.append(row)
    current = row_clues(observed)
    remaining = Counter((_position(row) for row in current))
    displaced = []
    for row in original:
        position = _position(row)
        if remaining[position]:
            remaining[position] -= 1
        else:
            displaced.append(row)
    if complete and displaced and (not reconciled):
        pending[key] = {'source_range': SourceRange.model_validate(source_range).model_dump(mode='json'), 'displaced_rows': displaced, 'initial_read_ids': list(read_ids)}
        archive[key] = original
    elif complete:
        pending.pop(key, None)
        archive[key] = current
    task['directory_clue_archive'], task['directory_reconciliation'] = (archive, pending)

def pending_reconciliation(task):
    from openclass_core.services.source_catalog_transcription import unit_key
    for unit in (task.get('directory_transcription') or {}).get('units', []):
        pending = task.get('directory_reconciliation', {}).get(unit_key(unit['source_range']))
        if pending:
            return {'source_range': pending['source_range'], 'displaced_rows': pending['displaced_rows'], 'current_rows': unit.get('nodes', []), 'usage': 'Fallible leads only; verify the original, allowing corrections and intentional removal.'}
    return None

def validate_reconciliation(task, source_range, read_ids):
    from openclass_core.services.source_catalog_transcription import unit_key
    from openclass_core.services.source_catalog_refinement_evidence import read_units, range_was_read
    key = unit_key(source_range)
    pending = task.get('directory_reconciliation', {}).get(key)
    if not pending:
        raise GoogleSourceError('This directory unit has no pending reconciliation.')
    fresh = [identity for identity in read_ids if identity not in pending['initial_read_ids'] and task.get('reads', {}).get(identity, {}).get('request_id') == task['request_id']]
    if not range_was_read(SourceRange.model_validate(source_range), read_units(task, fresh)):
        raise GoogleSourceError('Reconciliation needs a new current original read of this directory unit.')
