from __future__ import annotations
import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping
from pydantic import BaseModel, Field, model_validator
from openclass_core.models import BoardDocument, CoursePackage, now_iso
from openclass_core.services.rich_document import document_changed
from openclass_core.services.ridoc_lock import ridoc_file_lock
from openclass_core.services.ridoc_v34_models import DocumentOccurrence, DocumentState, NodeRef
RIDOC_V3_MEDIA_TYPE = 'application/vnd.openclass.ridoc+zip'
RIDOC_V3_PROFILE = 'learning.course-package'
RIDOC_V3_SPEC_VERSION = '3.0'
RIDOC_V3_HISTORY_EDIT_SPEC_VERSION = '3.1'
RIDOC_V3_LEARNING_SEQUENCE_SPEC_VERSION = '3.2'
RIDOC_V3_DOCUMENT_CURSOR_SPEC_VERSION = '3.3'
RIDOC_V34_SPEC_VERSION = '3.4'
RIDOC_V35_SPEC_VERSION = '3.5'
RIDOC_INDEPENDENT_STATE_SPEC_VERSIONS = {RIDOC_V34_SPEC_VERSION, RIDOC_V35_SPEC_VERSION}
RIDOC_V3_CURSOR_SPEC_VERSIONS = {RIDOC_V3_DOCUMENT_CURSOR_SPEC_VERSION, *RIDOC_INDEPENDENT_STATE_SPEC_VERSIONS}
RIDOC_V3_SEQUENCE_SPEC_VERSIONS = {RIDOC_V3_LEARNING_SEQUENCE_SPEC_VERSION, *RIDOC_V3_CURSOR_SPEC_VERSIONS}
RIDOC_V3_SUPPORTED_SPEC_VERSIONS = {RIDOC_V3_SPEC_VERSION, RIDOC_V3_HISTORY_EDIT_SPEC_VERSION, *RIDOC_V3_SEQUENCE_SPEC_VERSIONS}
RIDOC_V3_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
RIDOC_V3_MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
RIDOC_V3_MAX_ENTRIES = 8192
_EXECUTABLE_SUFFIXES = {'.app', '.bat', '.bin', '.cmd', '.com', '.dll', '.dmg', '.exe', '.js', '.msi', '.ps1', '.scr', '.sh'}
_RUNTIME_KEYS = {'agent_activity', 'cancelled', 'error', 'failed', 'latency', 'loading', 'log', 'logs', 'model_request', 'pending', 'retry', 'runtime_snapshot', 'system_prompt', 'token', 'tokens', 'tool_calls'}

class RidocV3Error(ValueError):
    pass

class RidocV3ConflictError(RidocV3Error):
    pass

class RidocV3NodeBase(BaseModel):
    model_config = {'extra': 'forbid'}
    id: str = Field(min_length=1)
    kind: Literal['M', 'C', 'E']
    lesson_id: str = Field(min_length=1)
    branch_name: str = Field(min_length=1)
    parent_ids: list[str] = Field(default_factory=list)
    created_at: str
    transaction_id: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    resource_refs: list[str] = Field(default_factory=list)
    input_state_ref: str = ''
    output_state_ref: str = ''
    decision_metadata: dict[str, Any] = Field(default_factory=dict)
    merge_source_branch: str = ''
    branch_origin_kind: Literal['regenerate', 'fork'] | None = None
    branch_origin_node_id: str = ''
    branch_origin_deleted: bool = False
    merge_target_node_id: str = ''
    merge_source_node_id: str = ''
    learning_context_node_ids: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def normalize_identity(self) -> 'RidocV3NodeBase':
        self.id = self.id.strip()
        self.lesson_id = self.lesson_id.strip()
        self.branch_name = self.branch_name.strip()
        self.transaction_id = self.transaction_id.strip()
        if not all((self.id, self.lesson_id, self.branch_name, self.transaction_id)):
            raise ValueError('RIDOC nodes require stable non-empty identities')
        return self

class RidocMessageNode(RidocV3NodeBase):
    kind: Literal['M'] = 'M'
    user_message: str = Field(min_length=1)
    assistant_message: str = Field(min_length=1)
    document_ref: str = Field(min_length=64, max_length=64)
    assistant_source: str = ''

    @model_validator(mode='after')
    def require_complete_exchange(self) -> 'RidocMessageNode':
        self.user_message = self.user_message.strip()
        self.assistant_message = self.assistant_message.strip()
        if not self.user_message or not self.assistant_message:
            raise ValueError('RIDOC M nodes require a complete user and assistant exchange')
        return self

