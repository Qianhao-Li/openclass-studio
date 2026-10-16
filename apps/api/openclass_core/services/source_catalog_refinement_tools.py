"""Single-file, authenticated tool broker and durable reading checkpoints."""
from __future__ import annotations
import json
import secrets
import threading
from dataclasses import asdict
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from openclass_core.models import SourceQueryRef, SourceRange
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_catalog_refinement_evidence import RefinementReport, RefinementNode, materialize, read_units, range_was_read
from openclass_core.services.source_content_contract import SourceReadBudget
from openclass_core.services.source_catalog_transcription import completed_nodes, plan_units, transcribe_unit, transcription_view

class RefinementTools:

    def __init__(self, service, source, *, cancelled=lambda: False, supports_images=True):
        self.service, self.source = (service, source)
        self.cancelled, self.supports_images = (cancelled, supports_images)
        self.lock = threading.RLock()
        self.budget = SourceReadBudget.continuation()
        self.request_id = source.metadata['catalog_refinement']['request_id']
        from openclass_core.services.model_image_limits import image_transfer_limits
        model = source.metadata['catalog_refinement']['model']
        self.image_limits = image_transfer_limits(model['provider'], model['model'])

    def current(self):
        runtime = getattr(self, 'runtime', None)
        if runtime is not None:
            runtime.remaining_timeout()
        source = self.service.state.current(self.source)
        task = source.metadata.get('catalog_refinement', {})
        if self.cancelled() or task.get('request_id') != self.request_id or task.get('status') != 'running':
            raise GoogleSourceError('This catalog refinement was cancelled or superseded.')
        if source.metadata.get('content_hash') != task.get('content_hash') or source.metadata.get('source_version') != task.get('source_version'):
            raise GoogleSourceError('The original file version changed.')
        view = self.service.structure_store.get_catalog_view(source=source)
        if view.catalog_version != task['published_version']:
            raise GoogleSourceError('A newer catalog has replaced this refinement checkpoint.')
        if runtime is not None:
            runtime.remaining_timeout()
        return (source, dict(task))

    def save(self, source, task):

        def save():
            current, latest = self.current()
            checkpoint = {**task, 'execution': latest.get('execution', {})}
            self.source = self.service.state.update(current, metadata={'catalog_refinement': checkpoint, 'catalog_search_engine': task.get('search_engine', 'ordinary'), 'catalog_search_warning': task.get('search_warning', '')})
        self.service.store.coordinator.run_write(self.service.store.path, save)

    def execute(self, name, args):
        with self.lock:
            source, task = self.current()
            if args.get('source_id', source.id) != source.id:
                raise GoogleSourceError('This refresh authorizes only its selected file.')
            line_stage = task.get('execution', {}).get('current_step', {}).get('stage') == 'review_directory_heading_line'
            if line_stage and name not in {'source_info', 'get_source_catalog', 'pdf_page_image', 'record_directory_heading_literal'}:
                raise GoogleSourceError('This independent heading step only reads and records its planned original line.')
            if name in {'source_info', 'get_source_catalog'}:
                from openclass_core.services.source_basic_cache import load_basic_content
                data = load_basic_content(self.service, source) if source.metadata.get('basic_content_artifact') else {}
                start, limit = (int(args.get('start_index', 0)), min(100, max(1, int(args.get('limit', 100)))))
                rows = (task.get('report') or {}).get('nodes', task['seed'])
                transcription = transcription_view(task)
                from openclass_core.services.source_catalog_fragments import fragment_work
                fragmented = fragment_work(task) if transcription['discovery_complete'] and (not task.get('directory_review_requested')) and (not transcription['pending_gap']) else None
                heading_inventory = bool(fragmented and fragmented.get('inspection_mode') == 'heading_inventory')
                heading_line = bool(fragmented and fragmented.get('inspection_mode') == 'heading_line')
                if heading_line:
                    rows, transcription = ([], {})
                elif heading_inventory:
                    scope = fragmented['source_range']
                    fragmented = {key: fragmented[key] for key in ('stage', 'inspection_mode', 'plan_revision', 'merged_revision', 'reading_regions', 'high_resolution_details_required') if key in fragmented}
                    fragmented['source_range'] = {key: scope[key] for key in ('kind', 'start', 'end')}
                    rows, transcription = ([], {})
                elif fragmented:
                    rows = fragmented.get('current_rows', [])
                    transcription = {**transcription, 'pending_reconciliation': None, 'current_rows': rows, 'open_ancestors': fragmented.get('open_ancestors', [])}
                elif transcription['pending_reconciliation']:
                    rows = transcription['pending_reconciliation']['current_rows']
                elif transcription['reviewing_saved_unit']:
                    rows = transcription['open_ancestors']
                else:
                    from openclass_core.services.source_catalog_seed_review import work as seeded_work
                    seeded = seeded_work(task)
                    if seeded:
                        rows = seeded['current_rows']
                return {'source_id': source.id, 'source_version': task['source_version'], 'content_hash': task['content_hash'], 'image_transfer_limits': asdict(self.image_limits), 'catalog': rows[start:start + limit], 'next_index': start + limit if start + limit < len(rows) else None, 'directory_evidence': [] if fragmented else source.metadata.get('catalog_directory_evidence', []), 'directory_candidates': [] if fragmented else source.metadata.get('catalog_directory_candidates', []), 'directory_transcription': transcription, 'directory_fragments': fragmented, 'pdf_pagination': {} if fragmented or transcription['reviewing_saved_unit'] else task.get('pdf_pagination', {}), 'prior_read_ranges': [] if fragmented else source.metadata.get('catalog_read_ranges', []), 'coverage': [] if heading_inventory or heading_line else task['coverage'], 'enhancement_status': source.metadata.get('enhancement_status', 'not_prepared'), 'page_count': source.metadata.get('catalog_pdf_page_count') or source.metadata.get('page_count', len(data.get('units', []))), 'unit_count': len(data.get('units', [])), 'native_entries': [] if fragmented or transcription['reviewing_saved_unit'] else data.get('native_entries', [])[:100], 'unit_positions': [] if heading_inventory or heading_line else [{'unit_id': u['id'], 'locator': u['locator']} for u in data.get('units', [])[start:start + limit]]}
            if name == 'search_sources':
                return self.search(source, task, args)
            if name in {'read_source_content', 'pdf_text', 'pdf_page_image'}:
                return self.read(source, task, name, args)
            if name in {'catalog_apply', 'write_catalog'}:
                return self.publish(source, task, name, args)
            if name == 'plan_directory_transcription':
                if task.get('directory_review_requested') and source.mime_type == 'application/pdf':
                    count = int(source.metadata.get('catalog_pdf_page_count') or source.metadata.get('page_count') or 4)
                    front = SourceRange(kind='pdf_pages', start=1, end=min(4, count))
                    ids = [identity for identity, entry in task.get('reads', {}).items() if entry.get('request_id') == task['request_id']]
                    if not range_was_read(front, read_units(task, ids)):
                        raise GoogleSourceError("Read this review's original front matter before accepting a prior directory plan; old body locations are only clues.")
                result = plan_units(task, args['source_ranges'], discovery_complete=bool(args.get('discovery_complete')))
                task['directory_review_requested'] = False
                return self.checkpoint(source, task, result)
            if name == 'confirm_directory_gap':
                from openclass_core.services.source_catalog_directory_gaps import confirm_gap
                confirm_gap(task, args['source_range'], args['read_ids'], is_directory=bool(args['is_directory']), reason=str(args['reason']))
                return self.checkpoint(source, task, transcription_view(task))
            if name == 'record_directory_headings':
                from openclass_core.services.source_catalog_heading_inventory import record
                result = record(task, args['source_range'], plan_revision=args['plan_revision'], merged_revision=args['merged_revision'], read_ids=args['read_ids'], headings=args['headings'])
                return self.checkpoint(source, task, result)
            if name == 'record_directory_heading_literal':
                from openclass_core.services.source_catalog_heading_lines import record
                result = record(source, task, line_id=args['line_id'], inventory_fingerprint=args['inventory_fingerprint'], read_ids=args['read_ids'], literal_title=args['literal_title'])
                return self.checkpoint(source, task, result)
            if name == 'record_pdf_chapter_boundaries':
                from openclass_core.services.source_catalog_scope_boundaries import record
                from openclass_core.services.source_catalog_scope_integration import parameters
                nodes, options = parameters(task, source)
                result = record(source, task, nodes, **options, plan_digest=args['plan_digest'], physical_page=args['physical_page'], pass_index=args['pass_index'], read_ids=args['read_ids'], layout=args['layout'], observations=args['observations'])
                self.publish(source, task, 'catalog_apply', {})
                source, task = self.current()
                return self.checkpoint(source, task, result)
            if name in {'plan_directory_fragments', 'transcribe_directory_fragment'}:
                from openclass_core.services.source_catalog_fragments import plan_fragments, transcribe_fragment
                if name == 'plan_directory_fragments':
                    result = plan_fragments(task, args['source_range'], args['fragments'], args['read_ids'], expected_plan_revision=args.get('expected_plan_revision'))
                else:
                    result = transcribe_fragment(task, args['source_range'], args['fragment_id'], plan_revision=args['plan_revision'], read_ids=args['read_ids'], nodes=args['nodes'], complete=args['complete'] is True, remove_keys=args.get('remove_keys', []), ordered_keys=args.get('ordered_keys'))
                return self.checkpoint(source, task, result or {'saved': True})
            if name in {'transcribe_directory_unit', 'verify_directory_page', 'confirm_directory_unit'}:
                if name == 'verify_directory_page':
                    from openclass_core.services.source_catalog_fragments import verify_page
                    result = verify_page(task, args['source_range'], plan_revision=args['plan_revision'], merged_revision=args['merged_revision'], read_ids=args['read_ids'], verified=args['verified'] is True, retry_fragment_ids=args.get('retry_fragment_ids', [])) or {'saved': True}
                elif name == 'confirm_directory_unit':
                    from openclass_core.services.source_catalog_seed_review import confirmed_rows
                    rows = confirmed_rows(source, task, args['source_range'], args['read_ids'], unchanged=args.get('unchanged'))
                    result = transcribe_unit(task, args['source_range'], rows, args['read_ids'], complete=True)
                else:
                    result = transcribe_unit(task, args['source_range'], args['nodes'], args['read_ids'], complete=bool(args['complete']), reconciliation_complete=args.get('reconciliation_complete') is True)
                nodes = completed_nodes(task)
                if nodes is None:
                    self.save(source, task)
                else:
                    ids = list(dict.fromkeys((identity for unit in task['directory_transcription']['units'] for identity in unit['read_ids'])))
                    self.publish(source, task, 'catalog_apply', {'replace_all': True, 'nodes': [node.model_dump(mode='json') for node in nodes], 'origin': 'authored', 'directory_complete': False, 'directory_read_ids': ids, 'summary': 'The planned original directory units have been transcribed. Body mapping remains independent.', 'next_plan': 'Verify chapter body starts and boundaries from original reads.'})
                source, task = self.current()
                return self.checkpoint(source, task, result)
            if name in {'set_pdf_pagination', 'confirm_pdf_heading'}:
                from openclass_core.services.source_catalog_pagination import register_regime, register_heading
                report = RefinementReport.model_validate(task.get('report') or {})
                pages = {page for unit in read_units(task, report.directory_read_ids) if unit['locator'].get('kind') == 'pdf_pages' for page in range(unit['locator']['start'], unit['locator']['end'] + 1)}
                if name == 'set_pdf_pagination':
                    register_regime(task, report.nodes, args['regime'], page_count=int(source.metadata.get('catalog_pdf_page_count') or source.metadata.get('page_count', 0)), directory_pages=pages)
                else:
                    register_heading(task, report.nodes, args['anchor'], directory_pages=pages)
                self.publish(source, task, 'catalog_apply', {})
                return {'saved': True, 'pagination': task['pdf_pagination'], 'catalog_version': task['published_version']}
            raise GoogleSourceError('Unknown catalog refinement tool.')

    def checkpoint(self, source, task, result):
        report = RefinementReport.model_validate(task.get('report') or {'nodes': [{'key': row['id'], 'title': row['title'], 'level': row['level'], 'source_range': row.get('range'), 'printed_page_label': row.get('metadata', {}).get('printed_page', '')} for row in task['seed']]})
        report.directory_complete = False
        report.next_plan = 'Transcribe the next actually read original directory unit.' if transcription_view(task)['next_unit'] else 'Verify pagination and chapter ranges from original body reads.'
        task['report'] = report.model_dump(mode='json')
        self.save(source, task)
        return {**result, 'checkpoint_report': task['report']}

    def search(self, source, task, args):
        ref = SourceQueryRef(source_ingestion_id=source.id, source_content_hash=task['content_hash'])
        kw = dict(owner_user_id=source.owner_user_id, package_id=source.package_id, query=str(args.get('query', '')), refs=[ref], limit=min(8, max(1, int(args.get('limit', 8)))), authorization='explicit')
        warning, engine = ('', 'ordinary')
        if source.metadata.get('search_ready') and source.metadata.get('enhancement_status') == 'ready':
            try:
                self.service.artifact(source)
                from openclass_core.services.google_sources.locate import locate_sources
                locations = locate_sources(self.service, **kw)
                engine = 'google'
            except GoogleSourceError as exc:
                warning = str(exc)
                locations = None
        else:
            locations = None
        if locations is None:
            if source.metadata.get('source_processor') == 'anythingllm':
                from openclass_core.services.anythingllm.provider import AnythingLLMProvider
                locations = AnythingLLMProvider(self.service).locate(**kw)
            else:
                from openclass_core.services.source_basic_search import locate_basic_sources
                kw.pop('authorization')
                proxy = _OrdinaryService(self.service)
                locations = locate_basic_sources(proxy, **kw)
        self.current()
        task['search_engine'], task['search_warning'] = (engine, warning)
        self.save(source, task)
        return {'locators': [item.model_dump(mode='json') for item in locations], 'engine': engine, 'warning': warning, 'complete': False, 'usage': 'Locations only. Read original content; extend nearby ranges and inspect TOC continuation pages.'}

    def read(self, source, task, name, args):
        from openclass_core.services.source_content_reader import read_source_content
        from openclass_core.services.source_catalog_image_detail import ImageDetailCapacityError, add_original_detail, add_original_pdf_detail, validate_region
        region = validate_region(args.get('region')) if name == 'pdf_page_image' else None
        from openclass_core.services.source_catalog_heading_lines import work as line_work, read_context as line_read_context
        line_stage = task.get('execution', {}).get('current_step', {}).get('stage') == 'review_directory_heading_line'
        line = line_work(task) if line_stage else None
        if line_stage and (line is None or name != 'pdf_page_image' or args.get('page') != line['source_range']['start'] or (region != line['region'])):
            raise GoogleSourceError("Read exactly the planned original heading line's physical page and detail region.")
        requested = args.get('source_range')
        if name in {'pdf_text', 'pdf_page_image'}:
            first = int(args.get('page', args.get('first_page', 1)))
            requested = {'kind': 'pdf_pages', 'start': first, 'end': int(args.get('last_page', first))}
            if name == 'pdf_page_image':
                requested['metadata'] = {'read_original_images': True}
        max_image_bytes = min(self.image_limits.total_images, (self.image_limits.request_bytes - self.budget.read_character_limit * 4 - 256 * 1024) * 3 // 4)
        result = read_source_content(self.service, owner_user_id=source.owner_user_id, package_id=source.package_id, source_id=source.id, content_hash=task['content_hash'], source_range=requested, locator=args.get('locator'), cursor=args.get('cursor'), authorization='explicit', role='source_catalog_refinement', grant_id=self.request_id, budget=self.budget, is_cancelled=self.cancelled, image_limits=self.image_limits, max_image_bytes=max_image_bytes)
        if result.image_inputs and (not self.supports_images):
            raise GoogleSourceError('The selected catalog model cannot read original page images. Select a vision model to refine this scanned material.')
        current, _ = self.current()
        payload = result.tool_payload()
        if region is not None:
            if self.budget.read_visual_limit < 1:
                raise GoogleSourceError('The original PDF detail exceeds the remaining source reading budget.')
            from openclass_core.services.google_sources.original_cache import original_path
            payload = add_original_pdf_detail(payload, region, self.image_limits, max_image_bytes, source_path=original_path(current), source_content_hash=task['content_hash'], source_version=task['source_version'], physical_page=requested['start'], is_cancelled=self.cancelled)
            self.current()
            self.budget.consume(characters=0, visual_units=1)
        elif len(payload['images']) == 1:
            view = transcription_view(task)
            unit = view['pending_gap'] or (view['pending_reconciliation'] or {}).get('source_range') or view['next_unit']
            if unit and requested and (requested.get('kind') == unit['kind'] == 'pdf_pages') and (requested.get('start') == requested.get('end') == unit['start']):
                try:
                    detailed = add_original_detail(payload, [0, 0, 0.55, 1], self.image_limits, max_image_bytes)
                    detailed = add_original_detail(detailed, [0.45, 0, 1, 1], self.image_limits, max_image_bytes)
                    payload = add_original_detail(detailed, [0, 0, 1, 0.25], self.image_limits, max_image_bytes)
                except ImageDetailCapacityError as exc:
                    payload['detail_warning'] = str(exc)
        from openclass_core.services.source_catalog_fragments import fragment_work
        heading_inventory = (fragment_work(task) or {}).get('inspection_mode') == 'heading_inventory'
        literal_review = heading_inventory or bool((task.get('directory_transcription') or {}).get('review_request_id') and (not task.get('directory_review_requested')))
        if not literal_review and requested and (requested.get('kind') == 'pdf_pages') and (requested.get('start') == requested.get('end')):
            view = transcription_view(task)
            unit = view['pending_gap'] or view['next_unit']
            if unit and unit.get('kind') == 'pdf_pages' and (unit.get('start') == requested['start']):
                from openclass_core.services.source_catalog_enhanced_clues import page_clues
                try:
                    clues = page_clues(self.service, source, requested['start'], max_characters=self.budget.read_character_limit)
                    if clues:
                        self.current()
                        self.budget.consume(characters=sum((len(block['text']) for block in clues['blocks'])), visual_units=0)
                        payload['enhanced_page_clues'] = clues
                        proof = {key: clues[key] for key in ('source_range', 'artifact_sha256', 'content_digest')}
                        proofs = task.setdefault('page_clue_reads', [])
                        if proof not in proofs:
                            proofs.append(proof)
                except GoogleSourceError as exc:
                    payload['enhanced_page_clue_warning'] = task['search_warning'] = str(exc)
        from openclass_core.services.source_basic_cache import load_basic_content
        data = load_basic_content(self.service, source) if source.metadata.get('basic_content_artifact') else {}
        units = []
        for item in result.evidence_items:
            locator = item.metadata['source_range']
            identity = locator.get('metadata', {}).get('basic_unit_id')
            ids = [identity] if identity else [unit['id'] for unit in data.get('units', []) if all((unit['locator'].get(key) == locator.get(key) for key in ('kind', 'start', 'end', 'container')))]
            units.append({'locator': locator, 'sha256': item.metadata['content_unit_sha256'], 'unit_ids': ids})
        detail_fields = ('sha256', 'parent_sha256', 'region', 'width', 'height', 'detail_source', 'render_dpi', 'source_content_hash', 'source_version', 'physical_page')
        details = [{key: image[key] for key in detail_fields if key in image} for image in payload['image_manifest'] if image.get('representation') == 'original_page_detail']
        from openclass_core.services.source_catalog_scope_integration import read_context
        page_fields = ('sha256', 'physical_page', 'source_content_hash', 'source_version', 'representation', 'source_range', 'crop_bbox')
        task['reads'][result.receipt['read_id']] = {'receipt': result.receipt, 'units': units, 'request_id': task['request_id'], 'image_details': details, 'page_images': [] if line_stage else [{key: item[key] for key in page_fields if key in item} for item in payload['image_manifest'] if not item.get('parent_sha256')], 'scope_read_context': read_context(source, task), 'line_read_context': line_read_context(task) if line_stage else {}}
        from openclass_core.services.source_catalog_fragments import fragment_read_context
        task['reads'][result.receipt['read_id']]['catalog_fragment_context'] = fragment_read_context(task, requested or result.receipt['range'], region=region, complete=result.complete)
        if line_stage:
            task['reads'][result.receipt['read_id']]['units'] = []
            task['reads'][result.receipt['read_id']]['catalog_fragment_context']['complete'] = False
            payload = {**payload, 'complete': False, 'delivery_scope': 'heading_line_detail_only', 'images': [item for item in payload['images'] if item.get('detail_source') == 'original_pdf' and item.get('region') == region], 'image_manifest': [item for item in payload['image_manifest'] if item.get('detail_source') == 'original_pdf' and item.get('region') == region]}
        task['coverage'].append({'range': result.receipt['range'], 'read_id': result.receipt['read_id'], 'complete': result.complete, 'next_cursor': result.next_cursor})
        self.save(source, task)
        return payload

    def publish(self, source, task, name, args):
        report = RefinementReport.model_validate(task.get('report') or {'nodes': [{'key': row['id'], 'title': row['title'], 'level': row['level'], 'source_range': row.get('range'), 'printed_page_label': row.get('metadata', {}).get('printed_page', '')} for row in task['seed']]})
        if name == 'catalog_apply':
            nodes = {} if args.get('replace_all') else {node.key: node for node in report.nodes}
            for key in args.get('remove_keys', []):
                nodes.pop(key, None)
            for raw in args.get('nodes', []):
                node = RefinementNode.model_validate(raw)
                if node.page_label_reviewed and (not read_units(task, node.directory_read_ids)):
                    raise GoogleSourceError('A reviewed page label needs an actual original directory read.')
                previous = nodes.get(node.key)
                if previous and previous.printed_page_label != node.printed_page_label and (not node.directory_printed_page_label):
                    node.directory_printed_page_label = previous.directory_printed_page_label or previous.printed_page_label
                nodes[node.key] = node
            report.nodes = list(nodes.values())
            if args.get('ordered_keys') is not None:
                keys = args['ordered_keys']
                if len(keys) != len(nodes) or set(keys) != set(nodes):
                    raise GoogleSourceError('Directory order must name each retained node exactly once.')
                report.nodes = [nodes[key] for key in keys]
        for key in ('origin', 'directory_complete', 'directory_read_ids', 'summary', 'next_plan'):
            if key in args:
                setattr(report, key, args[key])
        if transcription_view(task)['complete']:
            report.directory_read_ids = list(dict.fromkeys([*(identity for unit in task['directory_transcription']['units'] for identity in unit['read_ids']), *report.directory_read_ids]))
        report = RefinementReport.model_validate(report.model_dump())
        if len({node.key for node in report.nodes}) != len(report.nodes):
            raise GoogleSourceError('Catalog node keys must be unique.')
        from openclass_core.services.source_catalog_fragments import enabled as fragments_enabled
        if fragments_enabled(task) and (not transcription_view(task)['complete']):
            report.directory_complete = False
        if name == 'write_catalog' and task.get('directory_transcription') and (not transcription_view(task)['complete']):
            report.directory_complete = False
            task['report'] = report.model_dump(mode='json')
            self.save(source, task)
            view = self.service.structure_store.get_catalog_view(source=source)
            return {'published': False, 'retained_catalog': True, 'catalog_version': task['published_version'], 'node_count': len(view.chapters), 'directory_complete': False, 'report': task['report']}
        structure, chapters, metadata = materialize(source, task, report)
        task['report'] = report.model_dump(mode='json')
        task['published_version'] = structure.catalog_version

        def publish():
            current, latest = self.current()
            checkpoint = {**task, 'execution': latest.get('execution', {})}
            return self.service.state.publish(current, structure_store=self.service.structure_store, structure=structure, chapters=chapters, expected_catalog_version=structure.catalog_version - 1, metadata={**metadata, 'catalog_refinement': checkpoint, 'catalog_refinement_status': 'running', 'catalog_search_engine': task.get('search_engine', 'ordinary'), 'catalog_search_warning': task.get('search_warning', '')})
        current = self.service.store.coordinator.run_write(self.service.store.path, publish)
        self.source = current
        return {'published': True, 'catalog_version': task['published_version'], 'node_count': len(chapters), 'directory_complete': metadata['directory_status'] == 'complete', 'report': task['report'] if name == 'write_catalog' else None}

class _OrdinaryService:

    def __init__(self, service):
        self.service = service

    def __getattr__(self, name):
        return getattr(self.service, name)

    def get(self, **kwargs):
        source = self.service.get(**kwargs)
        return source.model_copy(update={'metadata': {**source.metadata, 'search_ready': False}})

@contextmanager
def refinement_broker(tools):
    token = secrets.token_hex(32)

    class Handler(BaseHTTPRequestHandler):

        def log_message(self, *_):
            pass

        def do_POST(self):
            from openclass_core.services.source_catalog_refinement_runtime import RefinementStopped
            try:
                if self.path not in {'/tool', '/provider-request'} or self.headers.get('Authorization') != 'Bearer ' + token:
                    raise GoogleSourceError('Invalid catalog tool grant.')
                size = int(self.headers.get('Content-Length', 0))
                if not 0 < size <= 4 * 1024 * 1024:
                    raise GoogleSourceError('Invalid tool request size.')
                payload = json.loads(self.rfile.read(size))
                if self.path == '/provider-request':
                    if payload != {} or getattr(tools, 'runtime', None) is None:
                        raise GoogleSourceError('Catalog provider request budget is unavailable.')
                    with tools.lock:
                        tools.current()
                        result = {'result': tools.runtime.before_provider_request()}
                else:
                    result = {'result': tools.execute(payload['name'], payload.get('args', {}))}
                status = 200
            except RefinementStopped as exc:
                if getattr(tools, 'runtime', None) is not None:
                    try:
                        tools.runtime.stop(exc.reason)
                    except RefinementStopped:
                        pass
                result, status = ({'error': 'Pi catalog refinement stopped: ' + exc.reason, 'reason': exc.reason}, 409)
            except Exception as exc:
                result, status = ({'error': str(exc) if isinstance(exc, (GoogleSourceError, ValueError)) else 'Catalog tool failed.'}, 409)
            raw = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (f'http://127.0.0.1:{server.server_port}', token)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
