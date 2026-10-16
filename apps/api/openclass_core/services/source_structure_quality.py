from __future__ import annotations
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from statistics import median
from typing import Any
from openclass_core.models import SourceChapter, SourceStructureQuality
_PAGE_MARKER_RE = re.compile('\\[\\s*page\\s+\\d+\\s*\\]', re.IGNORECASE)
_EXPECTED_LEAF_TEXT_SPAN = 120000
_MAX_EXPECTED_LEAF_COUNT = 12
_OVERSIZED_LEAF_MIN_CHARS = 160000
_OVERSIZED_LEAF_DOCUMENT_RATIO = 0.55

@dataclass(frozen=True)
class SourceStructureQualityResult:
    chapters: list[SourceChapter]
    quality: SourceStructureQuality
    warnings: list[str]

def evaluate_source_structure_quality(*, chapters: list[SourceChapter], text: str, strategy: str='linear_text', metadata: dict[str, Any] | None=None) -> SourceStructureQualityResult:
    """Validate grounded chapter ranges and grade the whole source structure.

    Format adapters are allowed to produce incomplete navigation candidates. This
    boundary decides which candidates are safe to quote and whether the document
    as a whole is fully verified, partially verified, or search-only.
    """
    initial_verified_count = sum((chapter.anchor_status == 'verified' for chapter in chapters))
    validated: list[SourceChapter] = []
    demoted_count = 0
    for chapter in chapters:
        reasons = _invalid_anchor_reasons(chapter, text=text)
        if chapter.anchor_status == 'verified' and reasons:
            validated.append(_demote_chapter(chapter, reasons=reasons))
            demoted_count += 1
        else:
            validated.append(_record_validation(chapter, reasons=reasons))
    validated, duplicate_range_count, duplicate_demotions = _demote_duplicate_ranges(validated)
    demoted_count += duplicate_demotions
    overlap_ratio = _sibling_overlap_ratio([chapter for chapter in validated if chapter.anchor_status == 'verified'])
    validated, overlap_demotions = _demote_overlapping_siblings(validated)
    demoted_count += overlap_demotions
    verified = [chapter for chapter in validated if chapter.anchor_status == 'verified']
    total_count = len(validated)
    verified_count = len(verified)
    unverified_count = total_count - verified_count
    verified_ratio = verified_count / total_count if total_count else 0.0
    boundary_valid_ratio = verified_count / initial_verified_count if initial_verified_count else 0.0
    coverage_ratio = _body_coverage_ratio(verified, text_length=len(text))
    duplicate_locator_ratio = _duplicate_locator_ratio(verified)
    non_monotonic_count = _non_monotonic_sibling_count(verified)
    leaf_chapters = _verified_leaf_chapters(verified)
    expected_leaf_count = _expected_leaf_count(len(text), strategy=strategy, parser=str((metadata or {}).get('parser') or ''))
    leaf_lengths = [max(0, (chapter.body_end_offset or 0) - (chapter.body_start_offset or 0)) for chapter in leaf_chapters]
    median_leaf_length = median(leaf_lengths) if leaf_lengths else 0.0
    oversized_leaf_count = sum((_is_oversized_leaf(chapter, text_length=len(text), median_leaf_length=median_leaf_length) for chapter in leaf_chapters))
    average_anchor_confidence = sum((chapter.confidence for chapter in verified)) / verified_count if verified_count else 0.0
    confidence_values = sorted((chapter.confidence for chapter in verified))
    lower_quartile_confidence = confidence_values[max(0, math.ceil(len(confidence_values) * 0.25) - 1)] if confidence_values else 0.0
    independent_anchor_ratio = sum((_has_independent_anchor(chapter) for chapter in verified)) / verified_count if verified_count else 0.0
    page_count = _positive_int((metadata or {}).get('page_count'))
    meaningful_character_count = meaningful_text_character_count(text)
    meaningful_characters_per_page = meaningful_character_count / page_count if page_count else 0.0
    text_readiness = _text_readiness(meaningful_character_count=meaningful_character_count, page_count=page_count, parser=str((metadata or {}).get('parser') or ''))
    granularity_score = min(1.0, len(leaf_chapters) / expected_leaf_count) if expected_leaf_count else 1.0
    consistency_penalty = max(overlap_ratio, non_monotonic_count / max(1, verified_count - 1), duplicate_range_count / max(1, total_count))
    consistency_score = max(0.0, 1.0 - consistency_penalty)
    confidence = round(max(0.0, min(1.0, 0.25 * boundary_valid_ratio + 0.15 * verified_ratio + 0.1 * coverage_ratio + 0.2 * granularity_score + 0.15 * consistency_score + 0.1 * independent_anchor_ratio + 0.05 * average_anchor_confidence)), 4)
    diagnostics = _quality_diagnostics(total_count=total_count, verified_count=verified_count, verified_ratio=verified_ratio, coverage_ratio=coverage_ratio, duplicate_range_count=duplicate_range_count, overlap_ratio=overlap_ratio, non_monotonic_count=non_monotonic_count, verified_leaf_count=len(leaf_chapters), expected_leaf_count=expected_leaf_count, oversized_leaf_count=oversized_leaf_count, duplicate_locator_ratio=duplicate_locator_ratio, text_readiness=text_readiness, meaningful_characters_per_page=meaningful_characters_per_page)
    if text_readiness == 'empty':
        level = 'unverified'
    elif not verified and total_count:
        level = 'unverified'
    elif not verified:
        level = 'search_only'
    elif verified_ratio < 0.3 or coverage_ratio < 0.2 or (not leaf_chapters):
        level = 'unverified'
    elif verified_ratio >= 0.98 and boundary_valid_ratio >= 0.98 and (coverage_ratio >= 0.8) and (duplicate_range_count == 0) and (overlap_ratio == 0.0) and (non_monotonic_count == 0) and (oversized_leaf_count == 0) and (len(leaf_chapters) >= expected_leaf_count) and (lower_quartile_confidence >= 0.72) and (independent_anchor_ratio >= 0.5) and (text_readiness in {'ready', 'unknown'}) and (demoted_count == 0):
        level = 'fully_verified'
    else:
        level = 'partially_verified'
    if level == 'partially_verified':
        confidence = min(confidence, 0.89)
    elif level == 'unverified':
        confidence = min(confidence, 0.49)
    elif level == 'search_only':
        confidence = 0.0
    quality = SourceStructureQuality(evaluator_version=1, level=level, text_readiness=text_readiness, confidence=confidence, total_chapter_count=total_count, verified_chapter_count=verified_count, unverified_chapter_count=unverified_count, demoted_chapter_count=demoted_count, verified_leaf_count=len(leaf_chapters), expected_leaf_count=expected_leaf_count, verified_ratio=round(verified_ratio, 4), boundary_valid_ratio=round(boundary_valid_ratio, 4), body_coverage_ratio=round(coverage_ratio, 4), independent_anchor_ratio=round(independent_anchor_ratio, 4), meaningful_characters_per_page=round(meaningful_characters_per_page, 2), duplicate_locator_ratio=round(duplicate_locator_ratio, 4), duplicate_range_count=duplicate_range_count, overlap_ratio=round(overlap_ratio, 4), non_monotonic_count=non_monotonic_count, oversized_leaf_count=oversized_leaf_count, diagnostics=diagnostics)
    warnings = _quality_warnings(level=level, text_readiness=text_readiness, diagnostics=diagnostics)
    return SourceStructureQualityResult(chapters=validated, quality=quality, warnings=warnings)

