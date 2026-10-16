"""File-scoped Layout Parser page hints, never original-reading proof."""
import hashlib
import json
from openclass_core.services.google_sources.config import GoogleSourceError

def page_clues(service, source, page, *, max_characters=48000):
    if source.metadata.get('enhancement_status') != 'ready' or not source.metadata.get('search_ready'):
        return None
    if type(page) is not int or page < 1:
        raise GoogleSourceError('The enhanced page clue requires a physical PDF page.')
    count = source.metadata.get('catalog_pdf_page_count') or source.metadata.get('page_count')
    if count and page > count:
        raise GoogleSourceError('The enhanced page clue is outside this original file.')
    args = dict(owner_user_id=source.owner_user_id, package_id=source.package_id, source_id=source.id, content_hash=source.metadata['content_hash'], ready=False)
    current = service.get(**args)
    if (current.id, current.owner_user_id, current.package_id) != (source.id, source.owner_user_id, source.package_id):
        raise GoogleSourceError('The enhanced page clue belongs to another source account.')
    if current.metadata.get('source_version') != source.metadata['source_version']:
        raise GoogleSourceError('The enhanced page clue belongs to an outdated source version.')
    if current.metadata.get('enhancement_status') != 'ready' or not current.metadata.get('search_ready'):
        return None
    value = service.artifact(current)
    for key, expected in (('source_id', source.id), ('source_version', source.metadata['source_version']), ('content_hash', source.metadata['content_hash'])):
        if value.get(key) != expected:
            raise GoogleSourceError('The enhanced page clue does not match the authorized original.')
    covering = [part for part in value.get('parts', []) if part.get('page_offset', 0) < page <= part.get('page_offset', 0) + part.get('page_count', 0)]
    if not covering or any((not part.get('layout_verified') or not part.get('processing_complete') for part in covering)):
        raise GoogleSourceError('This original page has no completed verified Layout Parser output.')
    from openclass_core.services.google_sources.catalog_structure import cloud_blocks
    blocks = [{key: block[key] for key in ('ref', 'text', 'type', 'page_start', 'page_end')} for block in cloud_blocks(covering) if block['page_start'] == block['page_end'] == page and block['text']]
    if sum((len(block['text']) for block in blocks)) > max_characters:
        raise GoogleSourceError("The enhanced page clues exceed this read's text capacity; original reading remains available.")
    fresh = service.get(**args)
    if (fresh.id, fresh.owner_user_id, fresh.package_id) != (source.id, source.owner_user_id, source.package_id):
        raise GoogleSourceError('The enhanced page clue belongs to another source account.')
    if fresh.metadata.get('source_version') != source.metadata['source_version'] or fresh.metadata.get('google_artifact') != current.metadata.get('google_artifact'):
        raise GoogleSourceError('The original version changed while reading its enhanced page clues.')
    if not blocks:
        return None
    return {'source_id': source.id, 'content_hash': source.metadata['content_hash'], 'source_version': source.metadata['source_version'], 'source_range': {'kind': 'pdf_pages', 'start': page, 'end': page}, 'representation': 'google_layout_navigation', 'artifact_sha256': current.metadata.get('google_artifact', {}).get('sha256', ''), 'content_digest': hashlib.sha256(json.dumps(blocks, ensure_ascii=False, sort_keys=True).encode()).hexdigest(), 'blocks': blocks, 'usage': 'Derived location and transcription clues only; original images determine wording, order and completeness. No original read receipt is issued.'}