class RidocDocumentNode(RidocV3NodeBase):
    kind: Literal['C'] = 'C'
    label: str = ''
    source: str = 'manual'
    before_document_ref: str | None = None
    after_document_ref: str = Field(min_length=64, max_length=64)
    action_id: str = Field(min_length=1)
    scope_block_ids: list[str] = Field(default_factory=list)
    initial: bool = False
    source_message_deleted: bool = False
    document_action: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_document_transition(self) -> 'RidocDocumentNode':
        if self.initial:
            if self.parent_ids or self.before_document_ref is not None:
                raise ValueError('RIDOC C0 cannot have parents or a before document')
        elif not self.parent_ids or not self.before_document_ref:
            raise ValueError('RIDOC C nodes require parents and a before document')
        if self.before_document_ref == self.after_document_ref:
            raise ValueError('RIDOC C nodes require a real document change')
        return self

class RidocMergeEventNode(RidocV3NodeBase):
    """A topology event that joins histories without pretending content changed."""
    kind: Literal['E'] = 'E'
    event_kind: Literal['merge'] = 'merge'
    label: str = ''
    document_ref: str = Field(min_length=64, max_length=64)

    @model_validator(mode='after')
    def validate_merge_event(self) -> 'RidocMergeEventNode':
        if self.merge_target_node_id or self.merge_source_node_id:
            if not self.merge_target_node_id or not self.merge_source_node_id:
                raise ValueError('RIDOC merge events require both parent roles')
            if self.parent_ids != list(dict.fromkeys((self.merge_target_node_id, self.merge_source_node_id))):
                raise ValueError('RIDOC merge event parents do not match their roles')
        elif len(self.parent_ids) != 2 or len(set(self.parent_ids)) != 2:
            raise ValueError('RIDOC merge events require two distinct parents')
        if not self.merge_source_branch.strip():
            raise ValueError('RIDOC merge events require a source branch')
        return self
RidocV3Node = RidocMessageNode | RidocDocumentNode | RidocMergeEventNode

def merge_parent_roles(node: RidocV3Node) -> tuple[str, str]:
    """Return semantic merge endpoints, including coincident endpoints after deletion."""
    if not node.merge_source_branch:
        return ('', '')
    if node.merge_target_node_id or node.merge_source_node_id:
        return (node.merge_target_node_id, node.merge_source_node_id)
    return (node.parent_ids[0] if node.parent_ids else '', node.parent_ids[1] if len(node.parent_ids) > 1 else '')

def _node_document_ref(node: RidocV3Node) -> str:
    return node.after_document_ref if isinstance(node, RidocDocumentNode) else node.document_ref

class RidocBranch(BaseModel):
    model_config = {'extra': 'forbid'}
    name: str = Field(min_length=1)
    head_node_id: str = Field(min_length=1)
    base_node_id: str = Field(min_length=1)
    archived: bool = False
    learning_node_ids: list[str] = Field(default_factory=list)
    replay_document_refs: dict[str, str] = Field(default_factory=dict)
    learning_order_merged: bool = False
    document_cursor_node_id: str | None = None
    document_occurrences: dict[str, DocumentOccurrence] = Field(default_factory=dict)
    current_state_ref: str | None = Field(default=None, min_length=64, max_length=64)
    current_trace_ref: str | None = Field(default=None, min_length=64, max_length=64)

class RidocLessonGraph(BaseModel):
    model_config = {'extra': 'forbid'}
    id: str = Field(min_length=1)
    title: str
    slug: str
    summary: str = ''
    tags: list[str] = Field(default_factory=list)
    node_order: list[str]
    branches: dict[str, RidocBranch]
    current_branch: str = 'main'
    merge_records: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = ''
    updated_at: str = ''
    merge_records: list[dict[str, Any]] = Field(default_factory=list)

class RidocCoursePackageV3(BaseModel):
    model_config = {'extra': 'forbid'}
    id: str = Field(min_length=1)
    kind: Literal['course', 'standalone'] = 'course'
    title: str
    summary: str = ''
    tags: list[str] = Field(default_factory=list)
    lesson_order: list[str]
    lessons: dict[str, RidocLessonGraph]
    course_graph: list[dict[str, Any]] = Field(default_factory=list)

class RidocRevision(BaseModel):
    value: str = Field(min_length=64, max_length=64)

@dataclass
class RidocArchiveV3:
    manifest: dict[str, Any]
    package: RidocCoursePackageV3
    nodes: dict[str, RidocV3Node]
    documents: dict[str, dict[str, Any]]
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    assets: dict[str, bytes] = field(default_factory=dict)
    revision: RidocRevision | None = None
    document_states: dict[str, DocumentState] = field(default_factory=dict)
    node_refs: dict[str, NodeRef] = field(default_factory=dict)
    range_proofs: dict[str, dict[str, Any]] = field(default_factory=dict)
    range_traces: dict[str, dict[str, Any]] = field(default_factory=dict)

