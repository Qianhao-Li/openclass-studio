from __future__ import annotations
import hashlib
import re
from typing import Any
from openclass_core.models import BoardChunk, BoardDocument, BoardSegment, BoardSegmentIndex, BoardSegmentKind
from openclass_core.services.rich_document import html_to_text
INDEXABLE_NODE_TYPES = {'heading', 'paragraph', 'bulletList', 'orderedList', 'listItem', 'table', 'codeBlock', 'image', 'blockMath'}

def build_board_segment_index(document: BoardDocument) -> BoardSegmentIndex:
    content = document.content_json if isinstance(document.content_json, dict) else {}
    raw_nodes = content.get('content') if isinstance(content, dict) else None
    segments: list[BoardSegment] = []
    heading_path: list[str] = []
    if isinstance(raw_nodes, list):
        for node in raw_nodes:
            if isinstance(node, dict):
                _collect_segments(node, document=document, heading_path=heading_path, segments=segments)
    if not segments:
        segments = _segments_from_plain_text(document)
    for index, segment in enumerate(segments):
        segments[index] = segment.model_copy(update={'before_segment_id': segments[index - 1].segment_id if index > 0 else None, 'after_segment_id': segments[index + 1].segment_id if index + 1 < len(segments) else None})
    return BoardSegmentIndex(document_id=document.id, document_title=document.title, segments=segments, chunks=build_board_chunks(segments))

def build_board_chunks(segments: list[BoardSegment], *, max_chars: int=1000, max_segments: int=6, overlap_segments: int=1) -> list[BoardChunk]:
    chunks: list[BoardChunk] = []
    current: list[BoardSegment] = []

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append(_make_chunk(current, len(chunks)))
            if overlap_segments > 0:
                current = [segment for segment in current[-overlap_segments:] if segment.kind != 'heading']
            else:
                current = []
    for segment in segments:
        if not segment.text.strip():
            continue
        if segment.kind == 'heading' and current:
            flush()
            current = []
        proposed_text = _chunk_text([*current, segment])
        if current and (len(proposed_text) > max_chars or len(current) >= max_segments):
            flush()
            if segment.kind == 'heading':
                current = []
        current.append(segment)
    if current:
        chunks.append(_make_chunk(current, len(chunks)))
    return chunks

def compact_segment_text(value: str, *, limit: int=1200) -> str:
    compact = re.sub('\\s+', ' ', value or '').strip()
    if len(compact) <= limit:
        return compact
    return f'{compact[:limit - 1]}...'

def segment_text_hash(value: str) -> str:
    return hashlib.sha256(compact_segment_text(value).encode('utf-8')).hexdigest()[:16]

def _make_chunk(source_segments: list[BoardSegment], chunk_index: int) -> BoardChunk:
    first = source_segments[0]
    last = source_segments[-1]
    text = _chunk_text(source_segments)
    text_hash = segment_text_hash(text)
    stable_seed = f'{first.document_id}:{first.order_index}:{last.order_index}:{text_hash}'
    return BoardChunk(chunk_id=f"chk_{hashlib.sha256(stable_seed.encode('utf-8')).hexdigest()[:16]}", document_id=first.document_id, source_segment_ids=[segment.segment_id for segment in source_segments], heading_path=last.heading_path or first.heading_path, order_start=first.order_index, order_end=last.order_index, text=text, text_hash=text_hash)

def _chunk_text(source_segments: list[BoardSegment]) -> str:
    lines: list[str] = []
    for segment in source_segments:
        heading = ' / '.join(segment.heading_path)
        label = f'[{segment.segment_id} | 第{segment.order_index + 1}段'
        if heading:
            label += f' | {heading}'
        label += ']'
        lines.append(f'{label}\n{segment.text}')
    return '\n'.join(lines)

