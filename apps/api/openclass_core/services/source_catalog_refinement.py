"""Explicit, version-bound Pi catalog work, independent of upload processing."""
from __future__ import annotations
import uuid
from openclass_core.models import AIModelSelection, now_iso
from openclass_core.services.google_sources.config import GoogleSourceError
PIPELINE = 'pi_catalog_refinement_v1'

def queue_refinement(service, source, *, task_manager, catalog_model=None, expected_content_hash=None, expected_catalog_version=None, request_id=None, mode=None, target_chapter_id=None):
    if mode not in {None, 'continue', 'reverify'}:
        raise GoogleSourceError('Unsupported catalog refinement mode.')
    if target_chapter_id is not None and (mode != 'continue' or not isinstance(target_chapter_id, str) or (not target_chapter_id)):
        raise GoogleSourceError('Preparing a chapter reference requires its current chapter identity and continue mode.')
    request_id = request_id or str(uuid.uuid4())

    def queue():
        current = service.state.current(source, active=False)
        if expected_content_hash and expected_content_hash != current.metadata.get('content_hash'):
            raise GoogleSourceError('The original file changed. Refresh the source before refining its catalog.')
        task = current.metadata.get('catalog_refinement', {})
        prior_request = current.metadata.get('catalog_refinement_requests', {}).get(request_id)
        if prior_request:
            if prior_request['source_version'] != current.metadata.get('source_version') or prior_request.get('mode') != mode or prior_request.get('target_chapter_id') != target_chapter_id or (expected_catalog_version is not None and prior_request['base_version'] != expected_catalog_version):
                raise GoogleSourceError('This request identity belongs to another source or catalog version.')
            return current
        if task.get('request_id') == request_id:
            if task.get('source_version') != current.metadata.get('source_version') or task.get('mode') != mode or task.get('target_chapter_id') != target_chapter_id or (expected_catalog_version is not None and task.get('base_version') != expected_catalog_version):
                raise GoogleSourceError('This request identity belongs to another source or catalog version.')
            return current
        view = service.structure_store.get_catalog_view(source=current)
        if expected_catalog_version is not None and expected_catalog_version != view.catalog_version:
            raise GoogleSourceError('The catalog changed. Refresh its current version before refining it.')
        key = (current.owner_user_id, current.package_id, current.id)
        if task_manager.is_active(key):
            raise GoogleSourceError('The source task is still running. Wait for it to finish or cancel it first.')
        if not current.metadata.get('original_read_ready', current.status == 'ready'):
            raise GoogleSourceError('The original file is not ready for catalog refinement.')
        from openclass_core.services.ai_model_catalog import default_text_selection
        selection = catalog_model or AIModelSelection.model_validate(current.metadata.get('catalog_model_selection') or default_text_selection().model_dump(mode='json'))
        resume = task if task.get('source_version') == current.metadata.get('source_version') and task.get('content_hash') == current.metadata.get('content_hash') and (task.get('status') in {'failed', 'cancelled', 'interrupted', 'partial', 'ready'}) else {}
        scope_target = ''
        if target_chapter_id:
            chapter = next((item for item in view.chapters if item.id == target_chapter_id), None)
            if chapter is None or current.mime_type != 'application/pdf':
                raise GoogleSourceError("The requested PDF chapter is not in this source's current catalog.")
            if chapter.metadata.get('location_state') == 'outside_uploaded_file':
                raise GoogleSourceError('This chapter is outside the uploaded file.')
            candidates = [row['key'] for row in (resume.get('report') or {}).get('nodes', []) if row.get('key') == chapter.id or resume.get('node_ids', {}).get(row.get('key')) == chapter.id]
            if len(candidates) != 1:
                raise GoogleSourceError('Complete catalog refinement before preparing this chapter reference.')
            scope_target = candidates[0]
        from openclass_core.services.source_catalog_pagination import PAGE_MAPPING_POLICY_VERSION
        heading_protocol = resume.get('heading_inventory_protocol_version', 0) if target_chapter_id else 0
        line_protocol = resume.get('heading_line_protocol_version', 0) if target_chapter_id else 0
        if mode == 'continue' and (not target_chapter_id) and (resume.get('completed_validation_version') == 2) and (resume.get('completed_page_mapping_policy_version') == PAGE_MAPPING_POLICY_VERSION) and (resume.get('status') == 'ready') and completion_reason(current, resume):
            return current
        review = mode == 'reverify'
        from openclass_core.services.source_catalog_reconciliation import snapshot_clues
        task = {'request_id': request_id, 'mode': mode, 'source_version': current.metadata.get('source_version'), 'target_chapter_id': target_chapter_id, 'scope_target_key': scope_target, 'page_mapping_policy_version': PAGE_MAPPING_POLICY_VERSION, 'scope_pdf_page_count': int(current.metadata.get('catalog_pdf_page_count') or current.metadata.get('page_count') or 0), 'chapter_scope_boundaries': {} if review else resume.get('chapter_scope_boundaries', {}), 'refinement_epoch': request_id if review else resume.get('refinement_epoch', resume.get('request_id', request_id)), 'fragment_protocol_version': 0, 'heading_inventory_protocol_version': heading_protocol, 'heading_line_protocol_version': line_protocol, 'directory_fragments': {} if review else resume.get('directory_fragments', {}), 'execution': {'created_at': now_iso(), 'phase': 'queued', 'model_calls': 0}, 'content_hash': current.metadata['content_hash'], 'base_version': view.catalog_version, 'published_version': view.catalog_version, 'status': 'queued', 'requested': True, 'seed': [chapter.model_dump(mode='json') for chapter in view.chapters], 'coverage': resume.get('coverage', []), 'reads': resume.get('reads', {}), 'node_ids': resume.get('node_ids', {}), 'directory_transcription': resume.get('directory_transcription', {}), 'directory_seed_review': resume.get('directory_seed_review', {}), 'directory_clue_archive': resume.get('directory_clue_archive') or snapshot_clues(resume), 'directory_reconciliation': resume.get('directory_reconciliation', {}), 'directory_review_requested': bool(resume.get('directory_transcription')) if mode is None else review or bool(resume.get('directory_review_requested')), 'review_completed_units': bool(resume.get('review_completed_units') or resume.get('status') == 'ready') if mode is None else review or bool(resume.get('review_completed_units')), 'pdf_pagination': resume.get('pdf_pagination', {}), 'bulk_mapping_key': '' if review else resume.get('bulk_mapping_key', ''), 'report': resume.get('report'), 'model': selection.model_dump(mode='json')}
        if mode == 'continue' and (not target_chapter_id) and (not task['directory_transcription']):
            from openclass_core.services.source_catalog_seed_review import seed_directory
            seeded = seed_directory(current, view.chapters, catalog_version=view.catalog_version)
            if seeded:
                task.update(seeded)
        if target_chapter_id:
            from openclass_core.services.source_catalog_scope_boundaries import reset_unverified_target
            from openclass_core.services.source_catalog_scope_integration import parameters
            nodes, options = parameters(task, current)
            reset_unverified_target(task, nodes, **options)
        if review and task['directory_transcription']:
            task['directory_transcription'] = {**task['directory_transcription'], 'review_request_id': request_id}
        current = service.state.update(current, active=False, metadata={'catalog_refinement': task, 'catalog_executor': 'pi', 'catalog_status': 'running', 'catalog_refinement_status': 'queued', 'catalog_refinement_requests': {**current.metadata.get('catalog_refinement_requests', {}), request_id: {'source_version': task['source_version'], 'content_hash': task['content_hash'], 'base_version': view.catalog_version, 'mode': mode, 'target_chapter_id': target_chapter_id}}, 'catalog_error': '', 'catalog_warning': '', 'update_status': 'queued', 'update_error': '', 'catalog_model_selection': selection.model_dump(mode='json')})
        service._job(current.model_copy(update={'status': 'queued'}), 'catalog_queued', 5)
        task_manager.submit(owner_user_id=current.owner_user_id, package_id=current.package_id, source_id=current.id, retry=True)
        return current
    return service.store.coordinator.run_write(service.store.path, queue)

