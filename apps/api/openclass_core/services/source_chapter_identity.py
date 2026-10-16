from __future__ import annotations
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable
from openclass_core.models import SelectionRef, SourceChapter

@dataclass(frozen=True)
class SourceChapterRebinding:
    chapter: SourceChapter | None = None
    matched_anchors: tuple[str, ...] = ()
    candidate_ids: tuple[str, ...] = ()

    @property
    def is_ambiguous(self) -> bool:
        return self.chapter is None and len(self.candidate_ids) > 1

def stable_source_chapter_id(*, source_ingestion_id: str, parent_path: Iterable[str], normalized_number: str, title: str, level: int, source_locator: str, order_index: int) -> str:
    """Return a semantic ID that survives parser strategy and unrelated order changes.

    ``order_index`` is the occurrence number among otherwise identical semantic
    siblings. The caller no longer passes the chapter's global list position.
    ``source_locator`` stays in the signature for compatibility, but locators are
    deliberately excluded because a structure rebuild may replace a PDF outline
    locator with an equivalent printed-TOC or body-heading locator.
    """
    identity = '\x1f'.join(['v2', source_ingestion_id.strip(), '>'.join((_normalize_text(part) for part in parent_path if part.strip())), _normalize_number(normalized_number), _normalize_text(title), str(max(1, level)), str(max(0, order_index))])
    digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]
    return f'sourcechapter_{digest}'

def rebind_stale_source_chapter_selection(*, selection: SelectionRef, source_ingestion_id: str, chapters: Iterable[SourceChapter]) -> SourceChapterRebinding:
    """Resolve a stale interactive source selection only with structural anchors.

    This is intentionally limited to the originally selected source. It never
    falls back to semantic retrieval or chooses arbitrarily among candidates.
    """
    if selection.kind != 'source' or selection.source_ingestion_id != source_ingestion_id:
        return SourceChapterRebinding()
    verified = [chapter for chapter in chapters if chapter.anchor_status == 'verified']
    if not verified:
        return SourceChapterRebinding()
    anchors: list[tuple[str, list[SourceChapter]]] = []
    locator = _normalize_text(selection.source_locator)
    if locator:
        locator_matches = [chapter for chapter in verified if _normalize_text(chapter.source_locator) == locator]
        if locator_matches:
            anchors.append(('source_locator', locator_matches))
    expected_number = _normalize_number(selection.source_chapter_number)
    expected_title = _normalize_text(selection.source_chapter_title)
    expected_path = tuple((_normalize_text(part) for part in selection.heading_path if part.strip()))
    has_page_bounds = selection.source_page_start is not None or selection.source_page_end is not None
    if expected_number:
        anchors.append(('chapter_number', [chapter for chapter in verified if _normalize_number(chapter.normalized_number or chapter.number) == expected_number]))
    if expected_title:
        anchors.append(('chapter_title', [chapter for chapter in verified if _normalize_text(chapter.title) == expected_title]))
    if expected_path:
        anchors.append(('heading_path', [chapter for chapter in verified if tuple((_normalize_text(part) for part in chapter.path if part.strip())) == expected_path]))
    if has_page_bounds:
        anchors.append(('page_bounds', [chapter for chapter in verified if chapter.page_start == selection.source_page_start and chapter.page_end == selection.source_page_end]))
    non_empty_anchors = [anchor for anchor in anchors if anchor[1]]
    if not non_empty_anchors:
        return SourceChapterRebinding()
    votes: dict[str, list[str]] = {}
    for anchor_name, candidates in non_empty_anchors:
        for chapter in candidates:
            votes.setdefault(chapter.id, []).append(anchor_name)
    max_votes = max((len(anchor_names) for anchor_names in votes.values()), default=0)
    winning_ids = {chapter_id for chapter_id, anchor_names in votes.items() if len(anchor_names) == max_votes}
    candidates = [chapter for chapter in verified if chapter.id in winning_ids]
    if max_votes >= 2 and len(candidates) == 1:
        matched_anchors = tuple(votes[candidates[0].id])
        return SourceChapterRebinding(chapter=candidates[0], matched_anchors=matched_anchors, candidate_ids=(candidates[0].id,))
    return SourceChapterRebinding(candidate_ids=tuple((chapter.id for chapter in candidates)))

def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize('NFKC', value).casefold()
    return re.sub('\\s+', ' ', normalized).strip()

def _normalize_number(value: str) -> str:
    normalized = _normalize_text(value)
    parts = [part for part in normalized.split('.') if part]
    if not parts:
        return ''
    if not all((part.isdigit() for part in parts)):
        return normalized
    return '.'.join((str(int(part)) for part in parts))
