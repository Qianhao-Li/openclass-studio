"""Version-bound completed-range observations survive catalog batch layout changes."""
import hashlib
import json
import time
from pathlib import Path
from openclass_core.services.google_sources.config import GoogleSourceError
from openclass_core.services.google_sources.storage import scope_prefix
from openclass_core.services.source_content_contract import content_digest
from openclass_core.services.source_processing_metrics import model_call, phase, cache_hit

class CatalogObservations:

    def __init__(self, service, source, selection, prompt, mode, input_scope=None):
        self.service = service
        self.source = source
        self.binding = content_digest({'artifact': source.metadata.get('basic_content_artifact'), 'source_version': source.metadata['source_version'], 'prompt': content_digest(prompt), 'model': selection.model_dump(mode='json'), 'algorithm': 'ordinary_catalog_speed_v2', 'mode': mode, **({'input_scope': input_scope} if input_scope is not None else {})})
        self.observations = []
        self.raw = []
        self.extra = []
        self.verified = []
        self.calls = 0
        self.started = time.monotonic()
        self.attempt_calls = 0
        ref = source.metadata.get('basic_catalog_checkpoint')
        if ref:
            service._verify_blob_binding(source, ref)
            from openclass_core.services.google_sources.original_cache import original_cache_root
            path = original_cache_root() / 'basic-content-cache' / (ref['sha256'] + '.json')
            raw = path.read_bytes() if path.is_file() and (not path.is_symlink()) else b''
            previous = json.loads(raw) if len(raw) == ref['size'] and hashlib.sha256(raw).hexdigest() == ref['sha256'] else service.storage.json(ref)
            if previous.get('binding') == self.binding:
                self.observations = previous.get('observations', [])
                self.extra = previous.get('visual_rows', [])
                self.raw = previous.get('pending_observations', [])
                self.verified = previous.get('visual_verified_units', [])
                self.calls = previous.get('model_calls', 0)

    def retained(self, schema):
        rows = []
        covered = set()
        for observed in self.observations:
            rows.extend((schema.model_validate(row) for row in observed['rows']))
            for start, end in observed['ranges']:
                covered.update(range(start, end + 1))
        if covered:
            cache_hit(self.service, self.source, 'catalog_observations')
        return (rows, covered)

    def save(self):
        payload = {'binding': self.binding, 'observations': self.observations, 'visual_rows': self.extra, 'visual_verified_units': self.verified, 'model_calls': self.calls, 'pending_observations': self.raw}
        blob = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        source = self.service.state.current(self.source)
        prefix = scope_prefix(source.owner_user_id, source.package_id, source.id, source.metadata['source_version'])
        ref = self.service.storage.put(bucket=self.service.config.artifact_bucket, name=f'{prefix}/basic-catalog/{hashlib.sha256(blob).hexdigest()}.json', content=blob, mime_type='application/json')
        from openclass_core.services.source_basic_cache import _save_local_artifact
        _save_local_artifact(ref, blob)
        self.source = self.service.state.update(source, metadata={'basic_catalog_checkpoint': ref, 'model_call_count': self.calls, 'google_objects': [*source.metadata.get('google_objects', []), ref]})

    def observe(self, adapter, prompt, payload, schema):
        from openclass_core.services.source_ingestion_jobs import source_task_cancel_requested
        self.check_budget()
        self.service._check(self.source)
        model_call(self.service, self.source)
        with phase(self.service, self.source, 'directory_model'):
            result = adapter.parse_structured(system_prompt=prompt, user_prompt=json.dumps(payload, ensure_ascii=False, separators=(',', ':')), schema=schema, is_cancelled=source_task_cancel_requested, allow_live_web_search=False)
        self.calls += 1
        model_call(self.service, self.source, result)
        self.service._check(self.source)
        observed = schema.model_validate(result.output_parsed)
        if not observed.complete or observed.unread_unit_ids:
            raise GoogleSourceError('The directory model did not finish its supplied original-source batch.')
        return observed

    def raw_observation(self, chunks, result):
        ids = [unit['id'] for chunk in chunks for unit in chunk.units]
        key = content_digest(ids)
        self.raw = [item for item in self.raw if item['key'] != key]
        self.raw.append({'key': key, 'unit_ids': ids, 'result': result.model_dump(mode='json')})
        self.save()

    def saved_result(self, chunks, schema):
        key = content_digest([unit['id'] for chunk in chunks for unit in chunk.units])
        found = next((item for item in self.raw if item['key'] == key), None)
        return schema.model_validate(found['result']) if found else None

    def check_budget(self):
        if self.attempt_calls >= 20 or time.monotonic() - self.started >= 15 * 60:
            raise GoogleSourceError('The catalog processing budget was reached. Saved observations can be resumed without rereading completed ranges.')
        self.attempt_calls += 1

    def add(self, chunks, rows, *, authored_directory=None):
        ids = sorted((unit['id'] for chunk in chunks for unit in chunk.units))
        ranges = []
        for identifier in ids:
            if ranges and identifier == ranges[-1][1] + 1:
                ranges[-1][1] = identifier
            else:
                ranges.append([identifier, identifier])
        self.observations.append({'ranges': ranges, 'rows': [row.model_dump(mode='json') for row in rows], 'authored_directory': authored_directory})
        key = content_digest(ids)
        self.raw = [item for item in self.raw if item['key'] != key]
        self.save()
