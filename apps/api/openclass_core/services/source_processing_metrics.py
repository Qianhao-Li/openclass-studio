"""Source-only runtime timings. No source body, course writes, or authorization."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import time
import threading
from openclass_core.models import AgentActivityEvent
from openclass_core.services.google_sources.config import GoogleSourceError
_PARSE_SLOTS = threading.BoundedSemaphore(2)

def _now():
    return datetime.now(timezone.utc).isoformat()

def update_metrics(service, source, **changes):

    def update():
        current = service.state.current(source, active=False)
        metrics = {**current.metadata.get('processing_metrics', {}), **changes}
        if metrics.get('started_at'):
            metrics['total_ms'] = max(0, round((datetime.now(timezone.utc) - datetime.fromisoformat(metrics['started_at'])).total_seconds() * 1000))
        current = service.state.update(current, active=False, metadata={'processing_metrics': metrics})
        job = service.job_store.latest_for_source(owner_user_id=current.owner_user_id, package_id=current.package_id, source_id=current.id)
        if job and job.run_id == current.metadata['source_version']:
            events = [event for event in job.agent_activity if not event.metadata.get('processing_metrics_snapshot')]
            status = metrics.get('status', 'running')
            events.append(AgentActivityEvent(id=f'source_metrics_{job.id}', turn_id=job.id, stage='resolve_target', role='SourceProcessor', status=status, label='Source processing timings', metadata={'processing_metrics_snapshot': True, 'processing_metrics': metrics}))
            service.job_store.save(job.model_copy(update={'agent_activity': events, 'processing_metrics': metrics}), owner_user_id=current.owner_user_id, package_id=current.package_id)
        return current
    return service.store.coordinator.run_write(service.store.path, update)

def begin_run(service, source):
    previous = source.metadata.get('processing_metrics', {})
    return update_metrics(service, source, started_at=_now(), finished_at=None, status='running', total_ms=0, stage_timings_ms={}, cache_hits={}, model_calls=0, known_token_calls=0, token_totals={}, input_tokens=None, output_tokens=None, cached_input_tokens=None, original_store_ms=previous.get('original_store_ms'), source_hash=source.metadata['content_hash'])

def cache_hit(service, source, stage):
    current = service.state.current(source, active=False)
    hits = dict(current.metadata.get('processing_metrics', {}).get('cache_hits', {}))
    hits[stage] = hits.get(stage, 0) + 1
    return update_metrics(service, current, cache_hits=hits)

@contextmanager
def phase(service, source, name):
    started = time.monotonic()
    try:
        yield
    finally:
        try:
            current = service.state.current(source, active=False)
            times = dict(current.metadata.get('processing_metrics', {}).get('stage_timings_ms', {}))
            times[name] = times.get(name, 0) + round((time.monotonic() - started) * 1000)
            update_metrics(service, current, stage_timings_ms=times)
        except GoogleSourceError:
            pass

def model_call(service, source, result=None):
    current = service.state.current(source, active=False)
    metrics = current.metadata.get('processing_metrics', {})
    if result is None:
        return update_metrics(service, current, model_calls=metrics.get('model_calls', 0) + 1, input_tokens=None, output_tokens=None, cached_input_tokens=None)
    usage = getattr(result, 'usage', None)
    for event in getattr(result, 'activity', []):
        if isinstance(event.metadata.get('usage'), dict):
            usage = event.metadata['usage']
    if not isinstance(usage, dict):
        return current
    totals = dict(metrics.get('token_totals', {}))
    known = metrics.get('known_token_calls', 0) + 1
    mapping = {'input_tokens': ('prompt_tokens', 'input_tokens'), 'output_tokens': ('completion_tokens', 'output_tokens'), 'cached_input_tokens': ('prompt_cache_hit_tokens', 'cached_tokens')}
    for target, keys in mapping.items():
        value = next((usage[key] for key in keys if type(usage.get(key)) is int), None)
        if value is not None:
            totals[target] = totals.get(target, 0) + value
    visible = {key: totals.get(key) if known == metrics.get('model_calls', 0) else None for key in mapping}
    return update_metrics(service, current, known_token_calls=known, token_totals=totals, **visible)

def finish_run(service, source, status='completed'):
    return update_metrics(service, source, finished_at=_now(), status=status)

@contextmanager
def parsing_slot(service, source, is_cancelled):
    started = time.monotonic()
    waiting = False
    while not _PARSE_SLOTS.acquire(timeout=0.1):
        if is_cancelled():
            raise GoogleSourceError('Ordinary content parsing was cancelled while waiting.')
        if not waiting:
            service._job(source.model_copy(update={'status': 'parsing'}), 'basic_content_waiting', 20)
            waiting = True
    try:
        if is_cancelled():
            raise GoogleSourceError('Ordinary content parsing was cancelled before it started.')
        current = service.state.current(source)
        metrics = current.metadata.get('processing_metrics', {})
        times = dict(metrics.get('stage_timings_ms', {}))
        times['parse_wait'] = times.get('parse_wait', 0) + round((time.monotonic() - started) * 1000)
        update_metrics(service, current, stage_timings_ms=times)
        yield
    finally:
        _PARSE_SLOTS.release()
