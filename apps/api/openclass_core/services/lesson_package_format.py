from __future__ import annotations
import hashlib
import json
import stat
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from openclass_core.models import EvidenceBundle, Lesson
RIDOC_MEDIA_TYPE = 'application/vnd.openclass.ridoc+zip'
RIDOC_SPEC_VERSION = '1.0'
RIDOC_PROFILE = 'learning.lesson'
RIDOC_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
RIDOC_MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
RIDOC_MAX_ENTRIES = 4096
RIDOC_MAX_JSON_BYTES = 64 * 1024 * 1024
_REQUIRED_JSON_PATHS = {'manifest.json', 'history/graph.json', 'evidence/index.json', 'integrity/checksums.json'}
_SENSITIVE_KEYS = {'access_token', 'api_key', 'apikey', 'asset_path', 'authorization', 'chain_of_thought', 'codex_parent_thread_id', 'codex_replaced_stale_thread_id', 'codex_thread_id', 'cookie', 'hidden_reasoning', 'owner_user_id', 'parser_artifacts_path', 'password', 'raw_reasoning', 'reasoning_content', 'refresh_token', 'secret', 'source_path', 'storage_key', 'system_prompt', 'token'}
_SENSITIVE_QUERY_KEYS = {'access_token', 'api_key', 'apikey', 'authorization', 'key', 'signature', 'token'}
_EXECUTABLE_SUFFIXES = {'.app', '.bat', '.bin', '.cmd', '.com', '.dll', '.dmg', '.exe', '.js', '.msi', '.ps1', '.scr', '.sh'}

class RidocFormatError(ValueError):
    pass

class RidocVersionError(RidocFormatError):
    pass

class RidocSizeError(RidocFormatError):
    pass

@dataclass(frozen=True)
class RidocAsset:
    original_id: str
    mime_type: str
    file_name: str
    content: bytes

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

@dataclass(frozen=True)
class RidocSourcePage:
    source_ingestion_id: str
    source_title: str
    source_file_name: str
    source_mime_type: str
    source_content_hash: str
    page_number: int
    mime_type: str
    content: bytes
    width: int
    height: int
    reference_ids: tuple[str, ...] = ()

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def archive_path(self) -> str:
        source_key = self.source_content_hash or hashlib.sha256(self.source_ingestion_id.encode('utf-8')).hexdigest()
        return f'assets/source-pages/{source_key}/page-{self.page_number:06d}.png'

@dataclass
class RidocArchive:
    manifest: dict[str, Any]
    graph: dict[str, Any]
    events: list[dict[str, Any]]
    snapshots: dict[str, dict[str, Any]]
    evidence: dict[str, Any]
    assets: dict[str, bytes] = field(default_factory=dict)

def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')

def content_hash(value: Any) -> str:
    payload = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(payload).hexdigest()

