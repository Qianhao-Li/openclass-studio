"""Allowlisted operational activity for catalog jobs, never provider text."""
from hashlib import sha256
import json
import math
import re
from openclass_core.models import AgentActivityEvent
from openclass_core.services.google_sources.config import GoogleSourceError
STAGES = {'prepare_original', 'discover_directory', 'inspect_directory_gap', 'plan_directory_fragments', 'transcribe_directory_fragment', 'verify_directory_page', 'transcribe_directory_unit', 'review_directory_unit', 'review_seeded_directory_unit', 'reconcile_directory_unit', 'calibrate_pdf', 'review_page_labels', 'publish_bulk_mapping', 'map_chapter', 'waiting_retry', 'verify_chapter_scope', 'review_directory_heading_line'}
TOOLS = {'source_info', 'get_source_catalog', 'search_sources', 'read_source_content', 'pdf_text', 'pdf_page_image', 'plan_directory_transcription', 'confirm_directory_gap', 'confirm_directory_unit', 'transcribe_directory_unit', 'set_pdf_pagination', 'confirm_pdf_heading', 'catalog_apply', 'write_catalog', 'plan_directory_fragments', 'transcribe_directory_fragment', 'verify_directory_page', 'record_directory_headings', 'record_pdf_chapter_boundaries', 'record_directory_heading_literal'}

def safe_identity(value):
    if not isinstance(value, str):
        return ''
    return value if re.fullmatch('[A-Za-z0-9_.:-]{1,128}', value) else 'id_' + sha256(value.encode()).hexdigest()[:20]

def safe_range(value):
    if not isinstance(value, dict) or value.get('kind') not in {'pdf_pages', 'ppt_slides', 'text_lines', 'page', 'pages', 'paragraphs', 'document_units'}:
        return None
    if any((type(value.get(key)) is not int or value[key] < 1 for key in ('start', 'end'))) or value['end'] < value['start']:
        return None
    return {key: value[key] for key in ('kind', 'start', 'end')}

def _arguments(value):
    if isinstance(value, str):
        if len(value) > 8192:
            return {}
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}

def project_activity(event, *, request_id, step, timestamp):
    raw = event.metadata
    kind = raw.get('kind')
    if kind not in {'pi_process', 'pi_tool_call', 'pi_tool_result', 'source_refinement_step', 'source_refinement_stop'}:
        return None
    metadata = {'kind': kind, 'agent_backend': 'pi', 'request_id': safe_identity(request_id), 'step_id': step.get('id', ''), 'phase': step.get('stage', ''), 'attempt': step.get('attempt', 0)}
    status = event.status if event.status in {'running', 'completed', 'failed', 'cancelled'} else 'running'
    if kind in {'pi_tool_call', 'pi_tool_result'}:
        tool = raw.get('tool')
        if tool not in TOOLS:
            return None
        metadata['tool'] = tool
        arguments = _arguments(raw.get('arguments'))
        source_range = safe_range(arguments.get('source_range'))
        first = arguments.get('page', arguments.get('first_page'))
        if source_range is None and type(first) is int:
            source_range = safe_range({'kind': 'pdf_pages', 'start': first, 'end': arguments.get('last_page', first)})
        if source_range:
            metadata['source_range'] = source_range
        region = arguments.get('region')
        if isinstance(region, list) and len(region) == 4 and all((type(value) in (float, int) and math.isfinite(value) and (0 <= value <= 1) for value in region)) and (region[0] < region[2]) and (region[1] < region[3]):
            metadata['region'] = region
        label = f'Pi tool {status}: {tool}'
        if status == 'failed':
            metadata['error_code'] = 'tool_failed'
    elif kind == 'pi_process':
        label = f'Pi model process {status}'
        if type(raw.get('returncode')) is int:
            metadata['returncode'] = raw['returncode']
        if status == 'failed':
            metadata['error_code'] = 'model_process_failed'
    else:
        label = f'Pi directory step {status}'
        metadata['source_range'] = step.get('source_range')
        metadata['fragment_id'] = step.get('fragment_id', '')
    event_id = 'refinement:' + sha256((request_id + ':' + event.id).encode()).hexdigest()[:32]
    return AgentActivityEvent(id=event_id, turn_id=safe_identity(request_id), stage='execute_role', role='pi', status=status, label=label, metadata=metadata, created_at=timestamp)

def save_activity(service, source, *, request_id, job_id, event):

    def save():
        try:
            current = service.state.current(source, active=False)
        except GoogleSourceError:
            return
        task = current.metadata.get('catalog_refinement', {})
        execution = task.get('execution', {})
        if task.get('request_id') != request_id or task.get('status') not in {'queued', 'running'} or task.get('content_hash') != current.metadata.get('content_hash') or (task.get('source_version') != current.metadata.get('source_version')) or (execution.get('job_id') != job_id) or execution.get('finished_at'):
            return
        job = service.job_store.latest_for_source(owner_user_id=source.owner_user_id, package_id=source.package_id, source_id=source.id)
        if job is None or job.id != job_id or job.cancel_requested:
            return
        activity = list(job.agent_activity)
        position = next((index for index, previous in enumerate(activity) if previous.id == event.id), None)
        if position is None:
            activity.append(event)
        else:
            previous = activity[position]
            activity[position] = event.model_copy(update={'created_at': previous.created_at, 'metadata': {**previous.metadata, **event.metadata}})
        service.job_store.save(job.model_copy(update={'agent_activity': activity, 'heartbeat_at': event.created_at}), owner_user_id=source.owner_user_id, package_id=source.package_id)
    service.store.coordinator.run_write(service.store.path, save)
