from __future__ import annotations
import hashlib
import json
import sqlite3
import stat
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from openclass_core.models import RetrievalEvidence, SourceCatalogBatchView, SourceCatalogRun, SourceCatalogSourceSummary, SourceCatalogView, SourceChapter, SourceChunk, SourceIngestionRecord, SourceRange, SourceStructure, SourceStructureQuality, SourceStructureView, SourceVisualAsset, SourceVisualEvidence, now_iso
from openclass_core.services import workspace_state
from openclass_core.services.account_source_library import source_library_path, source_storage_package
from openclass_core.services.native_source_index import NativeSearchMode, NativeSourceIndex, source_chunk_text_hash
from openclass_core.services.pdf_directory_contract import PDFDirectoryReadReport, validate_pdf_directory_report
from openclass_core.services.source_ingestion_jobs import SourceIngestionCoordinator, source_ingestion_coordinator
from openclass_core.services.source_visual_storage import MAX_SOURCE_VISUAL_BYTES, SourceVisualStorageError, read_source_visual_asset, remove_source_visual_asset_if_unstaged

def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)

def _loads(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback

def _ensure_columns(conn: sqlite3.Connection, table: str, definitions: dict[str, str]) -> None:
    existing = {str(row['name']) for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}
    for name, definition in definitions.items():
        if name not in existing:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')

@dataclass(frozen=True)
class PdfVisualCatalogProjection:
    quality: SourceStructureQuality
    locator_method: str
    pagination_segment_count: int
    summary: str

def project_pdf_visual_catalog(structure: SourceStructure | None, chapters: list[SourceChapter]) -> PdfVisualCatalogProjection | None:
    """Describe validated stored page mappings without reading or changing a PDF.

    Mapped physical-page coverage and actually inspected pages are distinct.
    This projection does not upgrade the stored catalog's trust or publication.
    """
    if structure is None or structure.status != 'ready' or (not structure.has_verified_toc):
        return None
    metadata = structure.metadata
    if metadata.get('input_method') != 'original_pdf_page_images' or metadata.get('original_pdf_sha256') != structure.source_content_hash or metadata.get('directory_status') != 'complete':
        return None
    page_count = metadata.get('pdf_page_count')
    if type(page_count) is not int or page_count < 1:
        return None
    node_ranges = {}
    physical_ranges = []
    for chapter in chapters:
        value = chapter.range
        if chapter.mapping_status != 'verified' or value is None or value.kind != 'pdf_pages' or (type(value.start) is not int) or (type(value.end) is not int) or (not 1 <= value.start <= value.end <= page_count):
            return None
        physical_ranges.append((value.start, value.end))
        key = chapter.metadata.get('proposal_key')
        if isinstance(key, str) and key:
            if key in node_ranges:
                return None
            node_ranges[key] = (value.start, value.end)
    if not node_ranges:
        return None
    try:
        report = PDFDirectoryReadReport.model_validate(metadata.get('pdf_read_report'))
        if report.status != 'complete':
            return None
        validate_pdf_directory_report(report, page_count=page_count, delivered_pages=metadata.get('delivered_pages', []), authorized_pages=range(1, page_count + 1), node_ranges=node_ranges)
    except (TypeError, ValueError):
        return None
    segments = report.pagination_segments
    expected_ranges = _merged_physical_ranges([(segment.physical_start, segment.physical_end) for segment in segments] if segments else [(1, page_count)])
    mapped_ranges = _merged_physical_ranges(physical_ranges)
    expected_pages = sum((end - start + 1 for start, end in expected_ranges))
    covered_pages = sum((max(0, min(mapped_end, expected_end) - max(mapped_start, expected_start) + 1) for mapped_start, mapped_end in mapped_ranges for expected_start, expected_end in expected_ranges))
    inspected_pages = len(set(report.inspected_pages))
    mapping_description = '; '.join((f'Printed {segment.printed_start}–{segment.printed_end} → PDF {segment.physical_start}–{segment.physical_end}' for segment in segments[:3]))
    if len(segments) > 3:
        mapping_description += f'; plus {len(segments) - 3} verified segments'
    coverage_label = 'Body page coverage' if segments else 'Located PDF pages'
    scope_description = f'{coverage_label}: {covered_pages}/{expected_pages} pages; original pages inspected: {inspected_pages}/{page_count}.'
    return PdfVisualCatalogProjection(quality=structure.quality.model_copy(update={'text_readiness': 'unknown', 'body_coverage_ratio': covered_pages / expected_pages, 'diagnostics': ['The catalog was read from original page images and verified against PDF page ranges. Citations read the selected original pages directly.']}), locator_method='pdf_visual_segments' if segments else 'pdf_visual_ranges', pagination_segment_count=len(segments), summary=(mapping_description + '. ' if mapping_description else '') + scope_description)

def _merged_physical_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged

class SourceStructureStore:

    def __init__(self, path: Path | None=None, *, native_index: NativeSourceIndex | None=None, coordinator: SourceIngestionCoordinator=source_ingestion_coordinator) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._initialized_paths: set[str] = set()
        self.native_index = native_index or NativeSourceIndex()
        self.coordinator = coordinator

    @property
    def path(self) -> Path:
        if self._path is not None:
            return self._path
        return source_library_path()

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
            conn.execute('PRAGMA synchronous = NORMAL')
            self._initialize_connection(conn, path)
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize_connection(self, conn: sqlite3.Connection, path: Path) -> None:
        with self._lock:
            path_key = str(path)
            if path_key in self._initialized_paths:
                return
            conn.executescript("\n                CREATE TABLE IF NOT EXISTS source_structures (\n                    id TEXT PRIMARY KEY,\n                    owner_user_id TEXT NOT NULL,\n                    package_id TEXT NOT NULL,\n                    source_ingestion_id TEXT NOT NULL,\n                    status TEXT NOT NULL,\n                    strategy TEXT NOT NULL,\n                    has_verified_toc INTEGER NOT NULL,\n                    chapter_count INTEGER NOT NULL,\n                    chunk_count INTEGER NOT NULL,\n                    visual_count INTEGER NOT NULL DEFAULT 0,\n                    visual_index_status TEXT NOT NULL DEFAULT 'pending',\n                    visual_index_version INTEGER NOT NULL DEFAULT 0,\n                    confidence REAL NOT NULL,\n                    quality_json TEXT NOT NULL DEFAULT '{}',\n                    catalog_version INTEGER NOT NULL DEFAULT 0,\n                    catalog_updated_at TEXT,\n                    source_content_hash TEXT NOT NULL DEFAULT '',\n                    catalog_schema_version TEXT NOT NULL DEFAULT 'legacy',\n                    catalog_model TEXT NOT NULL DEFAULT '',\n                    error TEXT NOT NULL,\n                    warnings_json TEXT NOT NULL DEFAULT '[]',\n                    created_at TEXT NOT NULL,\n                    updated_at TEXT NOT NULL,\n                    metadata_json TEXT NOT NULL DEFAULT '{}'\n                );\n                CREATE UNIQUE INDEX IF NOT EXISTS idx_source_structures_source\n                    ON source_structures(owner_user_id, package_id, source_ingestion_id);\n                CREATE INDEX IF NOT EXISTS idx_source_structures_status\n                    ON source_structures(owner_user_id, package_id, status, updated_at);\n\n                CREATE TABLE IF NOT EXISTS source_chapters (\n                    id TEXT PRIMARY KEY,\n                    owner_user_id TEXT NOT NULL,\n                    package_id TEXT NOT NULL,\n                    source_ingestion_id TEXT NOT NULL,\n                    parent_id TEXT,\n                    number TEXT NOT NULL,\n                    normalized_number TEXT NOT NULL,\n                    title TEXT NOT NULL,\n                    level INTEGER NOT NULL,\n                    path_json TEXT NOT NULL DEFAULT '[]',\n                    order_index INTEGER NOT NULL,\n                    source_locator TEXT NOT NULL,\n                    body_start_offset INTEGER,\n                    body_end_offset INTEGER,\n                    page_start INTEGER,\n                    page_end INTEGER,\n                    anchor_status TEXT NOT NULL,\n                    range_json TEXT,\n                    mapping_status TEXT NOT NULL DEFAULT 'unverified',\n                    source_content_hash TEXT NOT NULL DEFAULT '',\n                    catalog_evidence_json TEXT NOT NULL DEFAULT '[]',\n                    catalog_version INTEGER NOT NULL DEFAULT 0,\n                    confidence REAL NOT NULL,\n                    excerpt TEXT NOT NULL,\n                    metadata_json TEXT NOT NULL DEFAULT '{}'\n                );\n                CREATE INDEX IF NOT EXISTS idx_source_chapters_source\n                    ON source_chapters(owner_user_id, package_id, source_ingestion_id, order_index);\n                CREATE INDEX IF NOT EXISTS idx_source_chapters_number\n                    ON source_chapters(owner_user_id, package_id, normalized_number, anchor_status);\n\n                CREATE TABLE IF NOT EXISTS source_catalog_runs (\n                    id TEXT PRIMARY KEY,\n                    owner_user_id TEXT NOT NULL,\n                    package_id TEXT NOT NULL,\n                    source_ingestion_id TEXT NOT NULL,\n                    status TEXT NOT NULL,\n                    catalog_version INTEGER NOT NULL DEFAULT 0,\n                    model TEXT NOT NULL DEFAULT '',\n                    turn_count INTEGER NOT NULL DEFAULT 0,\n                    page_count INTEGER NOT NULL DEFAULT 0,\n                    inspected_page_count INTEGER NOT NULL DEFAULT 0,\n                    ocr_page_count INTEGER NOT NULL DEFAULT 0,\n                    chapter_count INTEGER NOT NULL DEFAULT 0,\n                    verified_chapter_count INTEGER NOT NULL DEFAULT 0,\n                    verification_rate REAL NOT NULL DEFAULT 0,\n                    duration_ms INTEGER NOT NULL DEFAULT 0,\n                    stage_history_json TEXT NOT NULL DEFAULT '[]',\n                    error TEXT NOT NULL DEFAULT '',\n                    created_at TEXT NOT NULL,\n                    updated_at TEXT NOT NULL,\n                    completed_at TEXT,\n                    metadata_json TEXT NOT NULL DEFAULT '{}'\n                );\n                CREATE INDEX IF NOT EXISTS idx_source_catalog_runs_source\n                    ON source_catalog_runs(owner_user_id, package_id, source_ingestion_id, created_at DESC);\n\n                CREATE TABLE IF NOT EXISTS source_chunks (\n                    id TEXT PRIMARY KEY,\n                    owner_user_id TEXT NOT NULL,\n                    package_id TEXT NOT NULL,\n                    source_ingestion_id TEXT NOT NULL,\n                    chapter_id TEXT,\n                    order_index INTEGER NOT NULL,\n                    source_locator TEXT NOT NULL,\n                    text TEXT NOT NULL,\n                    start_offset INTEGER NOT NULL,\n                    end_offset INTEGER NOT NULL,\n                    page_start INTEGER,\n                    page_end INTEGER,\n                    token_count INTEGER NOT NULL,\n                    text_hash TEXT NOT NULL DEFAULT '',\n                    metadata_json TEXT NOT NULL DEFAULT '{}'\n                );\n                CREATE INDEX IF NOT EXISTS idx_source_chunks_source\n                    ON source_chunks(owner_user_id, package_id, source_ingestion_id, order_index);\n                CREATE INDEX IF NOT EXISTS idx_source_chunks_chapter\n                    ON source_chunks(owner_user_id, package_id, chapter_id, order_index);\n\n                CREATE TABLE IF NOT EXISTS source_visual_assets (\n                    id TEXT PRIMARY KEY,\n                    owner_user_id TEXT NOT NULL,\n                    package_id TEXT NOT NULL,\n                    source_ingestion_id TEXT NOT NULL,\n                    chapter_id TEXT,\n                    kind TEXT NOT NULL,\n                    source_locator TEXT NOT NULL,\n                    page_start INTEGER,\n                    page_end INTEGER,\n                    paragraph_index INTEGER,\n                    slide_no INTEGER,\n                    sheet_name TEXT NOT NULL DEFAULT '',\n                    bbox_json TEXT NOT NULL DEFAULT '[]',\n                    before_chunk_id TEXT,\n                    after_chunk_id TEXT,\n                    caption TEXT NOT NULL,\n                    extracted_text TEXT NOT NULL,\n                    surrounding_text TEXT NOT NULL,\n                    anchor_status TEXT NOT NULL DEFAULT 'unverified',\n                    mime_type TEXT NOT NULL,\n                    asset_path TEXT NOT NULL,\n                    storage_key TEXT NOT NULL DEFAULT '',\n                    order_index INTEGER NOT NULL,\n                    content_hash TEXT NOT NULL,\n                    position_hash TEXT NOT NULL DEFAULT '',\n                    width INTEGER,\n                    height INTEGER,\n                    table_data_json TEXT NOT NULL DEFAULT '[]',\n                    confidence REAL NOT NULL,\n                    created_at TEXT NOT NULL DEFAULT '',\n                    metadata_json TEXT NOT NULL DEFAULT '{}'\n                );\n                CREATE INDEX IF NOT EXISTS idx_source_visual_assets_source\n                    ON source_visual_assets(owner_user_id, package_id, source_ingestion_id, order_index);\n                CREATE INDEX IF NOT EXISTS idx_source_visual_assets_chapter\n                    ON source_visual_assets(owner_user_id, package_id, chapter_id, order_index);\n                ")
            chunk_columns = {str(row['name']) for row in conn.execute('PRAGMA table_info(source_chunks)').fetchall()}
            if 'text_hash' not in chunk_columns:
                conn.execute("ALTER TABLE source_chunks ADD COLUMN text_hash TEXT NOT NULL DEFAULT ''")
            _ensure_columns(conn, 'source_structures', {'visual_count': 'INTEGER NOT NULL DEFAULT 0', 'visual_index_status': "TEXT NOT NULL DEFAULT 'pending'", 'visual_index_version': 'INTEGER NOT NULL DEFAULT 0', 'quality_json': "TEXT NOT NULL DEFAULT '{}'", 'catalog_version': 'INTEGER NOT NULL DEFAULT 0', 'catalog_updated_at': 'TEXT', 'source_content_hash': "TEXT NOT NULL DEFAULT ''", 'catalog_schema_version': "TEXT NOT NULL DEFAULT 'legacy'", 'catalog_model': "TEXT NOT NULL DEFAULT ''"})
            _ensure_columns(conn, 'source_chapters', {'range_json': 'TEXT', 'mapping_status': "TEXT NOT NULL DEFAULT 'unverified'", 'source_content_hash': "TEXT NOT NULL DEFAULT ''", 'catalog_evidence_json': "TEXT NOT NULL DEFAULT '[]'", 'catalog_version': 'INTEGER NOT NULL DEFAULT 0'})
            _ensure_columns(conn, 'source_visual_assets', {'structure_id': "TEXT NOT NULL DEFAULT ''", 'structure_version': 'INTEGER NOT NULL DEFAULT 0', 'slide_no': 'INTEGER', 'sheet_name': "TEXT NOT NULL DEFAULT ''", 'before_chunk_id': 'TEXT', 'after_chunk_id': 'TEXT', 'anchor_status': "TEXT NOT NULL DEFAULT 'unverified'", 'storage_key': "TEXT NOT NULL DEFAULT ''", 'position_hash': "TEXT NOT NULL DEFAULT ''", 'width': 'INTEGER', 'height': 'INTEGER', 'table_data_json': "TEXT NOT NULL DEFAULT '[]'", 'created_at': "TEXT NOT NULL DEFAULT ''"})
            conn.execute('\n                CREATE INDEX IF NOT EXISTS idx_source_chunks_text_hash\n                ON source_chunks(owner_user_id, package_id, source_ingestion_id, text_hash)\n                ')
            self.native_index.create_schema(conn)
            self.native_index.backfill(conn)
            self._initialized_paths.add(path_key)

    def attach_summary(self, record: SourceIngestionRecord) -> SourceIngestionRecord:
        structure = self.get_structure(owner_user_id=record.owner_user_id, package_id=record.package_id, source_id=record.id)
        if structure is None:
            return record
        return record.model_copy(update={'structure_status': structure.status, 'structure_strategy': structure.strategy, 'structure_has_verified_toc': structure.has_verified_toc, 'structure_quality': structure.quality, 'structure_error': structure.error, 'structure_updated_at': structure.catalog_updated_at or structure.updated_at})

    def delete_for_source(self, *, owner_user_id: str, package_id: str, source_id: str) -> None:
        asset_paths: list[str] = []
        storage_keys: list[str] = []

        def delete_structure() -> None:
            nonlocal asset_paths, storage_keys
            with self._lock, self._connect() as conn:
                visual_rows = conn.execute('\n                    SELECT asset_path, storage_key FROM source_visual_assets\n                    WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                    ', (owner_user_id, package_id, source_id)).fetchall()
                asset_paths = [str(row['asset_path']) for row in visual_rows if row['asset_path'] and (not row['storage_key'])]
                storage_keys = [str(row['storage_key']) for row in visual_rows if row['storage_key']]
                with conn:
                    self.native_index.delete_for_source(conn, owner_user_id=owner_user_id, package_id=package_id, source_ingestion_id=source_id)
                    for table in ('source_visual_assets', 'source_chunks', 'source_chapters', 'source_catalog_runs', 'source_structures'):
                        conn.execute(f'\n                            DELETE FROM {table}\n                            WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                            ', (owner_user_id, package_id, source_id))
        self.coordinator.run_write(self.path, delete_structure)
        _remove_asset_files(asset_paths)
        self.cleanup_unreferenced_visual_assets(storage_keys)

    def publish_catalog(self, *, structure: SourceStructure, chapters: list[SourceChapter], run: SourceCatalogRun | None=None) -> SourceStructure:
        """Atomically publish one complete catalog without creating a text or visual index."""
        return self.coordinator.run_write(self.path, lambda: self._publish_catalog(structure=structure, chapters=chapters, run=run))

    def _publish_catalog(self, *, structure: SourceStructure, chapters: list[SourceChapter], run: SourceCatalogRun | None=None) -> SourceStructure:
        self._validate_catalog_bundle(structure=structure, chapters=chapters, run=run)
        old_asset_paths: list[str] = []
        old_storage_keys: list[str] = []
        stamp = now_iso()
        published: SourceStructure
        with self._lock:
            with self._connect() as conn:
                expected_run_id = str((run.metadata.get('active_catalog_run_id') if run is not None else '') or '').strip()
                if expected_run_id:
                    source_row = conn.execute('\n                        SELECT metadata_json FROM source_ingestions\n                        WHERE id = ? AND owner_user_id = ? AND package_id = ?\n                        ', (structure.source_ingestion_id, structure.owner_user_id, structure.package_id)).fetchone()
                    if source_row is None:
                        raise RuntimeError('Source was removed before catalog publication.')
                    try:
                        source_metadata = json.loads(source_row['metadata_json'] or '{}')
                    except (TypeError, ValueError):
                        source_metadata = {}
                    if source_metadata.get('active_catalog_run_id') != expected_run_id:
                        raise RuntimeError('A newer catalog run superseded this publication.')
                    expected_hash = str(source_metadata.get('content_hash') or '')
                    if expected_hash and expected_hash != structure.source_content_hash:
                        raise RuntimeError('Catalog publication source fingerprint changed.')
                previous = conn.execute('\n                    SELECT catalog_version\n                    FROM source_structures\n                    WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                    ', (structure.owner_user_id, structure.package_id, structure.source_ingestion_id)).fetchone()
                current_version = int(previous['catalog_version'] or 0) if previous else 0
                requested_version = int(structure.catalog_version or 0)
                catalog_version = requested_version if requested_version > current_version else current_version + 1
                source_content_hash = structure.source_content_hash
                published_chapters = [chapter.model_copy(update={'catalog_version': catalog_version, 'source_content_hash': chapter.source_content_hash or source_content_hash, 'mapping_status': 'verified' if chapter.mapping_status == 'unverified' and chapter.anchor_status == 'verified' else chapter.mapping_status}) for chapter in chapters]
                published = structure.model_copy(update={'catalog_version': catalog_version, 'catalog_updated_at': stamp, 'updated_at': stamp, 'chapter_count': len(published_chapters), 'chunk_count': 0, 'visual_count': 0, 'visual_index_status': 'unsupported', 'visual_index_version': 0, 'has_verified_toc': bool(published_chapters) if structure.metadata.get('catalog_task_contract') == 'directory_pages_offset_tree_v1' or structure.metadata.get('directory_status') == 'complete' else any((chapter.mapping_status == 'verified' for chapter in published_chapters))})
                old_visual_rows = conn.execute('\n                    SELECT asset_path, storage_key FROM source_visual_assets\n                    WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                    ', (published.owner_user_id, published.package_id, published.source_ingestion_id)).fetchall()
                old_asset_paths = [str(row['asset_path']) for row in old_visual_rows if row['asset_path'] and (not row['storage_key'])]
                old_storage_keys = [str(row['storage_key']) for row in old_visual_rows if row['storage_key']]
                with conn:
                    self._delete_index_rows(conn, published)
                    self._upsert_structure(conn, published)
                    self._insert_chapters(conn, published_chapters)
                    if run is not None:
                        verified_count = sum((chapter.mapping_status == 'verified' for chapter in published_chapters))
                        published_run = run.model_copy(update={'catalog_version': catalog_version, 'chapter_count': len(published_chapters), 'verified_chapter_count': verified_count, 'verification_rate': verified_count / len(published_chapters) if published_chapters else 0.0, 'updated_at': stamp, 'completed_at': run.completed_at or (stamp if run.status == 'succeeded' else None)})
                        self._upsert_catalog_run(conn, published_run)
        _remove_asset_files(old_asset_paths)
        self.cleanup_unreferenced_visual_assets(old_storage_keys)
        return published

    def save_catalog_run(self, run: SourceCatalogRun) -> SourceCatalogRun:
        return self.coordinator.run_write(self.path, lambda: self._save_catalog_run(run))

    def _save_catalog_run(self, run: SourceCatalogRun) -> SourceCatalogRun:
        run = run.model_copy(update={'updated_at': now_iso()})
        with self._lock:
            with self._connect() as conn:
                with conn:
                    self._upsert_catalog_run(conn, run)
        return run

    def list_catalog_runs(self, *, owner_user_id: str, package_id: str, source_id: str, limit: int=20) -> list[SourceCatalogRun]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute('\n                    SELECT * FROM source_catalog_runs\n                    WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                    ORDER BY created_at DESC\n                    LIMIT ?\n                    ', (owner_user_id, package_id, source_id, max(1, limit))).fetchall()
        return [self._catalog_run_from_row(row) for row in rows]

    def save_structure_bundle(self, *, structure: SourceStructure, chapters: list[SourceChapter], chunks: list[SourceChunk], visuals: list[SourceVisualAsset] | None=None, connection: sqlite3.Connection | None=None) -> SourceStructure:
        if connection is not None:
            return self._save_structure_bundle(structure=structure, chapters=chapters, chunks=chunks, visuals=visuals, connection=connection)
        return self.coordinator.run_write(self.path, lambda: self._save_structure_bundle(structure=structure, chapters=chapters, chunks=chunks, visuals=visuals))

    def upsert_scoped_visuals(self, visuals: list[SourceVisualAsset]) -> None:
        """Persist Board Agent-approved visuals without replacing the source catalog."""
        if not visuals:
            return
        self.coordinator.run_write(self.path, lambda: self._upsert_scoped_visuals(visuals))

    def _upsert_scoped_visuals(self, visuals: list[SourceVisualAsset]) -> None:
        first = visuals[0]
        if any((visual.owner_user_id != first.owner_user_id or visual.package_id != first.package_id or visual.source_ingestion_id != first.source_ingestion_id for visual in visuals)):
            raise ValueError('Scoped visuals must belong to one authenticated source.')
        if any((visual.anchor_status != 'verified' or not visual.metadata.get('source_range_verified') for visual in visuals)):
            raise ValueError('Unverified scoped visuals cannot be persisted.')
        with self._lock:
            with self._connect() as conn:
                with conn:
                    conn.executemany('\n                        INSERT INTO source_visual_assets(\n                            id, owner_user_id, package_id, source_ingestion_id, structure_id,\n                            structure_version, chapter_id, kind, source_locator, page_start, page_end,\n                            paragraph_index, slide_no, sheet_name, bbox_json, before_chunk_id,\n                            after_chunk_id, caption, extracted_text, surrounding_text, anchor_status,\n                            mime_type, asset_path, storage_key, order_index, content_hash, position_hash,\n                            width, height, table_data_json, confidence, created_at, metadata_json\n                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                        ON CONFLICT(id) DO UPDATE SET\n                            structure_id = excluded.structure_id,\n                            structure_version = excluded.structure_version,\n                            chapter_id = excluded.chapter_id,\n                            kind = excluded.kind,\n                            source_locator = excluded.source_locator,\n                            page_start = excluded.page_start,\n                            page_end = excluded.page_end,\n                            paragraph_index = excluded.paragraph_index,\n                            slide_no = excluded.slide_no,\n                            sheet_name = excluded.sheet_name,\n                            bbox_json = excluded.bbox_json,\n                            before_chunk_id = excluded.before_chunk_id,\n                            after_chunk_id = excluded.after_chunk_id,\n                            caption = excluded.caption,\n                            extracted_text = excluded.extracted_text,\n                            surrounding_text = excluded.surrounding_text,\n                            anchor_status = excluded.anchor_status,\n                            mime_type = excluded.mime_type,\n                            asset_path = excluded.asset_path,\n                            storage_key = excluded.storage_key,\n                            order_index = excluded.order_index,\n                            content_hash = excluded.content_hash,\n                            position_hash = excluded.position_hash,\n                            width = excluded.width,\n                            height = excluded.height,\n                            table_data_json = excluded.table_data_json,\n                            confidence = excluded.confidence,\n                            metadata_json = excluded.metadata_json\n                        ', [(visual.id, visual.owner_user_id, visual.package_id, visual.source_ingestion_id, visual.structure_id, visual.structure_version, visual.chapter_id, visual.kind, visual.source_locator, visual.page_start, visual.page_end, visual.paragraph_index, visual.slide_no, visual.sheet_name, _dumps(visual.bbox), visual.before_chunk_id, visual.after_chunk_id, visual.caption, visual.extracted_text, visual.surrounding_text, visual.anchor_status, visual.mime_type, visual.asset_path, visual.storage_key, visual.order_index, visual.content_hash, visual.position_hash, visual.width, visual.height, _dumps(visual.table_data), visual.confidence, visual.created_at, _dumps(visual.metadata)) for visual in visuals])
                    conn.execute("\n                        UPDATE source_structures\n                        SET visual_count = (\n                            SELECT COUNT(*) FROM source_visual_assets\n                            WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                        ), visual_index_status = 'ready', visual_index_version = ?, updated_at = ?\n                        WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                        ", (first.owner_user_id, first.package_id, first.source_ingestion_id, first.structure_version, now_iso(), first.owner_user_id, first.package_id, first.source_ingestion_id))

    def scoped_visual_cache_complete(self, *, owner_user_id: str, package_id: str, source_ingestion_id: str, scope_cache_key: str) -> bool:
        with self._lock:
            with self._connect() as conn:
                row = conn.execute('\n                    SELECT metadata_json FROM source_structures\n                    WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                    ', (owner_user_id, package_id, source_ingestion_id)).fetchone()
        metadata = _loads(row['metadata_json'], {}) if row is not None else {}
        return scope_cache_key in set(metadata.get('completed_visual_scope_keys') or [])

    def mark_scoped_visual_cache_complete(self, *, owner_user_id: str, package_id: str, source_ingestion_id: str, scope_cache_key: str) -> None:

        def write() -> None:
            with self._lock:
                with self._connect() as conn:
                    with conn:
                        row = conn.execute('\n                            SELECT metadata_json FROM source_structures\n                            WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                            ', (owner_user_id, package_id, source_ingestion_id)).fetchone()
                        if row is None:
                            return
                        metadata = dict(_loads(row['metadata_json'], {}))
                        keys = [str(value) for value in metadata.get('completed_visual_scope_keys') or [] if str(value) and str(value) != scope_cache_key]
                        metadata['completed_visual_scope_keys'] = [*keys[-127:], scope_cache_key]
                        conn.execute('\n                            UPDATE source_structures SET metadata_json = ?, updated_at = ?\n                            WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                            ', (_dumps(metadata), now_iso(), owner_user_id, package_id, source_ingestion_id))
        self.coordinator.run_write(self.path, write)

    def _save_structure_bundle(self, *, structure: SourceStructure, chapters: list[SourceChapter], chunks: list[SourceChunk], visuals: list[SourceVisualAsset] | None=None, connection: sqlite3.Connection | None=None) -> SourceStructure:
        visuals = visuals or []
        chunks = [_chunk_with_text_hash(chunk) for chunk in chunks]
        old_asset_paths: list[str] = []
        old_storage_keys: list[str] = []
        stamp = now_iso()
        structure = structure.model_copy(update={'updated_at': stamp, 'chapter_count': len(chapters), 'chunk_count': len(chunks), 'visual_count': len(visuals), 'has_verified_toc': any((chapter.anchor_status == 'verified' for chapter in chapters))})
        with self._lock:
            with self._connect() if connection is None else nullcontext(connection) as conn:
                old_visual_rows = conn.execute('\n                        SELECT asset_path, storage_key FROM source_visual_assets\n                        WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                        ', (structure.owner_user_id, structure.package_id, structure.source_ingestion_id)).fetchall()
                old_asset_paths = [str(row['asset_path']) for row in old_visual_rows if row['asset_path'] and (not row['storage_key'])]
                old_storage_keys = [str(row['storage_key']) for row in old_visual_rows if row['storage_key']]
                with conn if connection is None else nullcontext():
                    self._delete_index_rows(conn, structure)
                    conn.execute('\n                        INSERT INTO source_structures(\n                            id, owner_user_id, package_id, source_ingestion_id, status, strategy, has_verified_toc,\n                            chapter_count, chunk_count, visual_count, visual_index_status,\n                            visual_index_version, confidence, quality_json, catalog_version, catalog_updated_at,\n                            source_content_hash, catalog_schema_version, catalog_model, error, warnings_json,\n                            created_at, updated_at, metadata_json\n                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                        ON CONFLICT(owner_user_id, package_id, source_ingestion_id) DO UPDATE SET\n                            id = excluded.id,\n                            status = excluded.status,\n                            strategy = excluded.strategy,\n                            has_verified_toc = excluded.has_verified_toc,\n                            chapter_count = excluded.chapter_count,\n                            chunk_count = excluded.chunk_count,\n                            visual_count = excluded.visual_count,\n                            visual_index_status = excluded.visual_index_status,\n                            visual_index_version = excluded.visual_index_version,\n                            confidence = excluded.confidence,\n                            quality_json = excluded.quality_json,\n                            catalog_version = excluded.catalog_version,\n                            catalog_updated_at = excluded.catalog_updated_at,\n                            source_content_hash = excluded.source_content_hash,\n                            catalog_schema_version = excluded.catalog_schema_version,\n                            catalog_model = excluded.catalog_model,\n                            error = excluded.error,\n                            warnings_json = excluded.warnings_json,\n                            updated_at = excluded.updated_at,\n                            metadata_json = excluded.metadata_json\n                        ', (structure.id, structure.owner_user_id, structure.package_id, structure.source_ingestion_id, structure.status, structure.strategy, int(structure.has_verified_toc), structure.chapter_count, structure.chunk_count, structure.visual_count, structure.visual_index_status, structure.visual_index_version, structure.confidence, _dumps(structure.quality.model_dump(mode='json')), structure.catalog_version, structure.catalog_updated_at, structure.source_content_hash, structure.catalog_schema_version, structure.catalog_model, structure.error, _dumps(structure.warnings), structure.created_at, structure.updated_at, _dumps(structure.metadata)))
                    conn.executemany('\n                        INSERT INTO source_chapters(\n                            id, owner_user_id, package_id, source_ingestion_id, parent_id, number,\n                            normalized_number, title, level, path_json, order_index, source_locator,\n                            body_start_offset, body_end_offset, page_start, page_end, anchor_status,\n                            range_json, mapping_status, source_content_hash, catalog_evidence_json,\n                            catalog_version, confidence, excerpt, metadata_json\n                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                        ', [(chapter.id, chapter.owner_user_id, chapter.package_id, chapter.source_ingestion_id, chapter.parent_id, chapter.number, chapter.normalized_number, chapter.title, chapter.level, _dumps(chapter.path), chapter.order_index, chapter.source_locator, chapter.body_start_offset, chapter.body_end_offset, chapter.page_start, chapter.page_end, chapter.anchor_status, _dumps(chapter.range.model_dump(mode='json')) if chapter.range else None, chapter.mapping_status, chapter.source_content_hash, _dumps([item.model_dump(mode='json') for item in chapter.catalog_evidence]), chapter.catalog_version, chapter.confidence, chapter.excerpt, _dumps(chapter.metadata)) for chapter in chapters])
                    conn.executemany('\n                        INSERT INTO source_chunks(\n                            id, owner_user_id, package_id, source_ingestion_id, chapter_id, order_index,\n                            source_locator, text, start_offset, end_offset, page_start, page_end, token_count,\n                            text_hash, metadata_json\n                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                        ', [(chunk.id, chunk.owner_user_id, chunk.package_id, chunk.source_ingestion_id, chunk.chapter_id, chunk.order_index, chunk.source_locator, chunk.text, chunk.start_offset, chunk.end_offset, chunk.page_start, chunk.page_end, chunk.token_count, str(chunk.metadata.get('text_hash') or source_chunk_text_hash(chunk.text)), _dumps(chunk.metadata)) for chunk in chunks])
                    conn.executemany('\n                        INSERT INTO source_visual_assets(\n                            id, owner_user_id, package_id, source_ingestion_id, structure_id,\n                            structure_version, chapter_id, kind, source_locator, page_start, page_end,\n                            paragraph_index, slide_no, sheet_name, bbox_json, before_chunk_id,\n                            after_chunk_id, caption, extracted_text, surrounding_text, anchor_status,\n                            mime_type, asset_path, storage_key, order_index, content_hash, position_hash,\n                            width, height, table_data_json, confidence, created_at, metadata_json\n                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                        ', [(visual.id, visual.owner_user_id, visual.package_id, visual.source_ingestion_id, visual.structure_id, visual.structure_version, visual.chapter_id, visual.kind, visual.source_locator, visual.page_start, visual.page_end, visual.paragraph_index, visual.slide_no, visual.sheet_name, _dumps(visual.bbox), visual.before_chunk_id, visual.after_chunk_id, visual.caption, visual.extracted_text, visual.surrounding_text, visual.anchor_status, visual.mime_type, visual.asset_path, visual.storage_key, visual.order_index, visual.content_hash, visual.position_hash, visual.width, visual.height, _dumps(visual.table_data), visual.confidence, visual.created_at, _dumps(visual.metadata)) for visual in visuals])
                    self.native_index.index_chunks(conn, chunks)
        if connection is None:
            retained_paths = {visual.asset_path for visual in visuals if visual.asset_path}
            _remove_asset_files((path for path in old_asset_paths if path not in retained_paths))
            retained_storage_keys = {visual.storage_key for visual in visuals if visual.storage_key}
            self.cleanup_unreferenced_visual_assets((key for key in old_storage_keys if key not in retained_storage_keys))
        return structure

    def cleanup_unreferenced_visual_assets(self, storage_keys) -> None:
        candidates = {str(key) for key in storage_keys if str(key)}
        if not candidates:
            return
        unreferenced: list[str] = []
        with self._lock:
            with self._connect() as conn:
                for storage_key in candidates:
                    row = conn.execute('SELECT 1 FROM source_visual_assets WHERE storage_key = ? LIMIT 1', (storage_key,)).fetchone()
                    if row is None:
                        unreferenced.append(storage_key)
        for storage_key in unreferenced:
            try:
                remove_source_visual_asset_if_unstaged(storage_key)
            except (OSError, SourceVisualStorageError):
                continue

    def record_rebuild_failure(self, *, structure: SourceStructure, error: str) -> SourceStructure:
        """Expose a failed reparse without replacing the last usable index."""
        return self.coordinator.run_write(self.path, lambda: self._record_rebuild_failure(structure=structure, error=error))

    def _record_rebuild_failure(self, *, structure: SourceStructure, error: str) -> SourceStructure:
        warning = 'Catalog rebuild failed. The last available catalog has been preserved.'
        warnings = list(dict.fromkeys([*structure.warnings, warning]))
        metadata = dict(structure.metadata)
        if metadata.get('catalog_pipeline') == 'agent_catalog_v3':
            metadata.update({'work_state': 'paused', 'can_refine': True, 'auto_continuing': False, 'background_refine_active': False, 'stop_reason': 'Background catalog building failed. The previous citable catalog has been preserved; resume it manually to continue.'})
        with self._lock:
            with self._connect() as conn:
                with conn:
                    conn.execute('\n                        UPDATE source_structures\n                        SET error = ?, warnings_json = ?, metadata_json = ?\n                        WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                        ', (error, _dumps(warnings), _dumps(metadata), structure.owner_user_id, structure.package_id, structure.source_ingestion_id))
        return structure.model_copy(update={'error': error, 'warnings': warnings, 'metadata': metadata})

    def get_structure(self, *, owner_user_id: str, package_id: str, source_id: str) -> SourceStructure | None:
        package_id = source_storage_package(self.path, owner_user_id, package_id, source_id)
        with self._lock:
            with self._connect() as conn:
                row = conn.execute('\n                    SELECT *\n                    FROM source_structures\n                    WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                    ', (owner_user_id, package_id, source_id)).fetchone()
        return self._structure_from_row(row) if row else None

    def find_reusable_verified_catalog(self, *, owner_user_id: str, source_content_hash: str, exclude_source_id: str) -> tuple[SourceStructure, list[SourceChapter]] | None:
        """Return a complete catalog only for byte-identical content owned by the same user."""
        if not source_content_hash:
            return None
        with self._lock:
            with self._connect() as conn:
                structure_row = conn.execute("\n                    SELECT structure.*\n                    FROM source_structures AS structure\n                    WHERE structure.owner_user_id = ?\n                      AND structure.source_content_hash = ?\n                      AND structure.source_ingestion_id != ?\n                      AND structure.status = 'ready'\n                      AND structure.has_verified_toc = 1\n                      AND structure.chapter_count > 0\n                      AND structure.chapter_count = (\n                          SELECT COUNT(*) FROM source_chapters AS chapter\n                          WHERE chapter.owner_user_id = structure.owner_user_id\n                            AND chapter.package_id = structure.package_id\n                            AND chapter.source_ingestion_id = structure.source_ingestion_id\n                            AND chapter.mapping_status = 'verified'\n                            AND chapter.range_json IS NOT NULL\n                      )\n                    ORDER BY structure.catalog_version DESC, structure.updated_at DESC\n                    LIMIT 1\n                    ", (owner_user_id, source_content_hash, exclude_source_id)).fetchone()
                if structure_row is None:
                    return None
                chapter_rows = conn.execute('\n                    SELECT * FROM source_chapters\n                    WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                    ORDER BY order_index\n                    ', (structure_row['owner_user_id'], structure_row['package_id'], structure_row['source_ingestion_id'])).fetchall()
        return (self._structure_from_row(structure_row), [self._chapter_from_row(row) for row in chapter_rows])

    def set_catalog_pause_requested(self, *, owner_user_id: str, package_id: str, source_id: str, requested: bool) -> SourceStructure | None:
        package_id = source_storage_package(self.path, owner_user_id, package_id, source_id)

        def write() -> SourceStructure | None:
            with self._lock:
                with self._connect() as conn:
                    row = conn.execute('\n                        SELECT * FROM source_structures\n                        WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                        ', (owner_user_id, package_id, source_id)).fetchone()
                    if row is None:
                        return None
                    structure = self._structure_from_row(row)
                    control_metadata = {'pause_requested': requested}
                    if not requested:
                        control_metadata.update({'work_state': 'working', 'can_refine': False, 'stop_reason': ''})
                    updated = structure.model_copy(update={'metadata': {**structure.metadata, **control_metadata}, 'updated_at': now_iso()})
                    with conn:
                        self._upsert_structure(conn, updated)
                    return updated
        return self.coordinator.run_write(self.path, write)

    def catalog_pause_requested(self, *, owner_user_id: str, package_id: str, source_id: str) -> bool:
        structure = self.get_structure(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id)
        return bool(structure and structure.metadata.get('pause_requested'))

    def get_catalog_view(self, *, source: SourceIngestionRecord) -> SourceCatalogView:
        batch = self.get_catalog_views(package_id=source.package_id, sources=[source])
        return batch.catalogs[0]

    def get_catalog_views(self, *, package_id: str, sources: list[SourceIngestionRecord]) -> SourceCatalogBatchView:
        if not sources:
            return SourceCatalogBatchView(package_id=package_id)
        owner_user_id = sources[0].owner_user_id
        if any((source.owner_user_id != owner_user_id for source in sources)):
            raise ValueError('Catalog batch sources must share one owner.')
        source_scopes = {source.id: source.package_id for source in sources}
        source_ids = set(source_scopes)
        with self._lock:
            with self._connect() as conn:
                structure_rows = conn.execute('\n                    SELECT * FROM source_structures\n                    WHERE owner_user_id = ?\n                    ', (owner_user_id,)).fetchall()
                chapter_rows = conn.execute('\n                    SELECT * FROM source_chapters\n                    WHERE owner_user_id = ?\n                    ORDER BY source_ingestion_id, order_index\n                    ', (owner_user_id,)).fetchall()
        structures = {str(row['source_ingestion_id']): self._structure_from_row(row) for row in structure_rows if source_scopes.get(str(row['source_ingestion_id'])) == row['package_id']}
        chapters_by_source: dict[str, list[SourceChapter]] = {source_id: [] for source_id in source_ids}
        for row in chapter_rows:
            source_id = str(row['source_ingestion_id'])
            if source_scopes.get(source_id) == row['package_id']:
                chapters_by_source[source_id].append(self._chapter_from_row(row))
        catalogs = [self._build_catalog_view(source=source, structure=structures.get(source.id), chapters=chapters_by_source.get(source.id, [])) for source in sources]
        return SourceCatalogBatchView(package_id=package_id, catalogs=catalogs)

    def get_catalog_chapter(self, *, owner_user_id: str, package_id: str, source_id: str, chapter_id: str) -> tuple[SourceStructure, SourceChapter] | None:
        package_id = source_storage_package(self.path, owner_user_id, package_id, source_id)
        with self._lock:
            with self._connect() as conn:
                structure_row = conn.execute('\n                    SELECT * FROM source_structures\n                    WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                    ', (owner_user_id, package_id, source_id)).fetchone()
                chapter_row = conn.execute('\n                    SELECT * FROM source_chapters\n                    WHERE owner_user_id = ? AND package_id = ?\n                      AND source_ingestion_id = ? AND id = ?\n                    ', (owner_user_id, package_id, source_id, chapter_id)).fetchone()
        if structure_row is None or chapter_row is None:
            return None
        return (self._structure_from_row(structure_row), self._chapter_from_row(chapter_row))

    @staticmethod
    def _build_catalog_view(*, source: SourceIngestionRecord, structure: SourceStructure | None, chapters: list[SourceChapter]) -> SourceCatalogView:
        structure_status = structure.status if structure else source.structure_status
        catalog_version = structure.catalog_version if structure else 0
        source_content_hash = str((structure.source_content_hash if structure else '') or source.metadata.get('source_content_hash') or source.metadata.get('content_hash') or source.metadata.get('sha256') or '')
        catalog_chapters = [SourceStructureStore._chapter_for_catalog(source=source, structure=structure, chapter=chapter, source_content_hash=source_content_hash, catalog_version=catalog_version) for chapter in chapters]
        verified_count = sum((chapter.mapping_status == 'verified' for chapter in catalog_chapters))
        structure_metadata = structure.metadata if structure else {}
        raw_payload = structure_metadata.get('agent_catalog_payload')
        agent_payload = raw_payload if isinstance(raw_payload, dict) else {}
        raw_regimes = structure_metadata.get('pagination_regimes')
        if not isinstance(raw_regimes, list):
            raw_regimes = agent_payload.get('pagination_regimes')
        pagination_regimes = raw_regimes if isinstance(raw_regimes, list) else []
        locator_sources = {str(chapter.metadata.get('locator_source') or '') for chapter in catalog_chapters if str(chapter.metadata.get('locator_source') or '')}
        if locator_sources and locator_sources <= {'native_navigation'}:
            locator_method = 'native_navigation'
        elif pagination_regimes:
            locator_method = 'exact_p_regimes'
        elif locator_sources:
            locator_method = '+'.join(sorted(locator_sources))
        else:
            locator_method = ''
        visual_projection = project_pdf_visual_catalog(structure, catalog_chapters)
        if visual_projection is not None:
            locator_method = visual_projection.locator_method
        elif structure is not None and structure.status == 'ready' and (structure.quality.level == 'fully_verified') and (structure_metadata.get('source_backend') in {'google', 'local'}) and (structure_metadata.get('catalog_mode') == 'cloud') and (source.metadata.get('content_hash') == source_content_hash) and (catalog_version > 0) and catalog_chapters and all((chapter.mapping_status == 'verified' and chapter.range is not None and (not chapter.range.metadata.get('legacy_page_end_exclusive')) and (chapter.source_content_hash == source_content_hash) and (chapter.catalog_version == catalog_version) for chapter in catalog_chapters)):
            locator_method = 'google_layout_ranges'
        raw_index_status = str(structure_metadata.get('index_status') or ('complete' if verified_count == len(catalog_chapters) else 'partial'))
        index_status = raw_index_status if raw_index_status in {'pending', 'in_progress', 'complete', 'partial'} else 'partial'
        view = SourceCatalogView(source=SourceCatalogSourceSummary(id=source.id, title=source.title, file_name=source.file_name, mime_type=source.mime_type, size_bytes=source.size_bytes, status=source.status, structure_status=structure_status), structure_id=structure.id if structure else None, status=structure_status, strategy=structure.strategy if structure else source.structure_strategy, has_verified_toc=structure.has_verified_toc if structure else False, catalog_version=catalog_version, catalog_updated_at=structure.catalog_updated_at or structure.updated_at if structure else source.structure_updated_at, source_content_hash=source_content_hash, catalog_schema_version=structure.catalog_schema_version if structure else 'legacy', catalog_model=structure.catalog_model if structure else '', catalog_authority=str(structure_metadata.get('catalog_authority') or '') if structure else '', model_call_count=max(0, int(structure_metadata.get('model_call_count') or 0)) if structure else 0, task_contract=str(structure.metadata.get('catalog_task_contract') or '') if structure else '', work_state=str(structure_metadata.get('work_state') or 'satisfied') if structure else 'satisfied', phase=str(structure_metadata.get('phase') or 'terminal') if structure else 'terminal', directory_status=str(structure_metadata.get('directory_status') or 'complete') if structure else 'uncertain' if source.metadata.get('processing_mode') == 'ordinary' else 'complete', index_status=index_status if structure else 'complete', summary=visual_projection.summary if visual_projection is not None else str(structure_metadata.get('summary') or '') if structure else '', next_plan=str(structure_metadata.get('next_plan') or '') if structure else '', stop_reason=str(structure_metadata.get('stop_reason') or '') if structure else '', completion_reason=str(structure_metadata.get('completion_reason') or '') if structure else '', directory_gaps=[str(item) for item in structure_metadata.get('directory_gaps', [])] if structure and isinstance(structure_metadata.get('directory_gaps'), list) else [], remaining_work=[item for item in structure_metadata.get('remaining_work', []) if isinstance(item, dict)] if structure and isinstance(structure_metadata.get('remaining_work'), list) else [], remaining_work_count=len([item for item in structure_metadata.get('remaining_work', []) if isinstance(item, dict)]) if structure and isinstance(structure_metadata.get('remaining_work'), list) else 0, snapshot_reason=str(structure_metadata.get('snapshot_reason')) if structure_metadata.get('snapshot_reason') in {'first_citable', 'top_level_subtree', 'batch', 'budget_increment', 'correction', 'pause', 'final'} else 'budget_increment', background_refine_active=bool(structure and structure_metadata.get('work_state') in {'working', 'partial'} and structure_metadata.get('can_refine', True) and (not structure_metadata.get('pause_requested'))), pagination_regime_count=visual_projection.pagination_segment_count if visual_projection is not None else len(pagination_regimes), unresolved_node_count=max(0, len(catalog_chapters) - verified_count), locator_method=locator_method, revision=catalog_version, can_refine=bool(structure.metadata.get('can_refine', True)) if structure else False, recent_tool_activity=list(structure.metadata.get('recent_tool_activity') or [])[-20:] if structure else [], chapter_count=len(catalog_chapters), verified_chapter_count=verified_count, confidence=structure.confidence if structure else 0.0, quality=visual_projection.quality if visual_projection is not None else structure.quality if structure else source.structure_quality, error=structure.error if structure else source.structure_error, warnings=structure.warnings if structure else [], chapters=catalog_chapters)
        direct = source.metadata.get('catalog_display_mode') == 'direct' or (structure is not None and structure.catalog_schema_version in {'pi_catalog_refinement_v1', 'anythingllm_source_catalog_v2', 'anythingllm_direct_catalog_v1'})
        if direct:
            return view
        from openclass_core.services.google_sources.catalog_contract import public_catalog_view
        return public_catalog_view(source, structure, catalog_chapters, view)

    @staticmethod
    def _chapter_for_catalog(*, source: SourceIngestionRecord, structure: SourceStructure | None, chapter: SourceChapter, source_content_hash: str, catalog_version: int) -> SourceChapter:
        source_range = chapter.range
        if source_range is None and chapter.page_start is not None:
            mime = source.mime_type.casefold()
            suffix = Path(source.file_name).suffix.casefold()
            range_kind = None
            if 'pdf' in mime or suffix == '.pdf':
                range_kind = 'pdf_pages'
            elif 'presentation' in mime or suffix in {'.ppt', '.pptx'}:
                range_kind = 'ppt_slides'
            if range_kind:
                legacy_end = max(chapter.page_start, (chapter.page_end or chapter.page_start + 1) - 1)
                display_label = f"{('p.' if range_kind == 'pdf_pages' else 'slide')} {chapter.page_start}" if legacy_end == chapter.page_start else f"{('pp.' if range_kind == 'pdf_pages' else 'slides')} {chapter.page_start}-{legacy_end}"
                source_range = SourceRange(kind=range_kind, start=chapter.page_start, end=legacy_end, display_label=display_label, metadata={'legacy_page_end_exclusive': True})
        mapping_status = chapter.mapping_status
        if mapping_status == 'unverified' and chapter.anchor_status == 'verified' and (not structure or structure.catalog_schema_version == 'legacy'):
            mapping_status = 'verified'
        return chapter.model_copy(update={'range': source_range, 'mapping_status': mapping_status, 'source_content_hash': chapter.source_content_hash or source_content_hash, 'catalog_version': chapter.catalog_version or catalog_version, 'excerpt': ''})

    def get_structure_view(self, *, source: SourceIngestionRecord, chunk_limit: int=20) -> SourceStructureView:
        with self._lock:
            with self._connect() as conn:
                structure_row = conn.execute('\n                    SELECT *\n                    FROM source_structures\n                    WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                    ', (source.owner_user_id, source.package_id, source.id)).fetchone()
                chapter_rows = conn.execute('\n                    SELECT *\n                    FROM source_chapters\n                    WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                    ORDER BY order_index\n                    ', (source.owner_user_id, source.package_id, source.id)).fetchall()
                chunk_rows = conn.execute('\n                    SELECT *\n                    FROM source_chunks\n                    WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                    ORDER BY order_index\n                    LIMIT ?\n                    ', (source.owner_user_id, source.package_id, source.id, chunk_limit)).fetchall()
                visual_rows = conn.execute('\n                    SELECT * FROM source_visual_assets\n                    WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                    ORDER BY order_index\n                    ', (source.owner_user_id, source.package_id, source.id)).fetchall()
        return SourceStructureView(source=self.attach_summary(source), structure=self._structure_from_row(structure_row) if structure_row else None, chapters=[self._chapter_from_row(row) for row in chapter_rows], chunks=[self._chunk_from_row(row) for row in chunk_rows], visuals=[self._visual_from_row(row) for row in visual_rows])

    def visual_evidence_for_scope(self, *, owner_user_id: str, package_id: str, source_ingestion_id: str, chapter_id: str | None=None, page_start: int | None=None, page_end: int | None=None, scope_cache_key: str='') -> list[SourceVisualEvidence]:
        return self._visual_evidence_for_scope(owner_user_id=owner_user_id, package_id=package_id, source_ingestion_id=source_ingestion_id, chapter_id=chapter_id, page_start=page_start, page_end=page_end, scope_cache_key=scope_cache_key)

    def _visual_evidence_for_scope(self, *, owner_user_id: str, package_id: str, source_ingestion_id: str, chapter_id: str | None=None, page_start: int | None=None, page_end: int | None=None, scope_cache_key: str='') -> list[SourceVisualEvidence]:
        package_id = source_storage_package(self.path, owner_user_id, package_id, source_ingestion_id)
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute('\n                    SELECT * FROM source_visual_assets\n                    WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                    ORDER BY order_index\n                    ', (owner_user_id, package_id, source_ingestion_id)).fetchall()
        assets = [self._visual_from_row(row) for row in rows]
        selected = [asset for asset in assets if asset.anchor_status == 'verified' and (not chapter_id or asset.chapter_id == chapter_id) and (not scope_cache_key or str(asset.metadata.get('scope_cache_key') or '') == scope_cache_key) and _visual_in_page_range(asset, page_start=page_start, page_end=page_end)]
        return [SourceVisualEvidence(visual_id=asset.id, package_id=asset.package_id, source_ingestion_id=asset.source_ingestion_id, source_chapter_id=asset.chapter_id or '', kind=asset.kind, source_locator=asset.source_locator, page_start=asset.page_start, page_end=asset.page_end, paragraph_index=asset.paragraph_index, slide_no=asset.slide_no, sheet_name=asset.sheet_name, bbox=asset.bbox, before_chunk_id=asset.before_chunk_id, after_chunk_id=asset.after_chunk_id, caption=asset.caption, extracted_text=asset.extracted_text, surrounding_text=asset.surrounding_text, anchor_status=asset.anchor_status, mime_type=asset.mime_type, order_index=asset.order_index, content_hash=asset.content_hash, position_hash=asset.position_hash, width=asset.width, height=asset.height, table_data=asset.table_data, confidence=asset.confidence, metadata=asset.metadata) for asset in selected]

    def scoped_visual_evidence(self, *, owner_user_id: str, package_id: str, source_ingestion_id: str, chapter_id: str, scope_cache_key: str) -> list[SourceVisualEvidence]:
        """Read only assets frozen by the authenticated on-demand extractor."""
        return self._visual_evidence_for_scope(owner_user_id=owner_user_id, package_id=package_id, source_ingestion_id=source_ingestion_id, chapter_id=chapter_id, scope_cache_key=scope_cache_key)

    def get_visual(self, *, owner_user_id: str, package_id: str, source_id: str, visual_id: str) -> SourceVisualAsset | None:
        """Return one visual only when every ownership and source key matches."""
        package_id = source_storage_package(self.path, owner_user_id, package_id, source_id)
        with self._lock:
            with self._connect() as conn:
                row = conn.execute('\n                    SELECT * FROM source_visual_assets\n                    WHERE owner_user_id = ? AND package_id = ?\n                      AND source_ingestion_id = ? AND id = ?\n                    LIMIT 1\n                    ', (owner_user_id, package_id, source_id, visual_id)).fetchone()
        return self._visual_from_row(row) if row is not None else None

    def read_visual_bytes(self, *, owner_user_id: str, package_id: str, source_id: str, visual_id: str) -> tuple[SourceVisualAsset, bytes] | None:
        """Read one source visual without weakening its ownership or hash boundary."""
        asset = self.get_visual(owner_user_id=owner_user_id, package_id=package_id, source_id=source_id, visual_id=visual_id)
        if asset is None:
            return None
        expected_hash = asset.content_hash.strip().lower()
        if not _is_sha256_hex(expected_hash):
            return None
        try:
            if asset.storage_key:
                content = read_source_visual_asset(asset.storage_key)
            else:
                content = _read_legacy_visual_path(asset.asset_path, source_id=asset.source_ingestion_id)
        except (OSError, SourceVisualStorageError):
            return None
        if not content or hashlib.sha256(content).hexdigest() != expected_hash:
            return None
        return (asset, content)

    def source_chunks_by_ids(self, *, owner_user_id: str, chunk_ids: list[str]) -> list[SourceChunk]:
        if not chunk_ids:
            return []
        placeholders = ', '.join(('?' for _ in chunk_ids))
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(f'\n                    SELECT * FROM source_chunks\n                    WHERE owner_user_id = ? AND id IN ({placeholders})\n                    ', [owner_user_id, *chunk_ids]).fetchall()
        chunks_by_id = {str(row['id']): self._chunk_from_row(row) for row in rows}
        return [chunks_by_id[chunk_id] for chunk_id in chunk_ids if chunk_id in chunks_by_id]

    def chapter_evidence_by_number(self, *, owner_user_id: str, package_id: str, normalized_number: str, limit: int, token_budget: int) -> list[RetrievalEvidence]:
        with self._lock:
            with self._connect() as conn:
                chapter_rows = conn.execute("\n                    SELECT source_chapters.*, source_ingestions.title AS source_title,\n                        source_ingestions.source_uri AS source_uri,\n                        source_ingestions.open_notebook_source_id AS open_notebook_source_id\n                    FROM source_chapters\n                    JOIN source_ingestions\n                        ON source_ingestions.owner_user_id = source_chapters.owner_user_id\n                        AND source_ingestions.package_id = source_chapters.package_id\n                        AND source_ingestions.id = source_chapters.source_ingestion_id\n                    JOIN source_structures\n                        ON source_structures.owner_user_id = source_chapters.owner_user_id\n                        AND source_structures.package_id = source_chapters.package_id\n                        AND source_structures.source_ingestion_id = source_chapters.source_ingestion_id\n                    WHERE source_chapters.owner_user_id = ?\n                        AND source_chapters.package_id = ?\n                        AND source_chapters.normalized_number = ?\n                        AND source_chapters.anchor_status = 'verified'\n                        AND source_structures.status = 'ready'\n                        AND source_ingestions.status = 'ready'\n                    ORDER BY source_chapters.confidence DESC, source_chapters.order_index ASC\n                    LIMIT ?\n                    ", (owner_user_id, package_id, normalized_number, limit)).fetchall()
                return self._chapter_evidence_from_rows(conn, owner_user_id=owner_user_id, package_id=package_id, chapter_rows=chapter_rows, limit=limit, token_budget=token_budget)

    def chapter_evidence_by_id(self, *, owner_user_id: str, package_id: str, chapter_id: str, limit: int, token_budget: int) -> list[RetrievalEvidence]:
        with self._lock:
            with self._connect() as conn:
                chapter_rows = conn.execute("\n                    SELECT source_chapters.*, source_ingestions.title AS source_title,\n                        source_ingestions.source_uri AS source_uri,\n                        source_ingestions.open_notebook_source_id AS open_notebook_source_id\n                    FROM source_chapters\n                    JOIN source_ingestions\n                        ON source_ingestions.owner_user_id = source_chapters.owner_user_id\n                        AND source_ingestions.package_id = source_chapters.package_id\n                        AND source_ingestions.id = source_chapters.source_ingestion_id\n                    JOIN source_structures\n                        ON source_structures.owner_user_id = source_chapters.owner_user_id\n                        AND source_structures.package_id = source_chapters.package_id\n                        AND source_structures.source_ingestion_id = source_chapters.source_ingestion_id\n                    WHERE source_chapters.owner_user_id = ?\n                        AND source_chapters.package_id = ?\n                        AND source_chapters.id = ?\n                        AND source_chapters.anchor_status = 'verified'\n                        AND source_structures.status = 'ready'\n                        AND source_ingestions.status = 'ready'\n                    ORDER BY source_chapters.confidence DESC, source_chapters.order_index ASC\n                    LIMIT ?\n                    ", (owner_user_id, package_id, chapter_id, limit)).fetchall()
                return self._chapter_evidence_from_rows(conn, owner_user_id=owner_user_id, package_id=package_id, chapter_rows=chapter_rows, limit=limit, token_budget=token_budget)

    def page_range_evidence(self, *, owner_user_id: str, package_id: str, source_ingestion_id: str, page_start: int, page_end: int, token_budget: int) -> list[RetrievalEvidence]:
        if page_start < 1 or page_end <= page_start:
            return []
        with self._lock:
            with self._connect() as conn:
                source_row = conn.execute("\n                    SELECT title, source_uri, open_notebook_source_id\n                    FROM source_ingestions\n                    WHERE owner_user_id = ? AND package_id = ? AND id = ? AND status = 'ready'\n                    ", (owner_user_id, package_id, source_ingestion_id)).fetchone()
                if source_row is None:
                    return []
                rows = conn.execute('\n                    SELECT source_chunks.*, source_chapters.path_json AS chapter_path_json\n                    FROM source_chunks\n                    LEFT JOIN source_chapters\n                        ON source_chapters.owner_user_id = source_chunks.owner_user_id\n                        AND source_chapters.package_id = source_chunks.package_id\n                        AND source_chapters.id = source_chunks.chapter_id\n                    WHERE source_chunks.owner_user_id = ?\n                        AND source_chunks.package_id = ?\n                        AND source_chunks.source_ingestion_id = ?\n                        AND source_chunks.page_start IS NOT NULL\n                        AND source_chunks.page_start < ?\n                        AND COALESCE(source_chunks.page_end, source_chunks.page_start + 1) > ?\n                    ORDER BY source_chunks.order_index\n                    ', (owner_user_id, package_id, source_ingestion_id, page_end, page_start)).fetchall()
        chunk_ids: list[str] = []
        text_parts: list[str] = []
        used_tokens = 0
        section_path: list[str] = []
        for row in rows:
            chunk = self._chunk_from_row(row)
            chunk_tokens = chunk.token_count or _estimate_tokens(chunk.text)
            if used_tokens and used_tokens + chunk_tokens > token_budget:
                break
            used_tokens += chunk_tokens
            chunk_ids.append(chunk.id)
            text_parts.append(chunk.text)
            if not section_path:
                section_path = _loads(row['chapter_path_json'], [])
        if not text_parts:
            return []
        expanded_text = '\n\n'.join(text_parts).strip()
        display_end = max(page_start, page_end - 1)
        page_range = f'p. {page_start}' if display_end == page_start else f'pp. {page_start}-{display_end}'
        return [RetrievalEvidence(source_ingestion_id=source_ingestion_id, open_notebook_source_id=str(source_row['open_notebook_source_id'] or ''), source_title=str(source_row['title'] or ''), source_uri=source_row['source_uri'], section_path=section_path, page_range=page_range, chunk_ids=chunk_ids, excerpt=_compact_text(expanded_text, 360), expanded_text=expanded_text, relevance_score=1.0, reason='Matched the source pages explicitly selected by the user.', token_count=used_tokens, metadata={'retrieval_mode': 'verified_page_range', 'page_start': page_start, 'page_end': page_end})]

    def _chapter_evidence_from_rows(self, conn: sqlite3.Connection, *, owner_user_id: str, package_id: str, chapter_rows: list[sqlite3.Row], limit: int, token_budget: int) -> list[RetrievalEvidence]:
        evidence: list[RetrievalEvidence] = []
        used_tokens = 0
        for chapter_row in chapter_rows:
            chapter = self._chapter_from_row(chapter_row)
            chunk_rows = conn.execute('\n                SELECT *\n                FROM source_chunks\n                WHERE owner_user_id = ?\n                    AND package_id = ?\n                    AND source_ingestion_id = ?\n                    AND (\n                        chapter_id = ?\n                        OR (\n                            ? IS NOT NULL\n                            AND ? IS NOT NULL\n                            AND end_offset > ?\n                            AND start_offset < ?\n                        )\n                    )\n                ORDER BY order_index\n                ', (owner_user_id, package_id, chapter.source_ingestion_id, chapter.id, chapter.body_start_offset, chapter.body_end_offset, chapter.body_start_offset, chapter.body_end_offset)).fetchall()
            chunks = [self._chunk_from_row(row) for row in chunk_rows]
            if not chunks:
                continue
            chunk_ids: list[str] = []
            text_parts: list[str] = []
            chunk_tokens = 0
            for chunk in chunks:
                chunk_text = _chunk_text_for_chapter(chunk, chapter)
                if not chunk_text:
                    continue
                chunk_token_count = _estimate_tokens(chunk_text)
                if used_tokens and used_tokens + chunk_token_count > token_budget:
                    break
                chunk_ids.append(chunk.id)
                text_parts.append(chunk_text)
                chunk_tokens += chunk_token_count
                used_tokens += chunk_token_count
            if not text_parts:
                break
            expanded_text = '\n\n'.join(text_parts).strip()
            evidence.append(RetrievalEvidence(source_ingestion_id=chapter.source_ingestion_id, open_notebook_source_id=str(chapter_row['open_notebook_source_id'] or ''), source_title=str(chapter_row['source_title'] or ''), source_uri=chapter_row['source_uri'], chapter_id=chapter.id, section_path=chapter.path or [chapter.title], page_range=_chapter_page_range(chapter), chunk_ids=chunk_ids, excerpt=chapter.excerpt or _compact_text(expanded_text, 360), expanded_text=expanded_text, relevance_score=chapter.confidence, reason='Matched a verified catalog entry and retrieved its corresponding content range.', token_count=chunk_tokens, metadata={'retrieval_mode': 'verified_chapter', 'chapter_number': chapter.normalized_number, 'source_locator': chapter.source_locator}))
            if len(evidence) >= limit or used_tokens >= token_budget:
                break
        return evidence

    def chunk_evidence_search(self, *, owner_user_id: str, package_id: str, query: str, limit: int, token_budget: int, source_ingestion_ids: list[str] | tuple[str, ...] | None=None, search_mode: NativeSearchMode='hybrid', chapter_ids: list[str] | tuple[str, ...] | None=None, page_ranges: dict[str, tuple[int, int]] | None=None) -> list[RetrievalEvidence]:
        if not query.strip() or limit <= 0:
            return []
        requested_source_ids = tuple(dict.fromkeys((source_id for source_id in source_ingestion_ids or [] if source_id)))
        with self._lock:
            with self._connect() as conn:
                active_source_params: list[object] = [owner_user_id, package_id]
                requested_filter = ''
                if requested_source_ids:
                    placeholders = ', '.join(('?' for _ in requested_source_ids))
                    requested_filter = f'AND source_ingestions.id IN ({placeholders})'
                    active_source_params.extend(requested_source_ids)
                active_source_rows = conn.execute(f"\n                    SELECT source_ingestions.id\n                    FROM source_ingestions\n                    JOIN source_structures\n                        ON source_structures.owner_user_id = source_ingestions.owner_user_id\n                        AND source_structures.package_id = source_ingestions.package_id\n                        AND source_structures.source_ingestion_id = source_ingestions.id\n                    WHERE source_ingestions.owner_user_id = ?\n                        AND source_ingestions.package_id = ?\n                        AND source_ingestions.status = 'ready'\n                        AND source_structures.status IN ('ready', 'linear_only')\n                        {requested_filter}\n                    ", active_source_params).fetchall()
                active_source_ids = tuple((str(row['id']) for row in active_source_rows))
                matches = self.native_index.search(conn, owner_user_id=owner_user_id, package_id=package_id, query=query, source_ingestion_ids=active_source_ids, limit=max(limit * 8, 32), search_mode=search_mode, chapter_ids=chapter_ids or (), page_ranges=page_ranges)
                if not matches:
                    return []
                chunk_ids = [match.chunk_id for match in matches]
                placeholders = ', '.join(('?' for _ in chunk_ids))
                rows = conn.execute(f'\n                    SELECT source_chunks.*, source_ingestions.title AS source_title,\n                        source_ingestions.source_uri AS source_uri,\n                        source_ingestions.open_notebook_source_id AS open_notebook_source_id,\n                        source_chapters.title AS chapter_title,\n                        source_chapters.path_json AS chapter_path_json\n                    FROM source_chunks\n                    JOIN source_ingestions\n                        ON source_ingestions.owner_user_id = source_chunks.owner_user_id\n                        AND source_ingestions.package_id = source_chunks.package_id\n                        AND source_ingestions.id = source_chunks.source_ingestion_id\n                    JOIN source_structures\n                        ON source_structures.owner_user_id = source_chunks.owner_user_id\n                        AND source_structures.package_id = source_chunks.package_id\n                        AND source_structures.source_ingestion_id = source_chunks.source_ingestion_id\n                    LEFT JOIN source_chapters\n                        ON source_chapters.owner_user_id = source_chunks.owner_user_id\n                        AND source_chapters.package_id = source_chunks.package_id\n                        AND source_chapters.id = source_chunks.chapter_id\n                    WHERE source_chunks.owner_user_id = ?\n                        AND source_chunks.package_id = ?\n                        AND source_chunks.id IN ({placeholders})\n                    ', [owner_user_id, package_id, *chunk_ids]).fetchall()
        rows_by_id = {str(row['id']): row for row in rows}
        evidence: list[RetrievalEvidence] = []
        used_tokens = 0
        for match in matches:
            row = rows_by_id.get(match.chunk_id)
            if row is None:
                continue
            chunk = self._chunk_from_row(row)
            token_count = chunk.token_count or _estimate_tokens(chunk.text)
            if used_tokens and used_tokens + token_count > token_budget:
                break
            used_tokens += token_count
            chapter_path = _loads(row['chapter_path_json'], [])
            if not chapter_path and row['chapter_title']:
                chapter_path = [str(row['chapter_title'])]
            evidence.append(RetrievalEvidence(source_ingestion_id=chunk.source_ingestion_id, open_notebook_source_id=str(row['open_notebook_source_id'] or ''), source_title=str(row['source_title'] or ''), source_uri=row['source_uri'], chapter_id=chunk.chapter_id or '', section_path=chapter_path, page_range=_chunk_page_range(chunk), chunk_ids=[chunk.id], excerpt=_compact_text(chunk.text, 360), expanded_text=chunk.text, relevance_score=match.hybrid_score, reason=_native_index_reason(search_mode), token_count=token_count, metadata={'retrieval_mode': 'local_chunk_search', 'native_index_mode': search_mode, 'keyword_score': match.keyword_score, 'semantic_score': match.semantic_score, 'hybrid_score': match.hybrid_score, 'match_modes': list(match.match_modes), 'source_locator': chunk.source_locator, 'page_start': chunk.page_start, 'page_end': chunk.page_end, 'bbox': chunk.metadata.get('bbox') or chunk.metadata.get('bbox_union') or [], 'parser_run_id': str(chunk.metadata.get('parser_run_id') or '')}))
            if len(evidence) >= limit or used_tokens >= token_budget:
                break
        return evidence

    @staticmethod
    def _validate_catalog_bundle(*, structure: SourceStructure, chapters: list[SourceChapter], run: SourceCatalogRun | None) -> None:
        chapter_ids = {chapter.id for chapter in chapters}
        if len(chapter_ids) != len(chapters):
            raise ValueError('Catalog chapter ids must be unique.')
        if structure.strategy in {'native_directory_v1', 'hybrid_directory_v1', 'ai_directory_v1', 'codex_directory_v1'} and (not structure.source_content_hash):
            raise ValueError('Directory catalogs require a source content hash.')
        for chapter in chapters:
            if chapter.owner_user_id != structure.owner_user_id or chapter.package_id != structure.package_id or chapter.source_ingestion_id != structure.source_ingestion_id:
                raise ValueError('Catalog chapter ownership does not match its structure.')
            if chapter.parent_id and chapter.parent_id not in chapter_ids:
                raise ValueError('Catalog chapter parent_id must reference the same catalog.')
            if structure.strategy in {'native_directory_v1', 'hybrid_directory_v1', 'ai_directory_v1', 'codex_directory_v1'} and (chapter.mapping_status == 'verified' or chapter.anchor_status == 'verified') and (chapter.range is None):
                raise ValueError('Verified directory chapters require a SourceRange.')
            if chapter.source_content_hash and chapter.source_content_hash != structure.source_content_hash:
                raise ValueError('Catalog chapter content hash does not match its structure.')
        if run is not None and (run.owner_user_id != structure.owner_user_id or run.package_id != structure.package_id or run.source_ingestion_id != structure.source_ingestion_id):
            raise ValueError('Catalog run ownership does not match its structure.')

    @staticmethod
    def _upsert_structure(conn: sqlite3.Connection, structure: SourceStructure) -> None:
        conn.execute('\n            INSERT INTO source_structures(\n                id, owner_user_id, package_id, source_ingestion_id, status, strategy,\n                has_verified_toc, chapter_count, chunk_count, visual_count,\n                visual_index_status, visual_index_version, confidence, quality_json,\n                catalog_version, catalog_updated_at, source_content_hash,\n                catalog_schema_version, catalog_model, error, warnings_json,\n                created_at, updated_at, metadata_json\n            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n            ON CONFLICT(owner_user_id, package_id, source_ingestion_id) DO UPDATE SET\n                id = excluded.id,\n                status = excluded.status,\n                strategy = excluded.strategy,\n                has_verified_toc = excluded.has_verified_toc,\n                chapter_count = excluded.chapter_count,\n                chunk_count = excluded.chunk_count,\n                visual_count = excluded.visual_count,\n                visual_index_status = excluded.visual_index_status,\n                visual_index_version = excluded.visual_index_version,\n                confidence = excluded.confidence,\n                quality_json = excluded.quality_json,\n                catalog_version = excluded.catalog_version,\n                catalog_updated_at = excluded.catalog_updated_at,\n                source_content_hash = excluded.source_content_hash,\n                catalog_schema_version = excluded.catalog_schema_version,\n                catalog_model = excluded.catalog_model,\n                error = excluded.error,\n                warnings_json = excluded.warnings_json,\n                updated_at = excluded.updated_at,\n                metadata_json = excluded.metadata_json\n            ', (structure.id, structure.owner_user_id, structure.package_id, structure.source_ingestion_id, structure.status, structure.strategy, int(structure.has_verified_toc), structure.chapter_count, structure.chunk_count, structure.visual_count, structure.visual_index_status, structure.visual_index_version, structure.confidence, _dumps(structure.quality.model_dump(mode='json')), structure.catalog_version, structure.catalog_updated_at, structure.source_content_hash, structure.catalog_schema_version, structure.catalog_model, structure.error, _dumps(structure.warnings), structure.created_at, structure.updated_at, _dumps(structure.metadata)))

    @staticmethod
    def _insert_chapters(conn: sqlite3.Connection, chapters: list[SourceChapter]) -> None:
        conn.executemany('\n            INSERT INTO source_chapters(\n                id, owner_user_id, package_id, source_ingestion_id, parent_id, number,\n                normalized_number, title, level, path_json, order_index, source_locator,\n                body_start_offset, body_end_offset, page_start, page_end, anchor_status,\n                range_json, mapping_status, source_content_hash, catalog_evidence_json,\n                catalog_version, confidence, excerpt, metadata_json\n            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n            ', [(chapter.id, chapter.owner_user_id, chapter.package_id, chapter.source_ingestion_id, chapter.parent_id, chapter.number, chapter.normalized_number, chapter.title, chapter.level, _dumps(chapter.path), chapter.order_index, chapter.source_locator, chapter.body_start_offset, chapter.body_end_offset, chapter.page_start, chapter.page_end, chapter.anchor_status, _dumps(chapter.range.model_dump(mode='json')) if chapter.range else None, chapter.mapping_status, chapter.source_content_hash, _dumps([item.model_dump(mode='json') for item in chapter.catalog_evidence]), chapter.catalog_version, chapter.confidence, chapter.excerpt, _dumps(chapter.metadata)) for chapter in chapters])

    @staticmethod
    def _upsert_catalog_run(conn: sqlite3.Connection, run: SourceCatalogRun) -> None:
        conn.execute('\n            INSERT INTO source_catalog_runs(\n                id, owner_user_id, package_id, source_ingestion_id, status, catalog_version,\n                model, turn_count, page_count, inspected_page_count, ocr_page_count,\n                chapter_count, verified_chapter_count, verification_rate, duration_ms,\n                stage_history_json, error, created_at, updated_at, completed_at, metadata_json\n            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n            ON CONFLICT(id) DO UPDATE SET\n                status = excluded.status,\n                catalog_version = excluded.catalog_version,\n                model = excluded.model,\n                turn_count = excluded.turn_count,\n                page_count = excluded.page_count,\n                inspected_page_count = excluded.inspected_page_count,\n                ocr_page_count = excluded.ocr_page_count,\n                chapter_count = excluded.chapter_count,\n                verified_chapter_count = excluded.verified_chapter_count,\n                verification_rate = excluded.verification_rate,\n                duration_ms = excluded.duration_ms,\n                stage_history_json = excluded.stage_history_json,\n                error = excluded.error,\n                updated_at = excluded.updated_at,\n                completed_at = excluded.completed_at,\n                metadata_json = excluded.metadata_json\n            ', (run.id, run.owner_user_id, run.package_id, run.source_ingestion_id, run.status, run.catalog_version, run.model, run.turn_count, run.page_count, run.inspected_page_count, run.ocr_page_count, run.chapter_count, run.verified_chapter_count, run.verification_rate, run.duration_ms, _dumps(run.stage_history), run.error, run.created_at, run.updated_at, run.completed_at, _dumps(run.metadata)))

    def _delete_index_rows(self, conn: sqlite3.Connection, structure: SourceStructure) -> None:
        self.native_index.delete_for_source(conn, owner_user_id=structure.owner_user_id, package_id=structure.package_id, source_ingestion_id=structure.source_ingestion_id)
        conn.execute('\n            DELETE FROM source_visual_assets\n            WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n            ', (structure.owner_user_id, structure.package_id, structure.source_ingestion_id))
        conn.execute('\n            DELETE FROM source_chunks\n            WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n            ', (structure.owner_user_id, structure.package_id, structure.source_ingestion_id))
        conn.execute('\n            DELETE FROM source_chapters\n            WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n            ', (structure.owner_user_id, structure.package_id, structure.source_ingestion_id))

    def _structure_from_row(self, row: sqlite3.Row) -> SourceStructure:
        return SourceStructure(id=row['id'], owner_user_id=row['owner_user_id'], package_id=row['package_id'], source_ingestion_id=row['source_ingestion_id'], status=row['status'], strategy=row['strategy'], has_verified_toc=bool(row['has_verified_toc']), chapter_count=row['chapter_count'], chunk_count=row['chunk_count'], visual_count=row['visual_count'], visual_index_status=row['visual_index_status'], visual_index_version=row['visual_index_version'], confidence=row['confidence'], quality=SourceStructureQuality.model_validate(_loads(row['quality_json'], {})), catalog_version=row['catalog_version'], catalog_updated_at=row['catalog_updated_at'], source_content_hash=row['source_content_hash'], catalog_schema_version=row['catalog_schema_version'], catalog_model=row['catalog_model'], error=row['error'], warnings=_loads(row['warnings_json'], []), created_at=row['created_at'], updated_at=row['updated_at'], metadata=_loads(row['metadata_json'], {}))

    def _chapter_from_row(self, row: sqlite3.Row) -> SourceChapter:
        return SourceChapter(id=row['id'], owner_user_id=row['owner_user_id'], package_id=row['package_id'], source_ingestion_id=row['source_ingestion_id'], parent_id=row['parent_id'], number=row['number'], normalized_number=row['normalized_number'], title=row['title'], level=row['level'], path=_loads(row['path_json'], []), order_index=row['order_index'], source_locator=row['source_locator'], body_start_offset=row['body_start_offset'], body_end_offset=row['body_end_offset'], page_start=row['page_start'], page_end=row['page_end'], anchor_status=row['anchor_status'], range=SourceRange.model_validate(_loads(row['range_json'], {})) if row['range_json'] else None, mapping_status=row['mapping_status'], source_content_hash=row['source_content_hash'], catalog_evidence=_loads(row['catalog_evidence_json'], []), catalog_version=row['catalog_version'], confidence=row['confidence'], excerpt=row['excerpt'], metadata=_loads(row['metadata_json'], {}))

    def _catalog_run_from_row(self, row: sqlite3.Row) -> SourceCatalogRun:
        return SourceCatalogRun(id=row['id'], owner_user_id=row['owner_user_id'], package_id=row['package_id'], source_ingestion_id=row['source_ingestion_id'], status=row['status'], catalog_version=row['catalog_version'], model=row['model'], turn_count=row['turn_count'], page_count=row['page_count'], inspected_page_count=row['inspected_page_count'], ocr_page_count=row['ocr_page_count'], chapter_count=row['chapter_count'], verified_chapter_count=row['verified_chapter_count'], verification_rate=row['verification_rate'], duration_ms=row['duration_ms'], stage_history=_loads(row['stage_history_json'], []), error=row['error'], created_at=row['created_at'], updated_at=row['updated_at'], completed_at=row['completed_at'], metadata=_loads(row['metadata_json'], {}))

    def _chunk_from_row(self, row: sqlite3.Row) -> SourceChunk:
        return SourceChunk(id=row['id'], owner_user_id=row['owner_user_id'], package_id=row['package_id'], source_ingestion_id=row['source_ingestion_id'], chapter_id=row['chapter_id'], order_index=row['order_index'], source_locator=row['source_locator'], text=row['text'], start_offset=row['start_offset'], end_offset=row['end_offset'], page_start=row['page_start'], page_end=row['page_end'], token_count=row['token_count'], metadata=_loads(row['metadata_json'], {}))

    def _visual_from_row(self, row: sqlite3.Row) -> SourceVisualAsset:
        return SourceVisualAsset(id=row['id'], owner_user_id=row['owner_user_id'], package_id=row['package_id'], source_ingestion_id=row['source_ingestion_id'], structure_id=row['structure_id'], structure_version=row['structure_version'], chapter_id=row['chapter_id'], kind=row['kind'], source_locator=row['source_locator'], page_start=row['page_start'], page_end=row['page_end'], paragraph_index=row['paragraph_index'], slide_no=row['slide_no'], sheet_name=row['sheet_name'], bbox=_loads(row['bbox_json'], []), before_chunk_id=row['before_chunk_id'], after_chunk_id=row['after_chunk_id'], caption=row['caption'], extracted_text=row['extracted_text'], surrounding_text=row['surrounding_text'], anchor_status=row['anchor_status'], mime_type=row['mime_type'], asset_path=row['asset_path'], storage_key=row['storage_key'], order_index=row['order_index'], content_hash=row['content_hash'], position_hash=row['position_hash'], width=row['width'], height=row['height'], table_data=_loads(row['table_data_json'], []), confidence=row['confidence'], created_at=row['created_at'] or now_iso(), metadata=_loads(row['metadata_json'], {}))

def _compact_text(text: str, limit: int) -> str:
    compacted = ' '.join(text.split())
    return compacted if len(compacted) <= limit else compacted[:limit - 1].rstrip() + '…'

def _chunk_with_text_hash(chunk: SourceChunk) -> SourceChunk:
    text_hash = source_chunk_text_hash(chunk.text)
    if chunk.metadata.get('text_hash') == text_hash:
        return chunk
    return chunk.model_copy(update={'metadata': {**chunk.metadata, 'text_hash': text_hash}})

def _native_index_reason(search_mode: NativeSearchMode) -> str:
    if search_mode == 'text':
        return 'Matched the OpenClass native full-text index.'
    if search_mode == 'semantic':
        return 'Matched the OpenClass native semantic index.'
    return 'Matched the OpenClass native hybrid full-text and semantic index.'

def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)

