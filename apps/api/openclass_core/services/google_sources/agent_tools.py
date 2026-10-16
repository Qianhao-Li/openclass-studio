from __future__ import annotations
import json
from openclass_core.models import SourceQueryRef
from openclass_core.services.source_retrieval_preferences import auto_source_retrieval_enabled
from .config import GoogleSourceError
from .service import get_google_source_service
TOOLS = {'list_sources', 'search_sources', 'get_source_catalog', 'read_source_content', 'read_cloud_source_range', 'query_source_table'}
MAX_TOOL_PAYLOAD_BYTES = 16 * 1024 * 1024

def _read_delivery(result, task, *, calculation=False):
    """Validate the exact original-content payload before it earns citation numbers."""
    from openclass_core.services.original_source_delivery import original_batch, bind_original_source_batch, validate_original_source_call
    batch = original_batch(result, owner_user_id=task['owner_user_id'], package_id=task['package_id'])
    from openclass_core.services.source_content_contract import verified_read_evidence
    payload = result.tool_payload()
    items = list(result.evidence_items)
    if any((not verified_read_evidence(item) for item in items)):
        raise GoogleSourceError('The source read has no valid original-content receipt.')
    serialized = [item.model_dump(mode='json') for item in items]
    if payload.get('items') != serialized:
        raise GoogleSourceError('The delivered source content does not match its read receipt.')
    if calculation:
        if len(items) != 1 or not payload.get('complete'):
            raise GoogleSourceError('The original worksheet calculation is incomplete and cannot be cited.')
        try:
            value = json.loads(items[0].expanded_text or '')
        except (TypeError, ValueError) as exc:
            raise GoogleSourceError('The original worksheet calculation returned an invalid result.') from exc
        if not isinstance(value, dict):
            raise GoogleSourceError('The original worksheet calculation returned an invalid result.')
        payload['calculation'] = value
    images = payload.get('images', [])
    if result.image_inputs and (not images):
        raise GoogleSourceError('Original source images could not be delivered to the model.')
    if images:
        from openclass_core.models import AIModelSelection
        from openclass_core.services.ai_model_catalog import text_model_supports_image_inputs
        if not text_model_supports_image_inputs(AIModelSelection.model_validate(task.get('model') or {}), user_id=task['owner_user_id'], **{'frozen_capabilities': task['model']['capability_snapshot']} if 'capability_snapshot' in task.get('model', {}) else {}):
            raise GoogleSourceError('The selected model cannot read original source images. Choose a model that supports image input.')
    if any((not isinstance(item, dict) or not item.get('data') or (item.get('mimeType') or item.get('mime_type')) not in {'image/png', 'image/jpeg', 'image/webp', 'image/gif'} for item in images)):
        raise GoogleSourceError('The original source image payload is invalid.')
    from openclass_core.services.source_prompt_projection import tool_payload_for_model
    wire = tool_payload_for_model(payload)
    wrapped = {**wire, 'text': json.dumps({key: value for key, value in wire.items() if key != 'images'}, ensure_ascii=False)}
    if len(json.dumps(wrapped, ensure_ascii=False).encode('utf-8')) > MAX_TOOL_PAYLOAD_BYTES - 65536:
        raise GoogleSourceError('Original source content exceeded the tool delivery limit. Read a smaller range.')
    with bind_original_source_batch(batch):
        validate_original_source_call(user_prompt=wrapped['text'], image_inputs=[f"data:{item.get('mimeType') or item.get('mime_type')};base64,{item['data']}" for item in wire.get('images', [])])
    return (payload, serialized)