def _invalid_anchor_reasons(chapter: SourceChapter, *, text: str) -> list[str]:
    if chapter.anchor_status != 'verified':
        return []
    reasons: list[str] = []
    start = chapter.body_start_offset
    end = chapter.body_end_offset
    if not chapter.title.strip():
        reasons.append('missing_title')
    if not chapter.source_locator.strip():
        reasons.append('missing_source_locator')
    if start is None or end is None:
        reasons.append('missing_body_range')
        return reasons
    if start < 0 or end <= start or end > len(text):
        reasons.append('invalid_body_range')
        return reasons
    if not _has_usable_text(text[start:end]):
        reasons.append('body_range_has_no_usable_text')
    if chapter.page_start is not None and chapter.page_end is not None and (chapter.page_end <= chapter.page_start):
        reasons.append('invalid_page_range')
    return reasons

def _has_usable_text(value: str) -> bool:
    return meaningful_text_character_count(value) > 0

def meaningful_text_character_count(value: str) -> int:
    """Count language-independent searchable characters, excluding page markers."""
    without_markers = _PAGE_MARKER_RE.sub('', value or '')
    return sum((character.isalnum() for character in without_markers))

def _record_validation(chapter: SourceChapter, *, reasons: list[str]) -> SourceChapter:
    metadata = {**chapter.metadata, 'anchor_validation': {'status': 'verified' if chapter.anchor_status == 'verified' else 'candidate', 'reasons': reasons}}
    return chapter.model_copy(update={'metadata': metadata})

def _demote_chapter(chapter: SourceChapter, *, reasons: list[str]) -> SourceChapter:
    metadata = {**chapter.metadata, 'anchor_validation': {'status': 'rejected', 'reasons': reasons}}
    return chapter.model_copy(update={'anchor_status': 'unverified', 'confidence': min(chapter.confidence, 0.49), 'excerpt': '', 'metadata': metadata})