def build_ridoc_archive(lesson: Lesson, *, evidence_bundles: Iterable[EvidenceBundle]=(), missing_evidence_ids: Iterable[str]=(), assets: Iterable[RidocAsset]=(), source_pages: Iterable[RidocSourcePage]=(), source_page_warnings: Iterable[str]=(), source_mode: str='evidence') -> RidocArchive:
    if source_mode not in {'evidence', 'references'}:
        raise RidocFormatError('Unsupported RIDOC source mode.')
    commits = lesson.history_graph.commits
    if not commits:
        raise RidocFormatError('A RIDOC lesson requires at least one commit.')
    snapshots: dict[str, dict[str, Any]] = {}
    snapshot_ref_by_commit: dict[str, str] = {}
    graph_commits: list[dict[str, Any]] = []
    for commit in commits:
        runtime_snapshot = commit.runtime_snapshot.model_dump(mode='json') if commit.runtime_snapshot is not None else None
        snapshot_payload = {'document': sanitize_export_value(commit.snapshot.model_dump(mode='json')), 'runtime_snapshot': sanitize_export_value(runtime_snapshot)}
        snapshot_ref = content_hash(snapshot_payload)
        snapshots[snapshot_ref] = snapshot_payload
        snapshot_ref_by_commit[commit.id] = snapshot_ref
        graph_commits.append({'id': commit.id, 'label': commit.label, 'message': commit.message, 'branch_name': commit.branch_name, 'created_at': commit.created_at, 'parent_ids': list(commit.parent_ids), 'operations': sanitize_export_value([operation.model_dump(mode='json') for operation in commit.operations]), 'snapshot_ref': snapshot_ref, 'metadata': sanitize_export_value(commit.metadata)})
    branches = {name: sanitize_export_value(branch.model_dump(mode='json')) for name, branch in lesson.history_graph.branches.items()}
    graph = {'lesson': sanitize_export_value({'id': lesson.id, 'title': lesson.title, 'slug': lesson.slug, 'summary': lesson.summary, 'tags': lesson.tags, 'document_id': lesson.board_document.id, 'teaching_guide': lesson.teaching_guide.model_dump(mode='json'), 'created_at': lesson.created_at, 'updated_at': lesson.updated_at}), 'current_branch': lesson.history_graph.current_branch, 'branches': branches, 'commits': graph_commits}
    validate_history_graph(graph, snapshots)
    events = build_semantic_events(graph, snapshot_ref_by_commit)
    bundle_payloads = [sanitize_export_value(bundle.model_dump(mode='json')) for bundle in evidence_bundles]
    missing_ids = sorted({value for value in missing_evidence_ids if value})
    evidence = {'source_mode': source_mode, 'bundles': bundle_payloads if source_mode == 'evidence' else [], 'missing_bundle_ids': missing_ids}
    asset_bytes: dict[str, bytes] = {}
    asset_index: list[dict[str, Any]] = []
    for asset in assets:
        digest = asset.content_hash
        path = f'assets/{digest}'
        asset_bytes[path] = asset.content
        asset_index.append({'original_id': asset.original_id, 'path': path, 'content_hash': digest, 'mime_type': asset.mime_type, 'file_name': Path(asset.file_name).name, 'size_bytes': len(asset.content)})
    source_page_index: list[dict[str, Any]] = []
    for source_page in source_pages:
        path = source_page.archive_path
        asset_bytes[path] = source_page.content
        source_page_index.append({'source_ingestion_id': source_page.source_ingestion_id, 'source_title': source_page.source_title, 'source_file_name': Path(source_page.source_file_name).name, 'source_mime_type': source_page.source_mime_type, 'source_content_hash': source_page.source_content_hash, 'page_number': source_page.page_number, 'page_number_basis': 'physical_1_based', 'representation': 'rendered_full_page', 'path': path, 'content_hash': source_page.content_hash, 'mime_type': source_page.mime_type, 'size_bytes': len(source_page.content), 'width': source_page.width, 'height': source_page.height, 'reference_ids': list(source_page.reference_ids)})
    lineage = _ridoc_lineage(graph_commits)
    page_warnings = [str(value) for value in source_page_warnings if str(value).strip()]
    warnings: list[str] = []
    if missing_ids:
        warnings.append('Some referenced evidence bundles were unavailable during export.')
    warnings.extend(page_warnings)
    manifest = {'document_id': f'ridoc_{uuid.uuid4().hex}', 'spec_version': RIDOC_SPEC_VERSION, 'profile': RIDOC_PROFILE, 'media_type': RIDOC_MEDIA_TYPE, 'exported_at': datetime.now(UTC).isoformat(), 'source_mode': source_mode, 'playback_branch': lesson.history_graph.current_branch, 'lesson': {'title': lesson.title, 'slug': lesson.slug, 'summary': lesson.summary}, 'capabilities': {'playback': True, 'continue': True, 'fork': True, 'merge_history': True, 'board_assets_embedded': bool(asset_index), 'source_evidence_embedded': source_mode == 'evidence' and bool(bundle_payloads), 'cited_source_pages_embedded': bool(source_page_index), 'cited_source_pages_complete': not page_warnings, 'original_sources_embedded': False, 'grounded_continuation': 'cited_pages' if source_page_index and (not warnings) else 'partial'}, 'asset_index': asset_index, 'source_page_index': source_page_index, 'lineage': lineage, 'warnings': warnings}
    archive = RidocArchive(manifest=manifest, graph=graph, events=events, snapshots=snapshots, evidence=evidence, assets=asset_bytes)
    validate_ridoc_archive(archive)
    return archive

