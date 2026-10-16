from __future__ import annotations
import json
import time
from collections.abc import Callable
from .client import GoogleCloudClient
from .config import GoogleSourceError, GoogleSourceUnavailableError

class GoogleLayoutProcessor:

    def __init__(self, client: GoogleCloudClient):
        self.client = client
        self.config = client.config
        self.resource = self.config.data_store

    def verify_configuration(self) -> None:
        datastore = self.client.json('GET', f'{self.config.endpoint}/v1/{self.config.data_store}')
        config = datastore.get('documentProcessingConfig', {})
        returned_name = str(datastore.get('name') or '')
        expected_suffix = self.config.data_store.split('/locations/', 1)[1]
        if returned_name and returned_name.startswith('projects/') and (returned_name.split('/locations/', 1)[-1] == expected_suffix):
            self.resource = returned_name
        if not config.get('chunkingConfig', {}).get('layoutBasedChunkingConfig'):
            raise GoogleSourceError('Layout-based chunking is not enabled in the Google data store. Please update its configuration.')
        parsing = config.get('defaultParsingConfig', {}).get('layoutParsingConfig')
        if not isinstance(parsing, dict) or not all((parsing.get(key) for key in ('enableImageAnnotation', 'enableTableAnnotation', 'enableGetProcessedDocument'))):
            raise GoogleSourceError('The Google data store must enable Layout Parser, image and table descriptions, and processed-document access.')
        for file_type, override in config.get('parsingConfigOverrides', {}).items():
            if file_type in {'pdf', 'html', 'docx', 'pptx', 'xlsx', 'xlsm'} and override != {'layoutParsingConfig': parsing}:
                candidate = override.get('layoutParsingConfig', {})
                if not all((candidate.get(key) for key in ('enableImageAnnotation', 'enableTableAnnotation', 'enableGetProcessedDocument'))):
                    raise GoogleSourceError('A format override in the Google parsing configuration bypasses the required Layout Parser.')

    def process(self, document: dict, checkpoint: dict, *, save: Callable[[dict], None], check: Callable[[], None]) -> tuple[dict, dict]:
        check()
        name = self.resource + '/branches/0/documents/' + document['id']
        if not checkpoint.get('operation'):
            if checkpoint.get('submitting'):
                existing = self.client.request('GET', f'{self.config.endpoint}/v1/{name}', allow_status=(404,))
                if existing.status_code == 404:
                    raise GoogleSourceError('The previous cloud import submission has an unknown result. Check that operation before retrying to avoid duplicate charges.')
            else:
                save({'submitting': True})
                response = self.client.json('POST', f'{self.config.endpoint}/v1/{self.config.documents}:import', json={'inlineSource': {'documents': [document]}, 'reconciliationMode': 'INCREMENTAL'})
                operation = response.get('name')
                if not operation:
                    raise GoogleSourceError('Google did not return a resumable import operation ID.')
                checkpoint = {'operation': operation, 'submitting': False}
                save(checkpoint)
        if checkpoint.get('operation'):
            result = self.wait_operation(checkpoint['operation'], check=check)
            if result.get('error') or result.get('response', {}).get('errorSamples'):
                raise GoogleSourceError('Google import or Layout parsing failed. Check the cloud operation error.')
        parsed = self.get_processed(name, 'PARSED_DOCUMENT', check=check)
        chunked = self.get_processed(name, 'CHUNKED_DOCUMENT', check=check)
        if not isinstance(chunked.get('chunks'), list):
            raise GoogleSourceError('Google returned no searchable content. Source indexing is incomplete.')
        return (parsed, chunked)

    def wait_operation(self, operation: str, *, check: Callable[[], None], speech: bool=False) -> dict:
        allowed_projects = {self.config.project_id, self.resource.split('/')[1]}
        if not any((operation.startswith(f'projects/{project}/locations/') for project in allowed_projects)) or any((item in operation for item in ('..', '?', '#'))):
            raise GoogleSourceError('The operation ID returned by Google does not belong to the current project.')
        endpoint = f'https://{self.config.speech_location}-speech.googleapis.com/v2' if speech else f'{self.config.endpoint}/v1'
        deadline = time.monotonic() + self.config.timeout_seconds
        while True:
            check()
            result = self.client.json('GET', f'{endpoint}/{operation}')
            if result.get('done'):
                if result.get('error'):
                    code = result['error'].get('code', 'unknown')
                    raise GoogleSourceError(f'Google source processing failed (status {code}).')
                return result
            if time.monotonic() >= deadline:
                raise GoogleSourceError('Google source processing is still running. Retrying later will resume the same cloud operation.')
            time.sleep(self.config.poll_seconds)

    def get_processed(self, name: str, kind: str, *, check: Callable[[], None]) -> dict:
        deadline = time.monotonic() + self.config.timeout_seconds
        while True:
            check()
            response = self.client.request('GET', f'{self.config.endpoint}/v1alpha/{name}:getProcessedDocument', params={'processedDocumentType': kind}, allow_status=(404, 409, 503))
            if response.status_code not in {404, 409, 503}:
                try:
                    envelope = response.json()
                    if envelope.get('document') != name:
                        raise GoogleSourceError('The Google processed document identity does not match.')
                    data = json.loads(envelope['jsonData'])
                    if not isinstance(data, dict):
                        raise ValueError()
                    if kind == 'CHUNKED_DOCUMENT':
                        data = data.get('chunkedDocument', data)
                        if not isinstance(data, dict):
                            raise ValueError()
                        for chunk in data.get('chunks', []):
                            if not isinstance(chunk, dict):
                                raise ValueError()
                            chunk['id'] = chunk.get('id') or chunk.get('chunkId')
                            if not chunk['id']:
                                raise ValueError()
                    elif kind == 'PARSED_DOCUMENT' and 'documentLayout' in data:
                        layout = data['documentLayout']
                        if not isinstance(layout, dict):
                            raise ValueError()
                        data = {**data, 'blocks': layout.get('blocks', [])}
                    return data
                except (KeyError, ValueError, TypeError) as exc:
                    raise GoogleSourceError('The Google processed document format is invalid.') from exc
            if time.monotonic() >= deadline:
                if response.status_code == 503:
                    raise GoogleSourceUnavailableError('Google processed-document access is temporarily unavailable. The operation has been preserved; catalog generation can proceed separately.')
                raise GoogleSourceError('The Google processed document is not readable yet. The operation has been saved; please retry later.')
            time.sleep(self.config.poll_seconds)

