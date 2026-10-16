from __future__ import annotations
import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from difflib import SequenceMatcher
from itertools import pairwise
from typing import Literal
from pydantic import BaseModel, Field
from openclass_core.models import BoardFocusRef, BoardSegment, Lesson, SelectionRef
from openclass_core.services.board_segment_index import build_board_segment_index, compact_segment_text, segment_text_hash
from openclass_core.services.history import current_head_commit
from openclass_core.services.rich_document import document_to_markdown
TargetResolutionStatus = Literal['resolved', 'target_not_resolved']
TargetResolutionReason = Literal['resolved_by_selection', 'resolved_by_heading', 'resolved_by_ordinal', 'resolved_by_text_clue', 'board_empty', 'target_missing', 'selection_kind_mismatch', 'selection_lesson_mismatch', 'selection_document_mismatch', 'selection_stale_version', 'selection_segment_missing', 'selection_stale_hash', 'selection_text_mismatch', 'selection_text_too_large', 'target_scope_too_large', 'whole_board_scope_requires_confirmation', 'ambiguous_candidates', 'below_confidence_threshold', 'target_not_found']
MAX_APPROVED_BOARD_TARGET_CHARS = 16000

class TargetResolution(BaseModel):
    status: TargetResolutionStatus
    machine_reason: TargetResolutionReason
    focus: BoardFocusRef | None = None
    candidates: list[BoardFocusRef] = Field(default_factory=list, max_length=5)

