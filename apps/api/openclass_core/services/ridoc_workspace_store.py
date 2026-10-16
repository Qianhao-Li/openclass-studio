from __future__ import annotations
import hashlib
import copy
import json
import os
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Literal
from pydantic import BaseModel, Field
from openclass_core.models import BoardDocument, BranchRef, CommitRecord, CoursePackage, Lesson, LessonMergeSession, LessonHistoryGraph, now_iso
from openclass_core.services.lesson_factory import build_teaching_guide, slugify
from openclass_core.services.rich_document import document_changed
from openclass_core.services.ridoc_document_actions import build_document_action
from openclass_core.services.ridoc_document_cursor import applied_branch_replay, applied_branch_sequence, create_document_divergence_branch, document_history_state
from openclass_core.services.ridoc_v34_states import archive_state, branch_refs_view, capture_node, continuation_node_id, install_merge_plan, is_v34, node_refs_view, saved_replay, state_result
from openclass_core.services.ridoc_v34_write_guards import history_write_lock
from openclass_core.services.ridoc_learning_sequence import branch_sequence, build_merge_plan, causal_ids, replay_sequence, require_replay, sequence_at_node, sequence_document_refs, set_branch_sequence
from openclass_core.services.ridoc_deletion_store import RidocDeletionJournal
from openclass_core.services.ridoc_history_mutations import RidocHistoryMutationMixin
from openclass_core.services.ridoc_lock import ridoc_file_lock
from openclass_core.services.ridoc_runtime_recovery import owned_runtime_payload
from openclass_core.services.ridoc_source_citations import source_citations_for_node
from openclass_core.services.studio_ports import host_service
from openclass_core.services.ridoc_v3 import RIDOC_V3_SEQUENCE_SPEC_VERSIONS, RidocArchiveV3, RidocBranch, RidocDocumentNode, RidocLessonGraph, RidocMergeEventNode, RidocMessageNode, RidocRevision, RidocV3ConflictError, RidocV3Error, content_hash, merge_parent_roles, new_ridoc_v3, read_ridoc_v3, store_document, write_ridoc_v3
CourseStoreMode = Literal['legacy', 'ridoc_new_only', 'ridoc_required']

def course_store_mode() -> CourseStoreMode:
    from openclass_core.services.learning_session_context import learning_session_scope
    if learning_session_scope.get() is not None:
        return 'ridoc_required'
    value = os.getenv('OPENCLASS_COURSE_STORE_MODE', 'legacy').strip().lower()
    if value not in {'legacy', 'ridoc_new_only', 'ridoc_required'}:
        raise RidocV3Error('OPENCLASS_COURSE_STORE_MODE is invalid')
    return value

class RidocLessonIndexView(BaseModel):
    id: str
    title: str
    slug: str
    summary: str = ''
    updated_at: str = ''
    current_branch: str | None = None
    created_at: str = ''
    tags: list[str] = Field(default_factory=list)

class RidocPackageIndexView(BaseModel):
    id: str
    package_kind: Literal['course', 'standalone'] = 'course'
    title: str
    summary: str = ''
    tags: list[str] = Field(default_factory=list)
    visibility: str = 'private'
    revision: str
    lessons: list[RidocLessonIndexView]
    active_lesson_id: str | None = None
    open_lesson_ids: list[str] = Field(default_factory=list)
    workspace_tab_order: list[str] = Field(default_factory=list)

class RidocWorkspaceRecord(BaseModel):
    owner_user_id: str
    package_id: str
    path: str
    revision: str
    index: RidocPackageIndexView

def _owner_component(owner_user_id: str) -> str:
    return hashlib.sha256(owner_user_id.encode('utf-8')).hexdigest()

def _package_component(package_id: str) -> str:
    normalized = re.sub('[^A-Za-z0-9._-]+', '-', package_id).strip('.-') or 'course'
    digest = hashlib.sha256(package_id.encode('utf-8')).hexdigest()[:12]
    return f'{normalized[:80]}-{digest}.ridoc'

def _node_document_ref(node: RidocMessageNode | RidocDocumentNode | RidocMergeEventNode) -> str:
    return node.after_document_ref if isinstance(node, RidocDocumentNode) else node.document_ref

def _set_lesson_title(archive: RidocArchiveV3, lesson_id: str, title: str) -> None:
    graph = _graph(archive, lesson_id)
    normalized = title.strip() or graph.title
    if normalized == graph.title:
        return
    graph.title = normalized
    graph.slug = slugify(normalized)
    graph.updated_at = now_iso()
    if archive.package.kind == 'standalone':
        archive.package.title = normalized

def _merge_node_metadata(node: RidocDocumentNode | RidocMergeEventNode, *, document_changed: bool) -> dict[str, Any]:
    decision = dict(node.decision_metadata)
    base_id = str(decision.get('merge_base_commit_id') or decision.get('merge_base_node_id') or '')
    target_head_id, source_head_id = merge_parent_roles(node)
    label = node.label or f'Merge {node.merge_source_branch} into {node.branch_name}'
    return {**decision, 'kind': 'branch_merge', 'history_node_kind': 'merge', 'history_node_title': label, 'history_node_summary': label, 'document_changed': document_changed, 'transaction_id': node.transaction_id, 'ridoc_native': True, 'merge_base_commit_id': base_id, 'merge_target_head_commit_id': target_head_id, 'merge_source_head_commit_id': source_head_id, 'merge_target_branch': node.branch_name, 'merge_source_branch': node.merge_source_branch, 'merge_target_node_id': target_head_id, 'merge_source_node_id': source_head_id}

def _graph(archive: RidocArchiveV3, lesson_id: str):
    graph = archive.package.lessons.get(lesson_id)
    if graph is None:
        raise RidocV3Error('RIDOC lesson is unavailable')
    return graph

def _node(archive: RidocArchiveV3, lesson_id: str, node_id: str):
    try:
        return archive.nodes[f'{lesson_id}/{node_id}']
    except KeyError as exc:
        raise RidocV3Error('RIDOC history node is unavailable') from exc

def _document_at(archive: RidocArchiveV3, lesson_id: str, node_id: str) -> BoardDocument:
    node = _node(archive, lesson_id, node_id)
    return BoardDocument.model_validate(archive.documents[_node_document_ref(node)])

def _write_input_state(archive: RidocArchiveV3, lesson_id: str, node_ids: list[str], parent_id: str) -> dict[str, Any]:
    if is_v34(archive):
        return saved_replay(archive, lesson_id, node_ids)
    legacy = any((isinstance(_node(archive, lesson_id, identity), RidocMergeEventNode) for identity in node_ids))
    if legacy:
        return {'document': _document_at(archive, lesson_id, parent_id), 'versions': {}, 'document_version': parent_id, 'structure_version': parent_id, 'legacy': True}
    return {**require_replay(replay_sequence(archive, lesson_id, node_ids)), 'legacy': False}

def resolve_history_branch_parent(archive: RidocArchiveV3, lesson_id: str, action_kind: Literal['regenerate', 'fork'], source_node_id: str) -> tuple[RidocMessageNode, str]:
    source = _node(archive, lesson_id, source_node_id)
    if not isinstance(source, RidocMessageNode):
        raise RidocV3Error('RIDOC conversation branches require a complete source M')
    if action_kind == 'regenerate':
        if not source.parent_ids:
            raise RidocV3Error('RIDOC source M has no regeneration parent')
        parent_id = source.parent_ids[0]
        if is_v34(archive) and f'{lesson_id}/{parent_id}' not in archive.nodes:
            graph = _graph(archive, lesson_id)
            context = source.learning_context_node_ids
            if not context:
                branch = next((branch for branch in graph.branches.values() if source.id in branch.learning_node_ids), None)
                context = branch.learning_node_ids[:branch.learning_node_ids.index(source.id)] if branch else []
            parent_id = next((identity for identity in reversed(context) if f'{lesson_id}/{identity}' in archive.nodes), graph.node_order[0])
        return (source, parent_id)
    if action_kind != 'fork':
        raise RidocV3Error('Unknown RIDOC conversation branch action')
    linked_changes = [node for node in archive.nodes.values() if isinstance(node, RidocDocumentNode) and node.lesson_id == lesson_id and (node.transaction_id == source.transaction_id) and (source.id in node.parent_ids)]
    if len(linked_changes) > 1:
        raise RidocV3Error('RIDOC source M has ambiguous linked document changes')
    return (source, linked_changes[0].id if linked_changes else source.id)

def resolve_conversation_branch_parent(archive: RidocArchiveV3, lesson_id: str, action_kind: Literal['regenerate', 'fork'], source_node_id: str) -> tuple[RidocMessageNode, str]:
    return resolve_history_branch_parent(archive, lesson_id, action_kind, source_node_id)

def _create_history_branch(graph: RidocLessonGraph, *, source_node: RidocMessageNode, parent_id: str, action_kind: Literal['regenerate', 'fork']) -> tuple[str, RidocBranch]:
    timestamp = now_iso().replace('-', '').replace(':', '')
    timestamp = timestamp.replace('T', '-').split('.', 1)[0]
    prefix = 'refresh' if action_kind == 'regenerate' else 'fork'
    base_name = f'{prefix}-{source_node.id[-10:]}-{timestamp}'
    branch_name = base_name
    suffix = 2
    while branch_name in graph.branches:
        branch_name = f'{base_name}-{suffix}'
        suffix += 1
    branch = RidocBranch(name=branch_name, head_node_id=parent_id, base_node_id=parent_id, document_occurrences={key: value.model_copy(deep=True) for key, value in graph.branches[graph.current_branch if parent_id in graph.branches[graph.current_branch].learning_node_ids else source_node.branch_name if source_node.branch_name in graph.branches else graph.current_branch].document_occurrences.items()})
    graph.branches[branch_name] = branch
    graph.current_branch = branch_name
    return (branch_name, branch)

def document_at_history_node(archive: RidocArchiveV3, lesson_id: str, node_id: str) -> BoardDocument:
    graph = _graph(archive, lesson_id)
    if graph.branches[graph.current_branch].learning_node_ids:
        return require_replay(replay_sequence(archive, lesson_id, sequence_at_node(archive, lesson_id, node_id)))['document'].model_copy(deep=True)
    return _document_at(archive, lesson_id, node_id).model_copy(deep=True)

def _indexed_lesson_summary(archive: RidocArchiveV3, lesson_id: str) -> str:
    from openclass_core.services.lesson_summary import document_content_summary
    graph = archive.package.lessons[lesson_id]
    branch = graph.branches[graph.current_branch]
    if is_v34(archive):
        document = archive_state(archive, lesson_id)['document']
    elif branch.document_cursor_node_id is not None:
        document = applied_branch_replay(archive, lesson_id)['document']
    elif branch.learning_node_ids:
        identity = branch_sequence(archive, lesson_id)[-1]
        document = BoardDocument.model_validate(archive.documents[branch.replay_document_refs[identity]])
    else:
        document = _document_at(archive, lesson_id, branch.head_node_id)
    return document_content_summary(document, title=graph.title, summary=graph.summary)

