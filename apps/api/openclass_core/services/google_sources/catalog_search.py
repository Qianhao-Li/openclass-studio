"""Use scoped search only as a locator hint for the complete directory traversal."""
from __future__ import annotations
import json
from .config import GoogleSourceError

def catalog_locator_hints(service, source, query, *, outputs=None):
    service._check_cloud(source)
    service.config.validate(write=True)
    current = service.state.current(source)
    allowed = set(current.metadata.get('google_documents', []))
    if not allowed or not str(query).strip():
        return []
    if outputs is None:
        if current.metadata.get('google_artifact'):
            outputs = service.artifact(current)['parts']
        else:
            outputs = []
            for checkpoint in current.metadata.get('google_checkpoints', {}).values():
                if checkpoint.get('output'):
                    service._verify_blob_binding(current, checkpoint['output'])
                    outputs.append(service.storage.json(checkpoint['output']))
    verified = {part['document_id']: part for part in outputs if part.get('document_id') in allowed}
    values = {'owner_user_id': current.owner_user_id, 'package_id': current.package_id, 'source_id': current.id, 'source_version': current.metadata['source_version']}
    filters = ' AND '.join((f'{key}: ANY({json.dumps(value)})' for key, value in values.items()))
    service._check_cloud(current)
    service.config.validate(write=True)
    result = service.client.json('POST', f'{service.config.endpoint}/v1/{service.config.data_store}/servingConfigs/{service.config.serving_config}:search', json={'query': str(query).strip()[:1000], 'pageSize': 8, 'filter': filters, 'contentSearchSpec': {'searchResultMode': 'CHUNKS'}})
    hints = []
    for item in result.get('results', []):
        chunk = item.get('chunk', {})
        name = str(chunk.get('name', ''))
        if '/documents/' not in name or '/chunks/' not in name:
            raise GoogleSourceError('Catalog location search returned an unauthorized source.')
        document_name = name.split('/chunks/', 1)[0]
        part = verified.get(document_name.rsplit('/', 1)[-1])
        if part is None or document_name != part.get('document_name'):
            raise GoogleSourceError('Catalog location search returned an unauthorized or outdated source version.')
        saved = next((value for value in part['chunks'] if str(value.get('id')) == str(chunk.get('id'))), None)
        if saved is None:
            raise GoogleSourceError('The catalog location chunk does not belong to the verified parsing output.')
        span = saved.get('pageSpan', {})
        if type(span.get('pageStart')) is int and type(span.get('pageEnd')) is int:
            hints.append({'page_start': span['pageStart'], 'page_end': span['pageEnd'], 'chunk_id': str(saved['id']), 'excerpt': str(saved.get('content') or '')[:600]})
    service._check_cloud(current)
    return hints