class FocusResolver:
    """Resolve a board target without model calls or full-document output."""

    def __init__(self, *, confidence_threshold: float=0.72, candidate_margin: float=0.12) -> None:
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError('confidence_threshold must be between 0 and 1')
        if not 0.0 <= candidate_margin <= 1.0:
            raise ValueError('candidate_margin must be between 0 and 1')
        self.confidence_threshold = confidence_threshold
        self.candidate_margin = candidate_margin

    def resolve(self, lesson: Lesson, *, target_text: str='', selection: SelectionRef | None=None, content_extent: str | None=None) -> TargetResolution:
        index = build_board_segment_index(lesson.board_document)
        segments = [segment for segment in index.segments if compact_segment_text(segment.text)]
        if not segments:
            return self._unresolved('board_empty')
        if selection is not None:
            return self._resolve_selection(lesson, segments, selection, content_extent=content_extent)
        target = compact_segment_text(target_text, limit=500)
        if not target:
            return self._unresolved('target_missing')
        heading_matches = [segment for segment in segments if segment.kind == 'heading' and _normalize(segment.text) == _normalize(target)]
        if len(heading_matches) == 1:
            return self._resolved(lesson, heading_matches[0], confidence=0.99, reason='resolved_by_heading', segments=segments, content_extent=content_extent)
        if len(heading_matches) > 1:
            return self._unresolved('ambiguous_candidates', candidates=self._candidate_refs(lesson, heading_matches, confidence=0.99))
        ordinal = _parse_ordinal_reference(target)
        if ordinal is not None:
            ordinal_matches = _ordinal_candidates(segments, *ordinal)
            if len(ordinal_matches) == 1:
                return self._resolved(lesson, ordinal_matches[0], confidence=0.96, reason='resolved_by_ordinal', segments=segments, content_extent=content_extent)
            if len(ordinal_matches) > 1:
                return self._unresolved('ambiguous_candidates', candidates=self._candidate_refs(lesson, ordinal_matches, confidence=0.96))
        ranked = sorted(((self._text_score(target, segment), segment) for segment in segments), key=lambda item: (-item[0], item[1].order_index, item[1].segment_id))
        best_score, best_segment = ranked[0]
        candidates = [self._focus_ref(lesson, segment, confidence=score, reason='candidate') for score, segment in ranked[:5] if score > 0.0]
        if best_score < 0.25:
            return self._unresolved('target_not_found', candidates=candidates)
        if best_score < self.confidence_threshold:
            return self._unresolved('below_confidence_threshold', candidates=candidates)
        runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
        if best_score - runner_up < self.candidate_margin:
            return self._unresolved('ambiguous_candidates', candidates=candidates)
        return self._resolved(lesson, best_segment, confidence=best_score, reason='resolved_by_text_clue', segments=segments, content_extent=content_extent)

    def resolve_many(self, lesson: Lesson, *, selections: Sequence[SelectionRef], content_extent: str | None=None) -> TargetResolution:
        """Resolve one frozen board range from ordered adjacent selections."""
        frozen = [selection.model_copy(deep=True) for selection in selections]
        if not frozen:
            return self._unresolved('target_missing')
        if len(frozen) == 1:
            return self.resolve(lesson, selection=frozen[0], content_extent=content_extent)
        index = build_board_segment_index(lesson.board_document)
        segments = [segment for segment in index.segments if compact_segment_text(segment.text)]
        results = [self.resolve(lesson, selection=selection) for selection in frozen]
        resolved = [result.focus for result in results if result.status == 'resolved' and result.focus]
        if len(resolved) != len(frozen):
            return self._unresolved('ambiguous_candidates', candidates=_collect_candidates(results))
        if any((selection.location_kind != 'target_range' for selection in frozen)):
            return self._unresolved('ambiguous_candidates', candidates=[focus.model_copy(deep=True) for focus in resolved[:5]])
        positions = [focus.order_start for focus in resolved]
        if any((position is None for position in positions)):
            return self._unresolved('ambiguous_candidates')
        ordered_positions = [int(position) for position in positions if position is not None]
        if any((current != previous + 1 for previous, current in pairwise(ordered_positions))):
            return self._unresolved('ambiguous_candidates', candidates=[focus.model_copy(deep=True) for focus in resolved[:5]])
        first_order = ordered_positions[0]
        last_order = ordered_positions[-1]
        if first_order < 0 or last_order >= len(segments):
            return self._unresolved('selection_segment_missing')
        if first_order == 0 and last_order == len(segments) - 1:
            return self._unresolved('whole_board_scope_requires_confirmation')
        source_segments = segments[first_order:last_order + 1]
        if [segment.segment_id for segment in source_segments] != [focus.segment_id for focus in resolved]:
            return self._unresolved('ambiguous_candidates')
        markdown = document_to_markdown(lesson.board_document)
        first_span = _segment_markdown_span(markdown, segments, source_segments[0])
        last_span = _segment_markdown_span(markdown, segments, source_segments[-1])
        if first_span is None or last_span is None:
            return self._unresolved('selection_text_mismatch')
        first_offset = _unique_exact_offset(source_segments[0].text, frozen[0].excerpt)
        last_offset = _unique_exact_offset(source_segments[-1].text, frozen[-1].excerpt)
        if first_offset is None or last_offset is None:
            return self._unresolved('selection_text_mismatch')
        range_start = first_span[0] + first_offset
        range_end = last_span[0] + last_offset + len(frozen[-1].excerpt)
        excerpt = markdown[range_start:range_end]
        if not excerpt:
            return self._unresolved('selection_text_mismatch')
        if len(excerpt) > MAX_APPROVED_BOARD_TARGET_CHARS:
            return self._unresolved('target_scope_too_large')
        return TargetResolution(status='resolved', machine_reason='resolved_by_selection', focus=self._focus_ref(lesson, source_segments[0], confidence=min((focus.confidence for focus in resolved)), reason='resolved_by_selection', excerpt_override=excerpt, source_segments=source_segments, freeze_range_hash=True))

    def _resolve_selection(self, lesson: Lesson, segments: list[BoardSegment], selection: SelectionRef, *, content_extent: str | None=None) -> TargetResolution:
        document = lesson.board_document
        if selection.kind != 'board':
            return self._unresolved('selection_kind_mismatch')
        if selection.lesson_id and selection.lesson_id != lesson.id:
            return self._unresolved('selection_lesson_mismatch')
        if selection.document_id and selection.document_id != document.id:
            return self._unresolved('selection_document_mismatch')
        if selection.source_commit_id and selection.source_commit_id != current_head_commit(lesson).id:
            return self._unresolved('selection_stale_version')
        if selection.location_kind == 'target_range' and _selection_covers_whole_board(lesson, segments, selection.excerpt):
            return self._unresolved('whole_board_scope_requires_confirmation')
        if selection.location_kind == 'target_range' and len(selection.excerpt) > MAX_APPROVED_BOARD_TARGET_CHARS:
            return self._unresolved('selection_text_too_large')
        if selection.segment_id:
            segment = next((candidate for candidate in segments if candidate.segment_id == selection.segment_id), None)
            if segment is None:
                return self._unresolved('selection_segment_missing')
            if selection.text_hash and (not _selection_hash_matches(selection, segment)):
                return self._unresolved('selection_stale_hash', candidates=self._candidate_refs(lesson, [segment], confidence=0.0))
            if selection.location_kind == 'insertion_anchor' and (not _anchor_context_matches(selection, segment.text)):
                return self._unresolved('selection_text_mismatch', candidates=self._candidate_refs(lesson, [segment], confidence=0.0))
            if selection.location_kind != 'insertion_anchor' and selection.excerpt and (not _selection_excerpt_matches(selection.excerpt, segment.text)):
                return self._unresolved('selection_text_mismatch', candidates=self._candidate_refs(lesson, [segment], confidence=0.0))
            return self._resolved(lesson, segment, confidence=1.0 if selection.text_hash else 0.99, reason='resolved_by_selection', selection=selection, segments=segments, content_extent=content_extent)
        if selection.location_kind == 'insertion_anchor':
            matches = [segment for segment in segments if _anchor_context_matches(selection, segment.text)]
            if not matches:
                return self._unresolved('selection_text_mismatch')
        elif selection.text_hash:
            matches = [segment for segment in segments if _selection_hash_matches(selection, segment)]
            if not matches:
                return self._unresolved('selection_stale_hash')
        else:
            matches = [segment for segment in segments if _selection_excerpt_matches(selection.excerpt, segment.text)]
            if not matches:
                compound_candidates = _compound_selection_segment_candidates(segments, selection.excerpt)
                if not compound_candidates:
                    return self._unresolved('selection_text_mismatch')
                if len(compound_candidates) > 1:
                    return self._unresolved('ambiguous_candidates', candidates=self._candidate_refs(lesson, [candidate[0] for candidate in compound_candidates], confidence=0.94))
                compound_segments = compound_candidates[0]
                if compound_segments[0].order_index == 0 and len(compound_segments) == len(segments):
                    return self._unresolved('whole_board_scope_requires_confirmation')
                return TargetResolution(status='resolved', machine_reason='resolved_by_selection', focus=self._focus_ref(lesson, compound_segments[0], confidence=0.98, reason='resolved_by_selection', excerpt_override=selection.excerpt, source_segments=compound_segments, freeze_range_hash=True))
        matches = _narrow_by_heading_path(matches, selection.heading_path)
        if len(matches) != 1:
            return self._unresolved('ambiguous_candidates', candidates=self._candidate_refs(lesson, matches, confidence=0.98 if selection.text_hash else 0.94))
        return self._resolved(lesson, matches[0], confidence=0.98 if selection.text_hash else 0.94, reason='resolved_by_selection', selection=selection, segments=segments, content_extent=content_extent)

    def _text_score(self, target: str, segment: BoardSegment) -> float:
        query = _normalize(target)
        text = _normalize(segment.text)
        if not query or not text:
            return 0.0
        if query == text:
            return 1.0
        if len(query) >= 2 and query in text:
            return 0.94
        if len(text) >= 4 and text in query:
            return 0.86
        query_grams = _character_grams(query)
        text_grams = _character_grams(text)
        overlap = len(query_grams & text_grams)
        coverage = overlap / len(query_grams) if query_grams else 0.0
        dice = 2 * overlap / (len(query_grams) + len(text_grams)) if query_grams and text_grams else 0.0
        sequence = SequenceMatcher(None, query, text, autojunk=False).ratio()
        score = 0.5 * coverage + 0.3 * dice + 0.2 * sequence
        heading_text = _normalize(' '.join(segment.heading_path))
        if heading_text and len(query) >= 2 and (query in heading_text):
            score += 0.04
        return min(round(score, 6), 1.0)

    def _resolved(self, lesson: Lesson, segment: BoardSegment, *, confidence: float, reason: TargetResolutionReason, selection: SelectionRef | None=None, segments: list[BoardSegment] | None=None, content_extent: str | None=None) -> TargetResolution:
        source_segments: list[BoardSegment] | None = None
        excerpt_override = selection.excerpt if selection is not None and selection.location_kind == 'target_range' and selection.excerpt else None
        freeze_range_hash = False
        if content_extent == 'section' and segment.kind == 'heading' and segments:
            source_segments = _section_segments(segments, segment)
            covers_whole_board = bool(source_segments) and source_segments[0].order_index == 0 and (len(source_segments) == len(segments))
            if covers_whole_board:
                return self._unresolved('whole_board_scope_requires_confirmation')
            excerpt_override = _section_markdown_excerpt(lesson, segments, source_segments)
            if not excerpt_override:
                return self._unresolved('selection_text_mismatch')
            if len(excerpt_override) > MAX_APPROVED_BOARD_TARGET_CHARS:
                return self._unresolved('target_scope_too_large')
            freeze_range_hash = True
        return TargetResolution(status='resolved', machine_reason=reason, focus=self._focus_ref(lesson, segment, confidence=confidence, reason=reason, excerpt_override=excerpt_override, before_text=selection.before_text[-240:] if selection is not None and selection.location_kind == 'insertion_anchor' else '', after_text=selection.after_text[:240] if selection is not None and selection.location_kind == 'insertion_anchor' else '', source_segments=source_segments, freeze_range_hash=freeze_range_hash))

    def _unresolved(self, reason: TargetResolutionReason, *, candidates: list[BoardFocusRef] | None=None) -> TargetResolution:
        return TargetResolution(status='target_not_resolved', machine_reason=reason, candidates=(candidates or [])[:5])

    def _candidate_refs(self, lesson: Lesson, segments: list[BoardSegment], *, confidence: float) -> list[BoardFocusRef]:
        return [self._focus_ref(lesson, segment, confidence=confidence, reason='candidate') for segment in segments[:5]]

    @staticmethod
    def _focus_ref(lesson: Lesson, segment: BoardSegment, *, confidence: float, reason: str, excerpt_override: str | None=None, before_text: str='', after_text: str='', source_segments: Sequence[BoardSegment] | None=None, freeze_range_hash: bool=False) -> BoardFocusRef:
        excerpt = excerpt_override or compact_segment_text(segment.text, limit=318)
        heading_path = [compact_segment_text(item, limit=118) for item in segment.heading_path[-5:]]
        label = heading_path[-1] if heading_path else f'{segment.kind}:{segment.order_index + 1}'
        frozen_segments = list(source_segments or [segment])
        text_hash = focus_range_text_hash(frozen_segments, excerpt) if freeze_range_hash else segment.text_hash
        return BoardFocusRef(source='board', lesson_id=lesson.id, document_id=lesson.board_document.id, segment_id=segment.segment_id, kind=segment.kind, heading_path=heading_path, excerpt=excerpt, before_text=before_text, after_text=after_text, text_hash=text_hash, excerpt_hash=segment_text_hash(excerpt), confidence=max(0.0, min(float(confidence), 1.0)), reason=reason, display_label=compact_segment_text(label, limit=118), match_id=f'focus_{segment.text_hash}_{segment.order_index}', source_segment_ids=[item.segment_id for item in frozen_segments], order_start=frozen_segments[0].order_index, order_end=frozen_segments[-1].order_index, score_breakdown={'deterministic_score': max(0.0, min(float(confidence), 1.0))})