def build_semantic_events(graph: Mapping[str, Any], snapshot_ref_by_commit: Mapping[str, str]) -> list[dict[str, Any]]:
    commits = list(graph.get('commits') or [])
    branches = dict(graph.get('branches') or {})
    events: list[dict[str, Any]] = []
    seq = 0

    def add_event(*, event_type: str, commit: Mapping[str, Any], actor: str, payload: Mapping[str, Any] | None=None, checkpoint_commit_id: str | None=None, occurred_at: str | None=None) -> None:
        nonlocal seq
        seq += 1
        metadata = commit.get('metadata') if isinstance(commit.get('metadata'), dict) else {}
        turn_id = _metadata_text(metadata, 'codex_turn_id') or f"turn_{commit.get('id', seq)}"
        events.append({'seq': seq, 'event_id': f'event_{seq:08d}', 'type': event_type, 'actor': actor, 'commit_id': commit.get('id'), 'turn_id': turn_id, 'checkpoint_commit_id': checkpoint_commit_id, 'occurred_at': occurred_at or commit.get('created_at') or '', 'timing': 'normalized', 'payload': sanitize_export_value(dict(payload or {}))})
    for commit in commits:
        commit_id = str(commit.get('id') or '')
        parent_ids = [str(value) for value in commit.get('parent_ids') or []]
        parent_id = parent_ids[0] if parent_ids else None
        metadata = commit.get('metadata') if isinstance(commit.get('metadata'), dict) else {}
        current_snapshot_ref = snapshot_ref_by_commit.get(commit_id)
        parent_snapshot_ref = snapshot_ref_by_commit.get(parent_id or '')
        board_changed = current_snapshot_ref != parent_snapshot_ref
        user_message = _metadata_text(metadata, 'user_message')
        if user_message:
            add_event(event_type='turn.user_message', commit=commit, actor='user', payload={'content': user_message}, checkpoint_commit_id=parent_id)
        selection = metadata.get('selection')
        has_source_selection = isinstance(selection, dict) and selection.get('kind') == 'source'
        if has_source_selection or metadata.get('verified_source_reference_used') is True:
            add_event(event_type='source.reference', commit=commit, actor='resource_resolver', payload={'selection': selection if has_source_selection else None, 'bundle_ids': metadata.get('verified_source_bundle_ids') or [], 'chapter_ids': metadata.get('verified_source_chapter_ids') or [], 'evidence_ids': metadata.get('verified_source_evidence_ids') or []}, checkpoint_commit_id=parent_id)
        activity = metadata.get('agent_activity')
        if isinstance(activity, list):
            for item in activity:
                if not isinstance(item, dict):
                    continue
                public_metadata = item.get('metadata') if isinstance(item.get('metadata'), dict) else {}
                public_metadata = {key: value for key, value in public_metadata.items() if key in {'detail', 'progress', 'reason', 'summary'}}
                add_event(event_type='agent.activity', commit=commit, actor=str(item.get('role') or 'system'), payload={'stage': item.get('stage'), 'label': item.get('label'), 'status': item.get('status'), 'metadata': public_metadata}, checkpoint_commit_id=parent_id, occurred_at=str(item.get('created_at') or commit.get('created_at') or ''))
        assistant_message = _metadata_text(metadata, 'assistant_message')
        if assistant_message:
            add_event(event_type='turn.assistant_message', commit=commit, actor='assistant', payload={'content': assistant_message, 'source': metadata.get('assistant_message_source')}, checkpoint_commit_id=parent_id if board_changed else commit_id)
        if board_changed:
            add_event(event_type='board.changed', commit=commit, actor='board_editor', payload={'before_snapshot_ref': parent_snapshot_ref, 'after_snapshot_ref': current_snapshot_ref, 'operations': commit.get('operations') or []}, checkpoint_commit_id=commit_id)
        if len(parent_ids) > 1:
            add_event(event_type='history.merge', commit=commit, actor='system', payload={'parent_ids': parent_ids, 'branch_name': commit.get('branch_name')}, checkpoint_commit_id=commit_id)
        add_event(event_type='turn.completed', commit=commit, actor='system', payload={'label': commit.get('label'), 'message': commit.get('message')}, checkpoint_commit_id=commit_id)
        for branch_name, branch in branches.items():
            if branch_name == commit.get('branch_name') or not isinstance(branch, dict):
                continue
            if branch.get('base_commit_id') != commit_id:
                continue
            add_event(event_type='history.branch_created', commit=commit, actor='system', payload={'branch_name': branch_name, 'base_commit_id': commit_id}, checkpoint_commit_id=commit_id, occurred_at=str(branch.get('created_at') or commit.get('created_at') or ''))
    return events