def canonical_json_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode='json')
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')

def content_hash(value: Any) -> str:
    payload = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(payload).hexdigest()

def document_payload(document: BoardDocument) -> dict[str, Any]:
    return document.model_dump(mode='json')

def store_document(documents: dict[str, dict[str, Any]], document: BoardDocument) -> str:
    payload = document_payload(document)
    digest = content_hash(payload)
    if digest not in documents:
        from openclass_core.services.latex_validation import DocumentMathError, validate_document_math
        try:
            validate_document_math(document)
        except DocumentMathError as exc:
            raise RidocV3Error(str(exc)) from exc
    documents[digest] = payload
    return digest

def _node_key(lesson_id: str, node_id: str) -> str:
    return f'{lesson_id}/{node_id}'

def new_ridoc_v3(package: CoursePackage, *, package_kind: Literal['course', 'standalone'] | None=None) -> RidocArchiveV3:
    documents: dict[str, dict[str, Any]] = {}
    nodes: dict[str, RidocV3Node] = {}
    lessons: dict[str, RidocLessonGraph] = {}
    lesson_order: list[str] = []
    for lesson in package.lessons:
        lesson_order.append(lesson.id)
        document_ref = store_document(documents, lesson.board_document)
        node_id = f'c0_{lesson.id}'
        initial_metadata = lesson.history_graph.commits[0].metadata if lesson.history_graph.commits else {}
        node = RidocDocumentNode(id=node_id, lesson_id=lesson.id, branch_name='main', parent_ids=[], created_at=lesson.created_at or now_iso(), transaction_id=f'initial:{lesson.id}', label='Initial document', source='initial', before_document_ref=None, after_document_ref=document_ref, action_id=f'initial:{lesson.id}', initial=True, decision_metadata={key: value for key, value in initial_metadata.items() if key.startswith('auto_title_')})
        nodes[_node_key(lesson.id, node_id)] = node
        lessons[lesson.id] = RidocLessonGraph(id=lesson.id, title=lesson.title, slug=lesson.slug, summary=lesson.summary, tags=list(lesson.tags), node_order=[node_id], branches={'main': RidocBranch(name='main', head_node_id=node_id, base_node_id=node_id)}, current_branch='main', created_at=lesson.created_at, updated_at=lesson.updated_at)
    course_graph = [item.model_dump(mode='json') for item in package.course_graph]
    archive = RidocArchiveV3(manifest={'spec_version': RIDOC_V3_SPEC_VERSION, 'profile': RIDOC_V3_PROFILE, 'media_type': RIDOC_V3_MEDIA_TYPE, 'capabilities': {'playback': True, 'continue': True, 'fork': True, 'merge': True, 'runtime_state_embedded': False}}, package=RidocCoursePackageV3(id=package.id, kind=package_kind or package.package_kind, title=package.title, summary=package.summary, tags=list(package.tags), lesson_order=lesson_order, lessons=lessons, course_graph=course_graph), nodes=nodes, documents=documents)
    validate_ridoc_v3(archive)
    return archive