def completion_reason(source, task):
    from openclass_core.services.source_catalog_transcription import transcription_view
    from openclass_core.services.source_catalog_heading_inventory import coverage_complete
    from openclass_core.services.source_catalog_heading_lines import coverage_complete as lines_complete
    precise_task = bool(task.get('target_chapter_id'))
    if task.get('directory_review_requested') or source.metadata.get('directory_status') != 'complete' or (precise_task and (not coverage_complete(task) or not lines_complete(task))):
        return None
    if task.get('directory_transcription') and (not transcription_view(task)['complete']):
        return None
    from openclass_core.services.source_catalog_scope_integration import current_work
    if precise_task and current_work(source, task) is not None:
        return None
    counts = source.metadata.get('catalog_location_summary')
    categories = ('verified', 'position_only', 'unresolved', 'outside_uploaded_file')
    if not isinstance(counts, dict) or type(counts.get('total')) is not int or counts['total'] < 1 or any((type(counts.get(key)) is not int or counts[key] < 0 for key in categories)) or (sum((counts[key] for key in categories)) != counts['total']) or counts['unresolved']:
        return None
    return 'completed_with_limits' if counts.get('position_only') or counts.get('outside_uploaded_file') else 'completed'

def process_refinement(service, source, *, runner=None):
    from openclass_core.services.google_sources.catalog_fallback import ensure_original_cache
    from openclass_core.services.source_catalog_refinement_agent import run_refinement
    from openclass_core.services.source_catalog_refinement_runtime import RefinementRuntime, RefinementStopped
    from openclass_core.services.source_catalog_refinement_finish import finish_refinement
    task = dict(source.metadata['catalog_refinement'])
    request_id = task['request_id']
    runtime = None
    try:
        runtime = RefinementRuntime(service, source)
        source = runtime.source

        def start(metadata):
            task = dict(metadata.get('catalog_refinement', {}))
            if task.get('request_id') != request_id:
                raise RefinementStopped('version_changed')
            if task.get('status') == 'cancelled':
                raise RefinementStopped('cancelled')
            task.update(status='running')
            return {'catalog_refinement': task, 'catalog_refinement_status': 'running', 'update_status': 'parsing'}
        source = service.state.update(source, metadata=start)
        runtime.begin_step({'stage': 'prepare_original'})
        service._job(source.model_copy(update={'status': 'parsing'}), 'pi_catalog_refinement', 10)
        source, original = ensure_original_cache(service, source)
        runtime.remaining_timeout()
        runtime.step_complete(True)
        source = service.state.current(source)
        (runner or run_refinement)(service, source, original)
        return finish_refinement(service, source, runtime, request_id)
    except Exception as exc:
        from openclass_core.services.source_ingestion_jobs import source_task_cancel_requested
        try:
            current = service.state.current(source, active=False)
        except GoogleSourceError:
            return finish_refinement(service, source, None, request_id, failure_reason='version_changed')
        latest = dict(current.metadata.get('catalog_refinement', {}))
        reason = latest.get('execution', {}).get('stop_reason') or (exc.reason if isinstance(exc, RefinementStopped) else 'failed')
        if source_task_cancel_requested():
            reason = 'cancelled'
        elif reason == 'failed':
            error = str(exc).casefold()
            if 'timed out' in error or 'timeout' in error:
                reason = 'model_timeout'
            elif any((marker in error for marker in ('newer catalog', 'catalog changed', 'version changed', 'superseded'))):
                reason = 'version_changed'
            elif 'not connected' in error or 'authentication' in error or 'unauthorized' in error:
                reason = 'authentication_required'
            elif 'unsupported' in error or 'cannot inspect images' in error:
                reason = 'unsupported_model'
        return finish_refinement(service, source, runtime, request_id, failure_reason=reason)