def _demote_duplicate_ranges(chapters: list[SourceChapter]) -> tuple[list[SourceChapter], int, int]:
    groups: dict[tuple[str, int, int, int], list[int]] = defaultdict(list)
    for index, chapter in enumerate(chapters):
        if chapter.anchor_status != 'verified' or chapter.body_start_offset is None or chapter.body_end_offset is None:
            continue
        groups[chapter.parent_id or '', chapter.level, chapter.body_start_offset, chapter.body_end_offset].append(index)
    updated = list(chapters)
    duplicate_range_count = 0
    demoted_count = 0
    for indexes in groups.values():
        if len(indexes) <= 1:
            continue
        duplicate_range_count += len(indexes) - 1
        keep_index = max(indexes, key=lambda item: (updated[item].confidence, -updated[item].order_index))
        for index in indexes:
            if index == keep_index:
                continue
            updated[index] = _demote_chapter(updated[index], reasons=['duplicate_grounded_range'])
            demoted_count += 1
    return (updated, duplicate_range_count, demoted_count)

def _demote_overlapping_siblings(chapters: list[SourceChapter]) -> tuple[list[SourceChapter], int]:
    groups: dict[str, list[tuple[int, SourceChapter]]] = defaultdict(list)
    for index, chapter in enumerate(chapters):
        if chapter.anchor_status == 'verified' and chapter.body_start_offset is not None and (chapter.body_end_offset is not None):
            groups[chapter.parent_id or ''].append((index, chapter))
    conflicting_indexes: set[int] = set()
    for siblings in groups.values():
        ordered = sorted(siblings, key=lambda item: (item[1].body_start_offset or 0, item[1].order_index))
        for (previous_index, previous), (current_index, current) in zip(ordered, ordered[1:]):
            if (previous.body_end_offset or 0) > (current.body_start_offset or 0):
                conflicting_indexes.update({previous_index, current_index})
    if not conflicting_indexes:
        return (chapters, 0)
    updated = list(chapters)
    for index in conflicting_indexes:
        updated[index] = _demote_chapter(updated[index], reasons=['overlapping_sibling_range'])
    return (updated, len(conflicting_indexes))

def _body_coverage_ratio(chapters: list[SourceChapter], *, text_length: int) -> float:
    if text_length <= 0:
        return 0.0
    intervals = sorted(((max(0, chapter.body_start_offset or 0), min(text_length, chapter.body_end_offset or text_length)) for chapter in chapters if chapter.body_start_offset is not None and chapter.body_end_offset is not None and (chapter.body_end_offset > chapter.body_start_offset)))
    if not intervals:
        return 0.0
    covered = 0
    current_start, current_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            continue
        covered += current_end - current_start
        current_start, current_end = (start, end)
    covered += current_end - current_start
    return min(1.0, covered / text_length)

def _duplicate_locator_ratio(chapters: list[SourceChapter]) -> float:
    locators = [chapter.source_locator.strip() for chapter in chapters if chapter.source_locator.strip()]
    if not locators:
        return 0.0
    return max(0.0, (len(locators) - len(set(locators))) / len(locators))

def _sibling_overlap_ratio(chapters: list[SourceChapter]) -> float:
    groups: dict[str, list[SourceChapter]] = defaultdict(list)
    for chapter in chapters:
        groups[chapter.parent_id or ''].append(chapter)
    overlaps = 0
    comparable_pairs = 0
    for siblings in groups.values():
        ordered = sorted((chapter for chapter in siblings if chapter.body_start_offset is not None and chapter.body_end_offset is not None), key=lambda chapter: (chapter.body_start_offset or 0, chapter.order_index))
        for previous, current in zip(ordered, ordered[1:]):
            comparable_pairs += 1
            if (previous.body_end_offset or 0) > (current.body_start_offset or 0):
                overlaps += 1
    return overlaps / comparable_pairs if comparable_pairs else 0.0

def _non_monotonic_sibling_count(chapters: list[SourceChapter]) -> int:
    groups: dict[str, list[SourceChapter]] = defaultdict(list)
    for chapter in chapters:
        groups[chapter.parent_id or ''].append(chapter)
    count = 0
    for siblings in groups.values():
        ordered = sorted(siblings, key=lambda chapter: chapter.order_index)
        starts = [chapter.body_start_offset for chapter in ordered if chapter.body_start_offset is not None]
        count += sum((current < previous for previous, current in zip(starts, starts[1:])))
    return count

def _verified_leaf_chapters(chapters: list[SourceChapter]) -> list[SourceChapter]:
    parent_ids = {chapter.parent_id for chapter in chapters if chapter.parent_id}
    return [chapter for chapter in chapters if chapter.id not in parent_ids]

