from __future__ import annotations
import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Literal, Mapping, Protocol, Sequence
from openclass_core.models import SourceChunk, now_iso
NativeSearchMode = Literal['text', 'semantic', 'hybrid']

class SourceEmbeddingProvider(Protocol):
    """Provider boundary for deterministic or model-backed source embeddings."""
    provider: str
    model: str
    dimensions: int

    def embed(self, text: str) -> list[float]:
        ...

class DeterministicHashEmbeddingProvider:
    """Dependency-free multilingual feature hashing for the native local index."""
    provider = 'openclass_local'
    model = 'feature-hash-v1'

    def __init__(self, *, dimensions: int=256) -> None:
        if dimensions < 32:
            raise ValueError('Embedding dimensions must be at least 32.')
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for feature, weight in _embedding_features(text):
            digest = hashlib.blake2b(feature.encode('utf-8'), digest_size=9).digest()
            index = int.from_bytes(digest[:8], 'big') % self.dimensions
            sign = -1.0 if digest[8] & 1 else 1.0
            vector[index] += sign * weight
        norm = math.sqrt(sum((value * value for value in vector)))
        if norm == 0:
            return vector
        return [round(value / norm, 8) for value in vector]

@dataclass(frozen=True)
class NativeChunkMatch:
    chunk_id: str
    keyword_score: float
    semantic_score: float
    hybrid_score: float
    match_modes: tuple[str, ...]

