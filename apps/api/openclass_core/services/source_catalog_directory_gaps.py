"""Inspect omitted pages inside a planned PDF directory, without withholding its rows."""
from openclass_core.models import SourceRange
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_catalog_refinement_evidence import read_units, range_was_read

def pending_gap(task):
    directory = task.get('directory_transcription') or {}
    pages = sorted({unit['source_range']['start'] for unit in directory.get('units', []) if unit['source_range'].get('kind') == 'pdf_pages'})
    marker = directory.get('review_request_id') or task.get('request_id')
    excluded = {entry['page'] for entry in task.get('directory_gap_decisions', []) if entry.get('review_id') == marker and range_was_read(SourceRange(kind='pdf_pages', start=entry['page'], end=entry['page']), read_units(task, entry.get('read_ids', [])))}
    for left, right in zip(pages, pages[1:]):
        for number in range(left + 1, right):
            if number not in excluded:
                return {'kind': 'pdf_pages', 'start': number, 'end': number}
    return None

def confirm_gap(task, source_range, read_ids, *, is_directory, reason):
    value = SourceRange.model_validate(source_range)
    expected = pending_gap(task)
    if not expected or (value.kind, value.start, value.end) != (expected['kind'], expected['start'], expected['end']):
        raise GoogleSourceError('Confirm only the currently pending original directory gap.')
    current = [identity for identity in read_ids if task.get('reads', {}).get(identity, {}).get('request_id') == task['request_id']]
    if not range_was_read(value, read_units(task, current)):
        raise GoogleSourceError("A directory gap requires this request's current original reading.")
    if not reason.strip():
        raise GoogleSourceError('Describe the observed directory or non-directory shape without quoting its body.')
    directory = task['directory_transcription']
    if is_directory:
        directory['units'].append({'source_range': expected, 'nodes': [], 'read_ids': [], 'complete': False, 'reviewed_for_request_id': ''})
        directory['units'].sort(key=lambda unit: unit['source_range']['start'])
    else:
        task.setdefault('directory_gap_decisions', []).append({'page': value.start, 'read_ids': current, 'review_id': directory.get('review_request_id') or task['request_id'], 'reason': reason[:300]})