def _chapter_page_range(chapter: SourceChapter) -> str:
    if chapter.page_start is None:
        return ''
    display_end = max(chapter.page_start, (chapter.page_end or chapter.page_start + 1) - 1)
    if display_end == chapter.page_start:
        return f'p. {chapter.page_start}'
    return f'pp. {chapter.page_start}-{display_end}'

def _chunk_page_range(chunk: SourceChunk) -> str:
    if chunk.page_start is None:
        return ''
    display_end = max(chunk.page_start, (chunk.page_end or chunk.page_start + 1) - 1)
    if display_end == chunk.page_start:
        return f'p. {chunk.page_start}'
    return f'pp. {chunk.page_start}-{display_end}'

def _chunk_text_for_chapter(chunk: SourceChunk, chapter: SourceChapter) -> str:
    if chapter.body_start_offset is None or chapter.body_end_offset is None:
        return chunk.text.strip()
    overlap_start = max(chunk.start_offset, chapter.body_start_offset)
    overlap_end = min(chunk.end_offset, chapter.body_end_offset)
    if overlap_end <= overlap_start:
        return ''
    start_index = max(0, overlap_start - chunk.start_offset)
    end_index = max(start_index, overlap_end - chunk.start_offset)
    return chunk.text[start_index:end_index].strip()