def _index_view(archive: RidocArchiveV3, *, visibility: str='private', active_lesson_id: str | None=None, open_lesson_ids: list[str] | None=None, workspace_tab_order: list[str] | None=None) -> RidocPackageIndexView:
    if archive.revision is None:
        raise RidocV3Error('RIDOC revision is unavailable')
    lessons = [RidocLessonIndexView(id=archive.package.lessons[lesson_id].id, title=archive.package.lessons[lesson_id].title, slug=archive.package.lessons[lesson_id].slug, summary=_indexed_lesson_summary(archive, lesson_id), updated_at=archive.package.lessons[lesson_id].updated_at, current_branch=archive.package.lessons[lesson_id].current_branch, created_at=archive.package.lessons[lesson_id].created_at, tags=list(archive.package.lessons[lesson_id].tags)) for lesson_id in archive.package.lesson_order]
    lesson_ids = [lesson.id for lesson in lessons]
    active = active_lesson_id if active_lesson_id in lesson_ids else lesson_ids[0] if lesson_ids else None
    opened = [lesson_id for lesson_id in open_lesson_ids or [] if lesson_id in lesson_ids]
    tabs = [lesson_id for lesson_id in workspace_tab_order or [] if lesson_id in lesson_ids]
    if active and active not in opened:
        opened.append(active)
    if active and active not in tabs:
        tabs.append(active)
    return RidocPackageIndexView(id=archive.package.id, package_kind=archive.package.kind, title=archive.package.title, summary=archive.package.summary, tags=list(archive.package.tags), visibility=visibility, revision=archive.revision.value, lessons=lessons, active_lesson_id=active, open_lesson_ids=opened, workspace_tab_order=tabs)

def materialize_course_package(archive: RidocArchiveV3, *, index: RidocPackageIndexView | None=None) -> CoursePackage:
    lessons: list[Lesson] = []
    for lesson_id in archive.package.lesson_order:
        graph = archive.package.lessons[lesson_id]
        commits: list[CommitRecord] = []
        for node_id in graph.node_order:
            node = _node(archive, lesson_id, node_id)
            snapshot = _document_at(archive, lesson_id, node_id)
            if isinstance(node, RidocMessageNode):
                origin_label = 'Original source deleted · ' if node.branch_origin_deleted else ''
                if node.branch_origin_kind and node.branch_origin_node_id:
                    origin_label = ('Regenerated from' if node.branch_origin_kind == 'regenerate' else 'Branched from') + f' {node.branch_origin_node_id[-10:]} · '
                metadata = {'kind': 'basic_chat', 'history_node_kind': 'chat', 'history_node_title': node.user_message, 'history_node_summary': node.assistant_message, 'user_message': node.user_message, 'assistant_message': node.assistant_message, 'assistant_message_source': node.assistant_source, 'transaction_id': node.transaction_id, 'ridoc_native': True, 'history_branch_action': node.branch_origin_kind, 'history_branch_source_node_id': node.branch_origin_node_id}
                citations = source_citations_for_node(archive, node)
                if citations:
                    metadata['source_citations'] = [citation.model_dump(mode='json') for citation in citations]
                label = f'{origin_label}{node.user_message[:80]}'
                message = node.assistant_message[:200]
            elif isinstance(node, RidocMergeEventNode):
                metadata = _merge_node_metadata(node, document_changed=False)
                label = node.label or f'Merge {node.merge_source_branch} into {node.branch_name}'
                message = label
            else:
                is_merge = bool(node.merge_source_branch)
                if is_merge:
                    metadata = {**_merge_node_metadata(node, document_changed=True), 'action_id': node.action_id, 'document_parent_commit_id': node.parent_ids[0]}
                    label = node.label or f'Merge {node.merge_source_branch} into {node.branch_name}'
                    message = label
                else:
                    metadata = {'kind': 'initial_document' if node.initial else 'board_document_edit', 'history_node_kind': 'document', 'history_node_title': 'Initial document' if node.initial else node.label, 'history_node_summary': '' if node.initial else node.label, 'document_changed': not node.initial, 'transaction_id': node.transaction_id, 'action_id': node.action_id, 'ridoc_native': True, 'ridoc_initial': node.initial}
                    if node.initial:
                        metadata.update({key: value for key, value in node.decision_metadata.items() if key.startswith('auto_title_')})
                    label = 'Initial document' if node.initial else node.label or 'Document update'
                    message = label
            metadata['branch_origin_deleted'] = node.branch_origin_deleted
            if is_v34(archive):
                metadata['node_refs'] = node_refs_view(archive, node)
            if isinstance(node, RidocDocumentNode):
                metadata['source_message_deleted'] = node.source_message_deleted
            commits.append(CommitRecord(id=node.id, label=label, message=message, branch_name=node.branch_name, created_at=node.created_at, parent_ids=[identity for identity in node.parent_ids if not is_v34(archive) or f'{lesson_id}/{identity}' in archive.nodes], operations=[], snapshot=snapshot, runtime_snapshot=None, metadata=metadata))
        document_states = {name: document_history_state(archive, lesson_id, name) for name in graph.branches if not any((isinstance(_node(archive, lesson_id, identity), RidocMergeEventNode) for identity in branch_sequence(archive, lesson_id, name)))}
        branches = {name: BranchRef(name=name, head_commit_id=continuation_node_id(archive, lesson_id, name) if is_v34(archive) else branch.head_node_id, base_commit_id=branch.base_node_id if f'{lesson_id}/{branch.base_node_id}' in archive.nodes else graph.node_order[0], document_cursor_commit_id=document_states[name]['current_document_node_id'] if name in document_states else branch.head_node_id, document_redo_commit_ids=document_states[name]['withdrawn_node_ids'] if name in document_states else [], node_refs=branch_refs_view(archive, lesson_id, branch) if is_v34(archive) else [], learning_node_ids=list(branch.learning_node_ids), learning_order_merged=branch.learning_order_merged or any((record.get('target_branch') == name for record in graph.merge_records)), replay_documents={identity: BoardDocument.model_validate(archive.documents[ref]) for identity, ref in (saved_replay(archive, lesson_id, branch.learning_node_ids, branch_name=name)['document_refs'] if is_v34(archive) else branch.replay_document_refs).items()}) for name, branch in graph.branches.items()}
        current_head = branches[graph.current_branch].head_commit_id
        learning_ids = branch_sequence(archive, lesson_id)
        current_branch = graph.branches[graph.current_branch]
        replay_refs = current_branch.replay_document_refs
        current_document = archive_state(archive, lesson_id)['document'] if is_v34(archive) else applied_branch_replay(archive, lesson_id)['document'] if current_branch.document_cursor_node_id is not None else BoardDocument.model_validate(archive.documents[replay_refs[learning_ids[-1]]]) if current_branch.learning_node_ids else _document_at(archive, lesson_id, current_head)
        lessons.append(Lesson(id=graph.id, title=graph.title, slug=graph.slug, summary=graph.summary, tags=list(graph.tags), board_document=current_document.model_copy(update={'title': graph.title}), teaching_guide=build_teaching_guide(graph.id, graph.title, current_document), history_graph=LessonHistoryGraph(branches=branches, commits=commits, current_branch=graph.current_branch), created_at=graph.created_at, updated_at=graph.updated_at))
    view = index or _index_view(archive)
    return CoursePackage(id=archive.package.id, title=archive.package.title, summary=archive.package.summary, tags=list(archive.package.tags), visibility=view.visibility, lessons=lessons, course_graph=[], resources=[], open_lesson_ids=list(view.open_lesson_ids), active_lesson_id=view.active_lesson_id, workspace_tab_order=list(view.workspace_tab_order), storage_kind='ridoc', ridoc_revision=view.revision, package_kind=archive.package.kind)