def _expected_leaf_count(text_length: int, *, strategy: str, parser: str) -> int:
    if text_length <= 0:
        return 0
    if parser in {'pptx', 'xlsx', 'image', 'vision_ocr'}:
        return 1
    if strategy == 'linear_text' and parser not in {'pdf', 'docx', 'html', 'epub', 'text'}:
        return 1
    return max(1, min(_MAX_EXPECTED_LEAF_COUNT, math.ceil(text_length / _EXPECTED_LEAF_TEXT_SPAN)))

def _is_oversized_leaf(chapter: SourceChapter, *, text_length: int, median_leaf_length: float) -> bool:
    if text_length < _OVERSIZED_LEAF_MIN_CHARS:
        return False
    start = chapter.body_start_offset or 0
    end = chapter.body_end_offset or start
    scope_length = max(0, end - start)
    absolute_limit = max(_OVERSIZED_LEAF_MIN_CHARS, round(text_length * _OVERSIZED_LEAF_DOCUMENT_RATIO))
    relative_limit = max(_OVERSIZED_LEAF_MIN_CHARS, round(median_leaf_length * 12))
    return scope_length >= min(absolute_limit, relative_limit)

def _quality_diagnostics(*, total_count: int, verified_count: int, verified_ratio: float, coverage_ratio: float, duplicate_range_count: int, overlap_ratio: float, non_monotonic_count: int, verified_leaf_count: int, expected_leaf_count: int, oversized_leaf_count: int, duplicate_locator_ratio: float, text_readiness: str, meaningful_characters_per_page: float) -> list[str]:
    diagnostics: list[str] = []
    if total_count and verified_ratio < 0.98:
        diagnostics.append(f'Only {verified_count}/{total_count} catalog entries link to verifiable source content.')
    if verified_count and coverage_ratio < 0.8:
        diagnostics.append('Verified chapters do not cover most of the searchable source content.')
    if duplicate_range_count:
        diagnostics.append(f'Found {duplicate_range_count} duplicate content ranges. Duplicate entries cannot be cited.')
    if overlap_ratio > 0:
        diagnostics.append('Some sibling chapter ranges overlap. Conflicting entries cannot be cited.')
    if non_monotonic_count:
        diagnostics.append('Some catalog entries are out of order relative to their source locations.')
    if verified_leaf_count < expected_leaf_count:
        diagnostics.append('The current chapters cover overly broad ranges for this source.')
    if oversized_leaf_count:
        diagnostics.append('Some leaf sections cover unusually large ranges. Narrow the range before citing them.')
    if duplicate_locator_ratio >= 0.5:
        diagnostics.append('Many catalog entries share file-level locations. Rebuilding will use heading paths and content ranges to distinguish them.')
    if text_readiness == 'very_sparse':
        diagnostics.append(f'The PDF text layer averages only about {meaningful_characters_per_page:.0f} readable characters per page. OCR is needed to complete the text.')
    elif text_readiness == 'sparse':
        diagnostics.append('The PDF text layer is sparse. Catalog confidence is limited to partially verified.')
    elif text_readiness == 'empty':
        diagnostics.append('No searchable source text was extracted.')
    return diagnostics

def _quality_warnings(*, level: str, text_readiness: str, diagnostics: list[str]) -> list[str]:
    if text_readiness == 'empty':
        return ['No searchable source text was extracted. Catalog citations and full-text search are currently unavailable.']
    if level == 'search_only':
        return ['No chapter content was verified for citation. Full-text chunk search remains available.']
    if level == 'partially_verified':
        return list(dict.fromkeys(['The catalog is partially verified. Only chapters marked as verified can be cited.', *diagnostics]))
    if level == 'unverified':
        return list(dict.fromkeys(['The catalog candidates do not yet provide reliable chapter boundaries. The full catalog has not been marked as verified.', *diagnostics]))
    return []

def _has_independent_anchor(chapter: SourceChapter) -> bool:
    source = str(chapter.metadata.get('source') or '')
    anchor_source = str(chapter.metadata.get('anchor_source') or '')
    verification = str(chapter.metadata.get('verification') or '')
    if not source and (not anchor_source) and (not verification):
        return False
    if source in {'markdown_heading', 'html_heading', 'docx_heading', 'pptx_slide', 'xlsx_sheet', 'epub_heading'}:
        return True
    if anchor_source in {'epub_fragment', 'body_title_match', 'pdf_destination_title'}:
        return True
    return verification in {'native_outline_anchor', 'verified_printed_page_mapping', 'verified_printed_page_mapping_inferred', 'body_title_match'}

def _positive_int(value: object) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)

def _text_readiness(*, meaningful_character_count: int, page_count: int, parser: str) -> str:
    if meaningful_character_count <= 0:
        return 'empty'
    if parser != 'pdf' or page_count < 10:
        return 'ready'
    density = meaningful_character_count / max(1, page_count)
    if density < 40:
        return 'very_sparse'
    if density < 120:
        return 'sparse'
    return 'ready'
