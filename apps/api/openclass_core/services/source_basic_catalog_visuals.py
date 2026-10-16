"""Read the image-only pages of a mixed PDF in the selected catalog role."""
import hashlib
import json
from openclass_core.services.google_sources.config import GoogleSourceError

def verify_catalog_visuals(service, source, data, adapter, *, checkpoint, save, check_budget=None):
    if data['format'] != 'pdf':
        return ([], [], 0)
    from openclass_core.services.google_sources.original_cache import original_path
    from openclass_core.services.source_content_reader import _pdf_parts
    from openclass_core.services.source_basic_catalog import BasicCatalogObservation, PROMPT
    from openclass_core.services.source_ingestion_jobs import source_task_cancel_requested
    from openclass_core.services import pdf_backend as fitz
    from openclass_core.services.source_processing_metrics import model_call, phase
    path = original_path(source)
    if hashlib.sha256(path.read_bytes()).hexdigest() != data['content_hash']:
        raise GoogleSourceError('The original PDF changed during catalog verification.')
    with fitz.open(path) as pdf:
        text_pages = {unit['locator']['start'] for unit in data['units'] if unit['text'].strip()}
        destinations = {entry['range']['start'] for entry in data.get('native_entries', []) if entry['range'].get('kind') == 'pdf_pages'}
        units = [unit for unit in data['units'] if not unit['text'].strip() and unit['locator']['start'] not in text_pages and (not destinations or unit['locator']['start'] in destinations) and (pdf[unit['locator']['start'] - 1].get_images() or pdf[unit['locator']['start'] - 1].get_drawings())]
    verified = list(checkpoint.get('visual_verified_units', []))
    rows = []
    calls = 0
    pending = [unit for unit in units if unit['id'] not in verified]
    read_pages = 0
    while pending:
        service._check(source)
        if read_pages >= 60:
            raise GoogleSourceError('The original-page catalog budget was reached. Saved verified pages can be resumed.')
        batch = pending[:6]
        pending = pending[6:]
        read_pages += len(batch)
        images, manifest = ([], [])
        for unit in batch:
            page = unit['locator']['start']
            output = _pdf_parts(path, source, {}, {'kind': 'pdf_pages', 'start': page, 'end': page}, 0, 1, 40 * 1024 * 1024)
            if len(output['parts']) != 1:
                raise GoogleSourceError('An original catalog image page could not be delivered.')
            images.append(output['parts'][0]['data_url'])
            manifest.append({'unit_id': unit['id'], 'physical_page': page, 'image_index': len(images) - 1})
        if check_budget:
            check_budget()
        model_call(service, source)
        with phase(service, source, 'directory_image_model'):
            result = adapter.parse_structured(system_prompt=PROMPT + '\nRead every attached original page. image_index maps that page to its unit_id. Native headings on these pages may have no text layer. Return their actual starts using those unit IDs.', user_prompt=json.dumps({'units': [[unit['id'], unit['text']] for unit in batch], 'original_images': manifest, 'native_navigation': data.get('native_entries', [])}, ensure_ascii=False), schema=BasicCatalogObservation, image_inputs=images, preserve_image_bytes=True, is_cancelled=source_task_cancel_requested, allow_live_web_search=False)
        model_call(service, source, result)
        observed = result.output_parsed
        observed = BasicCatalogObservation.model_validate(observed)
        calls += 1
        if not observed.complete or observed.unread_unit_ids:
            raise GoogleSourceError('The directory role could not read every original image page in this batch.')
        ids = {unit['id'] for unit in batch}
        if any((row.start_unit not in ids for row in observed.rows)):
            raise GoogleSourceError('The image directory role returned a body position outside its delivered pages.')
        rows.extend(observed.rows)
        verified.extend(ids)
        service._check(source)
        save(rows, verified, calls)
    return (rows, verified, calls)