def _collect_segments(node: dict[str, Any], *, document: BoardDocument, heading_path: list[str], segments: list[BoardSegment]) -> None:
    node_type = str(node.get('type') or '')
    if node_type == 'heading':
        text = compact_segment_text(_node_text(node), limit=800)
        level = _heading_level(node)
        if text:
            del heading_path[level - 1:]
            heading_path.append(text)
            _append_segment(document=document, segments=segments, kind='heading', text=text, heading_path=list(heading_path), attrs=node.get('attrs'))
        return
    if node_type in {'paragraph', 'codeBlock', 'image', 'blockMath'}:
        text = compact_segment_text(_node_text(node), limit=1200)
        if text or node_type == 'image':
            _append_segment(document=document, segments=segments, kind=_segment_kind(node_type), text=text or '图片内容', heading_path=list(heading_path), attrs=node.get('attrs'))
        return
    if node_type in {'bulletList', 'orderedList'}:
        children = node.get('content')
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    _collect_segments(child, document=document, heading_path=heading_path, segments=segments)
        return
    if node_type in {'listItem', 'table'}:
        text = compact_segment_text(_node_text(node), limit=1600)
        if text:
            _append_segment(document=document, segments=segments, kind=_segment_kind(node_type), text=text, heading_path=list(heading_path), attrs=node.get('attrs'))
        return
    children = node.get('content')
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                _collect_segments(child, document=document, heading_path=heading_path, segments=segments)

def _append_segment(*, document: BoardDocument, segments: list[BoardSegment], kind: BoardSegmentKind, text: str, heading_path: list[str], attrs: Any) -> None:
    order_index = len(segments)
    text_hash = segment_text_hash(text)
    explicit_id = attrs.get('id') if isinstance(attrs, dict) else None
    stable_seed = f'{document.id}:{order_index}:{kind}:{text_hash}'
    segment_id = str(explicit_id).strip() if explicit_id else f"seg_{hashlib.sha256(stable_seed.encode('utf-8')).hexdigest()[:16]}"
    segments.append(BoardSegment(segment_id=segment_id, document_id=document.id, kind=kind, heading_path=heading_path, order_index=order_index, text=text, html='', text_hash=text_hash))

def _node_text(node: dict[str, Any]) -> str:
    node_type = node.get('type')
    if node_type in {'inlineMath', 'blockMath'}:
        attrs = node.get('attrs')
        latex = attrs.get('latex') if isinstance(attrs, dict) else ''
        return str(latex or '').strip()
    text = node.get('text')
    if isinstance(text, str):
        return text
    children = node.get('content')
    if not isinstance(children, list):
        return ''
    parts: list[str] = []
    for child in children:
        if isinstance(child, dict):
            child_text = _node_text(child)
            if child_text:
                parts.append(child_text)
    return ' '.join(parts)

def _heading_level(node: dict[str, Any]) -> int:
    attrs = node.get('attrs')
    level = attrs.get('level') if isinstance(attrs, dict) else 1
    try:
        return max(1, min(int(level), 6))
    except (TypeError, ValueError):
        return 1

def _segment_kind(node_type: str) -> BoardSegmentKind:
    if node_type in {'bulletList', 'orderedList', 'listItem'}:
        return 'list'
    if node_type == 'codeBlock':
        return 'code'
    if node_type == 'table':
        return 'table'
    if node_type == 'image':
        return 'image'
    if node_type in {'inlineMath', 'blockMath'}:
        return 'formula'
    if node_type in INDEXABLE_NODE_TYPES:
        return node_type
    return 'other'

def _segments_from_plain_text(document: BoardDocument) -> list[BoardSegment]:
    text = document.content_text or html_to_text(document.content_html)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    segments: list[BoardSegment] = []
    heading_path: list[str] = []
    for line in lines:
        marker = re.match('^(#{1,6})\\s+(.+)$', line)
        kind: BoardSegmentKind = 'heading' if marker else 'paragraph'
        segment_text = marker.group(2).strip() if marker else line
        if kind == 'heading':
            level = len(marker.group(1))
            heading_path[level - 1:] = [segment_text]
        _append_segment(document=document, segments=segments, kind=kind, text=segment_text, heading_path=list(heading_path), attrs=None)
    return segments