def convert_legacy_package_to_ridoc_v3(package: CoursePackage, *, package_kind: Literal['course', 'standalone'] | None=None) -> RidocArchiveV3:
    """Explicit import compatibility; never used as an automatic workspace fallback."""
    base = new_ridoc_v3(package, package_kind=package_kind)
    for lesson in package.lessons:
        graph = base.package.lessons[lesson.id]
        c0_id = graph.node_order[0]
        commit_to_node: dict[str, str] = {}
        commits = list(lesson.history_graph.commits)
        if commits:
            first = commits[0]
            first_ref = store_document(base.documents, first.snapshot)
            c0 = base.nodes[_node_key(lesson.id, c0_id)]
            if not isinstance(c0, RidocDocumentNode):
                raise RidocV3Error('RIDOC C0 is invalid')
            c0.after_document_ref = first_ref
            c0.created_at = first.created_at
            commit_to_node[first.id] = c0_id

        def nearest_parent(commit) -> str:
            pending = list(commit.parent_ids)
            seen: set[str] = set()
            commits_by_id = {item.id: item for item in commits}
            while pending:
                parent_id = pending.pop(0)
                if parent_id in seen:
                    continue
                seen.add(parent_id)
                mapped = commit_to_node.get(parent_id)
                if mapped:
                    return mapped
                parent = commits_by_id.get(parent_id)
                if parent:
                    pending.extend(parent.parent_ids)
            return c0_id
        for commit in commits[1:]:
            metadata = commit.metadata if isinstance(commit.metadata, dict) else {}
            parent_id = nearest_parent(commit)
            parent_node = base.nodes[_node_key(lesson.id, parent_id)]
            before_ref = _node_document_ref(parent_node)
            before_document = BoardDocument.model_validate(base.documents[before_ref])
            user_message = str(metadata.get('user_message') or '').strip()
            assistant_message = str(metadata.get('assistant_message') or '').strip()
            transaction_id = str(metadata.get('turn_id') or metadata.get('chat_turn_id') or metadata.get('codex_turn_id') or metadata.get('client_action_id') or commit.id).strip()
            terminal_id = parent_id
            if user_message and assistant_message:
                message_id = commit.id if metadata.get('ridoc_native') is True else f'm_{commit.id}'
                message = RidocMessageNode(id=message_id, lesson_id=lesson.id, branch_name=commit.branch_name or 'main', parent_ids=[parent_id], created_at=commit.created_at, transaction_id=transaction_id, user_message=user_message, assistant_message=assistant_message, assistant_source=str(metadata.get('assistant_message_source') or 'legacy_import'), document_ref=before_ref)
                base.nodes[_node_key(lesson.id, message_id)] = message
                graph.node_order.append(message_id)
                terminal_id = message_id
            if document_changed(before_document, commit.snapshot):
                after_ref = store_document(base.documents, commit.snapshot)
                document_id = commit.id if metadata.get('ridoc_native') is True else f'c_{commit.id}'
                change = RidocDocumentNode(id=document_id, lesson_id=lesson.id, branch_name=commit.branch_name or 'main', parent_ids=[terminal_id], created_at=commit.created_at, transaction_id=transaction_id, label=commit.label or 'Imported document update', source='legacy_import', before_document_ref=before_ref, after_document_ref=after_ref, action_id=f'action_{commit.id}')
                base.nodes[_node_key(lesson.id, document_id)] = change
                graph.node_order.append(document_id)
                terminal_id = document_id
            commit_to_node[commit.id] = terminal_id
        branches: dict[str, RidocBranch] = {}
        for name, legacy_branch in lesson.history_graph.branches.items():
            head = commit_to_node.get(legacy_branch.head_commit_id, c0_id)
            base_id = commit_to_node.get(legacy_branch.base_commit_id, c0_id)
            branches[name] = RidocBranch(name=name, head_node_id=head, base_node_id=base_id)
        graph.branches = branches or {'main': RidocBranch(name='main', head_node_id=c0_id, base_node_id=c0_id)}
        graph.current_branch = lesson.history_graph.current_branch if lesson.history_graph.current_branch in graph.branches else next(iter(graph.branches))
    referenced = {_node_document_ref(node) for node in base.nodes.values()} | {node.before_document_ref for node in base.nodes.values() if isinstance(node, RidocDocumentNode) and node.before_document_ref}
    base.documents = {digest: payload for digest, payload in base.documents.items() if digest in referenced}
    validate_ridoc_v3(base)
    return base

def _safe_archive_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    return bool(path and (not path.startswith('/')) and ('\\' not in path) and ('..' not in candidate.parts) and (candidate.suffix.lower() not in _EXECUTABLE_SUFFIXES))

def _reject_runtime_metadata(value: Any, path: str='metadata') -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold()
            if normalized in _RUNTIME_KEYS:
                raise RidocV3Error(f'RIDOC formal history contains runtime field {path}.{key}')
            _reject_runtime_metadata(child, f'{path}.{key}')
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_runtime_metadata(child, f'{path}[{index}]')

