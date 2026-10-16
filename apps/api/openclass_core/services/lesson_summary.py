from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Any
from typing import Protocol
from openclass_core.models import BoardDocument

class SummarySource(Protocol):
    title: str
    summary: str
    board_document: BoardDocument
SUMMARY_MAX_CHARS = 180
_MIN_SUBSTANTIVE_CHARS = 16

def _normalize_text(value: str) -> str:
    return re.sub('\\s+', ' ', value.replace('\u200b', ' ')).strip()

def _node_text(node: Any) -> str:
    if not isinstance(node, dict):
        return ''
    if node.get('type') == 'hardBreak':
        return ' '
    raw_text = node.get('text')
    if isinstance(raw_text, str):
        return raw_text
    content = node.get('content')
    if not isinstance(content, list):
        return ''
    return ''.join((_node_text(child) for child in content))

def _truncate_summary(value: str) -> str:
    normalized = _normalize_text(value)
    if len(normalized) <= SUMMARY_MAX_CHARS:
        return normalized
    excerpt = normalized[:SUMMARY_MAX_CHARS]
    sentence_end = max((excerpt.rfind(mark) for mark in ('。', '！', '？', '.', '!', '?')))
    if sentence_end >= SUMMARY_MAX_CHARS // 2:
        return excerpt[:sentence_end + 1]
    return f'{excerpt.rstrip()}…'

def _structured_content_candidates(lesson: SummarySource) -> tuple[list[str], list[str]]:
    document = lesson.board_document.content_json
    nodes = document.get('content') if isinstance(document, dict) else None
    if not isinstance(nodes, list):
        return ([], [])
    paragraphs: list[str] = []
    headings: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        text = _normalize_text(_node_text(node))
        if not text:
            continue
        node_type = node.get('type')
        if node_type in {'paragraph', 'blockquote'}:
            paragraphs.append(text)
        elif node_type == 'heading':
            headings.append(text)
    return (paragraphs, headings)

def _plain_text_candidates(lesson: SummarySource) -> list[str]:
    excluded = {_normalize_text(lesson.title).casefold(), _normalize_text(lesson.board_document.title).casefold()}
    candidates: list[str] = []
    for raw_line in lesson.board_document.content_text.splitlines():
        text = _normalize_text(re.sub('^#{1,6}\\s*', '', raw_line))
        if not text or text.casefold() in excluded:
            continue
        candidates.append(text)
    return candidates

def lesson_content_summary(lesson: SummarySource) -> str:
    """Return an authored summary or a content-derived description for a lesson."""
    if (explicit_summary := _normalize_text(lesson.summary)):
        return explicit_summary
    paragraphs, headings = _structured_content_candidates(lesson)
    for paragraph in paragraphs:
        if len(paragraph) >= _MIN_SUBSTANTIVE_CHARS:
            return _truncate_summary(paragraph)
    if paragraphs:
        return _truncate_summary(paragraphs[0])
    excluded = {_normalize_text(lesson.title).casefold(), _normalize_text(lesson.board_document.title).casefold()}
    for heading in headings:
        if heading.casefold() not in excluded:
            return _truncate_summary(heading)
    plain_text_candidates = _plain_text_candidates(lesson)
    for candidate in plain_text_candidates:
        if len(candidate) >= _MIN_SUBSTANTIVE_CHARS:
            return _truncate_summary(candidate)
    if plain_text_candidates:
        return _truncate_summary(plain_text_candidates[0])
    return ''

@dataclass
class _DocumentSummarySource:
    title: str
    summary: str
    board_document: BoardDocument

def document_content_summary(document: BoardDocument, *, title: str, summary: str='') -> str:
    return lesson_content_summary(_DocumentSummarySource(title, summary, document))