class NativeSourceIndex:

    def __init__(self, *, embedding_provider: SourceEmbeddingProvider | None=None) -> None:
        self.embedding_provider = embedding_provider or DeterministicHashEmbeddingProvider()
        self.fts_available = False

    def create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript('\n            CREATE TABLE IF NOT EXISTS source_chunk_embeddings (\n                chunk_id TEXT NOT NULL REFERENCES source_chunks(id) ON DELETE CASCADE,\n                owner_user_id TEXT NOT NULL,\n                package_id TEXT NOT NULL,\n                source_ingestion_id TEXT NOT NULL,\n                provider TEXT NOT NULL,\n                model TEXT NOT NULL,\n                dimensions INTEGER NOT NULL,\n                text_hash TEXT NOT NULL,\n                embedding_json TEXT NOT NULL,\n                updated_at TEXT NOT NULL,\n                PRIMARY KEY (chunk_id, provider, model)\n            );\n            CREATE INDEX IF NOT EXISTS idx_source_chunk_embeddings_scope\n                ON source_chunk_embeddings(owner_user_id, package_id, source_ingestion_id, provider, model);\n            CREATE INDEX IF NOT EXISTS idx_source_chunk_embeddings_hash\n                ON source_chunk_embeddings(text_hash, provider, model);\n            ')
        try:
            conn.execute("\n                CREATE VIRTUAL TABLE IF NOT EXISTS source_chunks_fts USING fts5(\n                    chunk_id UNINDEXED,\n                    owner_user_id UNINDEXED,\n                    package_id UNINDEXED,\n                    source_ingestion_id UNINDEXED,\n                    chapter_id UNINDEXED,\n                    text,\n                    tokenize = 'unicode61 remove_diacritics 2'\n                )\n                ")
        except sqlite3.OperationalError:
            self.fts_available = False
        else:
            self.fts_available = True

    def backfill(self, conn: sqlite3.Connection) -> None:
        """Populate new index tables for databases created before the native index."""
        rows = conn.execute('SELECT * FROM source_chunks ORDER BY rowid').fetchall()
        existing_embeddings = {str(row['chunk_id']): str(row['text_hash']) for row in conn.execute('\n                SELECT chunk_id, text_hash\n                FROM source_chunk_embeddings\n                WHERE provider = ? AND model = ?\n                ', (self.embedding_provider.provider, self.embedding_provider.model)).fetchall()}
        fts_needs_rebuild = False
        if self.fts_available:
            fts_count = int(conn.execute('SELECT COUNT(*) FROM source_chunks_fts').fetchone()[0])
            fts_needs_rebuild = fts_count != len(rows)
        with conn:
            conn.execute('\n                DELETE FROM source_chunk_embeddings\n                WHERE NOT EXISTS (\n                    SELECT 1 FROM source_chunks WHERE source_chunks.id = source_chunk_embeddings.chunk_id\n                )\n                ')
            if fts_needs_rebuild:
                conn.execute('DELETE FROM source_chunks_fts')
            for row in rows:
                text = str(row['text'] or '')
                text_hash = source_chunk_text_hash(text)
                metadata = _load_json_object(row['metadata_json'])
                if metadata.get('text_hash') != text_hash or str(row['text_hash'] or '') != text_hash:
                    metadata['text_hash'] = text_hash
                    conn.execute('UPDATE source_chunks SET text_hash = ?, metadata_json = ? WHERE id = ?', (text_hash, _dump_json(metadata), row['id']))
                self._index_values(conn, chunk_id=str(row['id']), owner_user_id=str(row['owner_user_id']), package_id=str(row['package_id']), source_ingestion_id=str(row['source_ingestion_id']), chapter_id=str(row['chapter_id'] or ''), text=text, text_hash=text_hash, write_fts=fts_needs_rebuild, write_embedding=existing_embeddings.get(str(row['id'])) != text_hash)

    def index_chunks(self, conn: sqlite3.Connection, chunks: Sequence[SourceChunk]) -> None:
        for chunk in chunks:
            self._index_values(conn, chunk_id=chunk.id, owner_user_id=chunk.owner_user_id, package_id=chunk.package_id, source_ingestion_id=chunk.source_ingestion_id, chapter_id=chunk.chapter_id or '', text=chunk.text, text_hash=str(chunk.metadata.get('text_hash') or source_chunk_text_hash(chunk.text)))

    def delete_for_source(self, conn: sqlite3.Connection, *, owner_user_id: str, package_id: str, source_ingestion_id: str) -> None:
        conn.execute('\n            DELETE FROM source_chunk_embeddings\n            WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n            ', (owner_user_id, package_id, source_ingestion_id))
        if self.fts_available:
            conn.execute('\n                DELETE FROM source_chunks_fts\n                WHERE owner_user_id = ? AND package_id = ? AND source_ingestion_id = ?\n                ', (owner_user_id, package_id, source_ingestion_id))

    def search(self, conn: sqlite3.Connection, *, owner_user_id: str, package_id: str, query: str, source_ingestion_ids: Sequence[str], limit: int, search_mode: NativeSearchMode='hybrid', chapter_ids: Sequence[str]=(), page_ranges: Mapping[str, tuple[int, int]] | None=None) -> list[NativeChunkMatch]:
        if search_mode not in {'text', 'semantic', 'hybrid'}:
            raise ValueError(f'Unsupported native source search mode: {search_mode}')
        source_ids = tuple(dict.fromkeys((value for value in source_ingestion_ids if value)))
        if not query.strip() or not source_ids or limit <= 0:
            return []
        placeholders = ', '.join(('?' for _ in source_ids))
        scope_params: list[object] = [owner_user_id, package_id, *source_ids]
        scope_clauses: list[str] = []
        normalized_chapter_ids = tuple(dict.fromkeys((value for value in chapter_ids if value)))
        if normalized_chapter_ids:
            chapter_placeholders = ', '.join(('?' for _ in normalized_chapter_ids))
            scope_clauses.append(f'source_chunks.chapter_id IN ({chapter_placeholders})')
            scope_params.extend(normalized_chapter_ids)
        normalized_page_ranges = {source_id: page_range for source_id, page_range in (page_ranges or {}).items() if source_id in source_ids and page_range[0] >= 1 and (page_range[1] >= page_range[0])}
        if normalized_page_ranges:
            page_clauses: list[str] = []
            for source_id, (page_start, page_end) in normalized_page_ranges.items():
                page_clauses.append('(source_chunks.source_ingestion_id = ? AND source_chunks.page_start IS NOT NULL AND source_chunks.page_start <= ? AND COALESCE(source_chunks.page_end, source_chunks.page_start) >= ?)')
                scope_params.extend((source_id, page_end, page_start))
            scope_clauses.append('(' + ' OR '.join(page_clauses) + ')')
        extra_scope_sql = '' if not scope_clauses else ' AND ' + ' AND '.join(scope_clauses)
        rows = conn.execute(f'\n            SELECT source_chunks.id AS chunk_id, source_chunks.text,\n                source_chunk_embeddings.embedding_json\n            FROM source_chunks\n            JOIN source_chunk_embeddings\n                ON source_chunk_embeddings.chunk_id = source_chunks.id\n                AND source_chunk_embeddings.provider = ?\n                AND source_chunk_embeddings.model = ?\n            WHERE source_chunks.owner_user_id = ?\n                AND source_chunks.package_id = ?\n                AND source_chunks.source_ingestion_id IN ({placeholders})\n                {extra_scope_sql}\n            ', [self.embedding_provider.provider, self.embedding_provider.model, *scope_params]).fetchall()
        if not rows:
            return []
        query_embedding = self.embedding_provider.embed(query) if search_mode != 'text' else []
        terms = _search_terms(query) if search_mode != 'semantic' else []
        semantic_scores: dict[str, float] = {}
        lexical_scores: dict[str, float] = {}
        for row in rows:
            chunk_id = str(row['chunk_id'])
            if search_mode != 'text':
                vector = _load_vector(row['embedding_json'])
                semantic_scores[chunk_id] = max(0.0, cosine_similarity(query_embedding, vector))
            if search_mode != 'semantic':
                lexical_scores[chunk_id] = _lexical_score(str(row['text'] or ''), terms)
        fts_ranks = self._fts_ranks(conn, owner_user_id=owner_user_id, package_id=package_id, query=query, source_ingestion_ids=source_ids, limit=max(limit * 8, 32)) if search_mode != 'semantic' else {}
        max_lexical = max(lexical_scores.values(), default=0.0)
        valid_chunk_ids = {str(row['chunk_id']) for row in rows}
        candidate_ids = set(fts_ranks).intersection(valid_chunk_ids)
        top_semantic = sorted(semantic_scores.items(), key=lambda item: item[1], reverse=True)[:max(limit * 8, 32)]
        semantic_threshold = 0.0 if search_mode == 'semantic' else 0.12
        if search_mode != 'text':
            candidate_ids.update((chunk_id for chunk_id, score in top_semantic if score > semantic_threshold))
        if search_mode != 'semantic':
            candidate_ids.update((chunk_id for chunk_id, score in lexical_scores.items() if score > 0))
        matches: list[NativeChunkMatch] = []
        for chunk_id in candidate_ids:
            semantic_score = semantic_scores.get(chunk_id, 0.0)
            lexical_score = lexical_scores.get(chunk_id, 0.0)
            lexical_normalized = lexical_score / max_lexical if max_lexical else 0.0
            fts_position = fts_ranks.get(chunk_id)
            fts_normalized = 1.0 / fts_position if fts_position else 0.0
            keyword_score = max(lexical_normalized, fts_normalized)
            if search_mode == 'text':
                hybrid_score = keyword_score
            elif search_mode == 'semantic':
                hybrid_score = semantic_score
            else:
                hybrid_score = 0.55 * semantic_score + 0.45 * keyword_score
            modes: list[str] = []
            if search_mode != 'semantic' and fts_position:
                modes.append('fts5_bm25')
            if search_mode != 'semantic' and lexical_score > 0:
                modes.append('lexical')
            if search_mode != 'text' and semantic_score > semantic_threshold:
                modes.append('local_embedding')
            if not modes:
                continue
            matches.append(NativeChunkMatch(chunk_id=chunk_id, keyword_score=round(keyword_score, 6), semantic_score=round(semantic_score, 6), hybrid_score=round(hybrid_score, 6), match_modes=tuple(modes)))
        matches.sort(key=lambda item: (-item.hybrid_score, -item.keyword_score, item.chunk_id))
        return matches[:limit]

    def _index_values(self, conn: sqlite3.Connection, *, chunk_id: str, owner_user_id: str, package_id: str, source_ingestion_id: str, chapter_id: str, text: str, text_hash: str, write_fts: bool=True, write_embedding: bool=True) -> None:
        if self.fts_available and write_fts:
            conn.execute('\n                INSERT INTO source_chunks_fts(\n                    chunk_id, owner_user_id, package_id, source_ingestion_id, chapter_id, text\n                ) VALUES (?, ?, ?, ?, ?, ?)\n                ', (chunk_id, owner_user_id, package_id, source_ingestion_id, chapter_id, text))
        if not write_embedding:
            return
        embedding = self.embedding_provider.embed(text)
        conn.execute('\n            INSERT OR REPLACE INTO source_chunk_embeddings(\n                chunk_id, owner_user_id, package_id, source_ingestion_id, provider, model,\n                dimensions, text_hash, embedding_json, updated_at\n            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n            ', (chunk_id, owner_user_id, package_id, source_ingestion_id, self.embedding_provider.provider, self.embedding_provider.model, len(embedding), text_hash, _dump_json(embedding), now_iso()))

    def _fts_ranks(self, conn: sqlite3.Connection, *, owner_user_id: str, package_id: str, query: str, source_ingestion_ids: Sequence[str], limit: int) -> dict[str, int]:
        fts_query = _fts_query(query)
        if not self.fts_available or not fts_query:
            return {}
        placeholders = ', '.join(('?' for _ in source_ingestion_ids))
        try:
            rows = conn.execute(f'\n                SELECT chunk_id, bm25(source_chunks_fts) AS score\n                FROM source_chunks_fts\n                WHERE source_chunks_fts MATCH ?\n                    AND owner_user_id = ?\n                    AND package_id = ?\n                    AND source_ingestion_id IN ({placeholders})\n                ORDER BY score ASC, rowid ASC\n                LIMIT ?\n                ', [fts_query, owner_user_id, package_id, *source_ingestion_ids, limit]).fetchall()
        except sqlite3.OperationalError:
            return {}
        return {str(row['chunk_id']): index for index, row in enumerate(rows, start=1)}