def resolve_board_focus(lesson: Lesson, *, target_text: str='', selection: SelectionRef | None=None, content_extent: str | None=None, confidence_threshold: float=0.72, candidate_margin: float=0.12) -> TargetResolution:
    return FocusResolver(confidence_threshold=confidence_threshold, candidate_margin=candidate_margin).resolve(lesson, target_text=target_text, selection=selection, content_extent=content_extent)

def focus_range_text_hash(segments: Sequence[BoardSegment], excerpt: str) -> str:
    payload = {'segments': [{'segment_id': segment.segment_id, 'order_index': segment.order_index, 'text_hash': segment.text_hash} for segment in segments], 'excerpt': excerpt}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode('utf-8')).hexdigest()

def _normalize(value: str) -> str:
    normalized = unicodedata.normalize('NFKC', value or '').casefold()
    return ''.join((character for character in normalized if character.isalnum()))

def _selection_excerpt_matches(excerpt: str, segment_text: str) -> bool:
    selected = _normalize(excerpt)
    current = _normalize(segment_text)
    if not selected or not current:
        return False
    return selected == current or selected in current

def _compound_selection_segment_candidates(segments: Sequence[BoardSegment], excerpt: str) -> list[list[BoardSegment]]:
    """Resolve one browser selection that crosses adjacent document blocks."""
    selected = _normalize_range_text(excerpt)
    if not selected or len(segments) < 2:
        return []
    normalized_segments = [_normalize_range_text(segment.text) for segment in segments]
    boundary_slack = 2 * max((len(text) for text in normalized_segments), default=0)
    candidates: list[tuple[int, int, int, list[BoardSegment]]] = []
    for start in range(len(segments) - 1):
        parts: list[str] = []
        for end in range(start, len(segments)):
            if normalized_segments[end]:
                parts.append(normalized_segments[end])
            combined = ' '.join(parts)
            if end > start and selected in combined:
                candidates.append((len(combined) - len(selected), end - start + 1, start, list(segments[start:end + 1])))
            if len(combined) > len(selected) + boundary_slack:
                break
    if not candidates:
        return []
    candidates.sort(key=lambda item: item[:3])
    best_rank = candidates[0][:2]
    return [candidate[3] for candidate in candidates if candidate[:2] == best_rank]