def write_ridoc(archive: RidocArchive, target_path: Path) -> Path:
    validate_ridoc_archive(archive)
    entries: dict[str, bytes] = {'manifest.json': canonical_json_bytes(archive.manifest), 'history/graph.json': canonical_json_bytes(archive.graph), 'timeline/events.jsonl': b'\n'.join((canonical_json_bytes(event) for event in archive.events)) + b'\n', 'evidence/index.json': canonical_json_bytes(archive.evidence)}
    for digest, snapshot in archive.snapshots.items():
        entries[f'state/snapshots/{digest}.json'] = canonical_json_bytes(snapshot)
    entries.update(archive.assets)
    checksums = {path: hashlib.sha256(payload).hexdigest() for path, payload in entries.items()}
    entries['integrity/checksums.json'] = canonical_json_bytes({'algorithm': 'sha256', 'files': checksums})
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive_file:
        for path in sorted(entries):
            archive_file.writestr(path, entries[path])
    if target_path.stat().st_size > RIDOC_MAX_ARCHIVE_BYTES:
        target_path.unlink(missing_ok=True)
        raise RidocSizeError('RIDOC archive exceeds the 256 MiB limit.')
    read_ridoc(target_path)
    return target_path

def read_ridoc(source_path: Path) -> RidocArchive:
    if not source_path.is_file():
        raise RidocFormatError('RIDOC file does not exist.')
    if source_path.stat().st_size > RIDOC_MAX_ARCHIVE_BYTES:
        raise RidocSizeError('RIDOC archive exceeds the 256 MiB limit.')
    try:
        archive_file = zipfile.ZipFile(source_path, 'r')
    except (OSError, zipfile.BadZipFile) as exc:
        raise RidocFormatError('RIDOC file is not a valid ZIP archive.') from exc
    with archive_file:
        infos = archive_file.infolist()
        _validate_zip_entries(infos)
        info_by_name = {info.filename: info for info in infos if not info.is_dir()}
        missing = sorted((path for path in _REQUIRED_JSON_PATHS if path not in info_by_name))
        if 'timeline/events.jsonl' not in info_by_name:
            missing.append('timeline/events.jsonl')
        if missing:
            raise RidocFormatError(f"RIDOC archive is missing required entries: {', '.join(missing)}")
        integrity = _read_json_entry(archive_file, info_by_name['integrity/checksums.json'])
        expected_checksums = integrity.get('files') if isinstance(integrity, dict) else None
        if integrity.get('algorithm') != 'sha256' or not isinstance(expected_checksums, dict):
            raise RidocFormatError('RIDOC integrity manifest is invalid.')
        for path, expected in expected_checksums.items():
            info = info_by_name.get(path)
            if info is None or not isinstance(expected, str):
                raise RidocFormatError('RIDOC integrity manifest references a missing entry.')
            actual = hashlib.sha256(archive_file.read(info)).hexdigest()
            if actual != expected:
                raise RidocFormatError(f'RIDOC checksum mismatch for {path}.')
        manifest = _read_json_entry(archive_file, info_by_name['manifest.json'])
        graph = _read_json_entry(archive_file, info_by_name['history/graph.json'])
        evidence = _read_json_entry(archive_file, info_by_name['evidence/index.json'])
        events = _read_events_entry(archive_file, info_by_name['timeline/events.jsonl'])
        snapshots: dict[str, dict[str, Any]] = {}
        assets: dict[str, bytes] = {}
        for name, info in info_by_name.items():
            if name.startswith('state/snapshots/') and name.endswith('.json'):
                digest = PurePosixPath(name).stem
                snapshot = _read_json_entry(archive_file, info)
                if not isinstance(snapshot, dict):
                    raise RidocFormatError(f'RIDOC snapshot {name} is invalid.')
                snapshots[digest] = snapshot
            elif name.startswith('assets/'):
                assets[name] = archive_file.read(info)
    archive = RidocArchive(manifest=manifest, graph=graph, events=events, snapshots=snapshots, evidence=evidence, assets=assets)
    validate_ridoc_archive(archive)
    return archive

