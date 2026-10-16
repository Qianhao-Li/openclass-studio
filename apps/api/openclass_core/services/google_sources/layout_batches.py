"""Bounded cloud batches with serialized, resumable state updates."""
from __future__ import annotations
import hashlib
import json
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from openclass_core.services import source_ingestion_jobs
from .config import GoogleSourceError
from .coverage import chunk_page_coverage, validate_output
from .storage import scope_prefix
_GLOBAL_LAYOUT_SLOTS = threading.BoundedSemaphore(4)
MAX_LAYOUT_WORKERS = 4

def process_layout_batches(service, source, parts):
    """At most four active batches, across all uploads in this process.

    Existing source versions retain their original segmentation. Only immutable
    cloud work overlaps; checkpoint/object writes go through the state coordinator.
    """
    stopped = threading.Event()
    handle = getattr(source_ingestion_jobs._CURRENT_SOURCE_TASK, 'handle', None)
    prefix = scope_prefix(source.owner_user_id, source.package_id, source.id, source.metadata['source_version'])

    def check():
        if stopped.is_set() or (handle is not None and handle.cancel_event.is_set()):
            raise GoogleSourceError('Source processing cancelled.')
        service._check_cloud(source)

    def part_work(index, part):
        while not _GLOBAL_LAYOUT_SLOTS.acquire(timeout=0.25):
            check()
        try:
            check()
            current = service.state.current(source)
            checkpoint = dict(current.metadata.get('google_checkpoints', {}).get(str(index), {}))
            if checkpoint.get('output'):
                service._verify_blob_binding(source, checkpoint['output'])
                output = service.storage.json(checkpoint['output'])
                output.update(validate_output(part, output))
                return output
            part_hash = hashlib.sha256(part.content).hexdigest()
            part_ref = service.storage.put(bucket=service.config.artifact_bucket, name=f'{prefix}/inputs/{index}-{part_hash}', content=part.content, mime_type=part.mime_type)
            check()
            document_id = hashlib.sha256(f"{source.owner_user_id}:{source.package_id}:{source.id}:{source.metadata['source_version']}:{index}".encode()).hexdigest()
            document = {'id': document_id, 'structData': {'owner_user_id': source.owner_user_id, 'package_id': source.package_id, 'source_id': source.id, 'source_version': source.metadata['source_version'], 'content_hash': source.metadata['content_hash'], 'title': source.title, 'part_index': index}, 'content': {'mimeType': part.mime_type, 'uri': part_ref['uri']}}
            service.state.update(source, metadata=lambda current: {'google_objects': [*current.get('google_objects', []), part_ref], 'google_documents': list(dict.fromkeys([*current.get('google_documents', []), document_id]))})

            def save(value):
                check()
                service.state.update(source, metadata=lambda current: {'google_checkpoints': {**current.get('google_checkpoints', {}), str(index): {**current.get('google_checkpoints', {}).get(str(index), {}), **value}}})
            parsed, chunked = service.processor.process(document, checkpoint, save=save, check=check)
            check()
            chunks = chunked['chunks']
            coverage = chunk_page_coverage(part, chunks, normalize=True)
            output = {'document_id': document_id, 'document_name': service.processor.resource + '/branches/0/documents/' + document_id, 'parsed': parsed, 'chunks': chunks, 'page_offset': part.page_offset, **coverage, 'processing_complete': True, 'locator': part.locator, 'layout_required': part.layout_required, 'layout_verified': part.layout_required, 'input': part_ref}
            result_ref = service.storage.put(bucket=service.config.artifact_bucket, name=f'{prefix}/processed/{index}.json', content=json.dumps(output, ensure_ascii=False).encode(), mime_type='application/json')
            save({'output': result_ref})
            service.state.update(source, metadata=lambda current: {'google_objects': [*current.get('google_objects', []), result_ref]})
            return output
        finally:
            _GLOBAL_LAYOUT_SLOTS.release()
    outputs = [None] * len(parts)
    pending, next_index, finished = ({}, 0, 0)
    with ThreadPoolExecutor(max_workers=min(MAX_LAYOUT_WORKERS, len(parts)), thread_name_prefix='source-layout') as pool:
        try:
            while pending or next_index < len(parts):
                check()
                while next_index < len(parts) and len(pending) < MAX_LAYOUT_WORKERS:
                    future = pool.submit(part_work, next_index, parts[next_index])
                    pending[future] = next_index
                    next_index += 1
                done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                for future in done:
                    outputs[pending.pop(future)] = future.result()
                    finished += 1
                    service._job(service.state.current(source), 'google_layout_processing', 25 + int(55 * finished / len(parts)))
        except BaseException:
            stopped.set()
            for future in pending:
                future.cancel()
            raise
    check()
    return outputs