def source_chunk_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum((a * b for a, b in zip(left, right, strict=False)))
    left_norm = math.sqrt(sum((value * value for value in left)))
    right_norm = math.sqrt(sum((value * value for value in right)))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)

def _embedding_features(text: str) -> list[tuple[str, float]]:
    normalized = unicodedata.normalize('NFKC', text).casefold()
    normalized = re.sub('\\s+', ' ', normalized).strip()[:12000]
    if not normalized:
        return []
    features: list[tuple[str, float]] = []
    words = re.findall('[a-z0-9]+(?:[._-][a-z0-9]+)*', normalized)
    features.extend(((f'word:{word}', 2.2) for word in words))
    features.extend(((f'pair:{left}|{right}', 1.4) for left, right in zip(words, words[1:], strict=False)))
    compact = ''.join((char for char in normalized if char.isalnum()))
    for width, weight in ((3, 1.0),):
        features.extend(((f'char{width}:{compact[index:index + width]}', weight) for index in range(max(0, len(compact) - width + 1))))
    return features

def _search_terms(query: str) -> list[str]:
    normalized = unicodedata.normalize('NFKC', query).casefold()
    terms = [token for token in re.findall('[a-z0-9]+(?:[._-][a-z0-9]+)*', normalized) if len(token) >= 2]
    for sequence in re.findall('[\\u4e00-\\u9fff]{2,}', normalized):
        terms.append(sequence)
        if len(sequence) > 4:
            terms.extend((sequence[index:index + 2] for index in range(len(sequence) - 1)))
    return list(dict.fromkeys(terms))

def _lexical_score(text: str, terms: Sequence[str]) -> float:
    lowered = unicodedata.normalize('NFKC', text).casefold()
    score = 0.0
    matched = 0
    for term in terms:
        count = lowered.count(term)
        if count:
            matched += 1
            score += min(count, 3) * (1.0 + min(len(term), 12) / 12)
    return score + matched / max(len(terms), 1) if matched else 0.0

def _fts_query(query: str) -> str:
    terms = _search_terms(query)
    if not terms:
        return ''
    escaped = [f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms[:32]]
    return ' OR '.join(escaped)

def _load_vector(raw: str | None) -> list[float]:
    try:
        value = json.loads(raw or '[]')
    except json.JSONDecodeError:
        return []
    return [float(item) for item in value] if isinstance(value, list) else []

def _load_json_object(raw: str | None) -> dict[str, object]:
    try:
        value = json.loads(raw or '{}')
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}

def _dump_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