def _validate_lesson_dag(archive: RidocArchiveV3, lesson: RidocLessonGraph, *, verified_documents: set[str]) -> None:
    if not lesson.node_order or len(lesson.node_order) != len(set(lesson.node_order)):
        raise RidocV3Error('RIDOC lesson node order must be non-empty and unique')
    lesson_nodes: dict[str, RidocV3Node] = {}
    for node_id in lesson.node_order:
        node = archive.nodes.get(_node_key(lesson.id, node_id))
        if node is None or node.lesson_id != lesson.id:
            raise RidocV3Error('RIDOC lesson references a missing or foreign node')
        lesson_nodes[node_id] = node
    if set(lesson_nodes) != {node.id for node in archive.nodes.values() if node.lesson_id == lesson.id}:
        raise RidocV3Error('RIDOC lesson node order does not cover its nodes exactly')
    initial_nodes = [node for node in lesson_nodes.values() if isinstance(node, RidocDocumentNode) and node.initial]
    if len(initial_nodes) != 1:
        raise RidocV3Error('RIDOC lessons require exactly one C0')
    for node in lesson_nodes.values():
        try:
            type(node).model_validate(node.model_dump(mode='json'))
        except ValueError as exc:
            raise RidocV3Error(f'RIDOC node {node.id} is invalid') from exc
        _reject_runtime_metadata(node.decision_metadata)
        for parent_id in node.parent_ids:
            if parent_id not in lesson_nodes:
                raise RidocV3Error('RIDOC node references an unknown parent')
        document_refs = [_node_document_ref(node)]
        if isinstance(node, RidocDocumentNode):
            document_refs.append(node.before_document_ref)
        for digest in document_refs:
            if digest is None or digest in verified_documents:
                continue
            payload = archive.documents.get(digest)
            if payload is None or content_hash(payload) != digest:
                raise RidocV3Error('RIDOC node references a missing or invalid document')
            verified_documents.add(digest)
        for evidence_id in node.evidence_ids:
            if evidence_id not in archive.evidence:
                raise RidocV3Error('RIDOC node references missing evidence')
        for resource_ref in node.resource_refs:
            payload = archive.assets.get(resource_ref)
            if payload is None or content_hash(payload) != resource_ref:
                raise RidocV3Error('RIDOC node references a missing or invalid asset')
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise RidocV3Error('RIDOC node graph contains a cycle')
        if node_id in visited:
            return
        visiting.add(node_id)
        for parent_id in lesson_nodes[node_id].parent_ids:
            visit(parent_id)
        visiting.remove(node_id)
        visited.add(node_id)
    for node_id in lesson.node_order:
        visit(node_id)
    for node in lesson_nodes.values():
        if len(node.parent_ids) != len(set(node.parent_ids)):
            raise RidocV3Error('RIDOC topology parents must be unique')
        if node.branch_origin_node_id and node.branch_origin_node_id not in lesson_nodes:
            raise RidocV3Error('RIDOC branch origin references an unknown node')
        if node.branch_origin_deleted and node.branch_origin_node_id:
            raise RidocV3Error('RIDOC deleted branch origin cannot retain a node reference')
        if isinstance(node, RidocMessageNode):
            if len(node.parent_ids) != 1:
                raise RidocV3Error('RIDOC M nodes require exactly one parent')
            if not node.learning_context_node_ids and node.document_ref != _node_document_ref(lesson_nodes[node.parent_ids[0]]):
                raise RidocV3Error('RIDOC M document must match its parent document')
        if node.merge_source_branch:
            if isinstance(node, RidocMessageNode):
                raise RidocV3Error('RIDOC M nodes cannot represent merges')
            target_id, source_id = merge_parent_roles(node)
            if not target_id or not source_id or node.parent_ids != list(dict.fromkeys((target_id, source_id))):
                raise RidocV3Error('RIDOC merge topology does not match its parent roles')
            target_ref = _node_document_ref(lesson_nodes[target_id])
            before_ref = node.before_document_ref if isinstance(node, RidocDocumentNode) else node.document_ref
            if before_ref != target_ref:
                raise RidocV3Error('RIDOC merge document must match its target parent')
        elif node.merge_target_node_id or node.merge_source_node_id:
            raise RidocV3Error('RIDOC merge parent roles require a merge')
        if isinstance(node, RidocDocumentNode) and (not node.initial) and (not node.merge_source_branch):
            if len(node.parent_ids) != 1 or (not node.learning_context_node_ids and node.before_document_ref != _node_document_ref(lesson_nodes[node.parent_ids[0]])):
                raise RidocV3Error('RIDOC C before document must match its parent document')
        if (node.learning_context_node_ids or (isinstance(node, RidocDocumentNode) and node.document_action)) and archive.manifest.get('spec_version') not in RIDOC_V3_SEQUENCE_SPEC_VERSIONS:
            raise RidocV3Error('Document changes and learning context require RIDOC 3.2')
        if archive.manifest.get('spec_version') == RIDOC_V3_SPEC_VERSION and (node.branch_origin_deleted or node.merge_target_node_id or node.merge_source_node_id or (isinstance(node, RidocDocumentNode) and node.source_message_deleted)):
            raise RidocV3Error('RIDOC history edits require specification 3.1')
    if lesson.current_branch not in lesson.branches:
        raise RidocV3Error('RIDOC current branch is unavailable')
    for name, branch in lesson.branches.items():
        if name != branch.name or branch.head_node_id not in lesson_nodes or branch.base_node_id not in lesson_nodes:
            raise RidocV3Error('RIDOC branch references an invalid node')
    from openclass_core.services.ridoc_learning_sequence import validate_learning_sequences
    validate_learning_sequences(archive, lesson.id)
    from openclass_core.services.ridoc_document_cursor import document_history_state
    for branch in lesson.branches.values():
        if branch.document_cursor_node_id is not None:
            document_history_state(archive, lesson.id, branch.name)