def allowed_refs(task, service, *, allow_explicit_original=False):
    enabled = auto_source_retrieval_enabled(task['owner_user_id'], service.store.path)
    explicit_original = allow_explicit_original and task.get('source_access_mode') == 'explicit' and bool(task.get('source_grant_id')) and bool(task.get('authorized_refs'))
    explicit_material = False
    if not enabled and task.get('source_access_mode') == 'explicit' and task.get('source_grant_id'):
        for item in task.get('authorized_refs', []):
            if item.get('source_ingestion_id'):
                source = service.store.get_source(owner_user_id=task['owner_user_id'], package_id=task['package_id'], source_id=item['source_ingestion_id'])
                explicit_material |= bool(source and source.metadata.get('source_processor') == 'anythingllm')
    if not enabled and (not explicit_original) and (not explicit_material):
        return []
    refs = []
    for selection in task.get('authorized_refs', []):
        source_id = selection.get('source_ingestion_id')
        if not source_id:
            continue
        source = service.store.get_source(owner_user_id=task['owner_user_id'], package_id=task['package_id'], source_id=source_id)
        if source is None:
            raise GoogleSourceError('A source authorized for this turn has been deleted.')
        if source.metadata.get('source_backend') not in {'google', 'local'}:
            continue
        if source.metadata.get('external_turn_temporary'):
            raise GoogleSourceError('Add this external resource to Sources before reading or searching it.')
        ref = SourceQueryRef(source_ingestion_id=source_id, source_content_hash=selection.get('source_content_hash') or '', source_chapter_id=selection.get('source_chapter_id'), catalog_version=selection.get('catalog_version'), page_start=selection.get('source_page_start'), page_end=selection.get('source_page_end'), source_range=selection.get('source_range'))
        if selection.get('source_range') and (not getattr(ref, 'source_range', None)):
            raise GoogleSourceError('This selected native range cannot be represented by the source authorization contract.')
        refs.append(ref)
    if task.get('authorized_refs'):
        return refs
    mode = task.get('source_access_mode', 'none')
    if mode == 'explicit' or (mode == 'automatic' and auto_source_retrieval_enabled(task['owner_user_id'], service.store.path)):
        for source in service.store.list_sources(owner_user_id=task['owner_user_id'], package_id=task['package_id']):
            if source.metadata.get('source_backend') in {'google', 'local'} and source.status == 'ready' and (source.metadata.get('search_ready') or source.metadata.get('native_search_ready') or source.metadata.get('basic_content_artifact')) and (not source.metadata.get('external_turn_temporary')):
                refs.append(SourceQueryRef(source_ingestion_id=source.id, source_content_hash=source.metadata['content_hash']))
    return refs

def _temporary_source(service, task, source_id):
    store = getattr(service, 'store', None)
    if store is None:
        return False
    source = store.get_source(owner_user_id=task['owner_user_id'], package_id=task['package_id'], source_id=source_id)
    return bool(source and source.metadata.get('external_turn_temporary'))

def source_context(task, service=None):
    service = service or get_google_source_service()
    result = []
    enabled = auto_source_retrieval_enabled(task['owner_user_id'], service.store.path)
    for ref in allowed_refs(task, service, allow_explicit_original=True):
        temporary = _temporary_source(service, task, ref.source_ingestion_id)
        if not enabled and (not temporary) and (service.store.get_source(owner_user_id=task['owner_user_id'], package_id=task['package_id'], source_id=ref.source_ingestion_id).metadata.get('source_processor') != 'anythingllm'):
            continue
        source = service.get(owner_user_id=task['owner_user_id'], package_id=task['package_id'], source_id=ref.source_ingestion_id, content_hash=ref.source_content_hash, ready=False)
        result.append({'source_id': source.id, 'title': source.title, 'source_processor': source.metadata.get('source_processor', 'native'), 'source_content_hash': ref.source_content_hash, 'chapter_id': ref.source_chapter_id, 'catalog_version': ref.catalog_version, 'page_start': ref.page_start, 'page_end': ref.page_end, 'source_range': ref.source_range.model_dump(mode='json') if getattr(ref, 'source_range', None) else None})
    return result

