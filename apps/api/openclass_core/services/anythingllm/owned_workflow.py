"""Transport for the AnythingLLM-owned catalog role; no parsing or directory decisions."""
from __future__ import annotations
import hashlib
import json
from pydantic import BaseModel, ConfigDict, Field
from openclass_core.models import AIModelSelection, SourceChapter, SourceStructure
from openclass_core.services.ai_execution_adapter import build_ai_execution_adapter
from openclass_core.services.ai_model_catalog import resolve_text_model_selection, text_model_supports_image_inputs
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.google_sources.storage import scope_prefix
from openclass_core.services.source_basic_cache import _save_local_artifact
from openclass_core.services.source_ingestion_jobs import source_task_cancel_requested
from openclass_core.services.source_model_capacity import SourceInputCapacity
from openclass_core.services.source_processing_metrics import model_call, phase
from .client import source_identity
ENGINE = 'anythingllm_source_catalog_v2'

class CatalogRow(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str
    level: int = 1
    start_unit: int | None = None
    printed_page_label: str = ''
    body_unit: int | None = None
    body_heading: str | None = None
    row_index: int | None = None
    directory_unit: int | None = None

class CatalogObservation(BaseModel):
    model_config = ConfigDict(extra='forbid', title='AnythingLLMCatalogObservation')
    complete: bool
    authored_directory: bool = False
    directory_complete: bool = False
    directory_unit_ids: list[int] = Field(default_factory=list)
    rows: list[CatalogRow]
    unread_unit_ids: list[int] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)

def persist_content(service, source, content):
    """Persist the provider's returned artifact without modifying its positions."""
    if any((content.get(key) != value for key, value in {'schema': 'source_basic_content_v1', 'source_id': source.id, 'source_version': source.metadata['source_version'], 'content_hash': source.metadata['content_hash'], 'parser_provider': 'anythingllm', 'parser_version': 'anythingllm_positioned_document_v2'}.items())):
        raise GoogleSourceError('The AnythingLLM parsed artifact belongs to another original or parser.')
    units = content.get('units')
    if not isinstance(units, list) or not units or any((unit.get('id') != index for index, unit in enumerate(units, 1))):
        raise GoogleSourceError('The AnythingLLM parsed artifact has invalid original positions.')
    raw = json.dumps(content, ensure_ascii=False, separators=(',', ':')).encode()
    prefix = scope_prefix(source.owner_user_id, source.package_id, source.id, source.metadata['source_version'])
    ref = service.storage.put(bucket=service.config.artifact_bucket, name=f'{prefix}/anythingllm/{hashlib.sha256(raw).hexdigest()}.json', content=raw, mime_type='application/json')
    _save_local_artifact(ref, raw)
    visual = content.get('visual_only') or content.get('format') == 'visual'
    return service.state.update(source, status='ready', error='', metadata={'processing_mode': 'ordinary', 'basic_content_artifact': ref, 'basic_read_status': 'visual' if visual else 'ready', 'basic_read_error': '', 'basic_read_progress': 100, 'original_read_ready': True, 'basic_content_schema': content['schema'], 'basic_content_configuration': content['configuration'], 'basic_unit_count': len(units), 'source_processor': 'anythingllm', 'parser_provider': 'anythingllm', 'parser_version': content['parser_version'], 'google_objects': [*source.metadata.get('google_objects', []), ref], 'enhancement_status': source.metadata.get('enhancement_status', 'not_prepared')})

def original_images(source, path, requests):
    from openclass_core.services.source_content_reader import _pdf_parts
    from openclass_core.services.source_content_media import read_image_units
    values, receipts = ([], [])
    frames = None
    for request in requests:
        locator = request['locator']
        if locator['kind'] == 'pdf_pages':
            part = _pdf_parts(path, source, {'_original_read_exception': 'ordinary_source'}, {'kind': 'pdf_pages', 'start': locator['start'], 'end': locator['start']}, 0, 1, 40 * 1024 * 1024)['parts'][0]
        elif locator['kind'] == 'image_frames':
            frames = frames or read_image_units(source, path, {'_original_read_exception': 'ordinary_source'}, None)
            part = frames[locator['start'] - 1]['_load']()
        else:
            raise GoogleSourceError('AnythingLLM requested an unsupported original image position.')
        values.append(part['data_url'])
        receipts.append({'unit_id': request['unit_id'], 'content_hash': source.metadata['content_hash'], 'source_version': source.metadata['source_version'], 'payload_sha256': hashlib.sha256(values[-1].encode()).hexdigest()})
    return (values, receipts)

def prepare_images(source, path, request, capacity):
    images, receipts = ([], [])
    for image in request.get('original_image_requests', []):
        if source_task_cancel_requested():
            raise GoogleSourceError('Directory generation was cancelled.')
        values, proof = original_images(source, path, [image])
        if not capacity.fits_request(request['system_prompt'], request['user_prompt'], CatalogObservation, [*images, *values]):
            return (images, receipts, image['unit_id'])
        images.extend(values)
        receipts.extend(proof)
    return (images, receipts, None)

