from __future__ import annotations
from pathlib import Path
from typing import Callable
from openclass_core.models import SourceIngestionRecord, now_iso
from openclass_core.services.open_notebook_adapter import OpenNotebookAdapter, OpenNotebookAdapterError
from openclass_core.services.source_evidence_store import SourceEvidenceStore

class OpenNotebookSourceBackend:
    """Let Open Notebook own source processing without local structure parsing."""

    def __init__(self, *, adapter: OpenNotebookAdapter, store: SourceEvidenceStore, local_path: Callable[[SourceIngestionRecord], Path | None]) -> None:
        self.adapter = adapter
        self.store = store
        self.local_path = local_path

    def search(self, *, owner_user_id: str, package_id: str, query: str, records: list[SourceIngestionRecord], limit: int, source_ids: list[str] | None) -> list[dict[str, object]]:
        notebook_id = self.store.get_notebook_id(owner_user_id=owner_user_id, package_id=package_id)
        if not notebook_id:
            return []
        requested = set(source_ids or [])
        remote_ids = [record.open_notebook_source_id for record in records if record.open_notebook_source_id and (not requested or record.id in requested)]
        if not remote_ids:
            return []
        try:
            return self.adapter.search(notebook_id=notebook_id, query=query, limit=limit, source_ids=remote_ids)
        except OpenNotebookAdapterError as exc:
            raise RuntimeError(self.format_error(exc)) from exc

    def sync_file(self, record: SourceIngestionRecord) -> SourceIngestionRecord:
        path = self.local_path(record)
        if path is None:
            return self.store.save_source(self.failed_record(record, 'Source content is unavailable; import the source again.', phase='open_notebook_upload'))
        notebook_id, error = self.resolve_notebook(owner_user_id=record.owner_user_id, package_id=record.package_id, package_title=str(record.metadata.get('package_title') or record.package_id))
        if error:
            return self.store.save_source(self.failed_record(record, error, phase='create_notebook'))
        try:
            result = self.adapter.upload_file_source(notebook_id=notebook_id, file_name=record.file_name or path.name, content=path.read_bytes(), mime_type=record.mime_type, title=record.title)
        except OpenNotebookAdapterError as exc:
            return self.store.save_source(self.failed_record(record, self.format_error(exc), phase='add_source'))
        return self.store.save_source(record.model_copy(update={'status': status_from_open_notebook(result.status), 'error': '', 'open_notebook_notebook_id': notebook_id, 'open_notebook_source_id': result.source_id, 'open_notebook_command_id': result.command_id, 'metadata': {**record.metadata, 'adapter': 'open_notebook', 'source_processing_owner': 'open_notebook', 'open_notebook_sync_status': status_from_open_notebook(result.status), 'open_notebook_response': result.raw or {}}}))

    def replace_file(self, record: SourceIngestionRecord) -> SourceIngestionRecord:
        previous_remote_id = record.open_notebook_source_id
        replacement = self.sync_file(record.model_copy(update={'open_notebook_source_id': '', 'open_notebook_command_id': ''}))
        if replacement.status != 'failed' and previous_remote_id:
            try:
                self.adapter.delete_source(previous_remote_id)
            except OpenNotebookAdapterError:
                pass
        return replacement

    def resolve_notebook(self, *, owner_user_id: str, package_id: str, package_title: str) -> tuple[str, str]:
        existing = self.store.get_notebook_id(owner_user_id=owner_user_id, package_id=package_id)
        if existing:
            return (existing, '')
        try:
            notebook_id = self.adapter.create_notebook(title=f'OpenClass - {package_title}', description=f'Sources imported for OpenClass package {package_id}.')
        except OpenNotebookAdapterError as exc:
            return ('', self.format_error(exc))
        self.store.upsert_notebook(owner_user_id=owner_user_id, package_id=package_id, notebook_id=notebook_id, title=package_title)
        return (notebook_id, '')

    def refresh(self, record: SourceIngestionRecord, *, mirror_status: bool=False) -> SourceIngestionRecord:
        sync_status = str(record.metadata.get('open_notebook_sync_status') or '')
        if not record.open_notebook_command_id:
            return record
        if sync_status in {'ready', 'failed'}:
            if mirror_status and record.status != sync_status:
                return self.store.save_source(record.model_copy(update={'status': sync_status, 'error': record.error if sync_status == 'failed' else ''}))
            return record
        try:
            command = self.adapter.get_command(record.open_notebook_command_id)
        except OpenNotebookAdapterError as exc:
            return self.store.save_source(record.model_copy(update={'metadata': {**record.metadata, 'open_notebook_sync_warning': self.format_error(exc), 'open_notebook_refreshed_at': now_iso()}}))
        status = status_from_open_notebook(command_status(command))
        error = command_error(command) if status == 'failed' else ''
        return self.store.save_source(record.model_copy(update={'status': status if mirror_status else 'failed' if status == 'failed' else record.status, 'error': error if status == 'failed' else record.error, 'open_notebook_source_id': command_source_id(command) or record.open_notebook_source_id, 'metadata': {**record.metadata, 'open_notebook_sync_status': status, 'last_open_notebook_command': command, 'open_notebook_refreshed_at': now_iso()}}))

    def format_error(self, exc: OpenNotebookAdapterError) -> str:
        raw = str(exc).strip() or 'Open Notebook request failed.'
        lowered = raw.lower()
        if any((needle in lowered for needle in ('connection refused', 'connecterror', 'all connection attempts failed'))):
            return f'Open Notebook 服务未启动或不可达：{self.adapter.api_url}。请先启动 Open Notebook，或设置 OPEN_NOTEBOOK_API_URL 后重试。'
        if 'timed out' in lowered or 'timeout' in lowered:
            return f'Open Notebook 请求超时：{self.adapter.api_url}。请确认服务和 API 可访问。'
        return raw

    def failed_record(self, record: SourceIngestionRecord, error: str, *, phase: str) -> SourceIngestionRecord:
        return record.model_copy(update={'status': 'failed', 'error': error, 'metadata': {**record.metadata, 'error_phase': phase, 'open_notebook_api_url': self.adapter.api_url}})

def status_from_open_notebook(raw_status: str) -> str:
    normalized = (raw_status or '').strip().lower()
    if normalized in {'ready', 'completed', 'complete', 'success', 'succeeded', 'done'}:
        return 'ready'
    if normalized in {'failed', 'error', 'errored'}:
        return 'failed'
    if normalized in {'fetching', 'downloading'}:
        return 'fetching'
    if normalized in {'parsing', 'processing'}:
        return 'parsing'
    if normalized in {'indexing', 'embedding', 'vectorizing'}:
        return 'indexing'
    return 'queued'

def command_status(command: dict[str, object]) -> str:
    for key in ('status', 'state', 'phase'):
        value = command.get(key)
        if isinstance(value, str):
            return value
    nested = command.get('data')
    return command_status(nested) if isinstance(nested, dict) else ''

def command_error(command: dict[str, object]) -> str:
    for key in ('error', 'error_message', 'message', 'detail'):
        value = command.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    result = command.get('result')
    if isinstance(result, dict):
        return command_error(result)
    nested = command.get('data')
    return command_error(nested) if isinstance(nested, dict) else ''

def command_source_id(command: dict[str, object]) -> str:
    for key in ('source_id', 'id', 'record_id'):
        value = command.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    result = command.get('result')
    if isinstance(result, dict):
        return command_source_id(result)
    nested = command.get('data')
    return command_source_id(nested) if isinstance(nested, dict) else ''