def normalized_blocks(parsed: dict) -> list[dict]:
    """Flatten cloud layout without dropping table/list leaves or inventing OCR.

    Keep Google's blockId when present; structural path IDs are deterministic
    for formats that omit it. Heading page spans can cover their child subtree,
    so precise reading should prefer bounded leaf blocks.
    """
    result = []

    def visit(blocks, parents=(), path='blocks', inherited=None, context=None):
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            key = f'{path}/{index}'
            text_block = block.get('textBlock', {})
            text = str(block.get('text') or block.get('content') or text_block.get('text') or '')
            span = block.get('pageSpan') or inherited or {}
            start = span.get('pageStart', block.get('pageStart'))
            end = span.get('pageEnd', block.get('pageEnd', start))
            kind = str(block.get('type') or text_block.get('type') or ('TABLE' if 'tableBlock' in block else 'LIST' if 'listBlock' in block else '')).upper().replace('-', '_')
            identity = block.get('blockId') or block.get('id') or 'layout:' + key
            item = {'id': str(identity), 'text': text, 'type': kind, 'page_start': start, 'page_end': end, 'parents': list(parents), 'block_path': key, **(context or {})}
            bounds = block.get('boundingBox') or text_block.get('boundingBox')
            if bounds:
                item['bounding_box'] = bounds
            result.append(item)
            child_parents = (*parents, text) if kind.startswith('HEADING') else parents
            for child_key, children in (('blocks', block.get('blocks')), ('children', block.get('children')), ('textBlock/blocks', text_block.get('blocks'))):
                if isinstance(children, list):
                    visit(children, child_parents, f'{key}/{child_key}', span, context)
            table = block.get('tableBlock', {})
            for rows_key in ('headerRows', 'bodyRows'):
                for row_index, row in enumerate(table.get(rows_key, [])):
                    for column, cell in enumerate(row.get('cells', [])):
                        visit(cell.get('blocks', []), child_parents, f'{key}/tableBlock/{rows_key}/{row_index}/cells/{column}/blocks', span, {'table_id': str(identity), 'table_row': row_index, 'table_column': column, 'table_row_kind': rows_key})
            listing = block.get('listBlock', {})
            for entry_index, entry in enumerate(listing.get('listEntries', listing.get('entries', []))):
                visit(entry.get('blocks', []), child_parents, f'{key}/listBlock/listEntries/{entry_index}/blocks', span, {'list_id': str(identity), 'list_item': entry_index})
    visit(parsed.get('blocks', parsed.get('documentLayout', {}).get('blocks', [])))
    return result
