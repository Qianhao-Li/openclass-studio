from __future__ import annotations
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping
from openclass_core.models import EvidenceBundle, Lesson, SourceIngestionRecord
from openclass_core.services.lesson_package_format import RidocSourcePage
from openclass_core.services.source_evidence_store import SourceEvidenceStore
from openclass_core.services.source_ingestion_service import source_download_path
RIDOC_SOURCE_PAGE_RENDER_SCALE = 2.0
RIDOC_MAX_REFERENCED_SOURCE_PAGES = 512
_PAGE_RANGE_RE = re.compile('^\\s*(\\d+)\\s*(?:[-\\u2013\\u2014]\\s*(\\d+))?\\s*$')

@dataclass
class _SourcePageRequest:
    source_ingestion_id: str
    source_title: str = ''
    package_ids: list[str] = field(default_factory=list)
    page_numbers: set[int] = field(default_factory=set)
    reference_ids_by_page: dict[int, set[str]] = field(default_factory=dict)
    expected_hashes_by_page: dict[int, set[str]] = field(default_factory=dict)

    def add_pages(self, *, start: int | None, end: int | None, reference_id: str='', expected_source_hash: str='') -> None:
        if start is None or end is None or start < 1 or (end < start):
            return
        for page_number in range(start, end + 1):
            self.page_numbers.add(page_number)
            if reference_id:
                self.reference_ids_by_page.setdefault(page_number, set()).add(reference_id)
            normalized_hash = expected_source_hash.strip().lower()
            if re.fullmatch('[0-9a-f]{64}', normalized_hash):
                self.expected_hashes_by_page.setdefault(page_number, set()).add(normalized_hash)

def collect_referenced_source_pages(*, owner_user_id: str, package_id: str, lesson: Lesson, evidence_bundles: Iterable[EvidenceBundle], evidence_store: SourceEvidenceStore) -> tuple[list[RidocSourcePage], list[str]]:
    bundles = list(evidence_bundles)
    requests = _referenced_page_requests(lesson=lesson, package_id=package_id, evidence_bundles=bundles)
    requested_count = sum((len(request.page_numbers) for request in requests.values()))
    if requested_count > RIDOC_MAX_REFERENCED_SOURCE_PAGES:
        return ([], ['Referenced source pages were not embedded because the page count exceeds the RIDOC limit.'])
    pages: list[RidocSourcePage] = []
    warnings: list[str] = []
    for request in sorted(requests.values(), key=lambda item: item.source_ingestion_id):
        source = _find_source(owner_user_id=owner_user_id, request=request, evidence_store=evidence_store)
        if source is None:
            warnings.append(f'Referenced source {request.source_ingestion_id} was unavailable for page embedding.')
            continue
        path = source_download_path(source)
        if path is None:
            warnings.append(f'Referenced source {request.source_ingestion_id} has no available original file for page embedding.')
            continue
        if not _is_pdf_source(source, path):
            warnings.append(f'Referenced source {request.source_ingestion_id} is not a renderable paginated PDF.')
            continue
        rendered, render_warnings = _render_pdf_pages(source=source, path=path, page_numbers=sorted(request.page_numbers), reference_ids_by_page=request.reference_ids_by_page, expected_hashes_by_page=request.expected_hashes_by_page, source_title=request.source_title)
        pages.extend(rendered)
        warnings.extend(render_warnings)
    return (pages, warnings)

def _referenced_page_requests(*, lesson: Lesson, package_id: str, evidence_bundles: list[EvidenceBundle]) -> dict[str, _SourcePageRequest]:
    requests: dict[str, _SourcePageRequest] = {}

    def request_for(source_id: str, *, source_title: str='', candidate_package_id: str='') -> _SourcePageRequest | None:
        normalized_id = source_id.strip()
        if not normalized_id:
            return None
        request = requests.setdefault(normalized_id, _SourcePageRequest(source_ingestion_id=normalized_id))
        if source_title.strip() and (not request.source_title):
            request.source_title = source_title.strip()
        for value in (candidate_package_id, package_id):
            normalized_package_id = value.strip()
            if normalized_package_id and normalized_package_id not in request.package_ids:
                request.package_ids.append(normalized_package_id)
        return request
    for commit in lesson.history_graph.commits:
        selection = commit.metadata.get('selection')
        if isinstance(selection, dict) and selection.get('kind') == 'source':
            request = request_for(str(selection.get('source_ingestion_id') or ''), source_title=str(selection.get('source_title') or ''))
            if request is not None:
                start, end = _selection_page_range(selection)
                request.add_pages(start=start, end=end, reference_id=commit.id, expected_source_hash=str(selection.get('source_content_hash') or ''))
        runtime = commit.runtime_snapshot
        requirement = runtime.learning_requirements if runtime is not None else None
        grounding = requirement.source_grounding if requirement is not None else None
        if grounding is None:
            continue
        for reference in grounding.confirmed_references:
            request = request_for(reference.source_ingestion_id, source_title=reference.source_title)
            if request is not None:
                request.add_pages(start=reference.page_start, end=reference.page_end, reference_id=reference.evidence_bundle_id, expected_source_hash=reference.content_hash)
    for bundle in evidence_bundles:
        for evidence in bundle.evidence_items:
            request = request_for(evidence.source_ingestion_id, source_title=evidence.source_title, candidate_package_id=bundle.package_id)
            if request is None:
                continue
            start, end = _evidence_page_range(evidence.metadata, evidence.page_range)
            request.add_pages(start=start, end=end, reference_id=evidence.id, expected_source_hash=str(evidence.metadata.get('source_content_hash') or ''))
        for visual in bundle.visual_items:
            request = request_for(visual.source_ingestion_id, candidate_package_id=bundle.package_id)
            if request is not None:
                request.add_pages(start=visual.page_start, end=visual.page_end, reference_id=visual.visual_id, expected_source_hash=str(visual.metadata.get('source_content_hash') or ''))
    return {key: value for key, value in requests.items() if value.page_numbers}