def _visual_in_page_range(asset: SourceVisualAsset, *, page_start: int | None, page_end: int | None) -> bool:
    if page_start is None and page_end is None:
        return True
    if asset.page_start is None:
        return False
    asset_end = asset.page_end if asset.page_end is not None else asset.page_start
    if page_start is not None and page_end is not None:
        return asset.page_start < page_end and asset_end >= page_start
    if page_start is not None:
        return asset.page_start <= page_start <= asset_end
    assert page_end is not None
    return asset.page_start < page_end

def _is_sha256_hex(value: str) -> bool:
    if len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True

def _read_legacy_visual_path(raw_path: str, *, source_id: str) -> bytes:
    """Read only paths produced by the pre-content-addressed visual indexer."""
    if not raw_path:
        raise SourceVisualStorageError('Legacy source visual path is empty.')
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = workspace_state.UPLOAD_DIR / candidate
    if candidate.is_symlink():
        raise SourceVisualStorageError('Legacy source visual path is a symbolic link.')
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SourceVisualStorageError('Legacy source visual asset is unavailable.') from exc
    upload_root = workspace_state.UPLOAD_DIR.resolve()
    try:
        upload_relative = resolved.relative_to(upload_root)
    except ValueError:
        upload_relative = None
    inside_upload_visuals = bool(upload_relative is not None and len(upload_relative.parts) >= 3 and (upload_relative.parts[0] == 'source-visuals') and (upload_relative.parts[1] == source_id))
    parts = resolved.parts
    inside_external_visuals = any((part == '.openclass-source-visuals' and index + 3 < len(parts) and (parts[index + 1] == 'source-visuals') and (parts[index + 2] == source_id) for index, part in enumerate(parts)))
    if not inside_upload_visuals and (not inside_external_visuals):
        raise SourceVisualStorageError('Legacy source visual path is outside its asset directory.')
    file_stat = resolved.stat()
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size <= 0 or file_stat.st_size > MAX_SOURCE_VISUAL_BYTES:
        raise SourceVisualStorageError('Legacy source visual asset has an invalid size or type.')
    with resolved.open('rb') as handle:
        content = handle.read(MAX_SOURCE_VISUAL_BYTES + 1)
    if len(content) != file_stat.st_size or len(content) > MAX_SOURCE_VISUAL_BYTES:
        raise SourceVisualStorageError('Legacy source visual asset changed while reading.')
    return content

def _remove_asset_files(paths) -> None:
    parents: set[Path] = set()
    for raw_path in paths:
        path = Path(str(raw_path))
        try:
            if path.is_file():
                path.unlink()
            parents.add(path.parent)
        except OSError:
            continue
    for parent in sorted(parents, key=lambda item: len(item.parts), reverse=True):
        try:
            parent.rmdir()
        except OSError:
            pass
source_structure_store = SourceStructureStore()
