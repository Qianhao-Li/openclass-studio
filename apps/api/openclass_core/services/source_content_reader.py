"""Read authenticated originals after retrieval has supplied a location.

No search excerpt is an output of this module. Images are transient model inputs;
only hashes, ranges and receipt identities belong in runtime metadata.
"""
from __future__ import annotations
import base64
import hashlib
import json
import math
import tempfile
from pathlib import Path
from openclass_core.models import RetrievalEvidence
from openclass_core.services.source_content_contract import SourceLocator, SourceReadBudget, SourceReadResult, content_digest, issue_read_receipt, verified_locator
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_retrieval_preferences import auto_source_retrieval_enabled

def _range(value):
    return value.model_dump(mode='json') if hasattr(value, 'model_dump') else dict(value) if value else None

def _load_artifact(service, source):
    ref = source.metadata.get('google_artifact')
    if not ref:
        return {}
    service._verify_blob_binding(source, ref)
    result = service.storage.json(ref)
    if any((result.get(key) != expected for key, expected in {'source_id': source.id, 'source_version': source.metadata['source_version'], 'content_hash': source.metadata['content_hash']}.items())):
        raise GoogleSourceError('The parsed source snapshot does not match the original version.')
    result.pop('_original_read_exception', None)
    result.pop('_read_snapshots', None)
    return result

def _check_pdf_layout(artifact, requested):
    covered = set()
    for part in artifact.get('parts', []):
        if not part.get('layout_verified') or not part.get('processing_complete'):
            continue
        start = int(part.get('page_offset', 0)) + 1
        covered.update(range(start, start + int(part.get('page_count', 0))))
    if not set(range(requested['start'], requested['end'] + 1)) <= covered:
        raise GoogleSourceError('The requested original pages have not all completed Google Layout Parser processing.')

def _anchor_clip(artifact, requested, page_no, *, page_sha256=''):
    """Only normalized cloud coordinates can authorize a partial original page."""
    from openclass_core.services.google_sources.catalog_structure import cloud_blocks
    metadata = requested.get('metadata', {})
    clips = metadata.get('page_clips', {})
    if str(page_no) in clips:
        clip = clips[str(page_no)]
        if metadata.get('boundary_precision') != 'verified_visual_region' or metadata.get('boundary_verification_passes') != 2 or metadata.get('boundary_verified_image_hashes', {}).get(str(page_no)) != page_sha256 or (not isinstance(clip, list)) or (len(clip) != 4) or any((isinstance(value, bool) or not isinstance(value, (float, int)) or (not math.isfinite(value)) for value in clip)) or (not (0 <= clip[0] < clip[2] <= 1 and 0 <= clip[1] < clip[3] <= 1)):
            raise GoogleSourceError('The original-page crop has no matching verified image and boundary proof.')
        return clip
    anchors = [(requested.get('start_anchor'), 'start'), (requested.get('end_anchor'), 'end')]
    top, bottom = (0.0, 1.0)
    blocks = cloud_blocks(artifact.get('parts', []))
    for identity, edge in anchors:
        if not identity:
            continue
        found = [block for block in blocks if block['ref'] == identity]
        if len(found) != 1:
            raise GoogleSourceError('The source chapter boundary no longer matches its original location.')
        block = found[0]
        if block['page_start'] != page_no:
            continue
        bounds = block.get('bounding_box') or {}
        vertices = bounds.get('normalizedVertices') if isinstance(bounds, dict) else None
        if not vertices or not all((isinstance(v, dict) and 0 <= v.get('x', 0) <= 1 and (0 <= v.get('y', 0) <= 1) for v in vertices)):
            raise GoogleSourceError('This chapter shares a page with other content but has no verified crop boundary. Select an explicit page range or rebuild its catalog.')
        y = min((v.get('y', 0) for v in vertices))
        if edge == 'start':
            top = max(top, y)
        else:
            bottom = min(bottom, y)
    if not top < bottom:
        raise GoogleSourceError('The source page boundaries overlap or are reversed.')
    return [0.0, top, 1.0, bottom] if top or bottom < 1 else None