def validate_ridoc_v3(archive: RidocArchiveV3) -> None:
    verified_documents: set[str] = set()
    if archive.manifest.get('spec_version') not in RIDOC_V3_SUPPORTED_SPEC_VERSIONS:
        raise RidocV3Error('Unsupported RIDOC specification version')
    if archive.manifest.get('profile') != RIDOC_V3_PROFILE:
        raise RidocV3Error('Unsupported RIDOC profile')
    if archive.manifest.get('media_type') != RIDOC_V3_MEDIA_TYPE:
        raise RidocV3Error('Unsupported RIDOC media type')
    if archive.manifest.get('spec_version') in RIDOC_INDEPENDENT_STATE_SPEC_VERSIONS:
        from openclass_core.services.ridoc_v34_validation import validate_archive
        try:
            validate_archive(archive)
        except RidocV3Error:
            raise
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise RidocV3Error(f'The RIDOC independent state content format is invalid: {exc}') from exc
        return
    if archive.document_states or archive.node_refs or archive.range_proofs or archive.range_traces or any((node.input_state_ref or node.output_state_ref for node in archive.nodes.values())) or any((branch.document_occurrences or branch.current_state_ref or branch.current_trace_ref for graph in archive.package.lessons.values() for branch in graph.branches.values())):
        raise RidocV3Error('Independent document states and NodeRef require RIDOC 3.4')
    if archive.package.lesson_order != list(dict.fromkeys(archive.package.lesson_order)):
        raise RidocV3Error('RIDOC lesson order must be unique')
    if set(archive.package.lesson_order) != set(archive.package.lessons):
        raise RidocV3Error('RIDOC lesson order must cover every lesson')
    for key, node in archive.nodes.items():
        if key != _node_key(node.lesson_id, node.id) or node.lesson_id not in archive.package.lessons:
            raise RidocV3Error('RIDOC node key does not match its lesson and identity')
    for lesson_id in archive.package.lesson_order:
        lesson = archive.package.lessons[lesson_id]
        if lesson.id != lesson_id:
            raise RidocV3Error('RIDOC lesson key does not match its identity')
        _validate_lesson_dag(archive, lesson, verified_documents=verified_documents)
    referenced_assets = {asset for node in archive.nodes.values() for asset in node.resource_refs}
    if set(archive.assets) != referenced_assets:
        raise RidocV3Error('RIDOC may contain only assets referenced by effective nodes')
    referenced_evidence = {evidence for node in archive.nodes.values() for evidence in node.evidence_ids}
    if set(archive.evidence) != referenced_evidence:
        raise RidocV3Error('RIDOC may contain only evidence referenced by effective nodes')

def _omit_absent_extensions(payload: dict, model: BaseModel, fields: tuple[str, ...]) -> None:
    for name in fields:
        if name not in model.model_fields_set and (not payload.get(name)):
            payload.pop(name, None)

