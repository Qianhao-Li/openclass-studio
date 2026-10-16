"""One persisted clock and call budget for a complete Pi refinement request."""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import subprocess
import time
import httpx
from openclass_core.models import AgentActivityEvent, new_id
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.source_catalog_refinement_activity import STAGES, project_activity, safe_identity, safe_range, save_activity
REASONS = {'completed', 'completed_with_limits', 'budget_exhausted', 'call_limit', 'step_timeout', 'no_progress', 'cancelled', 'interrupted', 'version_changed', 'model_timeout', 'tool_failed', 'authentication_required', 'unsupported_model', 'failed'}
LONG_STEPS = {'transcribe_directory_fragment', 'verify_directory_page', 'transcribe_directory_unit', 'review_directory_unit', 'reconcile_directory_unit', 'verify_chapter_scope'}

class RefinementStopped(RuntimeError):

    def __init__(self, reason):
        self.reason = reason if reason in REASONS else 'failed'
        super().__init__('Pi catalog refinement stopped: ' + self.reason)

def _iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()

def _seconds(value):
    try:
        moment = datetime.fromisoformat(value)
        return moment.timestamp() if moment.tzinfo else None
    except (TypeError, ValueError):
        return None

class RefinementRuntime:

    def __init__(self, service, source, *, clock=time.time, monotonic=time.monotonic, budget_seconds=None):
        from openclass_core.services.pi_source_runtime import _source_timeout_seconds
        self.service, self.source = (service, source)
        self.clock, self.monotonic = (clock, monotonic)
        self.request_id = source.metadata['catalog_refinement']['request_id']
        self.content_hash = source.metadata['content_hash']
        self.source_version = source.metadata['source_version']
        self._step_deadlines = {}
        self._activity_steps = {}
        budget = min(900, _source_timeout_seconds(), 900 if budget_seconds is None else max(1, int(budget_seconds)))
        latest = service.job_store.latest_for_source(owner_user_id=source.owner_user_id, package_id=source.package_id, source_id=source.id)
        self.job_id = latest.id if latest else ''

        def initialize(execution):
            now = self.clock()
            if execution.get('request_id') not in {None, self.request_id}:
                raise RefinementStopped('version_changed')
            started = _seconds(execution.get('started_at'))
            if started is None:
                execution.update(started_at=_iso(now), deadline_at=_iso(now + budget), budget_seconds=budget, model_calls=0, pi_attempts=0, max_model_calls=60, stop_reason='', finished_at=None)
            else:
                old_deadline = _seconds(execution.get('deadline_at'))
                if old_deadline is None:
                    old_deadline = started + budget
                execution['deadline_at'] = _iso(min(old_deadline, started + 900))
                execution.setdefault('budget_seconds', budget)
                execution.setdefault('model_calls', 0)
                execution.setdefault('pi_attempts', execution['model_calls'])
                execution['max_model_calls'] = 60
            execution['model_call_counter'] = 'provider_requests'
            execution.setdefault('created_at', execution['started_at'])
            execution.setdefault('phase', 'prepare_original')
            execution['request_id'] = self.request_id
            execution.setdefault('job_id', self.job_id)
        execution = self._mutate(initialize)
        self.job_id = execution['job_id']
        self._run_deadline = self.monotonic() + max(0.0, _seconds(execution['deadline_at']) - self.clock())

    def _validate(self, metadata, *, allow_terminal=False):
        task = metadata.get('catalog_refinement', {})
        if task.get('request_id') != self.request_id or task.get('content_hash') != self.content_hash or metadata.get('content_hash') != self.content_hash or (task.get('source_version') != self.source_version) or (metadata.get('source_version') != self.source_version):
            raise RefinementStopped('version_changed')
        if not allow_terminal:
            from openclass_core.services.source_ingestion_jobs import source_task_cancel_requested
            if source_task_cancel_requested() or task.get('status') == 'cancelled':
                raise RefinementStopped('cancelled')
            execution = task.get('execution', {})
            if execution.get('finished_at'):
                raise RefinementStopped(execution.get('stop_reason') or 'interrupted')
            if task.get('status') not in {'queued', 'running'}:
                raise RefinementStopped('interrupted')
        return task

    def _current(self):
        try:
            source = self.service.state.current(self.source, active=False)
        except GoogleSourceError as exc:
            raise RefinementStopped('version_changed') from exc
        return self._validate(source.metadata)

    def _mutate(self, change, *, allow_terminal=False):
        result = {}

        def patch(metadata):
            task = deepcopy(self._validate(metadata, allow_terminal=allow_terminal))
            execution = deepcopy(task.get('execution', {}))
            change(execution)
            execution['updated_at'] = _iso(self.clock())
            task['execution'] = execution
            result.update(execution)
            return {'catalog_refinement': task}
        try:
            self.source = self.service.state.update(self.source, active=False, metadata=patch)
        except GoogleSourceError as exc:
            raise RefinementStopped('version_changed') from exc
        return result

    def _remaining(self, execution, *, include_step=True):
        remaining = min(_seconds(execution['deadline_at']) - self.clock(), self._run_deadline - self.monotonic())
        if remaining < 1:
            raise RefinementStopped('budget_exhausted')
        step = execution.get('current_step', {})
        if include_step and step and (not step.get('finished_at')):
            wall_remaining = _seconds(step['deadline_at']) - self.clock()
            mono_deadline = self._step_deadlines.setdefault(step['id'], self.monotonic() + max(0, wall_remaining))
            if min(wall_remaining, mono_deadline - self.monotonic()) < 1:
                raise RefinementStopped('step_timeout')
            remaining = min(remaining, wall_remaining, mono_deadline - self.monotonic())
        return int(remaining)

    def begin_step(self, work):
        stage = work.get('stage')
        if stage not in STAGES:
            raise ValueError('Unknown source catalog work stage.')
        scope = {'stage': stage, 'source_range': safe_range(work.get('source_range')), 'fragment_id': safe_identity(work.get('fragment_id', ''))}
        if stage == 'verify_chapter_scope':
            digest, page, observation = (work.get(key) for key in ('plan_digest', 'physical_page', 'pass_index'))
            if not isinstance(digest, str) or len(digest) != 64 or any((value not in '0123456789abcdef' for value in digest)) or (type(page) is not int) or (page < 1) or (type(observation) is not int) or (observation not in {1, 2}) or (scope['source_range'] != {'kind': 'pdf_pages', 'start': page, 'end': page}):
                raise ValueError('A chapter observation requires a bound plan, physical page and verification pass.')
            scope.update(plan_digest=digest, physical_page=page, pass_index=observation)
        if stage == 'review_directory_heading_line':
            line_id, inventory = (work.get(key) for key in ('line_id', 'inventory_fingerprint'))
            page = scope['source_range'] or {}
            if not isinstance(line_id, str) or not line_id or safe_identity(line_id) != line_id or (not isinstance(inventory, str)) or (len(inventory) != 64) or any((value not in '0123456789abcdef' for value in inventory)) or (page.get('kind') != 'pdf_pages') or (page.get('start') != page.get('end')):
                raise ValueError('A heading line review requires a safe line identity, inventory fingerprint and one PDF page.')
            scope.update(line_id=line_id, inventory_fingerprint=inventory)
        fingerprint = sha256(json.dumps({**scope, 'plan_revision': work.get('plan_revision'), 'merged_revision': work.get('merged_revision'), 'node_key': (work.get('node') or {}).get('key')}, sort_keys=True).encode()).hexdigest()

        def begin(execution):
            self._remaining(execution, include_step=False)
            current = execution.get('current_step', {})
            if current.get('fingerprint') == fingerprint and (not current.get('finished_at')):
                self._remaining(execution)
                return
            now = self.clock()
            execution.update(phase=stage, current_step={**scope, 'id': new_id('refinestep'), 'fingerprint': fingerprint, 'attempt': 0, 'started_at': _iso(now), 'deadline_at': _iso(min(_seconds(execution['deadline_at']), now + (300 if stage in LONG_STEPS else 120)))})
        result = self._mutate(begin)
        self._emit('source_refinement_step', 'running', result['current_step']['id'])
        return result['current_step']

    def remaining_timeout(self):
        return self._remaining(self._current()['execution'])

    def before_process(self, timeout):
        granted = []

        def reserve(execution):
            remaining = self._remaining(execution)
            step = execution.get('current_step')
            if not step:
                raise RuntimeError('Begin a directory work step before starting Pi.')
            if execution['model_calls'] >= 60 or execution['pi_attempts'] >= 60 or step['attempt'] >= 3:
                raise RefinementStopped('call_limit')
            if type(timeout) not in (int, float) or timeout < 1:
                raise RefinementStopped('step_timeout')
            granted.append(min(remaining, int(timeout)))
            execution['pi_attempts'] += 1
            step['attempt'] += 1
            step['last_attempt_at'] = _iso(self.clock())
        result = self._mutate(reserve)
        self._emit('source_refinement_step', 'running', result['current_step']['id'])
        return granted[0]

    def before_provider_request(self):
        """Reserve each actual provider request, including a Pi tool-followup turn."""
        approval = {}

        def reserve(execution):
            remaining = self._remaining(execution)
            step = execution.get('current_step', {})
            if not step.get('attempt'):
                raise RefinementStopped('interrupted')
            if execution['model_calls'] >= 60:
                raise RefinementStopped('call_limit')
            execution['model_calls'] += 1
            approval.update(approved=True, model_calls=execution['model_calls'], remaining_seconds=remaining, deadline_at=step['deadline_at'])
        self._mutate(reserve)
        return approval

    def step_complete(self, progress_bool):

        def complete(execution):
            step = execution.get('current_step', {})
            if not step:
                return
            if progress_bool:
                step['finished_at'] = _iso(self.clock())
                execution['last_checkpoint'] = {key: step[key] for key in ('stage', 'source_range', 'fragment_id')}
                execution['last_checkpoint'].update(saved_at=_iso(self.clock()), kind='durable_progress')
                execution['stalled_steps'] = 0
            else:
                execution['stalled_steps'] = execution.get('stalled_steps', 0) + 1
        result = self._mutate(complete)
        if progress_bool and result.get('current_step'):
            self._emit('source_refinement_step', 'completed', result['current_step']['id'])
        return result

    def stop(self, reason):
        reason = reason if reason in REASONS else 'failed'
        try:
            self._emit('source_refinement_stop', 'completed' if reason.startswith('completed') else 'cancelled' if reason == 'cancelled' else 'failed', 'stop')
        except RefinementStopped:
            pass

        def finish(execution):
            execution.update(stop_reason=reason, finished_at=_iso(self.clock()))
        return self._mutate(finish, allow_terminal=True)

    def recover_fragment(self, work):
        """Split only an expired fragment, retaining the same request budget."""
        from openclass_core.services.source_catalog_fragments import recover_timed_out_fragment
        from openclass_core.services.source_ingestion_jobs import source_task_cancel_requested
        recovered = []

        def patch(metadata):
            task = deepcopy(self._validate(metadata, allow_terminal=True))
            if source_task_cancel_requested() or task.get('status') == 'cancelled':
                raise RefinementStopped('cancelled')
            if task.get('status') != 'running':
                raise RefinementStopped('interrupted')
            execution = task['execution']
            self._remaining(execution, include_step=False)
            if execution.get('stop_reason') not in {None, '', 'step_timeout'}:
                return {}
            try:
                self._remaining(execution)
            except RefinementStopped as stopped:
                if stopped.reason != 'step_timeout':
                    raise
            else:
                return {}
            if execution.get('current_step', {}).get('stage') != 'transcribe_directory_fragment':
                return {}
            next_work = recover_timed_out_fragment(task, work, reason='step_timeout', request_id=self.request_id, content_hash=self.content_hash, source_version=self.source_version)
            if not next_work:
                return {}
            now = _iso(self.clock())
            execution.update(stop_reason='', finished_at=None, updated_at=now)
            execution['current_step'].update(finished_at=now, recovery='fragment_split')
            recovered.append(next_work)
            return {'catalog_refinement': task}
        try:
            self.source = self.service.state.update(self.source, active=False, metadata=patch)
        except GoogleSourceError as exc:
            raise RefinementStopped('version_changed') from exc
        return bool(recovered)

    def _emit(self, kind, status, identity):
        self.on_activity(AgentActivityEvent(id=identity, turn_id=self.request_id, stage='execute_role', role='pi', label='Pi directory execution', status=status, metadata={'kind': kind}))

    def on_activity(self, event):
        try:
            task = self._current()
        except RefinementStopped:
            return
        step = task['execution'].get('current_step', {})
        if event.metadata.get('kind') in {'pi_process', 'pi_tool_call', 'pi_tool_result'}:
            step = self._activity_steps.setdefault(event.turn_id, deepcopy(step))
        projected = project_activity(event, request_id=self.request_id, step=step, timestamp=_iso(self.clock()))
        if projected is not None:
            save_activity(self.service, self.source, request_id=self.request_id, job_id=self.job_id, event=projected)

    def can_retry(self, exc):
        if isinstance(exc, RefinementStopped):
            return False
        try:
            execution = self._current()['execution']
            self._remaining(execution)
        except RefinementStopped:
            return False
        if execution['model_calls'] >= 60 or execution['pi_attempts'] >= 60 or execution.get('current_step', {}).get('attempt', 0) >= 3:
            return False
        if isinstance(exc, (subprocess.TimeoutExpired, TimeoutError, ConnectionError, httpx.TimeoutException, httpx.NetworkError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in {429, 500, 502, 503, 504}
        text = str(exc).casefold()
        if any((marker in text for marker in ('unauthorized', 'forbidden', 'api key', 'authentication', 'not connected', 'permission', 'unsupported', 'version', 'changed', 'cancelled', 'revoked', 'quota', 'insufficient'))):
            return False
        if isinstance(exc, RuntimeError) and text.startswith('pi source model request failed:') and re.search('\\boverloaded(?:_error)?\\b', text):
            return True
        return isinstance(exc, RuntimeError) and any((marker in text for marker in ('connection reset', 'connection closed', 'stream_read_error', 'stream read error', 'temporarily unavailable', 'timed out', 'timeout', 'status 429', 'status 500', 'status 502', 'status 503', 'status 504', 'rate limit')))