def validate_ridoc_archive(archive: RidocArchive) -> None:
    manifest = archive.manifest
    if manifest.get('spec_version') != RIDOC_SPEC_VERSION:
        raise RidocVersionError('Unsupported RIDOC specification version.')
    if manifest.get('profile') != RIDOC_PROFILE:
        raise RidocVersionError('Unsupported RIDOC profile.')
    validate_history_graph(archive.graph, archive.snapshots)
    previous_seq = 0
    commit_ids = {str(commit.get('id')) for commit in archive.graph.get('commits') or []}
    for event in archive.events:
        if not isinstance(event, dict):
            raise RidocFormatError('RIDOC event entries must be JSON objects.')
        seq = event.get('seq')
        if not isinstance(seq, int) or seq != previous_seq + 1:
            raise RidocFormatError('RIDOC event sequence must be contiguous and start at one.')
        previous_seq = seq
        commit_id = event.get('commit_id')
        checkpoint_id = event.get('checkpoint_commit_id')
        if commit_id not in commit_ids:
            raise RidocFormatError('RIDOC event references an unknown commit.')
        if checkpoint_id is not None and checkpoint_id not in commit_ids:
            raise RidocFormatError('RIDOC event checkpoint references an unknown commit.')
    asset_index = manifest.get('asset_index') or []
    for item in asset_index:
        if not isinstance(item, dict):
            raise RidocFormatError('RIDOC asset index entry is invalid.')
        path = item.get('path')
        digest = item.get('content_hash')
        payload = archive.assets.get(path) if isinstance(path, str) else None
        if payload is None or hashlib.sha256(payload).hexdigest() != digest:
            raise RidocFormatError('RIDOC asset index does not match embedded asset bytes.')
    source_page_index = manifest.get('source_page_index') or []
    seen_source_pages: set[tuple[str, int]] = set()
    for item in source_page_index:
        if not isinstance(item, dict):
            raise RidocFormatError('RIDOC source page index entry is invalid.')
        source_id = item.get('source_ingestion_id')
        page_number = item.get('page_number')
        path = item.get('path')
        digest = item.get('content_hash')
        if not isinstance(source_id, str) or not source_id or (not isinstance(page_number, int)) or isinstance(page_number, bool) or (page_number < 1) or (not isinstance(path, str)) or (not path.startswith('assets/source-pages/')):
            raise RidocFormatError('RIDOC source page index entry is invalid.')
        identity = (source_id, page_number)
        if identity in seen_source_pages:
            raise RidocFormatError('RIDOC source page index contains duplicate pages.')
        seen_source_pages.add(identity)
        payload = archive.assets.get(path)
        if payload is None or hashlib.sha256(payload).hexdigest() != digest:
            raise RidocFormatError('RIDOC source page index does not match embedded page bytes.')

def validate_history_graph(graph: Mapping[str, Any], snapshots: Mapping[str, Mapping[str, Any]]) -> None:
    commits = graph.get('commits')
    branches = graph.get('branches')
    if not isinstance(commits, list) or not commits:
        raise RidocFormatError('RIDOC history requires at least one commit.')
    if not isinstance(branches, dict) or not branches:
        raise RidocFormatError('RIDOC history requires at least one branch.')
    commit_by_id: dict[str, Mapping[str, Any]] = {}
    for commit in commits:
        if not isinstance(commit, dict) or not isinstance(commit.get('id'), str):
            raise RidocFormatError('RIDOC history contains an invalid commit.')
        commit_id = commit['id']
        if commit_id in commit_by_id:
            raise RidocFormatError('RIDOC history contains duplicate commit identifiers.')
        commit_by_id[commit_id] = commit
        snapshot_ref = commit.get('snapshot_ref')
        snapshot = snapshots.get(snapshot_ref) if isinstance(snapshot_ref, str) else None
        if snapshot is None or content_hash(snapshot) != snapshot_ref:
            raise RidocFormatError('RIDOC commit references an invalid snapshot.')
    for commit in commits:
        for parent_id in commit.get('parent_ids') or []:
            if parent_id not in commit_by_id:
                raise RidocFormatError('RIDOC history contains a dangling parent commit.')
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(commit_id: str) -> None:
        if commit_id in visiting:
            raise RidocFormatError('RIDOC history contains a commit cycle.')
        if commit_id in visited:
            return
        visiting.add(commit_id)
        for parent_id in commit_by_id[commit_id].get('parent_ids') or []:
            visit(parent_id)
        visiting.remove(commit_id)
        visited.add(commit_id)
    for commit_id in commit_by_id:
        visit(commit_id)
    for name, branch in branches.items():
        if not isinstance(name, str) or not isinstance(branch, dict):
            raise RidocFormatError('RIDOC history contains an invalid branch.')
        if branch.get('head_commit_id') not in commit_by_id or branch.get('base_commit_id') not in commit_by_id:
            raise RidocFormatError('RIDOC branch references an unknown commit.')
    if graph.get('current_branch') not in branches:
        raise RidocFormatError('RIDOC current branch does not exist.')