class RidocWorkspaceStore(RidocHistoryMutationMixin):

    def __init__(self, root: Path, database_path: Path) -> None:
        self.root = root
        self.database_path = database_path
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()
        self._deletion_journal = RidocDeletionJournal(database_path)
        from openclass_core.services.ridoc_batch_delete import recover_ridoc_batch_deletions
        recover_ridoc_batch_deletions(self)
        from openclass_core.services.recently_deleted import _recover_entries, recycle_lock
        with self._lock, recycle_lock(self):
            _recover_entries(self)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA busy_timeout=30000')
            with connection:
                yield connection
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript("\n                CREATE TABLE IF NOT EXISTS ridoc_package_index (\n                    owner_user_id TEXT NOT NULL,\n                    package_id TEXT NOT NULL,\n                    package_kind TEXT NOT NULL DEFAULT 'course',\n                    ridoc_path TEXT NOT NULL,\n                    revision TEXT NOT NULL,\n                    title TEXT NOT NULL,\n                    summary TEXT NOT NULL,\n                    tags_json TEXT NOT NULL,\n                    visibility TEXT NOT NULL DEFAULT 'private',\n                    lessons_json TEXT NOT NULL,\n                    active_lesson_id TEXT,\n                    open_lesson_ids_json TEXT NOT NULL DEFAULT '[]',\n                    workspace_tab_order_json TEXT NOT NULL DEFAULT '[]',\n                    created_at TEXT NOT NULL,\n                    updated_at TEXT NOT NULL,\n                    PRIMARY KEY(owner_user_id, package_id)\n                );\n                CREATE TABLE IF NOT EXISTS ridoc_lesson_index (\n                    owner_user_id TEXT NOT NULL,\n                    lesson_id TEXT NOT NULL,\n                    package_id TEXT NOT NULL,\n                    sort_order INTEGER NOT NULL,\n                    title TEXT NOT NULL,\n                    slug TEXT NOT NULL,\n                    summary TEXT NOT NULL,\n                    updated_at TEXT NOT NULL,\n                    PRIMARY KEY(owner_user_id, lesson_id),\n                    FOREIGN KEY(owner_user_id, package_id)\n                        REFERENCES ridoc_package_index(owner_user_id, package_id)\n                        ON DELETE CASCADE\n                );\n                CREATE TABLE IF NOT EXISTS ridoc_runtime_turns (\n                    owner_user_id TEXT NOT NULL,\n                    package_id TEXT NOT NULL,\n                    lesson_id TEXT NOT NULL,\n                    turn_id TEXT NOT NULL,\n                    state TEXT NOT NULL,\n                    payload_json TEXT NOT NULL,\n                    error TEXT NOT NULL DEFAULT '',\n                    updated_at TEXT NOT NULL,\n                    PRIMARY KEY(owner_user_id, package_id, lesson_id, turn_id)\n                );\n                CREATE TABLE IF NOT EXISTS ridoc_runtime_audit (\n                    id TEXT PRIMARY KEY,\n                    owner_user_id TEXT NOT NULL,\n                    package_id TEXT,\n                    lesson_id TEXT,\n                    kind TEXT NOT NULL,\n                    payload_json TEXT NOT NULL,\n                    created_at TEXT NOT NULL\n                );\n                CREATE TABLE IF NOT EXISTS ridoc_lesson_runtime (\n                    owner_user_id TEXT NOT NULL,\n                    package_id TEXT NOT NULL,\n                    lesson_id TEXT NOT NULL,\n                    runtime_json TEXT NOT NULL,\n                    updated_at TEXT NOT NULL,\n                    PRIMARY KEY(owner_user_id, package_id, lesson_id)\n                );\n                CREATE TABLE IF NOT EXISTS ridoc_merge_sessions (\n                    owner_user_id TEXT NOT NULL,\n                    id TEXT NOT NULL,\n                    package_id TEXT NOT NULL,\n                    lesson_id TEXT NOT NULL,\n                    status TEXT NOT NULL,\n                    version INTEGER NOT NULL,\n                    session_json TEXT NOT NULL,\n                    updated_at TEXT NOT NULL,\n                    PRIMARY KEY(owner_user_id, id)\n                );\n                CREATE INDEX IF NOT EXISTS idx_ridoc_merge_sessions_active\n                    ON ridoc_merge_sessions(\n                        owner_user_id, lesson_id, status, updated_at DESC\n                    );\n                CREATE TABLE IF NOT EXISTS ridoc_repository_index (\n                    repository_id TEXT PRIMARY KEY,\n                    owner_user_id TEXT NOT NULL,\n                    working_ridoc_path TEXT NOT NULL,\n                    working_revision TEXT NOT NULL,\n                    published_ridoc_path TEXT,\n                    published_revision TEXT,\n                    updated_at TEXT NOT NULL\n                );\n                CREATE TABLE IF NOT EXISTS ridoc_workspace_meta (\n                    owner_user_id TEXT PRIMARY KEY,\n                    active_package_id TEXT,\n                    updated_at TEXT NOT NULL\n                );\n                CREATE TABLE IF NOT EXISTS ridoc_public_index (\n                    package_id TEXT PRIMARY KEY,\n                    owner_user_id TEXT NOT NULL,\n                    ridoc_path TEXT NOT NULL,\n                    revision TEXT NOT NULL,\n                    title TEXT NOT NULL,\n                    summary TEXT NOT NULL,\n                    tags_json TEXT NOT NULL,\n                    lesson_ids_json TEXT NOT NULL,\n                    published_at TEXT NOT NULL\n                );\n                ")
            package_columns = {str(row['name']) for row in connection.execute('PRAGMA table_info(ridoc_package_index)').fetchall()}
            package_additions = {'package_kind': "TEXT NOT NULL DEFAULT 'course'", 'visibility': "TEXT NOT NULL DEFAULT 'private'", 'active_lesson_id': 'TEXT', 'open_lesson_ids_json': "TEXT NOT NULL DEFAULT '[]'", 'workspace_tab_order_json': "TEXT NOT NULL DEFAULT '[]'", 'created_at': "TEXT NOT NULL DEFAULT ''", 'updated_at': "TEXT NOT NULL DEFAULT ''"}
            for name, declaration in package_additions.items():
                if name not in package_columns:
                    connection.execute(f'ALTER TABLE ridoc_package_index ADD COLUMN {name} {declaration}')
            stamp = now_iso()
            connection.execute("\n                UPDATE ridoc_package_index\n                SET created_at=CASE WHEN created_at='' THEN ? ELSE created_at END,\n                    updated_at=CASE WHEN updated_at='' THEN ? ELSE updated_at END\n                ", (stamp, stamp))
            workspace_columns = {str(row['name']) for row in connection.execute('PRAGMA table_info(ridoc_workspace_meta)').fetchall()}
            if 'active_package_id' not in workspace_columns:
                connection.execute('DROP TABLE IF EXISTS ridoc_workspace_meta_legacy')
                connection.execute('ALTER TABLE ridoc_workspace_meta RENAME TO ridoc_workspace_meta_legacy')
                connection.execute('\n                    CREATE TABLE ridoc_workspace_meta (\n                        owner_user_id TEXT PRIMARY KEY,\n                        active_package_id TEXT,\n                        updated_at TEXT NOT NULL\n                    )\n                    ')
                if {'key', 'value'} <= workspace_columns:
                    connection.execute("\n                        INSERT INTO ridoc_workspace_meta(owner_user_id, active_package_id, updated_at)\n                        SELECT owner_user_id,\n                               MAX(CASE WHEN key='active_package_id' THEN value END),\n                               ?\n                        FROM ridoc_workspace_meta_legacy\n                        GROUP BY owner_user_id\n                        ", (stamp,))
                connection.execute('DROP TABLE ridoc_workspace_meta_legacy')

    def package_path(self, owner_user_id: str, package_id: str) -> Path:
        return self.root / _owner_component(owner_user_id) / _package_component(package_id)

    def _row_view(self, row: sqlite3.Row) -> RidocPackageIndexView:
        return RidocPackageIndexView(id=row['package_id'], package_kind=row['package_kind'], title=row['title'], summary=row['summary'], tags=json.loads(row['tags_json']), visibility=row['visibility'], revision=row['revision'], lessons=[RidocLessonIndexView.model_validate(item) for item in json.loads(row['lessons_json'])], active_lesson_id=row['active_lesson_id'], open_lesson_ids=json.loads(row['open_lesson_ids_json']), workspace_tab_order=json.loads(row['workspace_tab_order_json']))

    def _existing_index(self, owner_user_id: str, package_id: str) -> RidocPackageIndexView | None:
        with self._connect() as connection:
            row = connection.execute('SELECT * FROM ridoc_package_index WHERE owner_user_id = ? AND package_id = ?', (owner_user_id, package_id)).fetchone()
        return self._row_view(row) if row else None

    def _upsert_index(self, owner_user_id: str, path: Path, archive: RidocArchiveV3, *, previous: RidocPackageIndexView | None=None) -> RidocWorkspaceRecord:
        old = previous or self._existing_index(owner_user_id, archive.package.id)
        view = _index_view(archive, visibility=old.visibility if old else 'private', active_lesson_id=old.active_lesson_id if old else None, open_lesson_ids=old.open_lesson_ids if old else [], workspace_tab_order=old.workspace_tab_order if old else [])
        timestamp = now_iso()
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                connection.execute('\n                    INSERT INTO ridoc_package_index(\n                        owner_user_id, package_id, package_kind, ridoc_path, revision, title, summary,\n                        tags_json, visibility, lessons_json, active_lesson_id,\n                        open_lesson_ids_json, workspace_tab_order_json, created_at, updated_at\n                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                    ON CONFLICT(owner_user_id, package_id) DO UPDATE SET\n                        package_kind=excluded.package_kind,\n                        ridoc_path=excluded.ridoc_path, revision=excluded.revision,\n                        title=excluded.title, summary=excluded.summary,\n                        tags_json=excluded.tags_json, visibility=excluded.visibility,\n                        lessons_json=excluded.lessons_json,\n                        active_lesson_id=excluded.active_lesson_id,\n                        open_lesson_ids_json=excluded.open_lesson_ids_json,\n                        workspace_tab_order_json=excluded.workspace_tab_order_json,\n                        updated_at=excluded.updated_at\n                    ', (owner_user_id, archive.package.id, view.package_kind, str(path), view.revision, view.title, view.summary, json.dumps(view.tags, ensure_ascii=False), view.visibility, json.dumps([item.model_dump(mode='json') for item in view.lessons], ensure_ascii=False), view.active_lesson_id, json.dumps(view.open_lesson_ids), json.dumps(view.workspace_tab_order), timestamp, timestamp))
                connection.execute('DELETE FROM ridoc_lesson_index WHERE owner_user_id = ? AND package_id = ?', (owner_user_id, archive.package.id))
                for order, lesson in enumerate(view.lessons):
                    connection.execute('\n                        INSERT INTO ridoc_lesson_index(\n                            owner_user_id, lesson_id, package_id, sort_order,\n                            title, slug, summary, updated_at\n                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)\n                        ', (owner_user_id, lesson.id, archive.package.id, order, lesson.title, lesson.slug, lesson.summary, lesson.updated_at))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return RidocWorkspaceRecord(owner_user_id=owner_user_id, package_id=archive.package.id, path=str(path), revision=view.revision, index=view)

    def create_from_package(self, owner_user_id: str, package: CoursePackage) -> RidocWorkspaceRecord:
        with self._lock:
            path = self.package_path(owner_user_id, package.id)
            if path.exists() or self._existing_index(owner_user_id, package.id):
                raise RidocV3ConflictError('RIDOC package already exists')
            archive = new_ridoc_v3(package)
            archive.revision = write_ridoc_v3(archive, path)
            return self._upsert_index(owner_user_id, path, archive)

    def import_archive(self, owner_user_id: str, archive: RidocArchiveV3) -> RidocWorkspaceRecord:
        with self._lock:
            package_id = archive.package.id
            path = self.package_path(owner_user_id, package_id)
            if path.exists() or self._existing_index(owner_user_id, package_id):
                raise RidocV3ConflictError('RIDOC package already exists')
            archive.revision = write_ridoc_v3(archive, path)
            return self._upsert_index(owner_user_id, path, archive)

    def list_packages(self, owner_user_id: str) -> list[RidocPackageIndexView]:
        from openclass_core.services.ridoc_batch_delete import recover_ridoc_batch_deletions
        recover_ridoc_batch_deletions(self)
        with self._connect() as connection:
            rows = connection.execute('SELECT * FROM ridoc_package_index WHERE owner_user_id = ? ORDER BY created_at, package_id', (owner_user_id,)).fetchall()
        return [self._row_view(row) for row in rows]

    def active_package_id(self, owner_user_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute('SELECT active_package_id FROM ridoc_workspace_meta WHERE owner_user_id = ?', (owner_user_id,)).fetchone()
        return str(row['active_package_id']) if row and row['active_package_id'] else None

    def set_active_package_id(self, owner_user_id: str, package_id: str | None) -> None:
        if package_id is not None and self._existing_index(owner_user_id, package_id) is None:
            raise RidocV3Error('RIDOC package is unavailable')
        with self._connect() as connection:
            connection.execute('\n                INSERT INTO ridoc_workspace_meta(owner_user_id, active_package_id, updated_at)\n                VALUES (?, ?, ?)\n                ON CONFLICT(owner_user_id) DO UPDATE SET\n                    active_package_id=excluded.active_package_id,\n                    updated_at=excluded.updated_at\n                ', (owner_user_id, package_id, now_iso()))

    def package_id_for_lesson(self, owner_user_id: str, lesson_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute('SELECT package_id FROM ridoc_lesson_index WHERE owner_user_id = ? AND lesson_id = ?', (owner_user_id, lesson_id)).fetchone()
        return str(row['package_id']) if row else None

    @staticmethod
    def _merge_session_payload(session: LessonMergeSession) -> dict[str, Any]:
        payload = session.model_dump(mode='json')
        payload['owner_user_id'] = session.owner_user_id
        payload['merge_blueprint'] = session.merge_blueprint
        return payload

    def load_merge_session(self, owner_user_id: str, session_id: str) -> LessonMergeSession | None:
        with self._connect() as connection:
            row = connection.execute('\n                SELECT sessions.session_json\n                FROM ridoc_merge_sessions AS sessions\n                JOIN ridoc_lesson_index AS lessons\n                  ON lessons.owner_user_id = sessions.owner_user_id\n                 AND lessons.lesson_id = sessions.lesson_id\n                 AND lessons.package_id = sessions.package_id\n                WHERE sessions.owner_user_id = ? AND sessions.id = ?\n                ', (owner_user_id, session_id)).fetchone()
        if row is None:
            return None
        return LessonMergeSession.model_validate(json.loads(row['session_json']))

    def load_active_merge_session(self, owner_user_id: str, lesson_id: str) -> LessonMergeSession | None:
        with self._connect() as connection:
            row = connection.execute("\n                SELECT sessions.session_json\n                FROM ridoc_merge_sessions AS sessions\n                JOIN ridoc_lesson_index AS lessons\n                  ON lessons.owner_user_id = sessions.owner_user_id\n                 AND lessons.lesson_id = sessions.lesson_id\n                 AND lessons.package_id = sessions.package_id\n                WHERE sessions.owner_user_id = ? AND sessions.lesson_id = ?\n                  AND sessions.status IN ('draft', 'ai_running', 'ready', 'stale', 'failed')\n                ORDER BY sessions.updated_at DESC\n                LIMIT 1\n                ", (owner_user_id, lesson_id)).fetchone()
        if row is None:
            return None
        return LessonMergeSession.model_validate(json.loads(row['session_json']))

    def save_merge_session(self, session: LessonMergeSession, *, expected_version: int | None=None) -> bool:
        """Persist a draft only when its RIDOC lesson belongs to its owner."""
        with self._lock:
            with self._connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                try:
                    owner_row = connection.execute('\n                        SELECT package_id\n                        FROM ridoc_lesson_index\n                        WHERE owner_user_id = ? AND lesson_id = ?\n                        ', (session.owner_user_id, session.lesson_id)).fetchone()
                    if owner_row is None:
                        raise ValueError('Merge session RIDOC lesson is not owned by the current user.')
                    package_id = str(owner_row['package_id'])
                    stored = connection.execute('\n                        SELECT package_id, lesson_id, version\n                        FROM ridoc_merge_sessions\n                        WHERE owner_user_id = ? AND id = ?\n                        ', (session.owner_user_id, session.id)).fetchone()
                    if expected_version is not None and (stored is None or int(stored['version']) != expected_version):
                        connection.rollback()
                        return False
                    if stored is not None and (str(stored['package_id']) != package_id or str(stored['lesson_id']) != session.lesson_id):
                        raise ValueError('Merge session RIDOC lesson cannot be changed.')
                    connection.execute('\n                        INSERT INTO ridoc_merge_sessions(\n                            owner_user_id, id, package_id, lesson_id, status,\n                            version, session_json, updated_at\n                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)\n                        ON CONFLICT(owner_user_id, id) DO UPDATE SET\n                            status=excluded.status,\n                            version=excluded.version,\n                            session_json=excluded.session_json,\n                            updated_at=excluded.updated_at\n                        WHERE ridoc_merge_sessions.package_id = excluded.package_id\n                          AND ridoc_merge_sessions.lesson_id = excluded.lesson_id\n                        ', (session.owner_user_id, session.id, package_id, session.lesson_id, session.status, session.version, json.dumps(self._merge_session_payload(session), ensure_ascii=False), session.updated_at))
                    connection.commit()
                    return True
                except Exception:
                    connection.rollback()
                    raise

    def load_archive(self, owner_user_id: str, *, package_id: str | None=None, lesson_id: str | None=None) -> RidocArchiveV3:
        resolved = package_id or self.package_id_for_lesson(owner_user_id, lesson_id or '')
        if not resolved:
            raise RidocV3Error('RIDOC package is unavailable')
        with self._connect() as connection:
            row = connection.execute('SELECT ridoc_path, revision FROM ridoc_package_index WHERE owner_user_id = ? AND package_id = ?', (owner_user_id, resolved)).fetchone()
        if row is None:
            raise RidocV3Error('RIDOC package is unavailable')
        path = Path(row['ridoc_path'])
        with ridoc_file_lock(path):
            archive = read_ridoc_v3(path)
            host_service('recover_workspace_pull', self, owner_user_id, path, archive)
            self._recover_deletions(owner_user_id, path, archive)
            index = self._existing_index(owner_user_id, resolved)
            if not archive.revision or not index or archive.revision.value != index.revision:
                raise RidocV3Error('RIDOC index revision does not match the package')
            return archive

    def load_package(self, owner_user_id: str, *, package_id: str | None=None, lesson_id: str | None=None) -> CoursePackage:
        resolved = package_id or self.package_id_for_lesson(owner_user_id, lesson_id or '')
        if not resolved:
            raise RidocV3Error('RIDOC package is unavailable')
        with ridoc_file_lock(self.package_path(owner_user_id, resolved)):
            return self._load_package_under_lock(owner_user_id, package_id=resolved, lesson_id=lesson_id)

    def _load_package_under_lock(self, owner_user_id: str, *, package_id: str | None=None, lesson_id: str | None=None) -> CoursePackage:
        archive = self.load_archive(owner_user_id, package_id=package_id, lesson_id=lesson_id)
        index = self._existing_index(owner_user_id, archive.package.id)
        if index is None:
            raise RidocV3Error('RIDOC package index is unavailable')
        package = materialize_course_package(archive, index=index)
        with self._connect() as connection:
            rows = connection.execute('\n                SELECT lesson_id, runtime_json FROM ridoc_lesson_runtime\n                WHERE owner_user_id = ? AND package_id = ?\n                ', (owner_user_id, archive.package.id)).fetchall()
        runtime_by_lesson = {str(row['lesson_id']): json.loads(row['runtime_json']) for row in rows}
        for lesson_index, lesson in enumerate(package.lessons):
            runtime = runtime_by_lesson.get(lesson.id)
            if not runtime:
                continue
            from openclass_core.services.ridoc_workflow_runtime import restore_runtime
            package.lessons[lesson_index] = restore_runtime(lesson, runtime, archive.revision.value if archive.revision else '')
        return package

    def save_lesson_runtime(self, *, owner_user_id: str, package_id: str, lesson: Lesson, expected_revision: str | None=None) -> None:
        from openclass_core.services.ridoc_workflow_runtime import runtime_payload
        path = self.package_path(owner_user_id, package_id)
        with self._lock, ridoc_file_lock(path):
            archive = read_ridoc_v3(path)
            revision = archive.revision.value if archive.revision else ''
            if expected_revision is not None and revision != expected_revision:
                raise RidocV3ConflictError('The course changed before its frozen requirement checkpoint')
            authoritative = materialize_course_package(archive, index=self._existing_index(owner_user_id, archive.package.id))
            current_lesson = next((item for item in authoritative.lessons if item.id == lesson.id), None)
            if current_lesson is None:
                raise RidocV3Error('The runtime checkpoint lesson does not belong to this course')
            if expected_revision is not None and (lesson.history_graph.current_branch != current_lesson.history_graph.current_branch or document_changed(lesson.board_document, current_lesson.board_document)):
                raise RidocV3ConflictError('A frozen requirement checkpoint cannot change the current board')
            payload = runtime_payload(lesson, current_lesson, revision)
            with self._connect() as connection:
                connection.execute('\n                    INSERT INTO ridoc_lesson_runtime(\n                        owner_user_id, package_id, lesson_id, runtime_json, updated_at\n                    ) VALUES (?, ?, ?, ?, ?)\n                    ON CONFLICT(owner_user_id, package_id, lesson_id) DO UPDATE SET\n                        runtime_json=excluded.runtime_json,\n                        updated_at=excluded.updated_at\n                    ', (owner_user_id, package_id, lesson.id, json.dumps(payload, ensure_ascii=False), now_iso()))

    def mutate(self, owner_user_id: str, package_id: str, expected_revision: str, callback: Callable[[RidocArchiveV3], None], *, activate_lesson_id: str | None=None) -> RidocWorkspaceRecord:
        path = self.package_path(owner_user_id, package_id)
        with self._lock, ridoc_file_lock(path):
            archive = self.load_archive(owner_user_id, package_id=package_id)
            from openclass_core.services.ridoc_edit_session_store import assert_no_pending_history
            assert_no_pending_history(self, owner_user_id, package_id)
            actual = archive.revision.value if archive.revision else ''
            if actual != expected_revision:
                raise RidocV3ConflictError('RIDOC revision changed; reload before saving')
            previous = self._existing_index(owner_user_id, package_id)
            callback(archive)
            if is_v34(archive):
                from openclass_core.services.ridoc_v34_references import prune_objects
                prune_objects(archive)
            archive.revision = write_ridoc_v3(archive, path, expected_revision=expected_revision)
            if activate_lesson_id is not None:
                previous = (previous or _index_view(archive)).model_copy(update={'active_lesson_id': activate_lesson_id, 'open_lesson_ids': list(dict.fromkeys([*(previous.open_lesson_ids if previous else []), activate_lesson_id])), 'workspace_tab_order': list(dict.fromkeys([*(previous.workspace_tab_order if previous else []), activate_lesson_id]))})
            return self._upsert_index(owner_user_id, path, archive, previous=previous)

    def _transaction_exists(self, archive: RidocArchiveV3, transaction_id: str) -> bool:
        return any((node.transaction_id == transaction_id for node in archive.nodes.values())) or any((record.get('transaction_id') == transaction_id for graph in archive.package.lessons.values() for record in graph.merge_records))

    def commit_turn(self, *, owner_user_id: str, package_id: str, lesson_id: str, expected_revision: str, transaction_id: str, user_message: str, assistant_message: str, assistant_source: str='', document: BoardDocument | None=None, new_lesson: Lesson | None=None, document_source: str='ai', document_label: str='Document update', evidence_ids: list[str] | None=None, resource_refs: list[str] | None=None, evidence_payloads: dict[str, dict] | None=None, asset_payloads: dict[str, bytes] | None=None, decision_metadata: dict[str, Any] | None=None, package_metadata: dict[str, Any] | None=None, lesson_metadata: dict[str, Any] | None=None, branch_action_kind: Literal['regenerate', 'fork'] | None=None, branch_source_node_id: str='') -> RidocWorkspaceRecord:
        transaction = transaction_id.strip()
        user = user_message.strip()
        assistant = assistant_message.strip()
        if not transaction or not user or (not assistant):
            raise RidocV3Error('RIDOC turns require a complete M')
        origin_lesson_id = lesson_id
        if new_lesson is not None:
            from openclass_core.services.rich_document import is_document_empty
            if branch_action_kind is not None or new_lesson.id == origin_lesson_id:
                raise RidocV3Error('A new lesson destination must be distinct from its original lesson')
            if not is_document_empty(new_lesson.board_document):
                raise RidocV3Error('A new lesson destination requires an empty initial document')
            if document is None or is_document_empty(document) or document.id != new_lesson.board_document.id:
                raise RidocV3Error('A new lesson destination requires a completed matching board')
        current = self.load_archive(owner_user_id, package_id=package_id)
        self.assert_turn_not_deleted(owner_user_id=owner_user_id, package_id=package_id, lesson_id=lesson_id, transaction_id=transaction)
        if new_lesson is not None:
            for existing_lesson_id in current.package.lesson_order:
                self.deletion_journal.assert_not_deleted(owner_user_id, self.deletion_scope_id(package_id), existing_lesson_id, transaction)
        if self._transaction_exists(current, transaction):
            matching = [node for node in current.nodes.values() if node.transaction_id == transaction and isinstance(node, RidocMessageNode)]
            if new_lesson is not None and (len(matching) != 1 or matching[0].decision_metadata.get('origin_lesson_id') != origin_lesson_id or matching[0].user_message != user):
                raise RidocV3ConflictError('The completed turn belongs to another lesson or request')
            index = self._existing_index(owner_user_id, package_id)
            if index is None or current.revision is None:
                raise RidocV3Error('RIDOC index is unavailable')
            return RidocWorkspaceRecord(owner_user_id=owner_user_id, package_id=package_id, path=str(self.package_path(owner_user_id, package_id)), revision=current.revision.value, index=index)

        def append(archive: RidocArchiveV3) -> None:
            _graph(archive, origin_lesson_id)
            lesson_id = new_lesson.id if new_lesson is not None else origin_lesson_id
            if new_lesson is not None:
                for existing_lesson_id in archive.package.lesson_order:
                    self.deletion_journal.assert_not_deleted(owner_user_id, self.deletion_scope_id(package_id), existing_lesson_id, transaction)
                from openclass_core.services.ridoc_turn_destination import append_empty_destination
                append_empty_destination(archive, new_lesson)
            from openclass_core.services.ridoc_v3 import content_hash
            for digest, payload in (evidence_payloads or {}).items():
                if digest not in (evidence_ids or []) or content_hash(payload) != digest:
                    raise RidocV3Error('Prepared evidence identity is invalid or unreferenced')
                archive.evidence[digest] = payload
            for digest, payload in (asset_payloads or {}).items():
                if digest not in (resource_refs or []) or content_hash(payload) != digest:
                    raise RidocV3Error('Prepared asset identity is invalid or unreferenced')
                archive.assets[digest] = payload
            self.deletion_journal.assert_not_deleted(owner_user_id, self.deletion_scope_id(package_id), origin_lesson_id, transaction)
            graph = _graph(archive, lesson_id)
            if package_metadata:
                for field in ('title', 'summary', 'tags'):
                    if field in package_metadata:
                        setattr(archive.package, field, package_metadata[field])
            if lesson_metadata:
                for field in ('title', 'slug', 'summary', 'tags'):
                    if field in lesson_metadata:
                        if field == 'title':
                            _set_lesson_title(archive, lesson_id, lesson_metadata[field])
                        else:
                            setattr(graph, field, lesson_metadata[field])
            branch_name = graph.current_branch
            branch = graph.branches[branch_name]
            parent_id = continuation_node_id(archive, lesson_id) if is_v34(archive) else branch.head_node_id
            input_sequence = branch_sequence(archive, lesson_id)
            inherited_merged = branch.learning_order_merged
            source_node: RidocMessageNode | None = None
            if branch_action_kind is not None:
                source_node, parent_id = resolve_history_branch_parent(archive, lesson_id, branch_action_kind, branch_source_node_id)
                if branch_action_kind == 'regenerate' and user != source_node.user_message:
                    raise RidocV3Error('RIDOC regenerated answers must reuse the original user message')
                input_sequence = sequence_at_node(archive, lesson_id, parent_id)
                branch_name, branch = _create_history_branch(graph, source_node=source_node, parent_id=parent_id, action_kind=branch_action_kind)
                branch.learning_order_merged = inherited_merged
            elif branch.document_cursor_node_id is not None and document_history_state(archive, lesson_id)['withdrawn_node_ids']:
                input_sequence = applied_branch_sequence(archive, lesson_id)
                branch_name, branch = create_document_divergence_branch(graph, transaction)
            input_state = _write_input_state(archive, lesson_id, input_sequence, parent_id)
            current_document = input_state['document']
            result_document = document.model_copy(update={'title': current_document.title}) if document is not None else None
            current_ref = store_document(archive.documents, current_document)
            identity = hashlib.sha256(transaction.encode('utf-8')).hexdigest()[:24]
            message_id = f'm_{identity}'
            message = RidocMessageNode(id=message_id, lesson_id=lesson_id, branch_name=branch_name, parent_ids=[parent_id], created_at=now_iso(), transaction_id=transaction, user_message=user, assistant_message=assistant, assistant_source=assistant_source, document_ref=current_ref, evidence_ids=list(evidence_ids or []), resource_refs=list(resource_refs or []), decision_metadata={**(decision_metadata or {}), **({'origin_lesson_id': origin_lesson_id, 'destination_lesson_id': lesson_id} if new_lesson is not None else {})}, branch_origin_kind=branch_action_kind, branch_origin_node_id=source_node.id if source_node is not None else '', learning_context_node_ids=[] if input_state['legacy'] else input_sequence)
            archive.nodes[f'{lesson_id}/{message_id}'] = message
            if is_v34(archive):
                capture_node(archive, message, input_state)
            graph.node_order.append(message_id)
            terminal_id = message_id
            if result_document is not None and document_changed(current_document, result_document):
                after_ref = store_document(archive.documents, result_document)
                document_id = f'c_{identity}'
                change = RidocDocumentNode(id=document_id, lesson_id=lesson_id, branch_name=branch_name, parent_ids=[message_id], created_at=message.created_at, transaction_id=transaction, label=document_label, source=document_source, before_document_ref=current_ref, after_document_ref=after_ref, action_id=f'action_{identity}', evidence_ids=list(evidence_ids or []), resource_refs=list(resource_refs or []), branch_origin_kind=branch_action_kind, branch_origin_node_id=source_node.id if source_node is not None else '', learning_context_node_ids=[] if input_state['legacy'] else input_sequence, document_action={} if input_state['legacy'] else build_document_action(current_document, result_document, versions=input_state['versions'], document_version=input_state['document_version'], structure_version=input_state['structure_version'], allow_local=document_source == 'manual'))
                archive.nodes[f'{lesson_id}/{document_id}'] = change
                if is_v34(archive):
                    capture_node(archive, change, input_state)
                graph.node_order.append(document_id)
                terminal_id = document_id
            branch.head_node_id = terminal_id
            if not input_state['legacy']:
                set_branch_sequence(archive, lesson_id, branch_name, [*input_sequence, message_id, *([terminal_id] if terminal_id != message_id else [])])
            graph.updated_at = now_iso()
        if new_lesson is None:
            return self.mutate(owner_user_id, package_id, expected_revision, append)
        return self.mutate(owner_user_id, package_id, expected_revision, append, activate_lesson_id=new_lesson.id)

    def add_lesson(self, *, owner_user_id: str, package_id: str, expected_revision: str, lesson: Lesson) -> RidocWorkspaceRecord:

        def append(archive: RidocArchiveV3) -> None:
            if archive.package.kind == 'standalone':
                raise RidocV3ConflictError('Create a separate RIDOC for each standalone lesson')
            if lesson.id in archive.package.lessons:
                raise RidocV3ConflictError('RIDOC lesson already exists')
            wrapper = CoursePackage(id=package_id, title=archive.package.title, summary=archive.package.summary, lessons=[lesson], package_kind=archive.package.kind)
            addition = new_ridoc_v3(wrapper, package_kind=archive.package.kind)
            if is_v34(archive):
                from openclass_core.services.ridoc_v34 import upgrade_archive
                upgrade_archive(addition)
            graph = addition.package.lessons[lesson.id]
            archive.package.lessons[lesson.id] = graph
            archive.package.lesson_order.append(lesson.id)
            for key, node in addition.nodes.items():
                archive.nodes[key] = node
            archive.documents.update(addition.documents)
            archive.document_states.update(addition.document_states)
            archive.node_refs.update(addition.node_refs)
        return self.mutate(owner_user_id, package_id, expected_revision, append)

    def update_lesson_metadata(self, *, owner_user_id: str, package_id: str, lesson_id: str, expected_revision: str, title: str | None=None, summary: str | None=None, tags: list[str] | None=None) -> RidocWorkspaceRecord:

        def update(archive: RidocArchiveV3) -> None:
            graph = _graph(archive, lesson_id)
            if title is not None:
                if not title.strip():
                    raise RidocV3Error('RIDOC lesson title is required')
                _set_lesson_title(archive, lesson_id, title)
            if summary is not None:
                graph.summary = summary.strip()
            if tags is not None:
                graph.tags = list(dict.fromkeys((tag.strip() for tag in tags if tag.strip())))
            if summary is not None or tags is not None:
                graph.updated_at = now_iso()
        return self.mutate(owner_user_id, package_id, expected_revision, update)

    def delete_lesson(self, *, owner_user_id: str, package_id: str, lesson_id: str, expected_revision: str) -> RidocWorkspaceRecord:
        from openclass_core.services.ridoc_batch_delete import delete_ridoc_lessons_batch
        with self._lock:
            if self.package_id_for_lesson(owner_user_id, lesson_id) != package_id:
                raise RidocV3Error('Selected lesson is unavailable')
            delete_ridoc_lessons_batch(self, owner_user_id, [lesson_id], {package_id: expected_revision})
            return self._load_package_record(owner_user_id, package_id)

    def _load_package_record(self, owner_user_id: str, package_id: str) -> RidocWorkspaceRecord:
        archive = self.load_archive(owner_user_id, package_id=package_id)
        return RidocWorkspaceRecord(owner_user_id=owner_user_id, package_id=package_id, path=str(self.package_path(owner_user_id, package_id)), revision=archive.revision.value, index=self._existing_index(owner_user_id, package_id))

    def reorder_lessons(self, *, owner_user_id: str, package_id: str, expected_revision: str, ordered_lesson_ids: list[str]) -> RidocWorkspaceRecord:

        def reorder(archive: RidocArchiveV3) -> None:
            if set(ordered_lesson_ids) != set(archive.package.lesson_order):
                raise RidocV3Error('RIDOC lesson order must contain every lesson exactly once')
            if len(ordered_lesson_ids) != len(set(ordered_lesson_ids)):
                raise RidocV3Error('RIDOC lesson order contains duplicates')
            archive.package.lesson_order = list(ordered_lesson_ids)
        return self.mutate(owner_user_id, package_id, expected_revision, reorder)

    def delete_package(self, *, owner_user_id: str, package_id: str, expected_revision: str) -> None:
        from openclass_core.services.ridoc_batch_delete import delete_ridoc_lessons_batch
        delete_ridoc_lessons_batch(self, owner_user_id, [], {package_id: expected_revision}, package_ids=[package_id])

    def _delete_package_to_journal(self, *, owner_user_id: str, package_id: str, expected_revision: str, tombstone: Path) -> None:
        with history_write_lock(self, owner_user_id, package_id):
            archive = self.load_archive(owner_user_id, package_id=package_id)
            actual = archive.revision.value if archive.revision else ''
            if actual != expected_revision:
                raise RidocV3ConflictError('RIDOC revision changed; reload before deleting')
            path = self.package_path(owner_user_id, package_id)
            os.replace(path, tombstone)
            from openclass_core.services.recently_deleted import _sync
            _sync(path.parent)
            _sync(tombstone.parent)
            try:
                with self._connect() as connection:
                    connection.execute('BEGIN IMMEDIATE')
                    connection.execute('DELETE FROM ridoc_lesson_index WHERE owner_user_id=? AND package_id=?', (owner_user_id, package_id))
                    connection.execute('DELETE FROM ridoc_runtime_turns WHERE owner_user_id=? AND package_id=?', (owner_user_id, package_id))
                    connection.execute('DELETE FROM ridoc_lesson_runtime WHERE owner_user_id=? AND package_id=?', (owner_user_id, package_id))
                    connection.execute('DELETE FROM ridoc_merge_sessions WHERE owner_user_id=? AND package_id=?', (owner_user_id, package_id))
                    connection.execute('DELETE FROM ridoc_package_index WHERE owner_user_id=? AND package_id=?', (owner_user_id, package_id))
                    connection.execute('\n                        UPDATE ridoc_workspace_meta\n                        SET active_package_id=NULL, updated_at=?\n                        WHERE owner_user_id=? AND active_package_id=?\n                        ', (now_iso(), owner_user_id, package_id))
                    connection.commit()
            except Exception:
                os.replace(tombstone, path)
                raise

    @staticmethod
    def _prune_documents(archive: RidocArchiveV3) -> None:
        if is_v34(archive):
            from openclass_core.services.ridoc_v34_references import prune_objects
            prune_objects(archive)
            return
        referenced = {_node_document_ref(node) for node in archive.nodes.values()} | {node.before_document_ref for node in archive.nodes.values() if isinstance(node, RidocDocumentNode) and node.before_document_ref}
        referenced.update(sequence_document_refs(archive))
        archive.documents = {digest: payload for digest, payload in archive.documents.items() if digest in referenced}

    def update_package_metadata(self, *, owner_user_id: str, package_id: str, expected_revision: str, title: str | None=None, summary: str | None=None, tags: list[str] | None=None) -> RidocWorkspaceRecord:

        def update(archive: RidocArchiveV3) -> None:
            if title is not None:
                if not title.strip():
                    raise RidocV3Error('RIDOC package title is required')
                archive.package.title = title.strip()
            if summary is not None:
                archive.package.summary = summary.strip()
            if tags is not None:
                archive.package.tags = list(dict.fromkeys((tag.strip() for tag in tags if tag.strip())))
        return self.mutate(owner_user_id, package_id, expected_revision, update)

    def create_branch(self, *, owner_user_id: str, package_id: str, lesson_id: str, expected_revision: str, name: str, from_node_id: str | None=None) -> RidocWorkspaceRecord:
        branch_name = name.strip()
        if not branch_name:
            raise RidocV3Error('RIDOC branch name is required')

        def create(archive: RidocArchiveV3) -> None:
            graph = _graph(archive, lesson_id)
            if branch_name in graph.branches:
                raise RidocV3ConflictError('RIDOC branch already exists')
            source = from_node_id or (continuation_node_id(archive, lesson_id) if is_v34(archive) else graph.branches[graph.current_branch].head_node_id)
            _node(archive, lesson_id, source)
            inherited = sequence_at_node(archive, lesson_id, source) if from_node_id else applied_branch_sequence(archive, lesson_id) if graph.branches[graph.current_branch].document_cursor_node_id is not None else branch_sequence(archive, lesson_id)
            inherited_merged = graph.branches[graph.current_branch].learning_order_merged
            graph.branches[branch_name] = RidocBranch(name=branch_name, head_node_id=source, base_node_id=source, learning_order_merged=inherited_merged, document_occurrences={key: value.model_copy(deep=True) for key, value in graph.branches[graph.current_branch if source in graph.branches[graph.current_branch].learning_node_ids else _node(archive, lesson_id, source).branch_name].document_occurrences.items()} if is_v34(archive) else {})
            graph.current_branch = branch_name
            if is_v34(archive) or not any((isinstance(_node(archive, lesson_id, identity), RidocMergeEventNode) for identity in inherited)):
                set_branch_sequence(archive, lesson_id, branch_name, inherited)
            graph.updated_at = now_iso()
        return self.mutate(owner_user_id, package_id, expected_revision, create)

    def switch_branch(self, *, owner_user_id: str, package_id: str, lesson_id: str, expected_revision: str, name: str) -> RidocWorkspaceRecord:

        def switch(archive: RidocArchiveV3) -> None:
            graph = _graph(archive, lesson_id)
            if name not in graph.branches:
                raise RidocV3Error('RIDOC branch is unavailable')
            graph.current_branch = name
            graph.updated_at = now_iso()
        return self.mutate(owner_user_id, package_id, expected_revision, switch)

    def rename_branch(self, *, owner_user_id: str, package_id: str, lesson_id: str, expected_revision: str, old_name: str, new_name: str) -> RidocWorkspaceRecord:
        next_name = new_name.strip()
        if old_name == 'main':
            raise RidocV3Error('The main branch cannot be renamed')
        if not next_name:
            raise RidocV3Error('RIDOC branch name is required')

        def rename(archive: RidocArchiveV3) -> None:
            graph = _graph(archive, lesson_id)
            if old_name not in graph.branches or next_name in graph.branches:
                raise RidocV3ConflictError('RIDOC branch cannot be renamed')
            branch = graph.branches.pop(old_name)
            branch.name = next_name
            graph.branches[next_name] = branch
            for record in graph.merge_records:
                for field in ('target_branch', 'source_branch'):
                    if record.get(field) == old_name:
                        record[field] = next_name
            if graph.current_branch == old_name:
                graph.current_branch = next_name
            for record in graph.merge_records:
                for field in ('source_branch', 'target_branch'):
                    if record.get(field) == old_name:
                        record[field] = next_name
            for node in archive.nodes.values():
                if node.lesson_id == lesson_id and node.branch_name == old_name:
                    node.branch_name = next_name
            graph.updated_at = now_iso()
        return self.mutate(owner_user_id, package_id, expected_revision, rename)

    def delete_branch(self, *, owner_user_id: str, package_id: str, lesson_id: str, expected_revision: str, name: str) -> RidocWorkspaceRecord:
        if name == 'main':
            raise RidocV3Error('The main branch cannot be deleted')

        def delete(archive: RidocArchiveV3) -> None:
            graph = _graph(archive, lesson_id)
            if graph.current_branch == name:
                raise RidocV3Error('The current branch cannot be deleted')
            if name not in graph.branches:
                raise RidocV3Error('RIDOC branch is unavailable')
            del graph.branches[name]
            reachable = {node_id for branch in graph.branches.values() for node_id in causal_ids(archive, lesson_id, [branch.head_node_id, branch.base_node_id, *branch.learning_node_ids])}
            for record in graph.merge_records:
                reachable.update(causal_ids(archive, lesson_id, [record[key] for key in ('source_head_id', 'target_head_id', 'base_node_id')]))
            removed = [node_id for node_id in graph.node_order if node_id not in reachable]
            graph.node_order = [node_id for node_id in graph.node_order if node_id in reachable]
            for node_id in removed:
                if is_v34(archive):
                    archive.node_refs[f'{lesson_id}/{node_id}'] = archive.node_refs[f'{lesson_id}/{node_id}'].model_copy(update={'status': 'deleted'})
                archive.nodes.pop(f'{lesson_id}/{node_id}', None)
            referenced_documents = {_node_document_ref(node) for node in archive.nodes.values()} | {node.before_document_ref for node in archive.nodes.values() if isinstance(node, RidocDocumentNode) and node.before_document_ref}
            referenced_documents.update(sequence_document_refs(archive))
            archive.documents = {digest: payload for digest, payload in archive.documents.items() if digest in referenced_documents}
            graph.updated_at = now_iso()
        return self.mutate(owner_user_id, package_id, expected_revision, delete)

    def _reachable_node_ids(self, archive: RidocArchiveV3, lesson_id: str, head_node_id: str) -> set[str]:
        reachable: set[str] = set()
        pending = [head_node_id]
        while pending:
            node_id = pending.pop()
            if node_id in reachable:
                continue
            reachable.add(node_id)
            pending.extend(_node(archive, lesson_id, node_id).parent_ids)
            pending.extend(_node(archive, lesson_id, node_id).learning_context_node_ids)
        return reachable

    def merge_branches(self, *, owner_user_id: str, package_id: str, lesson_id: str, expected_revision: str, transaction_id: str, source_branch_name: str, target_branch_name: str, expected_source_head_id: str, expected_target_head_id: str, document: BoardDocument, label: str, decision_metadata: dict[str, Any] | None=None, ordered_node_ids: list[str] | None=None) -> RidocWorkspaceRecord:
        transaction = transaction_id.strip()
        if not transaction:
            raise RidocV3Error('RIDOC merge requires a transaction identity')
        current = self.load_archive(owner_user_id, package_id=package_id)
        if self._transaction_exists(current, transaction):
            index = self._existing_index(owner_user_id, package_id)
            if index is None or current.revision is None:
                raise RidocV3Error('RIDOC index is unavailable')
            return RidocWorkspaceRecord(owner_user_id=owner_user_id, package_id=package_id, path=str(self.package_path(owner_user_id, package_id)), revision=current.revision.value, index=index)

        def merge(archive: RidocArchiveV3) -> None:
            graph = _graph(archive, lesson_id)
            if graph.current_branch != target_branch_name:
                raise RidocV3ConflictError('RIDOC merge target branch changed')
            source = graph.branches.get(source_branch_name)
            target = graph.branches.get(target_branch_name)
            if source is None or target is None or source is target:
                raise RidocV3Error('RIDOC merge branches are unavailable')
            if source.head_node_id != expected_source_head_id or target.head_node_id != expected_target_head_id:
                raise RidocV3ConflictError('RIDOC merge heads changed; recompute the merge')
            plan = build_merge_plan(archive, lesson_id, source_branch_name, ordered_node_ids)
            if plan['blockers']:
                blocker = plan['blockers'][0]
                raise RidocV3ConflictError(f"Node {blocker['node_id']} cannot be merged: {blocker['reason']}")
            if is_v34(archive):
                install_merge_plan(archive, lesson_id, target_branch_name, plan)
            else:
                set_branch_sequence(archive, lesson_id, target_branch_name, plan['ordered_node_ids'])
            target.learning_order_merged = True
            graph.merge_records.append({'transaction_id': transaction, 'source_branch': source_branch_name, 'target_branch': target_branch_name, 'source_head_id': source.head_node_id, 'target_head_id': target.head_node_id, 'base_node_id': plan['base_node_id'], 'label': label, 'created_at': now_iso()})
            graph.updated_at = now_iso()
        from openclass_core.services.ridoc_sequence_mutations import commit_sequence_mutation
        return commit_sequence_mutation(self, owner_user_id, package_id, expected_revision, transaction, merge)

    def commit_document(self, *, owner_user_id: str, package_id: str, lesson_id: str, expected_revision: str, transaction_id: str, document: BoardDocument, source: str='manual', label: str='Document update', scope_block_ids: list[str] | None=None, branch_action_kind: Literal['regenerate', 'fork'] | None=None, branch_source_node_id: str='') -> RidocWorkspaceRecord:
        transaction = transaction_id.strip()
        if not transaction:
            raise RidocV3Error('RIDOC C transactions require an identity')
        if branch_action_kind is not None:
            if branch_action_kind != 'fork':
                raise RidocV3Error('RIDOC document branches must use a fork action')
            if not branch_source_node_id.strip():
                raise RidocV3Error('RIDOC document forks require a source M')
        current = self.load_archive(owner_user_id, package_id=package_id)
        self.assert_action_not_deleted(owner_user_id=owner_user_id, package_id=package_id, lesson_id=lesson_id, transaction_id=transaction)
        existing_change = next((node for node in current.nodes.values() if isinstance(node, RidocDocumentNode) and (not node.initial) and (node.lesson_id == lesson_id) and (node.transaction_id == transaction)), None)
        if existing_change is not None:
            graph = _graph(current, lesson_id)
            branch = graph.branches[graph.current_branch]
            if branch.document_cursor_node_id is not None and document_history_state(current, lesson_id)['withdrawn_node_ids']:
                raise RidocV3ConflictError('The board change has been undone. Start a new editing session')
            if branch.head_node_id != existing_change.id:
                raise RidocV3ConflictError('RIDOC manual edit session is no longer at the branch head')
            current_document = BoardDocument.model_validate(current.documents[existing_change.after_document_ref])
            result_document = document.model_copy(update={'title': current_document.title})
            if not document_changed(current_document, result_document):
                if source == 'manual' and document.title.strip() and (document.title.strip() != graph.title) and (branch_action_kind is None or document.title.strip() != current_document.title):
                    return self.mutate(owner_user_id, package_id, expected_revision, lambda archive: _set_lesson_title(archive, lesson_id, document.title))
                index = self._existing_index(owner_user_id, package_id)
                if index is None or current.revision is None:
                    raise RidocV3Error('RIDOC index is unavailable')
                return RidocWorkspaceRecord(owner_user_id=owner_user_id, package_id=package_id, path=str(self.package_path(owner_user_id, package_id)), revision=current.revision.value, index=index)

            def extend(archive: RidocArchiveV3) -> None:
                self.assert_action_not_deleted(owner_user_id=owner_user_id, package_id=package_id, lesson_id=lesson_id, transaction_id=transaction)
                node = archive.nodes[f'{lesson_id}/{existing_change.id}']
                if not isinstance(node, RidocDocumentNode):
                    raise RidocV3Error('RIDOC manual edit action is invalid')
                graph = _graph(archive, lesson_id)
                if source == 'manual' and (branch_action_kind is None or document.title.strip() != current_document.title):
                    _set_lesson_title(archive, lesson_id, document.title)
                branch = graph.branches[graph.current_branch]
                if branch.document_cursor_node_id is not None and document_history_state(archive, lesson_id)['withdrawn_node_ids']:
                    raise RidocV3ConflictError('The board change has been undone. Start a new editing session')
                if any((node.id in causal_ids(archive, lesson_id, [record[key] for key in ('source_head_id', 'target_head_id', 'base_node_id')]) for record in graph.merge_records)) or any((node.id in other.learning_context_node_ids or node.id in other.parent_ids for other in archive.nodes.values())) or any((name != graph.current_branch and (node.id in {other.head_node_id, other.base_node_id} or node.id in branch_sequence(archive, lesson_id, name)) for name, other in graph.branches.items())):
                    raise RidocV3ConflictError('Later history references this document change. Start a new editing session')
                sequence = branch_sequence(archive, lesson_id)
                if not sequence or sequence[-1] != node.id:
                    raise RidocV3ConflictError('The document change is no longer at the end of the learning sequence')
                state = _write_input_state(archive, lesson_id, sequence[:-1], node.parent_ids[0])
                node.document_action = {} if state['legacy'] else build_document_action(state['document'], result_document, versions=state['versions'], document_version=state['document_version'], structure_version=state['structure_version'], allow_local=node.source == 'manual')
                node.after_document_ref = store_document(archive.documents, result_document)
                if is_v34(archive):
                    capture_node(archive, node, state)
                    branch.document_occurrences.pop(node.id, None)
                node.scope_block_ids = list(dict.fromkeys([*node.scope_block_ids, *(scope_block_ids or [])]))
                referenced = {_node_document_ref(item) for item in archive.nodes.values()} | {item.before_document_ref for item in archive.nodes.values() if isinstance(item, RidocDocumentNode) and item.before_document_ref}
                if not state['legacy']:
                    set_branch_sequence(archive, lesson_id, graph.current_branch, sequence)
                referenced.update(sequence_document_refs(archive))
                archive.documents = {digest: payload for digest, payload in archive.documents.items() if digest in referenced}
                _graph(archive, lesson_id).updated_at = now_iso()
            return self.mutate(owner_user_id, package_id, expected_revision, extend)
        if self._transaction_exists(current, transaction):
            index = self._existing_index(owner_user_id, package_id)
            if index is None or current.revision is None:
                raise RidocV3Error('RIDOC index is unavailable')
            return RidocWorkspaceRecord(owner_user_id=owner_user_id, package_id=package_id, path=str(self.package_path(owner_user_id, package_id)), revision=current.revision.value, index=index)

        def append(archive: RidocArchiveV3) -> None:
            self.assert_action_not_deleted(owner_user_id=owner_user_id, package_id=package_id, lesson_id=lesson_id, transaction_id=transaction)
            graph = _graph(archive, lesson_id)
            branch_name = graph.current_branch
            branch = graph.branches[branch_name]
            parent_id = continuation_node_id(archive, lesson_id) if is_v34(archive) else branch.head_node_id
            input_sequence = branch_sequence(archive, lesson_id)
            inherited_merged = branch.learning_order_merged
            source_node: RidocMessageNode | None = None
            diverging = False
            if branch_action_kind is not None:
                source_node, parent_id = resolve_history_branch_parent(archive, lesson_id, branch_action_kind, branch_source_node_id)
                input_sequence = sequence_at_node(archive, lesson_id, parent_id)
            elif branch.document_cursor_node_id is not None and document_history_state(archive, lesson_id)['withdrawn_node_ids']:
                input_sequence = applied_branch_sequence(archive, lesson_id)
                diverging = True
            input_state = _write_input_state(archive, lesson_id, input_sequence, parent_id)
            current_document = input_state['document']
            result_document = document.model_copy(update={'title': current_document.title})
            content_changed = document_changed(current_document, result_document)
            rename_requested = source == 'manual' and bool(document.title.strip()) and (document.title.strip() != graph.title) and (branch_action_kind is None or document.title.strip() != current_document.title)
            if branch_action_kind is not None and rename_requested and (not content_changed):
                raise RidocV3ConflictError('Return to the current version before changing the course title')
            if rename_requested:
                _set_lesson_title(archive, lesson_id, document.title)
            if not content_changed:
                return
            if branch_action_kind is not None and source_node is not None:
                branch_name, branch = _create_history_branch(graph, source_node=source_node, parent_id=parent_id, action_kind=branch_action_kind)
                branch.learning_order_merged = inherited_merged
            elif diverging:
                branch_name, branch = create_document_divergence_branch(graph, transaction)
            before_ref = store_document(archive.documents, current_document)
            after_ref = store_document(archive.documents, result_document)
            identity = hashlib.sha256(transaction.encode('utf-8')).hexdigest()[:24]
            node_id = f'c_{identity}'
            node = RidocDocumentNode(id=node_id, lesson_id=lesson_id, branch_name=branch_name, parent_ids=[parent_id], created_at=now_iso(), transaction_id=transaction, label=label, source=source, before_document_ref=before_ref, after_document_ref=after_ref, action_id=f'action_{identity}', scope_block_ids=list(scope_block_ids or []), branch_origin_kind=branch_action_kind, branch_origin_node_id=source_node.id if source_node is not None else '', learning_context_node_ids=[] if input_state['legacy'] else input_sequence, document_action={} if input_state['legacy'] else build_document_action(current_document, result_document, versions=input_state['versions'], document_version=input_state['document_version'], structure_version=input_state['structure_version'], allow_local=source == 'manual'))
            archive.nodes[f'{lesson_id}/{node_id}'] = node
            if is_v34(archive):
                capture_node(archive, node, input_state)
            graph.node_order.append(node_id)
            branch.head_node_id = node_id
            if not input_state['legacy']:
                set_branch_sequence(archive, lesson_id, branch_name, [*input_sequence, node_id])
            graph.updated_at = node.created_at
        return self.mutate(owner_user_id, package_id, expected_revision, append)

    def update_ui_state(self, owner_user_id: str, package_id: str, *, active_lesson_id: str | None, open_lesson_ids: list[str], workspace_tab_order: list[str]) -> RidocPackageIndexView:
        current = self._existing_index(owner_user_id, package_id)
        if current is None:
            raise RidocV3Error('RIDOC package index is unavailable')
        lesson_ids = {lesson.id for lesson in current.lessons}
        active = active_lesson_id if active_lesson_id in lesson_ids else None
        opened = [value for value in open_lesson_ids if value in lesson_ids]
        tabs = [value for value in workspace_tab_order if value in lesson_ids]
        with self._connect() as connection:
            connection.execute('\n                UPDATE ridoc_package_index\n                SET active_lesson_id = ?, open_lesson_ids_json = ?,\n                    workspace_tab_order_json = ?, updated_at = ?\n                WHERE owner_user_id = ? AND package_id = ?\n                ', (active, json.dumps(opened), json.dumps(tabs), now_iso(), owner_user_id, package_id))
        updated = self._existing_index(owner_user_id, package_id)
        if updated is None:
            raise RidocV3Error('RIDOC package index is unavailable')
        return updated

    def stage_runtime_turn(self, *, owner_user_id: str, package_id: str, lesson_id: str, turn_id: str, state: str, payload: dict[str, Any], error: str='') -> None:
        with history_write_lock(self, owner_user_id, package_id) if state == 'pending' else nullcontext(), self._connect() as connection:
            connection.execute('\n                INSERT INTO ridoc_runtime_turns(\n                    owner_user_id, package_id, lesson_id, turn_id,\n                    state, payload_json, error, updated_at\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)\n                ON CONFLICT(owner_user_id, package_id, lesson_id, turn_id) DO UPDATE SET\n                    state=excluded.state, payload_json=excluded.payload_json,\n                    error=excluded.error, updated_at=excluded.updated_at\n                ', (owner_user_id, package_id, lesson_id, turn_id, state, json.dumps(owned_runtime_payload(payload), ensure_ascii=False), error[:2000], now_iso()))

    def runtime_turn(self, *, owner_user_id: str, package_id: str, lesson_id: str, turn_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute('\n                SELECT state, payload_json, error, updated_at\n                FROM ridoc_runtime_turns\n                WHERE owner_user_id=? AND package_id=? AND lesson_id=? AND turn_id=?\n                ', (owner_user_id, package_id, lesson_id, turn_id)).fetchone()
        if row is None:
            return None
        return {'state': row['state'], 'payload': json.loads(row['payload_json']), 'error': row['error'], 'updated_at': row['updated_at']}

    def clear_runtime_turn(self, *, owner_user_id: str, package_id: str, lesson_id: str, turn_id: str) -> None:
        with self._connect() as connection:
            connection.execute('\n                DELETE FROM ridoc_runtime_turns\n                WHERE owner_user_id=? AND package_id=? AND lesson_id=? AND turn_id=?\n                ', (owner_user_id, package_id, lesson_id, turn_id))

    def audit(self, *, owner_user_id: str, kind: str, payload: dict[str, Any], package_id: str | None=None, lesson_id: str | None=None) -> None:
        with self._connect() as connection:
            connection.execute('\n                INSERT INTO ridoc_runtime_audit(\n                    id, owner_user_id, package_id, lesson_id, kind, payload_json, created_at\n                ) VALUES (?, ?, ?, ?, ?, ?, ?)\n                ', (f'audit_{uuid.uuid4().hex}', owner_user_id, package_id, lesson_id, kind, json.dumps(payload, ensure_ascii=False), now_iso()))

    def rebuild_owner_index(self, owner_user_id: str) -> list[RidocPackageIndexView]:
        owner_root = self.root / _owner_component(owner_user_id)
        if not owner_root.exists():
            return []
        rebuilt: list[RidocPackageIndexView] = []
        for path in sorted(owner_root.glob('*.ridoc')):
            archive = read_ridoc_v3(path)
            record = self._upsert_index(owner_user_id, path, archive)
            rebuilt.append(record.index)
        return rebuilt

    def publish_package(self, *, owner_user_id: str, package_id: str, expected_revision: str) -> RidocWorkspaceRecord:
        with history_write_lock(self, owner_user_id, package_id):
            archive = self.load_archive(owner_user_id, package_id=package_id)
            actual = archive.revision.value if archive.revision else ''
            if actual != expected_revision:
                raise RidocV3ConflictError('RIDOC revision changed; reload before publishing')
            public_component = package_id if re.fullmatch('[A-Za-z0-9._-]+', package_id) and package_id not in {'.', '..'} else _package_component(package_id)
            public_path = self.root / 'published' / public_component / f'{actual}.ridoc'
            if not public_path.exists():
                from openclass_core.services.ridoc_edit_session_store import save_base_snapshot
                save_base_snapshot(self.package_path(owner_user_id, package_id), public_path, actual)
            else:
                published = read_ridoc_v3(public_path)
                if published.revision is None or published.revision.value != actual or published.package.id != package_id:
                    raise RidocV3Error('The published immutable RIDOC snapshot does not match the requested revision')
            published_at = now_iso()
            with self._connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                try:
                    connection.execute('\n                        INSERT INTO ridoc_public_index(\n                            package_id, owner_user_id, ridoc_path, revision,\n                            title, summary, tags_json, lesson_ids_json, published_at\n                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)\n                        ON CONFLICT(package_id) DO UPDATE SET\n                            owner_user_id=excluded.owner_user_id,\n                            ridoc_path=excluded.ridoc_path,\n                            revision=excluded.revision,\n                            title=excluded.title,\n                            summary=excluded.summary,\n                            tags_json=excluded.tags_json,\n                            lesson_ids_json=excluded.lesson_ids_json,\n                            published_at=excluded.published_at\n                        ', (package_id, owner_user_id, str(public_path), actual, archive.package.title, archive.package.summary, json.dumps(archive.package.tags, ensure_ascii=False), json.dumps(archive.package.lesson_order), published_at))
                    connection.execute("\n                        UPDATE ridoc_package_index SET visibility='public', updated_at=?\n                        WHERE owner_user_id=? AND package_id=?\n                        ", (published_at, owner_user_id, package_id))
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            index = self._existing_index(owner_user_id, package_id)
            if index is None:
                raise RidocV3Error('RIDOC index is unavailable')
            return RidocWorkspaceRecord(owner_user_id=owner_user_id, package_id=package_id, path=str(self.package_path(owner_user_id, package_id)), revision=actual, index=index)

    def unpublish_package(self, *, owner_user_id: str, package_id: str) -> None:
        with history_write_lock(self, owner_user_id, package_id), self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                connection.execute('DELETE FROM ridoc_public_index WHERE package_id=? AND owner_user_id=?', (package_id, owner_user_id))
                connection.execute("\n                    UPDATE ridoc_package_index SET visibility='private', updated_at=?\n                    WHERE owner_user_id=? AND package_id=?\n                    ", (now_iso(), owner_user_id, package_id))
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def load_public_archive(self, package_id: str) -> tuple[RidocArchiveV3, str] | None:
        with self._connect() as connection:
            row = connection.execute('SELECT ridoc_path, published_at FROM ridoc_public_index WHERE package_id=?', (package_id,)).fetchone()
        if row is None:
            return None
        path = Path(row['ridoc_path'])
        if not path.resolve().is_relative_to((self.root / 'published').resolve()):
            raise RidocV3Error('Public RIDOC path is outside its published storage')
        return (read_ridoc_v3(path), str(row['published_at']))

    def fork_public_package(self, *, owner_user_id: str, public_package_id: str, lesson_id: str | None=None, repository_store=None) -> RidocWorkspaceRecord:
        loaded = self.load_public_archive(public_package_id)
        if loaded is None:
            raise RidocV3Error('Public RIDOC package is unavailable')
        source, _ = loaded
        source_repository = None
        if repository_store is not None:
            public_row = next((row for row in self.list_public_packages() if row['package_id'] == public_package_id), None)
            if public_row is None:
                raise RidocV3Error('Public RIDOC project is unavailable')
            source_repository = host_service('ensure_public_repository', repository_store, self, public_row)
            source = repository_store.published_archive(source_repository.id)
        fork = copy.deepcopy(source)
        selected_lessons = [lesson_id] if lesson_id is not None else list(fork.package.lesson_order)
        if any((item not in fork.package.lessons for item in selected_lessons)):
            raise RidocV3Error('Public RIDOC lesson is unavailable')
        lesson_map = {old_id: f'lesson_{uuid.uuid4().hex[:12]}' for old_id in selected_lessons}
        new_lessons = {}
        new_nodes = {}
        for old_id in selected_lessons:
            new_id = lesson_map[old_id]
            graph = fork.package.lessons[old_id]
            graph.id = new_id
            new_lessons[new_id] = graph
            for node_id in graph.node_order:
                node = fork.nodes[f'{old_id}/{node_id}']
                node.lesson_id = new_id
                new_nodes[f'{new_id}/{node_id}'] = node
        fork.package.id = f'course_{uuid.uuid4().hex[:12]}'
        fork.package.kind = 'standalone' if lesson_id is not None else 'course'
        fork.package.lesson_order = [lesson_map[item] for item in selected_lessons]
        fork.package.lessons = new_lessons
        fork.package.course_graph = [{**edge, 'source_lesson_id': lesson_map[edge['source_lesson_id']], 'target_lesson_id': lesson_map[edge['target_lesson_id']]} for edge in fork.package.course_graph if edge.get('source_lesson_id') in lesson_map and edge.get('target_lesson_id') in lesson_map]
        fork.nodes = new_nodes
        if is_v34(fork):
            from openclass_core.services.ridoc_v34_references import remap_lessons
            remap_lessons(fork, lesson_map)
        self._prune_documents(fork)
        referenced_evidence = {value for node in new_nodes.values() for value in node.evidence_ids}
        referenced_assets = {value for node in new_nodes.values() for value in node.resource_refs}
        if not is_v34(fork):
            fork.evidence = {key: value for key, value in fork.evidence.items() if key in referenced_evidence}
            fork.assets = {key: value for key, value in fork.assets.items() if key in referenced_assets}
        fork.revision = None
        record = self.import_archive(owner_user_id, fork)
        if source_repository is not None:
            host_service('record_workspace_fork', repository_store, owner_user_id=owner_user_id, package_id=record.package_id, source_repository_id=source_repository.id, source_revision=source.revision.value, lesson_map=lesson_map)
        self.set_active_package_id(owner_user_id, record.package_id)
        return record

    def public_package_id_for_lesson(self, lesson_id: str) -> str | None:
        with self._connect() as connection:
            rows = connection.execute('SELECT package_id, lesson_ids_json FROM ridoc_public_index').fetchall()
        for row in rows:
            if lesson_id in json.loads(row['lesson_ids_json']):
                return str(row['package_id'])
        return None

    def list_public_packages(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute('SELECT * FROM ridoc_public_index ORDER BY published_at DESC').fetchall()
        return [dict(row) for row in rows]