def _pdf_parts(path, source, artifact, requested, offset, max_units, max_image_bytes):
    from openclass_core.services.pdf_page_visual_input import PdfPageVisualReader
    with PdfPageVisualReader(path, expected_sha256=source.metadata['content_hash']) as reader:
        requested = requested or {'kind': 'pdf_pages', 'start': 1, 'end': reader.page_count}
        start, end = (requested.get('start'), requested.get('end'))
        if requested.get('kind') != 'pdf_pages' or type(start) is not int or type(end) is not int or (not 1 <= start <= end <= reader.page_count):
            raise GoogleSourceError('The original PDF requires a valid physical page range.')
        if not 0 <= offset <= end - start + 1:
            raise GoogleSourceError('The original-page cursor is outside the requested range.')
        pages = list(range(start + offset, min(end + 1, start + offset + max_units)))
        from openclass_core.services.google_sources.catalog_native_pdf import native_range_clips
        native_clips = native_range_clips(path, source, requested) or {}
        parts, used_bytes = ([], 0)
        for number in pages:
            image = reader.render_pages([number])[0]
            crop = native_clips.get(str(image.page_number)) or _anchor_clip(artifact, requested, image.page_number, page_sha256=image.sha256)
            data_url = image.data_url
            if crop:
                import io
                from PIL import Image
                with Image.open(io.BytesIO(base64.b64decode(data_url.partition(',')[2]))) as full:
                    box = (math.ceil(crop[0] * full.width), math.ceil(crop[1] * full.height), math.floor(crop[2] * full.width), math.floor(crop[3] * full.height))
                    if box[2] <= box[0] or box[3] <= box[1]:
                        raise GoogleSourceError('The verified source boundary has no readable pixels.')
                    output = io.BytesIO()
                    full.crop(box).save(output, format='PNG')
                    data_url = 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode()
            size = len(base64.b64decode(data_url.partition(',')[2]))
            if used_bytes + size > max_image_bytes:
                if not parts:
                    raise GoogleSourceError("One original page exceeds this model channel's transfer limit. Its image will not be reduced or replaced with text.")
                break
            used_bytes += size
            parts.append({'kind': 'image', 'data_url': data_url, 'locator': {'kind': 'pdf_pages', 'start': image.page_number, 'end': image.page_number, **{key: requested[key] for key in ('start_anchor', 'end_anchor', 'metadata') if requested.get(key)}}, 'provenance': {'representation': 'original_pdf_page_images', 'render_dpi': 180, **({'crop_bbox': crop} if crop else {})}})
        next_offset = offset + len(parts)
        return {'parts': parts, 'range': requested, 'next_cursor': next_offset if start + next_offset <= end else None, 'complete': start + next_offset > end, 'missing': [], 'unit_count': len(parts)}

def _prepare_format_input(service, source, path, artifact, directory):
    suffix = Path(source.file_name).suffix.lower()
    if suffix not in {'.doc', '.ppt', '.xls', '.odt', '.odp', '.ods', '.rtf'}:
        return path
    target = '.xlsx' if suffix in {'.xls', '.ods'} else '.pptx' if suffix in {'.ppt', '.odp'} else '.docx'
    mime = {'.docx': 'wordprocessingml', '.pptx': 'presentationml', '.xlsx': 'spreadsheetml'}[target]
    parts = [part for part in artifact.get('parts', []) if part.get('locator', {}).get('original_sha256') == source.metadata['content_hash'] and mime in part.get('input', {}).get('mime_type', '')]
    if len(parts) != 1:
        raise GoogleSourceError('The original Office file has no unambiguous verified conversion snapshot.')
    ref = parts[0]['input']
    service._verify_blob_binding(source, ref)
    converted = Path(directory) / ('converted' + target)
    converted.write_bytes(service.storage.read(ref))
    return converted