def execute_source_tool(task, name, args, *, task_store, service=None):
    service = service or get_google_source_service()
    if task['status'] != 'running':
        raise GoogleSourceError('The turn associated with this source tool has ended or been cancelled.')
    original_read = name in {'read_source_content', 'read_cloud_source_range', 'query_source_table'}
    refs = allowed_refs(task, service, allow_explicit_original=original_read)
    if not refs:
        raise GoogleSourceError('Source access is not authorized for this turn. Automatic search is off, or the explicit selection has no available cloud source.')

    def finish(result, *, read_items=None):
        current = task_store.get(task['id'])
        if current['status'] != 'running' or current.get('source_grant_id', '') != task.get('source_grant_id', '') or (not allowed_refs(current, service, allow_explicit_original=original_read)):
            raise GoogleSourceError('Source access was cancelled, automatic search was switched off, or authorization for this turn changed.')
        if read_items:
            result['page_coordinate_contract'] = 'physical_pdf_pages means PDF page numbers, not printed/book page numbers; use each evidence page_label and never infer a printed-page offset.'
            task_store.remember_source_evidence(task['id'], task.get('source_grant_id', ''), read_items)
            used = {item.get('id') for item in read_items}
            result['citation_map'] = [{'citation_number': item.get('citation_number'), 'evidence_id': item['evidence_id'], 'source_title': item.get('source_title', ''), 'page_start': item.get('page_start'), 'page_end': item.get('page_end')} for item in task_store.get(task['id']).get('source_evidence', []) if item['evidence_id'] in used]
        if read_items:
            from openclass_core.services.source_prompt_projection import tool_payload_for_model
            return tool_payload_for_model(result)
        return result
    selected_id = str(args.get('source_id') or '')
    if selected_id:
        refs = [ref for ref in refs if ref.source_ingestion_id == selected_id]
        if not refs:
            raise GoogleSourceError("The requested source is outside this turn's authorized scope.")
    if name == 'list_sources':
        sources = []
        seen = set()
        for ref in refs:
            if ref.source_ingestion_id in seen:
                continue
            source = service.get(owner_user_id=task['owner_user_id'], package_id=task['package_id'], source_id=ref.source_ingestion_id, content_hash=ref.source_content_hash, ready=False)
            sources.append({'source_id': source.id, 'title': source.title, 'source_content_hash': ref.source_content_hash, 'catalog_ready': source.metadata.get('catalog_ready', False), 'processor': source.metadata.get('source_processor', 'native')})
            seen.add(source.id)
        return finish({'sources': sources, 'authorization': task.get('source_access_mode', 'explicit')})
    if name == 'search_sources':
        if any((_temporary_source(service, task, ref.source_ingestion_id) for ref in refs)):
            raise GoogleSourceError('This external original has no Google search index. Read its authorized original content directly.')
        if not task_store.consume_source_search(task['id']):
            raise GoogleSourceError('This turn allows up to three searches. Continue with the available evidence or narrow the scope.')
        from openclass_core.services.source_locations import locate_sources
        locations = locate_sources(service, owner_user_id=task['owner_user_id'], package_id=task['package_id'], query=str(args.get('query') or ''), refs=refs, authorization=task.get('source_access_mode', 'automatic'))
        return finish({'locators': [item.model_dump(mode='json') for item in locations], 'authorization': task.get('source_access_mode', 'explicit'), 'complete': False, 'usage': 'These are navigation locations only, not source evidence. Call read_source_content with a locator to receive the original content before using or citing it. A search does not establish complete coverage.'})
    if len({ref.source_ingestion_id for ref in refs}) != 1:
        raise GoogleSourceError('Specify an authorized source.')
    ref = refs[0]
    if _temporary_source(service, task, ref.source_ingestion_id) and (not original_read):
        raise GoogleSourceError('This external original has no Google catalog or search index. Read its authorized original content directly.')
    source = service.get(owner_user_id=task['owner_user_id'], package_id=task['package_id'], source_id=ref.source_ingestion_id, content_hash=ref.source_content_hash, ready=not original_read)
    from .search import resolve_ref_range
    scopes = []
    for reference in refs:
        service.get(owner_user_id=task['owner_user_id'], package_id=task['package_id'], source_id=reference.source_ingestion_id, content_hash=reference.source_content_hash, ready=not original_read)
        scopes.append(resolve_ref_range(service, reference, source))
    whole_source_allowed = any((scope is None for scope in scopes))
    scope = None if whole_source_allowed else scopes[0]

    def original_payload(requested, *, locator=None, cursor=None):
        from openclass_core.services.source_content_contract import SourceReadBudget
        from openclass_core.services.source_content_reader import read_source_content
        budget = SourceReadBudget(**task_store.source_read_budget(task['id'], task.get('source_grant_id', ''), reserve=True))
        before_characters, before_visual_units = (budget.characters, budget.visual_units)
        result = read_source_content(service, owner_user_id=task['owner_user_id'], package_id=task['package_id'], source_id=source.id, content_hash=ref.source_content_hash, source_range=requested, locator=locator, cursor=cursor, authorization=task.get('source_access_mode', 'explicit'), grant_id=task.get('source_grant_id', ''), role='board_editor', budget=budget, max_image_bytes=11 * 1024 * 1024, is_cancelled=lambda: task_store.get(task['id'])['status'] != 'running')
        payload, read_items = _read_delivery(result, task, calculation=bool(requested and requested.get('kind') == 'table_query'))
        task_store.source_read_budget(task['id'], task.get('source_grant_id', ''), characters=budget.characters - before_characters, visual_units=budget.visual_units - before_visual_units)
        return (payload, read_items)
    if name == 'get_source_catalog':
        view = service.structure_store.get_catalog_view(source=source)
        payload = [{'id': chapter.id, 'title': chapter.title, 'path': chapter.path, 'range': chapter.range.model_dump(mode='json') if chapter.range else None, 'mapping_status': chapter.mapping_status} for chapter in view.chapters]
        if not whole_source_allowed:
            return finish({'source_id': source.id, 'authorized_range': scope.model_dump(mode='json') if len(scopes) == 1 else None, 'authorized_ranges': [item.model_dump(mode='json') for item in scopes], 'chapter_id': ref.source_chapter_id if len(refs) == 1 else None, 'scope_limited': True})
        return finish({'source_id': source.id, 'catalog': payload, 'catalog_ready': source.metadata.get('catalog_ready', False)})
    if name in {'read_source_content', 'read_cloud_source_range'}:
        from openclass_core.services.source_content_contract import SourceLocator, verified_locator
        from openclass_core.services.source_content_scope import ensure_unit_authorized
        locator = SourceLocator.model_validate(args['locator']) if args.get('locator') else None
        if locator is not None and (not verified_locator(locator) or locator.source_id != source.id or locator.content_hash != ref.source_content_hash):
            raise GoogleSourceError('The source locator does not belong to this authorized source.')
        if not args.get('source_range') and locator is None and (not whole_source_allowed) and (len(scopes) > 1):
            raise GoogleSourceError('Multiple ranges are selected for this turn. Specify the authorized range to read.')
        requested = args.get('source_range') or (locator.source_range if locator is not None else scope.model_dump(mode='json') if scope is not None else None)
        if requested is not None and (not isinstance(requested, dict)):
            raise GoogleSourceError('The source range must contain verified native coordinates.')

        def includes(authorized):
            if authorized is None:
                return True
            authorized = authorized.model_dump(mode='json')
            if requested == authorized:
                return True
            if requested is None:
                return False
            if locator is not None and requested == locator.source_range and (locator.authorized_range == authorized):
                return True
            if any((authorized.get(key) and requested.get(key) != authorized[key] for key in ('start_anchor', 'end_anchor'))):
                return False
            try:
                ensure_unit_authorized(requested, authorized)
                return True
            except GoogleSourceError:
                return False
        if not any((includes(authorized) for authorized in scopes)):
            raise GoogleSourceError('The reading range must stay within the user-selected range.')
        payload, read_items = original_payload(requested, locator=args.get('locator'), cursor=args.get('cursor'))
        return finish(payload, read_items=read_items)
    if name == 'query_source_table':
        if not whole_source_allowed:
            raise GoogleSourceError('Spreadsheet calculations currently require explicit authorization for the entire spreadsheet in this turn. Chapter or page-range authorization cannot be expanded.')
        query_plan = args.get('query_plan', {})
        if not isinstance(query_plan, dict) or not isinstance(query_plan.get('sheet'), str) or (not query_plan['sheet'].strip()):
            raise GoogleSourceError('Specify an original worksheet and a bounded calculation plan.')
        payload, read_items = original_payload({'kind': 'table_query', 'container': query_plan['sheet'], 'query_plan': query_plan})
        return finish(payload, read_items=read_items)
    raise GoogleSourceError('Unknown source tool.')