def _content_entries(archive: RidocArchiveV3) -> dict[str, bytes]:
    package = archive.package.model_dump(mode='json')
    for lesson_id, graph in archive.package.lessons.items():
        lesson = package['lessons'][lesson_id]
        _omit_absent_extensions(lesson, graph, ('merge_records',))
        for name, branch in graph.branches.items():
            _omit_absent_extensions(lesson['branches'][name], branch, ('learning_node_ids', 'replay_document_refs', 'learning_order_merged', 'document_cursor_node_id'))
    if archive.manifest.get('spec_version') != RIDOC_V35_SPEC_VERSION:
        for lesson in package['lessons'].values():
            for branch in lesson['branches'].values():
                branch.pop('current_state_ref', None)
                branch.pop('current_trace_ref', None)
                for occurrence in branch.get('document_occurrences', {}).values():
                    for name in ('range_proof_ref', 'input_trace_ref', 'output_trace_ref'):
                        occurrence.pop(name, None)
    if archive.manifest.get('spec_version') not in RIDOC_INDEPENDENT_STATE_SPEC_VERSIONS:
        for lesson in package['lessons'].values():
            for branch in lesson['branches'].values():
                branch.pop('document_occurrences', None)
    else:
        for lesson in package['lessons'].values():
            for branch in lesson['branches'].values():
                branch.pop('replay_document_refs', None)
    entries = {'manifest.json': canonical_json_bytes(archive.manifest), 'package.json': canonical_json_bytes(package), 'evidence/index.json': canonical_json_bytes(archive.evidence)}
    for key, node in archive.nodes.items():
        payload = node.model_dump(mode='json')
        if archive.manifest.get('spec_version') not in RIDOC_INDEPENDENT_STATE_SPEC_VERSIONS:
            payload.pop('input_state_ref', None)
            payload.pop('output_state_ref', None)
        _omit_absent_extensions(payload, node, ('branch_origin_deleted', 'merge_target_node_id', 'merge_source_node_id', 'learning_context_node_ids', 'source_message_deleted', 'document_action'))
        entries[f'nodes/{key}.json'] = canonical_json_bytes(payload)
    if archive.manifest.get('spec_version') in RIDOC_INDEPENDENT_STATE_SPEC_VERSIONS:
        entries['references/index.json'] = canonical_json_bytes({key: ref.model_dump(mode='json') for key, ref in archive.node_refs.items()})
        for digest, state in archive.document_states.items():
            entries[f'states/{digest}.json'] = canonical_json_bytes(state)
    if archive.manifest.get('spec_version') == RIDOC_V35_SPEC_VERSION:
        for digest, proof in archive.range_proofs.items():
            entries[f'range-proofs/{digest}.json'] = canonical_json_bytes(proof)
        for digest, trace in archive.range_traces.items():
            entries[f'range-traces/{digest}.json'] = canonical_json_bytes(trace)
    for digest, document in archive.documents.items():
        entries[f'documents/{digest}.json'] = canonical_json_bytes(document)
    for digest, payload in archive.assets.items():
        entries[f'assets/{digest}'] = payload
    return entries

def _revision(entries: Mapping[str, bytes]) -> tuple[str, dict[str, str]]:
    checksums = {path: content_hash(payload) for path, payload in sorted(entries.items())}
    return (content_hash(checksums), checksums)

def ridoc_revision_for_archive(archive: RidocArchiveV3) -> str:
    validate_ridoc_v3(archive)
    return _revision(_content_entries(archive))[0]