def _selection_covers_whole_board(lesson: Lesson, segments: Sequence[BoardSegment], excerpt: str) -> bool:
    selected = _normalize_range_text(excerpt)
    if not selected:
        return False
    markdown = _normalize_range_text(document_to_markdown(lesson.board_document))
    plain_segments = _normalize_range_text('\n\n'.join((segment.text for segment in segments)))
    return selected in {markdown, plain_segments}

def _normalize_range_text(value: str) -> str:
    normalized = unicodedata.normalize('NFKC', value or '')
    return ' '.join(normalized.split())

def _anchor_context_matches(selection: SelectionRef, segment_text: str) -> bool:
    current = _normalize(segment_text)
    if not current:
        return False
    before = _normalize(selection.before_text)
    after = _normalize(selection.after_text)
    if before or after:
        before_needle = before[-80:]
        after_needle = after[:80]
        return (not before_needle or before_needle in current) and (not after_needle or after_needle in current)
    return _selection_excerpt_matches(selection.excerpt, segment_text)

def _selection_hash_matches(selection: SelectionRef, segment: BoardSegment) -> bool:
    if not selection.text_hash:
        return True
    if selection.text_hash == segment.text_hash:
        return True
    return selection.text_hash == segment_text_hash(selection.excerpt) and _selection_excerpt_matches(selection.excerpt, segment.text)