def _selection_page_range(selection: Mapping[str, Any]) -> tuple[int | None, int | None]:
    source_range = selection.get('source_range')
    if isinstance(source_range, dict) and source_range.get('kind') == 'pdf_pages':
        return (_positive_int(source_range.get('start')), _positive_int(source_range.get('end')))
    start = _positive_int(selection.get('source_page_start'))
    end = _positive_int(selection.get('source_page_end'))
    if start is not None and end is not None:
        return (start, end)
    return _parse_page_range(str(selection.get('source_page_range') or ''))

def _evidence_page_range(metadata: Mapping[str, Any], page_range: str) -> tuple[int | None, int | None]:
    source_range = metadata.get('source_range')
    if isinstance(source_range, dict) and source_range.get('kind') == 'pdf_pages':
        return (_positive_int(source_range.get('start')), _positive_int(source_range.get('end')))
    return _parse_page_range(page_range)

def _parse_page_range(value: str) -> tuple[int | None, int | None]:
    match = _PAGE_RANGE_RE.match(value)
    if match is None:
        return (None, None)
    start = int(match.group(1))
    end = int(match.group(2) or start)
    return (start, end) if end >= start else (None, None)

def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and (not isinstance(value, bool)) and (value > 0) else None

def _find_source(*, owner_user_id: str, request: _SourcePageRequest, evidence_store: SourceEvidenceStore) -> SourceIngestionRecord | None:
    for candidate_package_id in request.package_ids:
        source = evidence_store.get_source(owner_user_id=owner_user_id, package_id=candidate_package_id, source_id=request.source_ingestion_id)
        if source is not None:
            return source
    return None

def _is_pdf_source(source: SourceIngestionRecord, path: Path) -> bool:
    mime_type = source.mime_type.split(';', 1)[0].strip().lower()
    return mime_type == 'application/pdf' or path.suffix.lower() == '.pdf'

def _render_pdf_pages(*, source: SourceIngestionRecord, path: Path, page_numbers: list[int], reference_ids_by_page: Mapping[int, set[str]], expected_hashes_by_page: Mapping[int, set[str]], source_title: str) -> tuple[list[RidocSourcePage], list[str]]:
    try:
        from openclass_core.services import pdf_backend as fitz
    except Exception:
        return ([], ['Referenced PDF pages could not be embedded because PyMuPDF is unavailable.'])
    try:
        document = fitz.open(str(path))
    except Exception:
        return ([], [f'Referenced source {source.id} could not be opened for page embedding.'])
    rendered: list[RidocSourcePage] = []
    warnings: list[str] = []
    source_hash = _file_sha256(path)
    try:
        with document:
            for page_number in page_numbers:
                expected_hashes = expected_hashes_by_page.get(page_number, set())
                if expected_hashes and any((value != source_hash for value in expected_hashes)):
                    warnings.append(f'Referenced source {source.id} page {page_number} was not embedded because the original file changed.')
                    continue
                if page_number > document.page_count:
                    warnings.append(f'Referenced source {source.id} page {page_number} exceeds the PDF page count.')
                    continue
                try:
                    page = document[page_number - 1]
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(RIDOC_SOURCE_PAGE_RENDER_SCALE, RIDOC_SOURCE_PAGE_RENDER_SCALE), alpha=False)
                    content = pixmap.tobytes('png')
                except Exception:
                    warnings.append(f'Referenced source {source.id} page {page_number} could not be rendered.')
                    continue
                rendered.append(RidocSourcePage(source_ingestion_id=source.id, source_title=source_title or source.title, source_file_name=Path(source.file_name or path.name).name, source_mime_type=source.mime_type or 'application/pdf', source_content_hash=source_hash, page_number=page_number, mime_type='image/png', content=content, width=int(pixmap.width), height=int(pixmap.height), reference_ids=tuple(sorted(reference_ids_by_page.get(page_number, set())))))
    except Exception:
        return ([], [f'Referenced source {source.id} failed during page embedding.'])
    return (rendered, warnings)

def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()