def read_source_content(service, *, owner_user_id, package_id, source_id, content_hash, source_range=None, locator=None, cursor=None, authorization='explicit', grant_id='', role='chatbot', budget=None, is_cancelled=None, max_image_bytes=40 * 1024 * 1024, image_limits=None):
    if authorization not in {'automatic', 'explicit'}:
        raise GoogleSourceError('The source reading authorization is invalid.')
    budget = budget or SourceReadBudget()
    pinned_version = None

    def check():
        if is_cancelled and is_cancelled():
            raise GoogleSourceError('Original source reading was cancelled.')
        if authorization == 'automatic' and (not auto_source_retrieval_enabled(owner_user_id, service.store.path)):
            raise GoogleSourceError('Automatic source search was switched off.')
        current = service.get(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id, content_hash=content_hash, ready=False)
        if current.metadata.get('external_turn_temporary'):
            raise GoogleSourceError('Add this external resource to Sources before reading its content.')
        if current.metadata.get('content_hash') != content_hash or (pinned_version is not None and current.metadata.get('source_version') != pinned_version):
            raise GoogleSourceError('The original source identity changed during reading.')
        service._check(current)
        return current
    source = check()
    pinned_version = source.metadata.get('source_version')
    if source.status != 'ready' and (not source.metadata.get('original_read_ready')):
        raise GoogleSourceError('The original source is not ready for verified reading yet.')
    requested = _range(source_range)
    location = SourceLocator.model_validate(locator) if locator is not None else None
    if location is not None:
        if not verified_locator(location) or location.source_id != source_id or location.content_hash != content_hash or (location.source_version != source.metadata['source_version']):
            raise GoogleSourceError('The source location is invalid, modified or belongs to an older source version.')
        if requested is not None and requested != location.source_range:
            raise GoogleSourceError('The requested reading range differs from the authenticated search location.')
        requested = dict(location.source_range)
    scope_key = content_digest({'source_id': source_id, 'version': source.metadata['source_version'], 'hash': content_hash, 'range': requested, 'grant': grant_id, 'parser_artifact': source.metadata.get('basic_content_artifact', {}).get('sha256')})
    prefix = scope_key + ':'
    try:
        if cursor and (not cursor.startswith(prefix)):
            raise ValueError()
        offset = int(cursor[len(prefix):]) if cursor else 0
        if offset < 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise GoogleSourceError('The source reading cursor belongs to another range or authorization.') from None
    budget.begin_read()
    pdf = source.mime_type == 'application/pdf' or source.file_name.lower().endswith('.pdf')
    cloud_enabled = auto_source_retrieval_enabled(owner_user_id, service.store.path)
    ordinary = source.metadata.get('processing_mode') == 'ordinary'
    cloud_location = (requested or {}).get('kind') == 'cloud_location'
    artifact = {} if ordinary and (not cloud_location) or (pdf and (not cloud_enabled)) else _load_artifact(service, source)
    if authorization == 'explicit' and (not cloud_enabled):
        artifact = {**artifact, '_original_read_exception': 'explicit_switch_off'}
    if ordinary:
        artifact = {**artifact, '_original_read_exception': 'ordinary_source'}
    from openclass_core.services.google_sources.catalog_fallback import ensure_original_cache
    source, original_path = ensure_original_cache(service, source)
    if hashlib.sha256(original_path.read_bytes()).hexdigest() != content_hash:
        raise GoogleSourceError('The cached original does not match the source hash.')
    visual_limit = budget.read_visual_limit
    from openclass_core.services.source_basic_cache import basic_content_available, load_basic_content, cached_content_parts
    visual_scope = bool((requested or {}).get('object') or any(((requested or {}).get('metadata', {}).get(key) for key in ('page_clips', 'crop_bbox', 'read_original_images'))))
    legacy_pdf_boundary = pdf and (requested or {}).get('metadata', {}).get('native_boundary_points') and ((requested or {}).get('metadata', {}).get('basic_unit_start') is None)
    legacy_epub = source.file_name.lower().endswith('.epub') and source.metadata.get('parser_provider') != 'anythingllm'
    basic_text = basic_content_available(source) and (not visual_scope) and (not legacy_pdf_boundary) and (not legacy_epub) and ((requested or {}).get('kind') not in {'cloud_location', 'table_query', 'structured_path', 'dom_anchor', 'sheet_cells', 'notebook_cells'})
    if basic_text:
        data = load_basic_content(service, source)
        pdf = data['format'] == 'pdf'
        output = cached_content_parts(data, requested, offset, max_chars=budget.read_character_limit, max_visual_units=visual_limit, include_visuals=True)
        for index, part in enumerate(output['parts']):
            if part['kind'] == 'text':
                part['provenance'].update(original_sha256=content_hash, parsed_artifact_sha256=source.metadata['basic_content_artifact']['sha256'])
            if part['kind'] == 'original_archive_image':
                from openclass_core.services.anythingllm.native_images import read_archive_image
                output['parts'][index] = read_archive_image(original_path, source, artifact, part)
            if part['kind'] == 'original_pdf_page':
                value = part['locator']
                bounds = (requested or {}).get('metadata', {})
                page_units = [unit for unit in data['units'] if unit['locator']['start'] == value['start']]
                if not (bounds.get('boundary_precision') == 'verified_native_region' and bounds.get('native_boundary_points')) and (bounds.get('basic_unit_start', 1) > page_units[0]['id'] or bounds.get('basic_unit_end', len(data['units'])) < page_units[-1]['id'] or (bounds.get('basic_unit_start') == page_units[0]['id'] and bounds.get('basic_start_char', 0)) or (bounds.get('basic_unit_end') == page_units[-1]['id'] and bounds.get('basic_end_char') not in (None, len(page_units[-1]['text'])))):
                    raise GoogleSourceError('This PDF text layer is incomplete and its original image cannot be safely confined to the authorized chapter boundary.')
                rendered = _pdf_parts(original_path, source, artifact, value, 0, 1, max_image_bytes)
                if not rendered['parts']:
                    raise GoogleSourceError('The original scanned PDF page could not be read.')
                output['parts'][index] = rendered['parts'][0]
    elif pdf:
        from openclass_core.services.source_pdf_native import native_pdf_parts
        output = native_pdf_parts(original_path, source, requested, offset, 6, budget.read_character_limit) if source.metadata.get('native_pdf_reading') else None
        if output is None:
            if visual_limit < 1:
                raise GoogleSourceError('The original-page reading budget is exhausted.')
            if cloud_enabled and (not ordinary):
                check_range = requested or {'start': 1, 'end': artifact.get('page_count', 0)}
                if not check_range.get('end'):
                    raise GoogleSourceError('The PDF has no verified Layout Parser page coverage yet.')
                _check_pdf_layout(artifact, {'start': check_range['start'] + offset, 'end': min(check_range['end'], check_range['start'] + offset + visual_limit - 1)})
            output = _pdf_parts(original_path, source, artifact, requested, offset, visual_limit, max_image_bytes)
    else:
        from openclass_core.services.source_content_formats import NATIVE_UNITS_PER_READ, read_original_units, SourceContentReadError
        from openclass_core.services.google_sources.original_cache import original_cache_root
        root = original_cache_root() / '.source-read'
        root.mkdir(parents=True, exist_ok=True, mode=448)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            path = _prepare_format_input(service, source, original_path, artifact, directory)
            snapshots = []
            for index, part in enumerate(artifact.get('parts', [])):
                if part.get('locator', {}).get('kind') == 'workbook_visual_pages':
                    ref = part['input']
                    service._verify_blob_binding(source, ref)
                    target = Path(directory) / f'visual-{index}.pdf'
                    target.write_bytes(service.storage.read(ref))
                    snapshots.append({'document_id': part['document_id'], 'path': str(target), 'sha256': ref['sha256']})
            if snapshots:
                artifact = {**artifact, '_read_snapshots': snapshots}
            try:
                if requested and requested.get('kind') == 'table_query':
                    from openclass_core.services.source_content_calculations import read_original_calculation
                    output = read_original_calculation(source=source, original_path=path, artifact=artifact, query_plan=requested.get('query_plan', {}))
                else:
                    format_range = location.authorized_range if location is not None and location.authorized_range is not None and (location.authorized_range.get('kind') == 'sheet_cells') else requested
                    output = read_original_units(source=source, original_path=path, artifact=artifact, source_range=format_range, cursor=offset, max_chars=budget.read_character_limit, max_units=NATIVE_UNITS_PER_READ, max_images=max(0, visual_limit), max_image_bytes=max_image_bytes)
            except SourceContentReadError as exc:
                raise GoogleSourceError(str(exc)) from exc
    check()
    if hashlib.sha256(original_path.read_bytes()).hexdigest() != content_hash:
        raise GoogleSourceError('The original changed during source reading.')
    items, images, manifests = ([], [], [])
    characters = total_image_bytes = 0
    for part in output['parts']:
        coordinate = dict(part['locator'])
        if location is not None and location.authorized_range is not None:
            from openclass_core.services.source_content_scope import ensure_unit_authorized
            ensure_unit_authorized(coordinate, location.authorized_range)
        text = str(part.get('text') or '') if part['kind'] == 'text' else ''
        raw = text.encode()
        if part['kind'] == 'image':
            try:
                raw = base64.b64decode(part['data_url'].partition(',')[2], validate=True)
            except (ValueError, KeyError):
                raise GoogleSourceError('The source image payload is invalid.') from None
            total_image_bytes += len(raw)
            if len(raw) > (image_limits.per_image if image_limits else 20 * 1024 * 1024) or total_image_bytes > (image_limits.total_images if image_limits else 40 * 1024 * 1024):
                raise GoogleSourceError('The original source images exceed the model input limit. Read fewer units.')
        digest = hashlib.sha256(raw).hexdigest()
        identity = 'evidence_' + content_digest({'source': source_id, 'version': source.metadata['source_version'], 'range': coordinate, 'content': digest})[:24]
        metadata = {'source_backend': source.metadata.get('source_backend'), 'source_version': source.metadata['source_version'], 'source_content_hash': content_hash, 'source_range': coordinate, 'source_read_request': requested, 'content_unit_sha256': digest, 'provenance': {**part.get('provenance', {}), **({'external_source': source.metadata.get('origin', {})} if source.metadata.get('external_turn_temporary') else {})}, 'catalog_version': source.metadata.get('catalog_version', 1), 'source_transport': 'basic_source_text' if basic_text and part['kind'] == 'text' else part.get('provenance', {}).get('representation', 'original_pdf_page_images') if pdf else 'original_source_content', 'read_contract': 'source_read_v1', 'input_kind': part['kind']}
        if pdf and part['kind'] == 'image':
            metadata.update(page_start=coordinate['start'], page_end=coordinate['end'], page_coordinate_system='physical_pdf_pages', page_label=f"PDF page {coordinate['start']}", page_image_sha256=digest, render_dpi=180)
            if part.get('provenance', {}).get('crop_bbox'):
                metadata['crop_bbox'] = part['provenance']['crop_bbox']
        elif pdf:
            metadata.update(page_start=coordinate['start'], page_end=coordinate['end'], page_coordinate_system='physical_pdf_pages', page_label=f"PDF page {coordinate['start']}")
        item = RetrievalEvidence(id=identity, source_ingestion_id=source_id, source_title=source.title, excerpt=text[:1600], expanded_text=text, token_count=max(1, math.ceil(len(text) / 3)), page_range=str(coordinate['start']) if pdf else '', reason='Original source content read after verified location', metadata=metadata)
        items.append(item)
        characters += len(text)
        if part['kind'] == 'image':
            data_url = part['data_url']
            manifests.append({'image_index': len(images), 'evidence_id': identity, 'source_id': source_id, 'source_content_hash': content_hash, 'source_version': source.metadata['source_version'], 'source_range': coordinate, 'physical_page': coordinate.get('start') if pdf else None, 'sha256': digest, 'mime_type': data_url[5:].split(';', 1)[0], 'size_bytes': len(raw)})
            images.append(data_url)
    budget.consume(characters=characters, visual_units=len(images))
    read_range = output.get('range') or requested or {'kind': 'structured_path', 'container': source_id}
    receipt = issue_read_receipt(items=items, owner_user_id=owner_user_id, package_id=package_id, source_id=source_id, source_version=source.metadata['source_version'], content_hash=content_hash, source_range=read_range, authorization=authorization, grant_id=grant_id, role=role, authorized_range=location.authorized_range if location is not None else requested)
    check()
    from openclass_core.services.source_prompt_projection import context_for_model
    model_context = context_for_model(items, image_manifest=manifests, coverage=[{'source_id': source_id, 'read_range': read_range, 'complete': bool(output['complete']), 'missing': output.get('missing', [])}])
    return SourceReadResult(evidence_items=items, image_inputs=images, image_manifest=manifests, receipt=receipt, source_identity={'source_id': source_id, 'source_version': source.metadata['source_version'], 'source_content_hash': content_hash}, complete=bool(output['complete']), next_cursor=prefix + str(output['next_cursor']) if output.get('next_cursor') is not None else None, missing=list(output.get('missing', [])), coverage=[{'source_id': source_id, 'source_version': source.metadata['source_version'], 'source_content_hash': content_hash, 'read_range': read_range, 'complete': bool(output['complete']), 'missing': list(output.get('missing', [])), 'read_id': receipt['read_id']}], prompt_context='Original source content; search results are only locations. ' + json.dumps(model_context, ensure_ascii=False, separators=(',', ':')))
