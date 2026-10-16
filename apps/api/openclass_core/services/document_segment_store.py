from __future__ import annotations
import json
import sqlite3
from typing import Any, Callable
from openclass_core.models import BoardDocument, BoardSegmentKind, DocumentSegmentSearchResult
from openclass_core.services.board_segment_index import build_board_segment_index
DocumentFromRow = Callable[[sqlite3.Row, str], BoardDocument]

class DocumentSegmentStore:

    def __init__(self) -> None:
        self.fts_available = False

    def create_fts_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute('\n            CREATE TABLE IF NOT EXISTS board_document_chunks (\n                lesson_id TEXT NOT NULL,\n                document_id TEXT NOT NULL,\n                chunk_id TEXT NOT NULL,\n                order_start INTEGER NOT NULL,\n                order_end INTEGER NOT NULL,\n                heading_path_json TEXT NOT NULL,\n                source_segment_ids_json TEXT NOT NULL,\n                text TEXT NOT NULL,\n                text_hash TEXT NOT NULL,\n                PRIMARY KEY (lesson_id, chunk_id)\n            )\n            ')
        conn.execute('\n            CREATE INDEX IF NOT EXISTS idx_board_document_chunks_lesson\n                ON board_document_chunks(lesson_id, order_start, order_end)\n            ')
        try:
            conn.execute("\n                CREATE VIRTUAL TABLE IF NOT EXISTS board_document_segments_fts USING fts5(\n                    lesson_id UNINDEXED,\n                    document_id UNINDEXED,\n                    segment_id UNINDEXED,\n                    kind UNINDEXED,\n                    heading_path,\n                    text,\n                    tokenize = 'unicode61'\n                )\n                ")
            conn.execute("\n                CREATE VIRTUAL TABLE IF NOT EXISTS board_document_chunks_fts USING fts5(\n                    lesson_id UNINDEXED,\n                    document_id UNINDEXED,\n                    chunk_id UNINDEXED,\n                    heading_path,\n                    text,\n                    tokenize = 'unicode61'\n                )\n                ")
        except sqlite3.OperationalError:
            self.fts_available = False
        else:
            self.fts_available = True

    def backfill(self, conn: sqlite3.Connection, document_from_row: DocumentFromRow) -> None:
        segment_count = conn.execute('SELECT count(*) FROM board_document_segments').fetchone()[0]
        chunk_count = conn.execute('SELECT count(*) FROM board_document_chunks').fetchone()[0]
        if segment_count and chunk_count:
            return
        lesson_rows = conn.execute('SELECT * FROM lessons').fetchall()
        for row in lesson_rows:
            self.replace_segments(conn, row['id'], document_from_row(row, 'board'))

    def replace_segments(self, conn: sqlite3.Connection, lesson_id: str, document: BoardDocument) -> None:
        conn.execute('DELETE FROM board_document_segments WHERE lesson_id = ?', (lesson_id,))
        conn.execute('DELETE FROM board_document_chunks WHERE lesson_id = ?', (lesson_id,))
        self.delete_for_lesson(conn, lesson_id)
        segment_index = build_board_segment_index(document)
        for segment in segment_index.segments:
            conn.execute('\n                INSERT INTO board_document_segments(\n                    lesson_id, document_id, segment_id, kind, order_index,\n                    heading_path_json, text, text_hash, parent_id, before_segment_id, after_segment_id\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                ', (lesson_id, segment.document_id, segment.segment_id, segment.kind, segment.order_index, _dumps(segment.heading_path), segment.text, segment.text_hash, segment.parent_id, segment.before_segment_id, segment.after_segment_id))
            self._insert_fts(conn, lesson_id, segment.document_id, segment.segment_id, segment.kind, segment.heading_path, segment.text)
        for chunk in segment_index.chunks:
            conn.execute('\n                INSERT INTO board_document_chunks(\n                    lesson_id, document_id, chunk_id, order_start, order_end,\n                    heading_path_json, source_segment_ids_json, text, text_hash\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)\n                ', (lesson_id, chunk.document_id, chunk.chunk_id, chunk.order_start, chunk.order_end, _dumps(chunk.heading_path), _dumps(chunk.source_segment_ids), chunk.text, chunk.text_hash))
            self._insert_chunk_fts(conn, lesson_id, chunk.document_id, chunk.chunk_id, chunk.heading_path, chunk.text)

    def delete_for_owner(self, conn: sqlite3.Connection, owner_user_id: str | None) -> None:
        if owner_user_id is None:
            lesson_ids = [row['id'] for row in conn.execute('SELECT id FROM lessons').fetchall()]
        else:
            lesson_ids = [row['id'] for row in conn.execute('\n                    SELECT lessons.id\n                    FROM lessons\n                    JOIN course_packages ON course_packages.id = lessons.package_id\n                    WHERE course_packages.owner_user_id = ?\n                    ', (owner_user_id,)).fetchall()]
        for lesson_id in lesson_ids:
            conn.execute('DELETE FROM board_document_chunks WHERE lesson_id = ?', (lesson_id,))
            self.delete_for_lesson(conn, lesson_id)

    def delete_for_lesson(self, conn: sqlite3.Connection, lesson_id: str) -> None:
        if not self.fts_available:
            return
        try:
            conn.execute('DELETE FROM board_document_segments_fts WHERE lesson_id = ?', (lesson_id,))
            conn.execute('DELETE FROM board_document_chunks_fts WHERE lesson_id = ?', (lesson_id,))
        except sqlite3.OperationalError:
            self.fts_available = False

    def search(self, conn: sqlite3.Connection, query: str='', *, owner_user_id: str | None=None, kind: BoardSegmentKind | None=None, limit: int=20) -> list[DocumentSegmentSearchResult]:
        normalized_query = query.strip()
        normalized_limit = max(1, min(limit, 100))
        rows: list[sqlite3.Row] = []
        if normalized_query and self.fts_available:
            rows = self._search_fts(conn, normalized_query, owner_user_id=owner_user_id, kind=kind, limit=normalized_limit)
        if not rows:
            rows = self._search_like(conn, normalized_query, owner_user_id=owner_user_id, kind=kind, limit=normalized_limit)
        return [_segment_result_from_row(row) for row in rows]

    def _insert_fts(self, conn: sqlite3.Connection, lesson_id: str, document_id: str, segment_id: str, kind: str, heading_path: list[str], text: str) -> None:
        if not self.fts_available:
            return
        try:
            conn.execute('\n                INSERT INTO board_document_segments_fts(\n                    lesson_id, document_id, segment_id, kind, heading_path, text\n                ) VALUES (?, ?, ?, ?, ?, ?)\n                ', (lesson_id, document_id, segment_id, kind, ' / '.join(heading_path), text))
        except sqlite3.OperationalError:
            self.fts_available = False

    def _insert_chunk_fts(self, conn: sqlite3.Connection, lesson_id: str, document_id: str, chunk_id: str, heading_path: list[str], text: str) -> None:
        if not self.fts_available:
            return
        try:
            conn.execute('\n                INSERT INTO board_document_chunks_fts(\n                    lesson_id, document_id, chunk_id, heading_path, text\n                ) VALUES (?, ?, ?, ?, ?)\n                ', (lesson_id, document_id, chunk_id, ' / '.join(heading_path), text))
        except sqlite3.OperationalError:
            self.fts_available = False

    def _search_fts(self, conn: sqlite3.Connection, query: str, *, owner_user_id: str | None, kind: BoardSegmentKind | None, limit: int) -> list[sqlite3.Row]:
        conditions = ['board_document_segments_fts MATCH ?']
        params: list[Any] = [_fts_phrase(query)]
        if owner_user_id is not None:
            conditions.append('course_packages.owner_user_id = ?')
            params.append(owner_user_id)
        if kind is not None:
            conditions.append('segments.kind = ?')
            params.append(kind)
        params.append(limit)
        try:
            return conn.execute(f"\n                SELECT\n                    course_packages.id AS package_id,\n                    course_packages.title AS package_title,\n                    lessons.id AS lesson_id,\n                    lessons.title AS lesson_title,\n                    segments.document_id AS document_id,\n                    lessons.board_document_title AS document_title,\n                    segments.segment_id AS segment_id,\n                    segments.kind AS kind,\n                    segments.order_index AS order_index,\n                    segments.heading_path_json AS heading_path_json,\n                    segments.text AS text,\n                    segments.text_hash AS text_hash\n                FROM board_document_segments_fts\n                JOIN board_document_segments AS segments\n                    ON segments.lesson_id = board_document_segments_fts.lesson_id\n                    AND segments.segment_id = board_document_segments_fts.segment_id\n                JOIN lessons ON lessons.id = segments.lesson_id\n                JOIN course_packages ON course_packages.id = lessons.package_id\n                WHERE {' AND '.join(conditions)}\n                ORDER BY\n                    bm25(board_document_segments_fts),\n                    course_packages.sort_order,\n                    lessons.sort_order,\n                    segments.order_index\n                LIMIT ?\n                ", params).fetchall()
        except sqlite3.OperationalError:
            self.fts_available = False
            return []

    def _search_like(self, conn: sqlite3.Connection, query: str, *, owner_user_id: str | None, kind: BoardSegmentKind | None, limit: int) -> list[sqlite3.Row]:
        conditions: list[str] = []
        params: list[Any] = []
        if query:
            conditions.append("segments.text LIKE ? ESCAPE '\\'")
            params.append(f'%{_escape_like(query)}%')
        if owner_user_id is not None:
            conditions.append('course_packages.owner_user_id = ?')
            params.append(owner_user_id)
        if kind is not None:
            conditions.append('segments.kind = ?')
            params.append(kind)
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ''
        params.append(limit)
        return conn.execute(f'\n            SELECT\n                course_packages.id AS package_id,\n                course_packages.title AS package_title,\n                lessons.id AS lesson_id,\n                lessons.title AS lesson_title,\n                segments.document_id AS document_id,\n                lessons.board_document_title AS document_title,\n                segments.segment_id AS segment_id,\n                segments.kind AS kind,\n                segments.order_index AS order_index,\n                segments.heading_path_json AS heading_path_json,\n                segments.text AS text,\n                segments.text_hash AS text_hash\n            FROM board_document_segments AS segments\n            JOIN lessons ON lessons.id = segments.lesson_id\n            JOIN course_packages ON course_packages.id = lessons.package_id\n            {where_clause}\n            ORDER BY course_packages.sort_order, lessons.sort_order, segments.order_index\n            LIMIT ?\n            ', params).fetchall()

def _segment_result_from_row(row: sqlite3.Row) -> DocumentSegmentSearchResult:
    return DocumentSegmentSearchResult(package_id=row['package_id'], package_title=row['package_title'], lesson_id=row['lesson_id'], lesson_title=row['lesson_title'], document_id=row['document_id'], document_title=row['document_title'], segment_id=row['segment_id'], kind=row['kind'], heading_path=_loads(row['heading_path_json'], []), order_index=row['order_index'], text=row['text'], text_hash=row['text_hash'])

def _fts_phrase(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) + chr(34))}"'

def _escape_like(value: str) -> str:
    return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

def _loads(raw: str | None, default: Any) -> Any:
    if raw is None or raw == '':
        return default
    return json.loads(raw)
