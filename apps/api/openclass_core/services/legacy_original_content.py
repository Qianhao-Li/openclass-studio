"""Original native carriers for locally stored, pre-cloud source catalogs.

Catalog text only supplies coordinates. Every returned fact is re-read from the
hash-verified original; no chunk body or prior generated text is an input.
"""
from __future__ import annotations
import base64
import hashlib
from pathlib import Path
from openclass_core.models import RetrievalEvidence
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_content_contract import SourceReadResult, content_digest, issue_read_receipt
from openclass_core.services.source_content_formats import read_original_units
from openclass_core.services.source_content_scope import ensure_unit_authorized

def native_selection_range(*, source, structure, chapter, selection, path):
    digest = source.metadata.get('content_hash')
    if (structure.owner_user_id, structure.package_id, structure.source_ingestion_id) != (source.owner_user_id, source.package_id, source.id):
        raise GoogleSourceError('The original catalog belongs to another source authorization.')
    if structure.source_content_hash and structure.source_content_hash != digest:
        raise GoogleSourceError('The original catalog hash no longer matches the file.')
    if chapter is not None and ((chapter.owner_user_id, chapter.package_id, chapter.source_ingestion_id) != (source.owner_user_id, source.package_id, source.id) or (chapter.source_content_hash and chapter.source_content_hash != digest) or (chapter.catalog_version and chapter.catalog_version != structure.catalog_version)):
        raise GoogleSourceError('The original chapter identity or catalog version changed.')
    if chapter is None or (chapter.anchor_status != 'verified' and chapter.mapping_status != 'verified'):
        raise GoogleSourceError('Select a verified original source chapter.')
    if chapter.range is not None:
        scope = chapter.range.model_dump(mode='json', exclude_none=True)
    elif Path(source.file_name).suffix.lower() in {'.md', '.markdown', '.txt'} and structure.strategy in {'markdown_heading', 'linear_text'}:
        text = path.read_bytes().decode('utf-8')
        start, end = (chapter.body_start_offset, chapter.body_end_offset)
        if type(start) is not int or type(end) is not int or (not 0 <= start < end <= len(text)) or (start and text[start - 1] != '\n') or (end < len(text) and text[end - 1] != '\n'):
            raise GoogleSourceError('The legacy native chapter has no exact original line boundaries. Rebuild its catalog.')
        scope = {'kind': 'text_lines', 'start': text[:start].count('\n') + 1, 'end': text[:end].count('\n') + (0 if text[end - 1] == '\n' else 1)}
    else:
        raise GoogleSourceError('The legacy chapter has no exact original coordinates. Rebuild its original-content catalog.')
    if selection.source_range is not None:
        submitted = selection.source_range.model_dump(mode='json', exclude_none=True)
        clean = lambda value: {key: val for key, val in value.items() if val not in (None, '', [], {})}
        if clean(submitted) != clean(scope):
            raise GoogleSourceError('The selected original range was changed. Select it again.')
    if selection.catalog_version is not None and selection.catalog_version != structure.catalog_version:
        raise GoogleSourceError('The original catalog changed. Select the chapter again.')
    return scope

def iter_native_original_batches(*, source, structure, source_range, owner_user_id, package_id, is_cancelled=None, authorized_range=None):
    from openclass_core.services.source_ingestion_service import source_download_path
    digest = source.metadata.get('content_hash')
    if (source.owner_user_id, source.package_id) != (owner_user_id, package_id) or not digest:
        raise GoogleSourceError('The native original belongs to another authorization.')
    version = source.metadata.get('source_version') or f'original:{digest}:catalog:{structure.catalog_version}'
    offset = 0
    while True:
        if is_cancelled and is_cancelled():
            raise GoogleSourceError('Original source reading was cancelled.')
        path = source_download_path(source)
        if path is None or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise GoogleSourceError('The original source file is unavailable or its content has changed.')
        output = read_original_units(source=source, original_path=path, artifact={}, source_range=source_range, cursor=offset)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise GoogleSourceError('The original file changed during reading.')
        items, images, manifest = ([], [], [])
        for part in output['parts']:
            coordinate = part['locator']
            ensure_unit_authorized(coordinate, authorized_range or source_range)
            raw = part.get('text', '').encode() if part['kind'] == 'text' else base64.b64decode(part['data_url'].partition(',')[2], validate=True)
            sha = hashlib.sha256(raw).hexdigest()
            identity = 'evidence_' + content_digest({'source': source.id, 'version': version, 'range': coordinate, 'content': sha})[:24]
            item = RetrievalEvidence(id=identity, source_ingestion_id=source.id, source_title=source.title, excerpt=part.get('text', '')[:1600], expanded_text=part.get('text', ''), token_count=max(1, len(raw) // 3) if part['kind'] == 'text' else 0, metadata={'source_version': version, 'source_content_hash': digest, 'catalog_version': structure.catalog_version, 'source_range': coordinate, 'source_read_request': source_range, 'content_unit_sha256': sha, 'provenance': {**part.get('provenance', {}), 'reader': 'authenticated_native_original'}, 'read_contract': 'source_read_v1', 'input_kind': part['kind']})
            items.append(item)
            if part['kind'] == 'image':
                manifest.append({'image_index': len(images), 'evidence_id': identity, 'source_id': source.id, 'source_range': coordinate, 'sha256': sha, 'size_bytes': len(raw), 'mime_type': part['data_url'][5:].split(';', 1)[0]})
                images.append(part['data_url'])
        receipt = issue_read_receipt(items=items, owner_user_id=owner_user_id, package_id=package_id, source_id=source.id, source_version=version, content_hash=digest, source_range=source_range, authorized_range=authorized_range or source_range, authorization='explicit', grant_id='native:' + content_digest(source_range), role='legacy_native_reader')
        yield SourceReadResult(evidence_items=items, image_inputs=images, image_manifest=manifest, complete=output['complete'], receipt=receipt, coverage=[{'source_id': source.id, 'read_range': source_range, 'complete': output['complete']}])
        next_offset = output.get('next_cursor')
        if next_offset is None:
            if not output['complete']:
                raise GoogleSourceError('Original source reading is incomplete without a continuation.')
            return
        if not isinstance(next_offset, int) or next_offset <= offset:
            raise GoogleSourceError('The original reader made no progress.')
        offset = next_offset