def _narrow_by_heading_path(matches: list[BoardSegment], heading_path: list[str]) -> list[BoardSegment]:
    if len(matches) <= 1 or not heading_path:
        return matches
    expected = [_normalize(item) for item in heading_path if _normalize(item)]
    narrowed = [segment for segment in matches if [_normalize(item) for item in segment.heading_path[-len(expected):]] == expected]
    return narrowed or matches
_ORDINAL_RE = re.compile('第\\s*([0-9０-９零一二两三四五六七八九十百]+)\\s*(小节|章|节|部分|段|项|条)')

def _parse_ordinal_reference(value: str) -> tuple[int, str] | None:
    match = _ORDINAL_RE.search(unicodedata.normalize('NFKC', value or ''))
    if match is None:
        return None
    number = _ordinal_number(match.group(1))
    if number is None or number < 1:
        return None
    return (number, match.group(2))

def _ordinal_number(value: str) -> int | None:
    normalized = unicodedata.normalize('NFKC', value)
    if normalized.isdigit():
        return int(normalized)
    digits = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
    total = 0
    current = 0
    for character in normalized:
        if character in digits:
            current = digits[character]
        elif character == '十':
            total += (current or 1) * 10
            current = 0
        elif character == '百':
            total += (current or 1) * 100
            current = 0
        else:
            return None
    return total + current