def sanitize_export_value(value: Any, *, key: str='') -> Any:
    normalized_key = key.lower().replace('-', '_')
    if normalized_key in _SENSITIVE_KEYS or normalized_key.endswith('_api_key'):
        return None
    if isinstance(value, dict):
        return {str(item_key): sanitized for item_key, item_value in value.items() if (sanitized := sanitize_export_value(item_value, key=str(item_key))) is not None}
    if isinstance(value, list):
        return [sanitize_export_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_export_value(item) for item in value]
    if isinstance(value, str):
        if normalized_key in {'source_uri', 'url'}:
            return _sanitize_url(value)
        if normalized_key.endswith('_path'):
            return None
        return value
    return value

def _sanitize_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if not parts.scheme or not parts.netloc:
        return value
    query = [(key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True) if key.lower() not in _SENSITIVE_QUERY_KEYS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))

def _ridoc_lineage(commits: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    origins = []
    for commit in commits:
        metadata = commit.get('metadata') if isinstance(commit.get('metadata'), dict) else {}
        origin = metadata.get('ridoc_origin')
        if isinstance(origin, dict):
            origins.append(origin)
    return sanitize_export_value(origins[-1] if origins else {})

def _metadata_text(metadata: Mapping[str, Any], key: str) -> str:
    value = metadata.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else ''

def _validate_zip_entries(infos: list[zipfile.ZipInfo]) -> None:
    if len(infos) > RIDOC_MAX_ENTRIES:
        raise RidocSizeError('RIDOC archive contains too many entries.')
    names: set[str] = set()
    expanded_bytes = 0
    for info in infos:
        name = info.filename
        path = PurePosixPath(name)
        if not name or name.startswith(('/', '\\')) or '\\' in name or any((part in {'', '.', '..'} for part in path.parts)):
            raise RidocFormatError('RIDOC archive contains an unsafe path.')
        if name in names:
            raise RidocFormatError('RIDOC archive contains duplicate entries.')
        names.add(name)
        if info.flag_bits & 1:
            raise RidocFormatError('Encrypted RIDOC entries are not supported.')
        mode = info.external_attr >> 16
        if mode and stat.S_ISLNK(mode):
            raise RidocFormatError('RIDOC archive cannot contain symbolic links.')
        if not info.is_dir() and path.suffix.lower() in _EXECUTABLE_SUFFIXES:
            raise RidocFormatError('RIDOC archive cannot contain executable files.')
        expanded_bytes += info.file_size
        if expanded_bytes > RIDOC_MAX_EXPANDED_BYTES:
            raise RidocSizeError('RIDOC expanded content exceeds the 1 GiB limit.')
        if info.file_size > 1024 * 1024 and info.compress_size > 0 and (info.file_size / info.compress_size > 200):
            raise RidocSizeError('RIDOC entry has an unsafe compression ratio.')

def _read_json_entry(archive_file: zipfile.ZipFile, info: zipfile.ZipInfo) -> dict[str, Any]:
    if info.file_size > RIDOC_MAX_JSON_BYTES:
        raise RidocSizeError(f'RIDOC JSON entry {info.filename} is too large.')
    try:
        value = json.loads(archive_file.read(info))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RidocFormatError(f'RIDOC JSON entry {info.filename} is invalid.') from exc
    if not isinstance(value, dict):
        raise RidocFormatError(f'RIDOC JSON entry {info.filename} must be an object.')
    return value

def _read_events_entry(archive_file: zipfile.ZipFile, info: zipfile.ZipInfo) -> list[dict[str, Any]]:
    if info.file_size > RIDOC_MAX_JSON_BYTES:
        raise RidocSizeError('RIDOC event timeline is too large.')
    try:
        lines = archive_file.read(info).decode('utf-8').splitlines()
        events = [json.loads(line) for line in lines if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RidocFormatError('RIDOC event timeline is invalid.') from exc
    if not all((isinstance(event, dict) for event in events)):
        raise RidocFormatError('RIDOC event timeline must contain JSON objects.')
    return events
