from __future__ import annotations
import re
from dataclasses import dataclass
from openclass_core.models import RetrievalEvidence, SourceChapter, SourceIngestionRecord
from openclass_core.services.image_ocr import extract_pdf_pages_text
from openclass_core.services.source_ingestion_service import source_local_path
MAX_ON_DEMAND_OCR_PAGES = 24
_PAGE_MARKER_RE = re.compile('\\[\\s*page\\s+\\d+\\s*\\]', re.IGNORECASE)
_MEANINGFUL_CHARACTER_RE = re.compile('[A-Za-z0-9\\u3400-\\u9fff]')

class SourceScopeOcrError(RuntimeError):
    """Raised when a selected PDF scope cannot be recovered through bounded OCR."""

@dataclass(frozen=True)
class SourceScope:
    page_start: int
    page_end_exclusive: int

    @property
    def page_end_inclusive(self) -> int:
        return max(self.page_start, self.page_end_exclusive - 1)

    @property
    def page_count(self) -> int:
        return self.page_end_inclusive - self.page_start + 1

    @property
    def page_range(self) -> str:
        if self.page_start == self.page_end_inclusive:
            return f'p. {self.page_start}'
        return f'pp. {self.page_start}-{self.page_end_inclusive}'

def has_usable_source_text(text: str) -> bool:
    """Reject structural placeholders while accepting short, real source passages."""
    without_markers = _PAGE_MARKER_RE.sub('', text or '')
    meaningful = _MEANINGFUL_CHARACTER_RE.findall(without_markers)
    return len(meaningful) >= 8

def recover_pdf_scope_evidence(*, source: SourceIngestionRecord, chapter: SourceChapter | None, page_start: int | None, page_end_exclusive: int | None, following_chapter: SourceChapter | None=None) -> list[RetrievalEvidence]:
    """OCR only the user-selected PDF pages when the native text layer is empty."""
    if not _is_pdf(source):
        return []
    if page_start is None or page_start < 1:
        raise SourceScopeOcrError('The selected source range has no identifiable starting page.')
    scope = SourceScope(page_start=page_start, page_end_exclusive=max(page_start + 1, page_end_exclusive or page_start + 1))
    if scope.page_count > MAX_ON_DEMAND_OCR_PAGES:
        raise SourceScopeOcrError(f'The selected source range contains {scope.page_count} pages, exceeding the per-request limit of {MAX_ON_DEMAND_OCR_PAGES} pages. Select a smaller page range.')
    path = source_local_path(source)
    if path is None:
        raise SourceScopeOcrError('The original PDF file could not be found, so scanned content cannot be recognized.')
    text = extract_pdf_pages_text(path, page_start=scope.page_start, page_end=scope.page_end_inclusive, max_pages=scope.page_count)
    if not text or not has_usable_source_text(text):
        raise SourceScopeOcrError(f'No usable content was obtained after attempting to recognize the selected range ({scope.page_range}).')
    normalized_text = _normalize_ocr_text(text, chapter=chapter, following_chapter=following_chapter)
    if not has_usable_source_text(normalized_text):
        raise SourceScopeOcrError(f'The selected range ({scope.page_range}) was recognized, but the chapter content boundaries could not be located.')
    return [RetrievalEvidence(source_ingestion_id=source.id, open_notebook_source_id=source.open_notebook_source_id, source_title=source.title, source_uri=source.source_uri, chapter_id=chapter.id if chapter is not None else '', section_path=chapter.path if chapter is not None else [], page_range=scope.page_range, excerpt=_compact_text(normalized_text, 360), expanded_text=normalized_text, relevance_score=chapter.confidence if chapter is not None else 1.0, reason='The native text layer is empty. On-demand OCR was performed on the explicitly selected PDF page range.', token_count=_estimate_tokens(normalized_text), metadata={'retrieval_mode': 'on_demand_pdf_ocr', 'page_start': scope.page_start, 'page_end': scope.page_end_exclusive, 'source_locator': chapter.source_locator if chapter is not None else ''})]

def _is_pdf(source: SourceIngestionRecord) -> bool:
    return source.mime_type.lower() == 'application/pdf' or source.file_name.lower().endswith('.pdf')

def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)

def _normalize_ocr_text(text: str, *, chapter: SourceChapter | None, following_chapter: SourceChapter | None) -> str:
    lines = _dedupe_contained_lines(text.splitlines())
    normalized = '\n'.join(lines).strip()
    if chapter is None:
        return normalized
    start = _chapter_heading_offset(normalized, chapter)
    if start is not None:
        normalized = normalized[start:]
    if following_chapter is not None:
        end = _chapter_heading_offset(normalized, following_chapter)
        if end is not None and end > 0:
            normalized = normalized[:end]
    return normalized.strip()

def _chapter_heading_offset(text: str, chapter: SourceChapter) -> int | None:
    numbers = tuple(dict.fromkeys((value.strip() for value in (chapter.normalized_number, chapter.number) if value and value.strip())))
    for number in numbers:
        match = re.search(f'(?m)^\\s*{re.escape(number)}(?:\\s|$)', text)
        if match is not None:
            return match.start()
    title = re.sub('\\s+', ' ', chapter.title).strip()
    if title:
        match = re.search(f'(?m)^\\s*{re.escape(title)}\\s*$', text)
        if match is not None:
            return match.start()
    return None

def _dedupe_contained_lines(lines: list[str]) -> list[str]:
    result: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if result:
            previous = result[-1]
            compact_line = re.sub('\\s+', '', line)
            compact_previous = re.sub('\\s+', '', previous)
            if min(len(compact_line), len(compact_previous)) >= 8 and (compact_line in compact_previous or compact_previous in compact_line):
                if len(compact_line) > len(compact_previous):
                    result[-1] = line
                continue
            shared_suffix = _shared_suffix_length(compact_previous, compact_line)
            if shared_suffix >= 8 and len(compact_line) <= len(compact_previous):
                continue
        result.append(line)
    return result

def _shared_suffix_length(left: str, right: str) -> int:
    matched = 0
    for left_char, right_char in zip(reversed(left), reversed(right), strict=False):
        if left_char != right_char:
            break
        matched += 1
    return matched

def _compact_text(text: str, limit: int) -> str:
    compact = ' '.join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:max(1, limit - 1)].rstrip() + '…'