def refers_to_board_section(target_hint: str) -> bool:
    """Recognize a section boundary, never the requested answer length."""
    normalized = unicodedata.normalize('NFKC', target_hint or '')
    refs = list(_ORDINAL_RE.finditer(normalized))
    if len(refs) != 1 or refs[0].group(2) not in {'章', '节', '小节', '部分'}:
        return False
    return not re.search('句|段落|标题|\\b(?:sentence|paragraph|heading|title)\\b', normalized, re.IGNORECASE)

def _ordinal_candidates(segments: list[BoardSegment], number: int, unit: str) -> list[BoardSegment]:
    if unit == '段':
        pool = [segment for segment in segments if segment.kind == 'paragraph']
    elif unit in {'项', '条'}:
        pool = [segment for segment in segments if segment.kind == 'list']
    else:
        headings = [segment for segment in segments if segment.kind == 'heading']
        pool = headings if unit == '章' else _structural_headings(headings)
        explicit = [segment for segment in pool if _heading_ordinal(segment.text, unit=unit) == number]
        if explicit:
            return explicit
    return [pool[number - 1]] if number <= len(pool) else []

def _heading_ordinal(value: str, *, unit: str) -> int | None:
    normalized = unicodedata.normalize('NFKC', value or '')
    chinese = re.match('^\\s*第\\s*([0-9零一二两三四五六七八九十百]+)\\s*(小节|章|节|部分)', normalized)
    if chinese:
        if (chinese.group(2) == '章') != (unit == '章'):
            return None
        return _ordinal_number(chinese.group(1))
    chinese_list = re.match('^\\s*(?:\\()?([零一二两三四五六七八九十百]+)\\s*[、.)]', normalized)
    if chinese_list:
        return _ordinal_number(chinese_list.group(1))
    numeric = re.match('^\\s*(\\d+)\\s*(?:[.．](?!\\s*\\d)|[、)])', normalized)
    return int(numeric.group(1)) if numeric else None

def _structural_headings(headings: list[BoardSegment]) -> list[BoardSegment]:
    if len(headings) <= 1:
        return headings
    outer_depth = min((len(segment.heading_path) for segment in headings))
    outer = [segment for segment in headings if len(segment.heading_path) == outer_depth]
    if len(outer) > 1:
        return outer
    deeper = [segment for segment in headings if len(segment.heading_path) > outer_depth]
    if not deeper:
        return outer
    direct_depth = min((len(segment.heading_path) for segment in deeper))
    return [segment for segment in deeper if len(segment.heading_path) == direct_depth]