def fit_original_batch(source, path, client, task, capacity):
    while True:
        if source_task_cancel_requested():
            raise GoogleSourceError('Directory generation was cancelled.')
        request = task['request']
        fits_text = capacity.fits_request(request['system_prompt'], request['user_prompt'], CatalogObservation)
        images, receipts, before_unit = prepare_images(source, path, request, capacity) if fits_text else ([], [], None)
        if fits_text and before_unit is None:
            return (task, images, receipts)
        units = request['unit_ids']
        if before_unit is None:
            if len(units) < 2:
                raise GoogleSourceError('The selected catalog model cannot accept this indivisible original object within its token and transfer capacity.')
            before_unit = units[max(1, len(units) // 2)]
        reduced = client.request('/catalog/owned/resize', {'source': source_identity(source), 'task_id': task['task_id'], 'request_hash': request['request_hash'], 'before_unit': before_unit})
        remaining = reduced['request']['unit_ids']
        if not remaining or len(remaining) >= len(units) or (not set(remaining) < set(units)):
            raise GoogleSourceError('The selected catalog model cannot accept this indivisible original object within its token and transfer capacity.')
        task = reduced

def run_catalog(service, source, path, client, model_adapter=None):
    selection = resolve_text_model_selection(AIModelSelection.model_validate(source.metadata['catalog_model_selection']), user_id=source.owner_user_id)
    adapter = model_adapter or build_ai_execution_adapter(selection, owner_user_id=source.owner_user_id)
    capacity = SourceInputCapacity(selection, adapter)
    packet = {'source': source_identity(source), 'options': {'model': selection.model_dump(mode='json'), 'max_input_bytes': capacity.max_input_bytes, 'supports_images': text_model_supports_image_inputs(selection, user_id=source.owner_user_id), 'max_image_count': capacity.max_image_count}}
    task = client.request('/catalog/owned/start', packet)
    try:
        while task.get('request'):
            service._check(source)
            if source_task_cancel_requested():
                raise GoogleSourceError('Source catalog processing was cancelled.')
            request = task['request']
            if request.get('schema', {}).get('title') != 'AnythingLLMCatalogObservation':
                raise GoogleSourceError('The AnythingLLM model request has an incompatible catalog schema.')
            source = service.state.update(source, metadata={'catalog_owner': 'anythingllm', 'catalog_engine': ENGINE, 'catalog_progress': task['progress'], 'catalog_read_unit_count': task['read_units'], 'catalog_total_unit_count': task['total_units'], 'model_call_count': task['model_calls'], 'catalog_recognized_count': task['recognized_count']})
            service._job(source.model_copy(update={'status': 'parsing'}), 'anythingllm_' + task['phase'], task['progress'])
            task, images, receipts = fit_original_batch(source, path, client, task, capacity)
            request = task['request']
            if not capacity.fits_request(request['system_prompt'], request['user_prompt'], CatalogObservation, images):
                raise GoogleSourceError('The selected catalog model cannot accept this verified original batch.')
            model_call(service, source)
            with phase(service, source, 'anythingllm_directory_model'):
                result = adapter.parse_structured(system_prompt=request['system_prompt'], user_prompt=request['user_prompt'], schema=CatalogObservation, image_inputs=images or None, preserve_image_bytes=bool(images), is_cancelled=source_task_cancel_requested, allow_live_web_search=False)
            model_call(service, source, result)
            service._check(source)
            task = client.request('/catalog/owned/advance', {'source': source_identity(source), 'task_id': task['task_id'], 'request_hash': request['request_hash'], 'output': CatalogObservation.model_validate(result.output_parsed.model_dump(mode='json') if isinstance(result.output_parsed, BaseModel) else result.output_parsed).model_dump(mode='json'), 'image_receipts': receipts})
            if task.get('result') and task.get('status') == 'processing':
                value = task['result']
                intermediate = SourceStructure.model_validate(value['structure'])
                source = service.state.publish(source, structure_store=service.structure_store, structure=intermediate, chapters=[SourceChapter.model_validate(row) for row in value['chapters']], metadata={**value['metadata'], 'catalog_ready': True, 'anythingllm_catalog_ready': True, 'catalog_status': 'running', 'catalog_progress': task['progress']})
        if task.get('engine') != ENGINE or task.get('status') != 'ready' or (not task.get('result')):
            raise GoogleSourceError('AnythingLLM has not verified its complete original directory.')
        value = task['result']
        return (SourceStructure.model_validate(value['structure']), [SourceChapter.model_validate(row) for row in value['chapters']])
    except Exception:
        if task.get('task_id'):
            try:
                client.request('/catalog/owned/stop', {'source': source_identity(source), 'task_id': task['task_id'], 'reason': 'cancelled' if source_task_cancel_requested() else 'Directory execution failed; completed batches remain saved.'})
            except GoogleSourceError:
                pass
        raise
