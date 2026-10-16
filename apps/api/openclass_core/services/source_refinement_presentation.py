"""Project execution progress without exposing internal reads, paths or text."""
from datetime import datetime
import re
from openclass_core.source_refinement_models import SourceCatalogRefinementView, SourceRefinementCheckpoint, SourceRefinementRange, SourceRefinementStep
_REASONS = {'completed', 'completed_with_limits', 'budget_exhausted', 'call_limit', 'step_timeout', 'no_progress', 'cancelled', 'interrupted', 'version_changed', 'model_timeout', 'tool_failed', 'authentication_required', 'unsupported_model', 'failed'}
_STATUSES = {'queued', 'running', 'ready', 'partial', 'failed', 'cancelled', 'interrupted'}

def _object(value):
    return value if isinstance(value, dict) else {}

def _identity(value):
    return value if isinstance(value, str) and re.fullmatch('[A-Za-z0-9_.:-]{1,128}', value) else ''

def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        return value if datetime.fromisoformat(value).tzinfo is not None else None
    except ValueError:
        return None

def _count(value, default=0):
    return value if type(value) is int and value >= 0 else default

def _choice(value, allowed, default=None):
    return value if isinstance(value, str) and value in allowed else default

def _range(value):
    value = _object(value)
    kind = _identity(value.get('kind'))
    if not kind:
        return None
    return SourceRefinementRange(kind=kind, start=_count(value.get('start'), None), end=_count(value.get('end'), None))

def public_refinement(source):
    task = _object(source.metadata.get('catalog_refinement'))
    request_id = task.get('request_id')
    content_hash, source_version = (source.metadata.get('content_hash'), source.metadata.get('source_version'))
    if not isinstance(content_hash, str) or not content_hash or (not isinstance(source_version, str)) or (not source_version) or (not isinstance(request_id, str)) or (not 1 <= len(request_id) <= 128) or (task.get('content_hash') != content_hash) or (task.get('source_version') != source.metadata.get('source_version')):
        return None
    execution = _object(task.get('execution'))
    step = _object(execution.get('current_step'))
    checkpoint = _object(execution.get('last_checkpoint'))
    directory = _object(task.get('directory_transcription'))
    units = directory.get('units') if isinstance(directory.get('units'), list) else []
    review_id = directory.get('review_request_id')
    job = source.ingestion_job
    job_id = _identity(execution.get('job_id'))
    matching_job = job if job and job.phase_history and job.phase_history[-1].startswith('pi_catalog_refinement') and (not job_id or job.id == job_id) else None
    reason = execution.get('stop_reason', '')
    locations = _object(source.metadata.get('catalog_location_summary'))
    return SourceCatalogRefinementView(request_id=task['request_id'], job_id=job_id or (matching_job.id if matching_job else None), mode=_choice(task.get('mode'), {'continue', 'reverify'}), target_chapter_id=_identity(task.get('target_chapter_id')) or None, status=_choice(task.get('status'), _STATUSES, 'interrupted'), phase=_identity(execution.get('phase')), created_at=_timestamp(execution.get('created_at')), started_at=_timestamp(execution.get('started_at')) or (matching_job.created_at if matching_job else None), updated_at=_timestamp(execution.get('updated_at')) or (matching_job.updated_at if matching_job else None), deadline_at=_timestamp(execution.get('deadline_at')), finished_at=_timestamp(execution.get('finished_at')), current_step=SourceRefinementStep(id=_identity(step.get('id')), stage=_identity(step.get('stage')), source_range=_range(step.get('source_range')), fragment_id=_identity(step.get('fragment_id')), attempt=_count(step.get('attempt')), started_at=_timestamp(step.get('started_at')), deadline_at=_timestamp(step.get('deadline_at'))) if step else None, last_checkpoint=SourceRefinementCheckpoint(stage=_identity(checkpoint.get('stage')), source_range=_range(checkpoint.get('source_range')), fragment_id=_identity(checkpoint.get('fragment_id')), saved_at=_timestamp(checkpoint.get('saved_at')), kind=_identity(checkpoint.get('kind'))) if checkpoint else None, stop_reason=_choice(reason, _REASONS, 'failed' if reason else ''), model_calls=_count(execution.get('model_calls')), budget_seconds=_count(execution.get('budget_seconds'), None), max_model_calls=_count(execution.get('max_model_calls'), None), completed_units=sum((_object(unit).get('complete') is True and (not review_id or _object(unit).get('reviewed_for_request_id') == review_id) for unit in units)), total_units=len(units), discovery_complete=directory.get('discovery_complete') is True, location_summary={key: _count(locations[key]) for key in ('verified', 'position_only', 'unresolved', 'outside_uploaded_file', 'total') if key in locations})