def _character_grams(value: str) -> set[str]:
    if not value:
        return set()
    if len(value) == 1:
        return {value}
    return {value[index:index + 2] for index in range(len(value) - 1)}

def _section_segments(segments: Sequence[BoardSegment], heading: BoardSegment) -> list[BoardSegment]:
    try:
        start = next((index for index, segment in enumerate(segments) if segment.segment_id == heading.segment_id))
    except StopIteration:
        return []
    heading_path = list(heading.heading_path)
    selected: list[BoardSegment] = []
    for segment in segments[start:]:
        if selected and segment.kind == 'heading' and (list(segment.heading_path[:len(heading_path)]) != heading_path):
            break
        selected.append(segment)
    return selected

def _section_markdown_excerpt(lesson: Lesson, all_segments: Sequence[BoardSegment], source_segments: Sequence[BoardSegment]) -> str:
    if not source_segments:
        return ''
    markdown = document_to_markdown(lesson.board_document)

    def boundary(segment):
        if segment.kind == 'heading':
            from openclass_core.services.existing_board.markdown_heading_spans import heading_block_span
            return heading_block_span(markdown, all_segments, segment)
        return _segment_markdown_span(markdown, all_segments, segment)
    first = boundary(source_segments[0])
    if first is None:
        return ''
    start = first[2]
    next_order = source_segments[-1].order_index + 1
    if next_order < len(all_segments):
        following = boundary(all_segments[next_order])
        if following is None:
            return ''
        end = following[2]
    else:
        end = len(markdown)
    return markdown[start:end].rstrip('\n')

def _segment_markdown_span(markdown: str, segments: Sequence[BoardSegment], target: BoardSegment) -> tuple[int, int, int, int] | None:
    peers = [segment for segment in segments if segment.kind == target.kind and segment.text == target.text]
    candidates = [start for start in _all_occurrences(markdown, target.text) if _matches_markdown_block(markdown, start, target)]
    if len(candidates) != len(peers):
        return None
    target_ordinal = next((index for index, segment in enumerate(peers) if segment.segment_id == target.segment_id), None)
    if target_ordinal is None or target_ordinal >= len(candidates):
        return None
    text_start = candidates[target_ordinal]
    text_end = text_start + len(target.text)
    block_start = markdown.rfind('\n', 0, text_start) + 1
    next_newline = markdown.find('\n', text_end)
    block_end = len(markdown) if next_newline < 0 else next_newline
    return (text_start, text_end, block_start, block_end)

def _matches_markdown_block(markdown: str, start: int, segment: BoardSegment) -> bool:
    line_start = markdown.rfind('\n', 0, start) + 1
    line_end = markdown.find('\n', start + len(segment.text))
    if line_end < 0:
        line_end = len(markdown)
    line = markdown[line_start:line_end]
    if segment.kind == 'heading':
        return bool(re.fullmatch('#{1,6}\\s+' + re.escape(segment.text), line))
    if segment.kind == 'list':
        return bool(re.fullmatch('(?:[-+*]|\\d+\\.)\\s+' + re.escape(segment.text), line))
    if segment.kind == 'paragraph':
        return line == segment.text
    return segment.text in line

def _all_occurrences(value: str, needle: str) -> list[int]:
    if not needle:
        return []
    starts: list[int] = []
    offset = 0
    while True:
        found = value.find(needle, offset)
        if found < 0:
            return starts
        starts.append(found)
        offset = found + len(needle)

def _unique_exact_offset(value: str, needle: str) -> int | None:
    matches = _all_occurrences(value, needle)
    return matches[0] if len(matches) == 1 else None

def _collect_candidates(results: Sequence[TargetResolution]) -> list[BoardFocusRef]:
    candidates: dict[str, BoardFocusRef] = {}
    for result in results:
        for focus in [*([result.focus] if result.focus else []), *result.candidates]:
            key = focus.segment_id or focus.match_id or focus.excerpt
            candidates.setdefault(key, focus.model_copy(deep=True))
    return list(candidates.values())[:5]
