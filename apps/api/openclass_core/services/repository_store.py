from __future__ import annotations
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from openclass_core.models import GitHubInstallationView, RepositoryFileEntry, RepositoryMapNode, RepositoryMapView, RepositoryNodeEvidence, RepositorySnapshot, SourceIngestionRecord, now_iso
from openclass_core.services import workspace_state
from openclass_core.services.account_source_library import source_library_path
from openclass_core.services.source_ingestion_jobs import SourceIngestionCoordinator, source_ingestion_coordinator

def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

def _loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback

class RepositoryStore:

    def __init__(self, path: Path | None=None, *, coordinator: SourceIngestionCoordinator=source_ingestion_coordinator) -> None:
        self._path = path
        self.coordinator = coordinator

    @property
    def path(self) -> Path:
        return self._path or source_library_path()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys = ON')
            conn.execute('PRAGMA busy_timeout = 5000')
            conn.execute('PRAGMA journal_mode = WAL')
            self._initialize(conn)
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self, conn: sqlite3.Connection) -> None:
        conn.executescript("\n            CREATE TABLE IF NOT EXISTS repository_snapshots (\n                id TEXT PRIMARY KEY,\n                owner_user_id TEXT NOT NULL,\n                package_id TEXT NOT NULL,\n                source_ingestion_id TEXT NOT NULL UNIQUE,\n                provider TEXT NOT NULL,\n                repository_id INTEGER,\n                owner TEXT NOT NULL,\n                name TEXT NOT NULL,\n                visibility TEXT NOT NULL,\n                requested_ref TEXT NOT NULL,\n                resolved_commit_sha TEXT NOT NULL,\n                scope_path TEXT NOT NULL,\n                scope_kind TEXT NOT NULL,\n                default_branch TEXT NOT NULL,\n                archive_path TEXT NOT NULL,\n                archive_hash TEXT NOT NULL,\n                manifest_hash TEXT NOT NULL,\n                license_spdx TEXT NOT NULL,\n                supersedes_source_id TEXT,\n                created_at TEXT NOT NULL,\n                metadata_json TEXT NOT NULL DEFAULT '{}'\n            );\n            CREATE INDEX IF NOT EXISTS idx_repository_snapshots_scope\n                ON repository_snapshots(owner_user_id, package_id, source_ingestion_id);\n\n            CREATE TABLE IF NOT EXISTS repository_files (\n                id TEXT PRIMARY KEY,\n                source_ingestion_id TEXT NOT NULL,\n                path TEXT NOT NULL,\n                blob_sha TEXT NOT NULL,\n                content_hash TEXT NOT NULL,\n                size_bytes INTEGER NOT NULL,\n                line_count INTEGER NOT NULL,\n                language TEXT NOT NULL,\n                text_status TEXT NOT NULL,\n                skip_reason TEXT NOT NULL,\n                archive_entry TEXT NOT NULL,\n                order_index INTEGER NOT NULL,\n                metadata_json TEXT NOT NULL DEFAULT '{}',\n                UNIQUE(source_ingestion_id, path)\n            );\n            CREATE INDEX IF NOT EXISTS idx_repository_files_source\n                ON repository_files(source_ingestion_id, order_index);\n\n            CREATE TABLE IF NOT EXISTS repository_map_nodes (\n                id TEXT PRIMARY KEY,\n                source_ingestion_id TEXT NOT NULL,\n                tree_kind TEXT NOT NULL,\n                node_kind TEXT NOT NULL,\n                parent_id TEXT,\n                title TEXT NOT NULL,\n                path TEXT NOT NULL,\n                description TEXT NOT NULL,\n                level INTEGER NOT NULL,\n                order_index INTEGER NOT NULL,\n                selectable INTEGER NOT NULL,\n                coverage_status TEXT NOT NULL,\n                metadata_json TEXT NOT NULL DEFAULT '{}'\n            );\n            CREATE INDEX IF NOT EXISTS idx_repository_nodes_source\n                ON repository_map_nodes(source_ingestion_id, tree_kind, order_index);\n\n            CREATE TABLE IF NOT EXISTS repository_node_evidence (\n                node_id TEXT NOT NULL,\n                file_id TEXT NOT NULL,\n                path TEXT NOT NULL,\n                line_start INTEGER NOT NULL,\n                line_end INTEGER NOT NULL,\n                reason TEXT NOT NULL,\n                confidence REAL NOT NULL,\n                order_index INTEGER NOT NULL,\n                PRIMARY KEY(node_id, file_id, line_start, line_end)\n            );\n            CREATE INDEX IF NOT EXISTS idx_repository_node_evidence_node\n                ON repository_node_evidence(node_id, order_index);\n\n            CREATE TABLE IF NOT EXISTS github_connection_states (\n                state TEXT PRIMARY KEY,\n                owner_user_id TEXT NOT NULL,\n                next_path TEXT NOT NULL,\n                created_at TEXT NOT NULL,\n                expires_at TEXT NOT NULL,\n                connection_id TEXT NOT NULL DEFAULT '',\n                consumed_at TEXT\n            );\n\n            CREATE TABLE IF NOT EXISTS github_app_installations (\n                owner_user_id TEXT NOT NULL,\n                installation_id INTEGER NOT NULL,\n                account_id INTEGER,\n                account_login TEXT NOT NULL,\n                account_type TEXT NOT NULL,\n                repository_selection TEXT NOT NULL,\n                status TEXT NOT NULL,\n                permissions_json TEXT NOT NULL DEFAULT '{}',\n                repository_count INTEGER NOT NULL DEFAULT 0,\n                created_at TEXT NOT NULL,\n                updated_at TEXT NOT NULL,\n                PRIMARY KEY(owner_user_id, installation_id)\n            );\n            CREATE INDEX IF NOT EXISTS idx_github_installations_id\n                ON github_app_installations(installation_id, status);\n            ")
        if 'connection_id' not in {row[1] for row in conn.execute('PRAGMA table_info(github_connection_states)')}:
            conn.execute("ALTER TABLE github_connection_states ADD COLUMN connection_id TEXT NOT NULL DEFAULT ''")

    def save_repository(self, *, snapshot: RepositorySnapshot, files: list[RepositoryFileEntry], nodes: list[RepositoryMapNode]) -> None:

        def operation() -> None:
            with self._connect() as conn:
                conn.execute('DELETE FROM repository_node_evidence WHERE node_id IN (SELECT id FROM repository_map_nodes WHERE source_ingestion_id = ?)', (snapshot.source_ingestion_id,))
                conn.execute('DELETE FROM repository_map_nodes WHERE source_ingestion_id = ?', (snapshot.source_ingestion_id,))
                conn.execute('DELETE FROM repository_files WHERE source_ingestion_id = ?', (snapshot.source_ingestion_id,))
                conn.execute('DELETE FROM repository_snapshots WHERE source_ingestion_id = ?', (snapshot.source_ingestion_id,))
                conn.execute('\n                    INSERT INTO repository_snapshots(\n                        id, owner_user_id, package_id, source_ingestion_id, provider, repository_id,\n                        owner, name, visibility, requested_ref, resolved_commit_sha, scope_path,\n                        scope_kind, default_branch, archive_path, archive_hash, manifest_hash,\n                        license_spdx, supersedes_source_id, created_at, metadata_json\n                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                    ', (snapshot.id, snapshot.owner_user_id, snapshot.package_id, snapshot.source_ingestion_id, snapshot.provider, snapshot.repository_id, snapshot.owner, snapshot.name, snapshot.visibility, snapshot.requested_ref, snapshot.resolved_commit_sha, snapshot.scope_path, snapshot.scope_kind, snapshot.default_branch, snapshot.archive_path, snapshot.archive_hash, snapshot.manifest_hash, snapshot.license_spdx, snapshot.supersedes_source_id, snapshot.created_at, _dumps(snapshot.metadata)))
                conn.executemany('\n                    INSERT INTO repository_files(\n                        id, source_ingestion_id, path, blob_sha, content_hash, size_bytes,\n                        line_count, language, text_status, skip_reason, archive_entry,\n                        order_index, metadata_json\n                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                    ', [(item.id, item.source_ingestion_id, item.path, item.blob_sha, item.content_hash, item.size_bytes, item.line_count, item.language, item.text_status, item.skip_reason, item.archive_entry, item.order_index, _dumps(item.metadata)) for item in files])
                for node in nodes:
                    conn.execute('\n                        INSERT INTO repository_map_nodes(\n                            id, source_ingestion_id, tree_kind, node_kind, parent_id, title,\n                            path, description, level, order_index, selectable, coverage_status,\n                            metadata_json\n                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                        ', (node.id, node.source_ingestion_id, node.tree_kind, node.node_kind, node.parent_id, node.title, node.path, node.description, node.level, node.order_index, int(node.selectable), node.coverage_status, _dumps(node.metadata)))
                    conn.executemany('\n                        INSERT INTO repository_node_evidence(\n                            node_id, file_id, path, line_start, line_end, reason,\n                            confidence, order_index\n                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)\n                        ', [(node.id, evidence.file_id, evidence.path, evidence.line_start, evidence.line_end, evidence.reason, evidence.confidence, index) for index, evidence in enumerate(node.evidence)])
        self.coordinator.run_write(self.path, operation)

    def get_snapshot(self, *, owner_user_id: str, package_id: str, source_id: str) -> RepositorySnapshot | None:
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM repository_snapshots WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?', (owner_user_id, package_id, source_id)).fetchone()
        return self._snapshot(row) if row is not None else None

    def get_file(self, *, source_id: str, file_id: str) -> RepositoryFileEntry | None:
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM repository_files WHERE source_ingestion_id = ? AND id = ?', (source_id, file_id)).fetchone()
        return self._file(row) if row is not None else None

    def files_for_source(self, source_id: str) -> list[RepositoryFileEntry]:
        with self._connect() as conn:
            rows = conn.execute('SELECT * FROM repository_files WHERE source_ingestion_id = ? ORDER BY order_index', (source_id,)).fetchall()
        return [self._file(row) for row in rows]

    def get_node(self, *, source_id: str, node_id: str) -> RepositoryMapNode | None:
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM repository_map_nodes WHERE source_ingestion_id = ? AND id = ?', (source_id, node_id)).fetchone()
            if row is None:
                return None
            return self._node(conn, row)

    def get_map(self, *, source: SourceIngestionRecord) -> RepositoryMapView | None:
        snapshot = self.get_snapshot(owner_user_id=source.owner_user_id, package_id=source.package_id, source_id=source.id)
        if snapshot is None:
            return None
        with self._connect() as conn:
            rows = conn.execute('SELECT * FROM repository_map_nodes WHERE source_ingestion_id = ? ORDER BY tree_kind, order_index', (source.id,)).fetchall()
            nodes = [self._node(conn, row) for row in rows]
            counts = conn.execute("\n                SELECT count(*) AS total,\n                       sum(CASE WHEN text_status = 'ready' THEN 1 ELSE 0 END) AS readable,\n                       sum(CASE WHEN json_extract(metadata_json, '$.analyzed') = 1 THEN 1 ELSE 0 END) AS analyzed\n                FROM repository_files WHERE source_ingestion_id = ?\n                ", (source.id,)).fetchone()
        readable = int(counts['readable'] or 0)
        analyzed = int(counts['analyzed'] or 0)
        return RepositoryMapView(source=source, snapshot=snapshot, project_nodes=[node for node in nodes if node.tree_kind == 'project'], learning_nodes=[node for node in nodes if node.tree_kind == 'learning'], analyzed_file_count=analyzed, readable_file_count=readable, total_file_count=int(counts['total'] or 0), coverage_ratio=analyzed / readable if readable else 0.0, warnings=list(snapshot.metadata.get('warnings') or []))

    def has_snapshot(self, *, source: SourceIngestionRecord) -> bool:
        with self._connect() as conn:
            return conn.execute('\n                SELECT 1 FROM repository_snapshots\n                WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                LIMIT 1\n                ', (source.owner_user_id, source.package_id, source.id)).fetchone() is not None

    def delete_source(self, source_id: str) -> None:
        from openclass_core.services.repository_originals import repository_original_store
        with self._connect() as conn:
            snapshot_row = conn.execute('SELECT * FROM repository_snapshots WHERE source_ingestion_id = ?', (source_id,)).fetchone()
            file_rows = conn.execute('SELECT * FROM repository_files WHERE source_ingestion_id = ?', (source_id,)).fetchall()
        if snapshot_row is not None:
            snapshot = self._snapshot(snapshot_row)
            seen: set[str] = set()
            for row in file_rows:
                file = self._file(row)
                if file.metadata.get('storage') == 'source_original' and file.content_hash not in seen:
                    repository_original_store.delete(snapshot=snapshot, file=file)
                    seen.add(file.content_hash)

        def operation() -> None:
            with self._connect() as conn:
                conn.execute('DELETE FROM repository_node_evidence WHERE node_id IN (SELECT id FROM repository_map_nodes WHERE source_ingestion_id = ?)', (source_id,))
                conn.execute('DELETE FROM repository_map_nodes WHERE source_ingestion_id = ?', (source_id,))
                conn.execute('DELETE FROM repository_files WHERE source_ingestion_id = ?', (source_id,))
                conn.execute('DELETE FROM repository_snapshots WHERE source_ingestion_id = ?', (source_id,))
        self.coordinator.run_write(self.path, operation)

    def create_connection_state(self, *, state: str, owner_user_id: str, next_path: str, created_at: str, expires_at: str, connection_id: str='') -> None:

        def operation() -> None:
            with self._connect() as conn:
                conn.execute('INSERT INTO github_connection_states(state, owner_user_id, next_path, created_at, expires_at, connection_id) VALUES (?, ?, ?, ?, ?, ?)', (state, owner_user_id, next_path, created_at, expires_at, connection_id))
        self.coordinator.run_write(self.path, operation)

    def consume_connection_state(self, state: str, *, now: str) -> sqlite3.Row | None:
        result: sqlite3.Row | None = None

        def operation() -> None:
            nonlocal result
            with self._connect() as conn:
                result = conn.execute('SELECT * FROM github_connection_states WHERE state = ? AND consumed_at IS NULL AND expires_at >= ?', (state, now)).fetchone()
                if result is not None:
                    conn.execute('UPDATE github_connection_states SET consumed_at = ? WHERE state = ?', (now, state))
        self.coordinator.run_write(self.path, operation)
        return result

    def save_installation(self, *, owner_user_id: str, installation: GitHubInstallationView) -> None:

        def operation() -> None:
            with self._connect() as conn:
                now = now_iso()
                conn.execute('\n                    INSERT INTO github_app_installations(\n                        owner_user_id, installation_id, account_id, account_login, account_type,\n                        repository_selection, status, permissions_json, repository_count,\n                        created_at, updated_at\n                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                    ON CONFLICT(owner_user_id, installation_id) DO UPDATE SET\n                        account_id=excluded.account_id,\n                        account_login=excluded.account_login,\n                        account_type=excluded.account_type,\n                        repository_selection=excluded.repository_selection,\n                        status=excluded.status,\n                        permissions_json=excluded.permissions_json,\n                        repository_count=excluded.repository_count,\n                        updated_at=excluded.updated_at\n                    ', (owner_user_id, installation.installation_id, installation.account_id, installation.account_login, installation.account_type, installation.repository_selection, installation.status, _dumps(installation.permissions), installation.repository_count, now, installation.updated_at))
        self.coordinator.run_write(self.path, operation)

    def list_installations(self, owner_user_id: str) -> list[GitHubInstallationView]:
        with self._connect() as conn:
            rows = conn.execute('SELECT * FROM github_app_installations WHERE owner_user_id = ? ORDER BY updated_at DESC', (owner_user_id,)).fetchall()
        return [self._installation(row) for row in rows]

    def owners_for_installation(self, installation_id: int) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute('SELECT owner_user_id FROM github_app_installations WHERE installation_id = ?', (installation_id,)).fetchall()
        return [str(row['owner_user_id']) for row in rows]

    def set_installation_status(self, *, installation_id: int, status: str) -> None:

        def operation() -> None:
            with self._connect() as conn:
                conn.execute('UPDATE github_app_installations SET status = ?, updated_at = ? WHERE installation_id = ?', (status, now_iso(), installation_id))
        self.coordinator.run_write(self.path, operation)

    def disconnect_user(self, owner_user_id: str) -> None:

        def operation() -> None:
            with self._connect() as conn:
                conn.execute("UPDATE github_app_installations SET status = 'disconnected', updated_at = ? WHERE owner_user_id = ?", (now_iso(), owner_user_id))
        self.coordinator.run_write(self.path, operation)

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> RepositorySnapshot:
        return RepositorySnapshot(id=row['id'], owner_user_id=row['owner_user_id'], package_id=row['package_id'], source_ingestion_id=row['source_ingestion_id'], provider=row['provider'], repository_id=row['repository_id'], owner=row['owner'], name=row['name'], visibility=row['visibility'], requested_ref=row['requested_ref'], resolved_commit_sha=row['resolved_commit_sha'], scope_path=row['scope_path'], scope_kind=row['scope_kind'], default_branch=row['default_branch'], archive_path=row['archive_path'], archive_hash=row['archive_hash'], manifest_hash=row['manifest_hash'], license_spdx=row['license_spdx'], supersedes_source_id=row['supersedes_source_id'], created_at=row['created_at'], metadata=_loads(row['metadata_json'], {}))

    @staticmethod
    def _file(row: sqlite3.Row) -> RepositoryFileEntry:
        return RepositoryFileEntry(id=row['id'], source_ingestion_id=row['source_ingestion_id'], path=row['path'], blob_sha=row['blob_sha'], content_hash=row['content_hash'], size_bytes=row['size_bytes'], line_count=row['line_count'], language=row['language'], text_status=row['text_status'], skip_reason=row['skip_reason'], archive_entry=row['archive_entry'], order_index=row['order_index'], metadata=_loads(row['metadata_json'], {}))

    @staticmethod
    def _node(conn: sqlite3.Connection, row: sqlite3.Row) -> RepositoryMapNode:
        evidence_rows = conn.execute('SELECT * FROM repository_node_evidence WHERE node_id = ? ORDER BY order_index', (row['id'],)).fetchall()
        return RepositoryMapNode(id=row['id'], source_ingestion_id=row['source_ingestion_id'], tree_kind=row['tree_kind'], node_kind=row['node_kind'], parent_id=row['parent_id'], title=row['title'], path=row['path'], description=row['description'], level=row['level'], order_index=row['order_index'], selectable=bool(row['selectable']), coverage_status=row['coverage_status'], evidence=[RepositoryNodeEvidence(file_id=item['file_id'], path=item['path'], line_start=item['line_start'], line_end=item['line_end'], reason=item['reason'], confidence=item['confidence']) for item in evidence_rows], metadata=_loads(row['metadata_json'], {}))

    @staticmethod
    def _installation(row: sqlite3.Row) -> GitHubInstallationView:
        return GitHubInstallationView(installation_id=row['installation_id'], account_id=row['account_id'], account_login=row['account_login'], account_type=row['account_type'], repository_selection=row['repository_selection'], status=row['status'], permissions=_loads(row['permissions_json'], {}), repository_count=row['repository_count'], updated_at=row['updated_at'])
repository_store = RepositoryStore()