def write_ridoc_v3(archive: RidocArchiveV3, target_path: Path, *, expected_revision: str | None=None) -> RidocRevision:
    validate_ridoc_v3(archive)
    entries = _content_entries(archive)
    revision, checksums = _revision(entries)
    entries['integrity/checksums.json'] = canonical_json_bytes({'algorithm': 'sha256', 'revision': revision, 'files': checksums})
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with ridoc_file_lock(target_path):
        if expected_revision is not None:
            if not target_path.exists():
                raise RidocV3ConflictError('RIDOC revision changed; reload before saving')
            current = read_ridoc_v3(target_path)
            actual = current.revision.value if current.revision else ''
            if actual != expected_revision:
                raise RidocV3ConflictError('RIDOC revision changed; reload before saving')
        descriptor, temporary_name = tempfile.mkstemp(prefix=f'.{target_path.name}.', suffix='.tmp', dir=target_path.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, 'w+b') as raw:
                with zipfile.ZipFile(raw, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as output:
                    for path, payload in sorted(entries.items()):
                        output.writestr(path, payload)
                raw.flush()
                os.fsync(raw.fileno())
            if temporary_path.stat().st_size > RIDOC_V3_MAX_ARCHIVE_BYTES:
                raise RidocV3Error('RIDOC archive exceeds the size limit')
            restored = read_ridoc_v3(temporary_path)
            if not restored.revision or restored.revision.value != revision:
                raise RidocV3Error('RIDOC temporary archive revision mismatch')
            os.replace(temporary_path, target_path)
            directory_fd = os.open(target_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary_path.unlink(missing_ok=True)
    archive.revision = RidocRevision(value=revision)
    return archive.revision

def read_ridoc_v3(source_path: Path) -> RidocArchiveV3:
    try:
        return _read_ridoc_v3(source_path)
    except RidocV3Error:
        raise
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RidocV3Error('The RIDOC course object format is invalid. The course file has not been changed') from exc

def _read_ridoc_v3(source_path: Path) -> RidocArchiveV3:
    if not source_path.is_file() or source_path.stat().st_size > RIDOC_V3_MAX_ARCHIVE_BYTES:
        raise RidocV3Error('RIDOC file is unavailable or too large')
    try:
        archive_file = zipfile.ZipFile(source_path, 'r')
    except (OSError, zipfile.BadZipFile) as exc:
        raise RidocV3Error('RIDOC is not a valid ZIP archive') from exc
    with archive_file:
        infos = archive_file.infolist()
        if len({item.filename for item in infos}) != len(infos):
            raise RidocV3Error('RIDOC archive contains duplicate paths')
        if len(infos) > RIDOC_V3_MAX_ENTRIES:
            raise RidocV3Error('RIDOC contains too many entries')
        if sum((item.file_size for item in infos)) > RIDOC_V3_MAX_EXPANDED_BYTES:
            raise RidocV3Error('RIDOC expanded size exceeds the limit')
        for info in infos:
            if not _safe_archive_path(info.filename):
                raise RidocV3Error('RIDOC contains an unsafe path')
            mode = info.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                raise RidocV3Error('RIDOC symbolic links are not allowed')
        payloads = {info.filename: archive_file.read(info) for info in infos if not info.is_dir()}
    required = {'manifest.json', 'package.json', 'evidence/index.json', 'integrity/checksums.json'}
    if (missing := sorted(required - set(payloads))):
        raise RidocV3Error(f"RIDOC is missing required entries: {', '.join(missing)}")
    integrity = json.loads(payloads.pop('integrity/checksums.json'))
    revision, checksums = _revision(payloads)
    if integrity != {'algorithm': 'sha256', 'revision': revision, 'files': checksums}:
        raise RidocV3Error('RIDOC integrity metadata does not match its content')
    manifest = json.loads(payloads['manifest.json'])
    if manifest.get('spec_version') not in RIDOC_V3_SUPPORTED_SPEC_VERSIONS:
        raise RidocV3Error('Unsupported RIDOC specification version; supported: 3.0–3.5')
    package = RidocCoursePackageV3.model_validate(json.loads(payloads['package.json']))
    evidence = json.loads(payloads['evidence/index.json'])
    nodes: dict[str, RidocV3Node] = {}
    documents: dict[str, dict[str, Any]] = {}
    assets: dict[str, bytes] = {}
    document_states: dict[str, DocumentState] = {}
    node_refs: dict[str, NodeRef] = {}
    range_proofs: dict[str, dict[str, Any]] = {}
    range_traces: dict[str, dict[str, Any]] = {}
    for path, payload in payloads.items():
        if manifest.get('spec_version') == RIDOC_V35_SPEC_VERSION and path.startswith(('documents/', 'states/', 'range-proofs/', 'range-traces/')):
            if not re.fullmatch('(?:documents|states|range-proofs|range-traces)/[0-9a-f]{64}\\.json', path):
                raise RidocV3Error('The RIDOC content-addressed object path is invalid')
        if path.startswith('nodes/') and path.endswith('.json'):
            key = path[len('nodes/'):-len('.json')]
            raw = json.loads(payload)
            if raw.get('kind') == 'M':
                nodes[key] = RidocMessageNode.model_validate(raw)
            elif raw.get('kind') == 'C':
                nodes[key] = RidocDocumentNode.model_validate(raw)
            elif raw.get('kind') == 'E':
                nodes[key] = RidocMergeEventNode.model_validate(raw)
            else:
                raise RidocV3Error('RIDOC contains an unknown node type')
        elif path.startswith('documents/') and path.endswith('.json'):
            digest = PurePosixPath(path).stem
            documents[digest] = json.loads(payload)
        elif path.startswith('assets/'):
            assets[path[len('assets/'):]] = payload
        elif path.startswith('states/') and path.endswith('.json'):
            document_states[PurePosixPath(path).stem] = DocumentState.model_validate(json.loads(payload))
        elif path == 'references/index.json':
            node_refs = {key: NodeRef.model_validate(value) for key, value in json.loads(payload).items()}
        elif manifest.get('spec_version') == RIDOC_V35_SPEC_VERSION and path.startswith('range-proofs/') and path.endswith('.json'):
            range_proofs[PurePosixPath(path).stem] = json.loads(payload)
        elif manifest.get('spec_version') == RIDOC_V35_SPEC_VERSION and path.startswith('range-traces/') and path.endswith('.json'):
            range_traces[PurePosixPath(path).stem] = json.loads(payload)
        elif manifest.get('spec_version') in RIDOC_INDEPENDENT_STATE_SPEC_VERSIONS and path not in {'manifest.json', 'package.json', 'evidence/index.json'}:
            raise RidocV3Error('RIDOC contains undeclared learning objects')
    restored = RidocArchiveV3(manifest=manifest, package=package, nodes=nodes, documents=documents, evidence=evidence, assets=assets, revision=RidocRevision(value=revision), document_states=document_states, node_refs=node_refs, range_proofs=range_proofs, range_traces=range_traces)
    validate_ridoc_v3(restored)
    return restored
