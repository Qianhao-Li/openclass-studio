from __future__ import annotations
from openclass_core.services.account_source_library import source_accessible
import csv
import hashlib
import json
import posixpath
import re
import subprocess
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from openclass_core.models import RetrievalEvidence, SelectionRef, SourceChapter, SourceIngestionRecord, SourceRange
from openclass_core.services.image_ocr import extract_pdf_pages_layout, extract_pdf_pages_text, ordered_ocr_lines
from openclass_core.services.source_archive import SafeSourceArchive
from openclass_core.services.source_document_toolchain import SourceDocumentToolchainError, resolve_poppler_root
from openclass_core.services.source_ooxml_navigation import OoxmlNavigationError, ordered_pptx_slide_parts, read_docx_paragraph_blocks
from openclass_core.services.source_xml import parse_untrusted_xml
from openclass_core.services.pdf_page_visual_input import PdfPageVisualInputError, PdfPageVisualReader
CATALOG_PIPELINES = frozenset({'native_directory_v1', 'hybrid_directory_v1', 'ai_directory_v1', 'codex_directory_v1'})
MAX_PDF_AI_INPUT_BYTES = 40 * 1024 * 1024
MAX_PDF_AI_PAGE_BYTES = 20 * 1024 * 1024
PDF_NATIVE_BATCH_PAGES = 12
PDF_OCR_BATCH_PAGES = 8
PDF_NATIVE_TEXT_TIMEOUT_SECONDS = 45
TEXT_BATCH_CHARS = 48000
TEXT_BATCH_UNITS = 400
_MEANINGFUL_TEXT_RE = re.compile('[A-Za-z0-9\\u3400-\\u9fff]')

class SourceRangeReadError(RuntimeError):
    """Raised when a catalog range cannot be authenticated or read safely."""

@dataclass(frozen=True)
class SourceRangeReadResult:
    evidence_items: list[RetrievalEvidence]
    catalog_version: int
    source_content_hash: str
    source_range: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    source_path: Path | None = None
    image_inputs: list[str] = field(default_factory=list)
    image_manifest: list[dict[str, Any]] = field(default_factory=list)
    preserve_image_bytes: bool = False
    next_page_offset: int | None = None
    complete: bool = True

@dataclass(frozen=True)
class AuthenticatedSourceTextUnit:
    text: str
    locator: str
    display_label: str
    range_index: int = 0
    page_start: int | None = None
    page_end: int | None = None

@dataclass(frozen=True)
class AuthenticatedSourceRangeReadResult:
    units: list[AuthenticatedSourceTextUnit]
    source_content_hash: str
    warnings: list[str] = field(default_factory=list)

@dataclass(frozen=True)
class _ReadUnit:
    text: str
    locator: str
    display_label: str
    start: int | str | None = None
    end: int | str | None = None
    page_start: int | None = None
    page_end: int | None = None
    mode: str = 'native_range_read'

def is_on_demand_directory_catalog(structure: Any) -> bool:
    metadata = _metadata(structure)
    return bool(str(getattr(structure, 'strategy', '') or '') in CATALOG_PIPELINES or str(metadata.get('catalog_pipeline') or '') in CATALOG_PIPELINES)

def read_verified_source_range(*, owner_user_id: str, package_id: str, source: SourceIngestionRecord, structure: Any, chapter: SourceChapter, selection: SelectionRef) -> SourceRangeReadResult:
    """Read only the authoritative catalog range selected by the learner.

    The caller supplies objects already loaded through the authenticated stores,
    but this boundary deliberately validates every identity, catalog and file
    invariant again before opening the source. It never writes a global chunk,
    vector or visual index.
    """
    path, source_hash, authoritative_range, catalog_version = _authorize_selected_source_range(owner_user_id=owner_user_id, package_id=package_id, source=source, structure=structure, chapter=chapter, selection=selection)
    try:
        units, warnings = _read_range(path, source=source, source_range=authoritative_range)
    except SourceRangeReadError:
        raise
    except Exception as exc:
        raise SourceRangeReadError('Could not read the selected source range. Check that the file is still available.') from exc
    if _file_sha256(path) != source_hash:
        raise SourceRangeReadError('The source file changed while it was being read. Rebuild the catalog.')
    units = [unit for unit in units if _has_any_source_text(unit.text)]
    if not units:
        raise SourceRangeReadError('No readable content was found in the selected source range.')
    evidence = _evidence_from_units(source=source, chapter=chapter, units=units, source_range=authoritative_range, catalog_version=catalog_version, source_content_hash=source_hash, warnings=warnings)
    if not is_pdf_source(source):
        _seal_original_range_evidence(evidence, owner_user_id=owner_user_id, package_id=package_id, source=source, source_hash=source_hash, catalog_version=catalog_version, source_range=authoritative_range)
    return SourceRangeReadResult(evidence_items=evidence, catalog_version=catalog_version, source_content_hash=source_hash, source_range=authoritative_range, warnings=warnings, source_path=path)

def _authorize_selected_source_range(*, owner_user_id: str, package_id: str, source: SourceIngestionRecord, structure: Any, chapter: SourceChapter, selection: SelectionRef, require_catalog_pipeline: bool=True) -> tuple[Path, str, dict[str, Any], int]:
    if require_catalog_pipeline and (not is_on_demand_directory_catalog(structure)):
        raise SourceRangeReadError('This source does not use an on-demand reading catalog and cannot use this range reader.')
    _validate_identity(owner_user_id=owner_user_id, package_id=package_id, source=source, structure=structure, chapter=chapter, selection=selection)
    authoritative_range = _range_payload(getattr(chapter, 'range', None))
    selected_range = _range_payload(getattr(selection, 'source_range', None))
    if not authoritative_range or not selected_range:
        raise SourceRangeReadError('This chapter reference has no authoritative source range. Select it from the catalog again.')
    if _range_identity(authoritative_range) != _range_identity(selected_range):
        raise SourceRangeReadError('This chapter reference has an outdated or modified source range. Select it again.')
    _validate_range(authoritative_range)
    catalog_version = _catalog_version(structure)
    selection_version = _positive_int(getattr(selection, 'catalog_version', None))
    chapter_version = _positive_int(getattr(chapter, 'catalog_version', None))
    if catalog_version < 1 or selection_version != catalog_version:
        raise SourceRangeReadError('This catalog reference is outdated. Select the chapter from the source catalog again.')
    if chapter_version and chapter_version != catalog_version:
        raise SourceRangeReadError('The chapter catalog version does not match the current source catalog. Rebuild the catalog.')
    source_hash = _verified_content_hash(source=source, structure=structure, chapter=chapter, selection=selection)
    from openclass_core.services.source_catalog_quality import chapter_range_issue
    issue = chapter_range_issue(source, structure, chapter)
    if issue:
        raise SourceRangeReadError(issue)
    path = _source_path(source)
    if path is None:
        raise SourceRangeReadError('The original source file could not be found, so the selected chapter cannot be read.')
    actual_hash = _file_sha256(path)
    if not actual_hash or actual_hash != source_hash:
        raise SourceRangeReadError('The source content has changed and the old catalog reference is outdated. Rebuild the catalog.')
    return (path, source_hash, authoritative_range, catalog_version)

def is_pdf_source(source: SourceIngestionRecord) -> bool:
    return Path(source.file_name or '').suffix.lower() == '.pdf' or source.mime_type.split(';', 1)[0].strip().lower() == 'application/pdf'

def read_source_range_for_ai(*, owner_user_id: str, package_id: str, source: SourceIngestionRecord, structure: Any, chapter: SourceChapter | None, selection: SelectionRef, is_cancelled: Callable[[], bool] | None=None, page_offset: int | None=None, pages_per_batch: int=6) -> SourceRangeReadResult:
    """Prepare original PDF page images for the final role, never derived text.

    Non-PDF chapter readers retain their existing behavior. The legacy/UI PDF
    reader remains separate and is never called by this PDF AI input boundary.
    Image payloads are transient; only page identities belong in stored evidence.
    """
    if is_cancelled is not None and is_cancelled():
        raise SourceRangeReadError('Source page reading was cancelled.')
    if not is_pdf_source(source):
        if chapter is None:
            raise SourceRangeReadError('Select a verified chapter before reading this source.')
        return read_verified_source_range(owner_user_id=owner_user_id, package_id=package_id, source=source, structure=structure, chapter=chapter, selection=selection)
    if chapter is not None:
        path, source_hash, source_range, catalog_version = _authorize_selected_source_range(owner_user_id=owner_user_id, package_id=package_id, source=source, structure=structure, chapter=chapter, selection=selection, require_catalog_pipeline=False)
        if source_range['kind'] != 'pdf_pages':
            raise SourceRangeReadError('This PDF chapter has no verified physical page range. Rebuild the catalog.')
        if any((key in source_range.get('metadata', {}) for key in ('native_boundary_points', 'basic_unit_start', 'basic_unit_end', 'basic_start_char', 'basic_end_char'))):
            raise SourceRangeReadError("This reader cannot deliver the chapter's precise native boundary. Use the positioned original reader or explicitly select physical pages.")
    else:
        if not owner_user_id or not package_id:
            raise SourceRangeReadError('This source reference is missing its course or user identity.')
        if selection.kind != 'source' or selection.source_ingestion_id != source.id or selection.source_chapter_id or (selection.source_scope_kind not in {'source', 'page_range'}):
            raise SourceRangeReadError('This source reference is missing explicit file or page-range authorization.')
        catalog_version = _catalog_version(structure)
        if catalog_version < 1 or selection.catalog_version != catalog_version:
            raise SourceRangeReadError('This catalog reference is outdated. Select the source range again.')
        path, source_hash = _authenticated_structure_source_path(owner_user_id=owner_user_id, package_id=package_id, source=source, structure=structure)
        if (selection.source_content_hash or '').strip().lower() != source_hash:
            raise SourceRangeReadError('The source file fingerprint does not match the reference. Select it again.')
        source_range = _range_payload(selection.source_range)
        if selection.source_scope_kind == 'page_range' and (not source_range):
            raise SourceRangeReadError('Select an explicit physical PDF page range.')
        if not source_range and (selection.source_page_start is not None or selection.source_page_end is not None):
            raise SourceRangeReadError('This PDF page reference has no explicitly authorized range. Select it again.')
        if source_range:
            _validate_range(source_range)
            if source_range['kind'] != 'pdf_pages':
                raise SourceRangeReadError('PDF references must use physical page ranges.')
    if source_range:
        for field_name, range_key in (('source_page_start', 'start'), ('source_page_end', 'end')):
            submitted = getattr(selection, field_name)
            if submitted is not None and submitted != source_range[range_key]:
                raise SourceRangeReadError('The PDF reference pages do not match the authorized range. Select them again.')
    images: list[str] = []
    manifest: list[dict[str, Any]] = []
    evidence: list[RetrievalEvidence] = []
    total_bytes = 0
    next_page_offset = None
    try:
        with PdfPageVisualReader(path, expected_sha256=source_hash, is_cancelled=is_cancelled) as reader:
            if not source_range:
                source_range = SourceRange(kind='pdf_pages', start=1, end=reader.page_count).model_dump(mode='json')
            start, end = (int(source_range['start']), int(source_range['end']))
            if start < 1 or end > reader.page_count:
                raise SourceRangeReadError('The selected range exceeds the physical PDF pages. Select it again.')
            read_start, read_end = (start, end)
            if page_offset is not None:
                if type(page_offset) is not int or not 0 <= page_offset <= end - start or (not 1 <= pages_per_batch <= 6):
                    raise SourceRangeReadError('The original page continuation is outside its authorized range.')
                read_start = start + page_offset
                read_end = min(end, read_start + pages_per_batch - 1)
            source_range = {key: value for key, value in source_range.items() if key in {'kind', 'start', 'end', 'container', 'start_anchor', 'end_anchor', 'path', 'display_label', 'end_inclusive'}} | {'metadata': {key: value for key, value in source_range.get('metadata', {}).items() if key in {'index_base', 'page_clips', 'boundary_precision', 'boundary_verification_passes', 'boundary_verified_image_hashes', 'end_anchor_exclusive'}}}
            for batch_start in range(read_start, read_end + 1, 6):
                for page_number in range(batch_start, min(read_end + 1, batch_start + 6)):
                    page = reader.render_pages([page_number])[0]
                    page, crop_bbox = _authorized_original_page(page, source_range)
                    encoded = page.data_url.partition(',')[2]
                    size_bytes = len(encoded) * 3 // 4 - (len(encoded) - len(encoded.rstrip('=')))
                    if size_bytes > MAX_PDF_AI_PAGE_BYTES:
                        raise SourceRangeReadError('Original PDF page images exceed the 20 MiB per-page or 40 MiB per-request limit. Select a smaller page range.')
                    if total_bytes + size_bytes > MAX_PDF_AI_INPUT_BYTES:
                        if page_offset is not None and evidence:
                            break
                        raise SourceRangeReadError('Original PDF page images exceed the per-request input limit.')
                    total_bytes += size_bytes
                    page_identity = {'source_transport': 'original_pdf_page_images', 'source_content_hash': source_hash, 'catalog_version': catalog_version, 'source_range': source_range, 'page_start': page.page_number, 'page_end': page.page_number, 'page_image_sha256': page.sha256, **({'crop_bbox': crop_bbox} if crop_bbox else {})}
                    item = RetrievalEvidence(source_ingestion_id=source.id, source_title=source.title, source_uri=source.source_uri, chapter_id=chapter.id if chapter is not None else '', section_path=chapter.path if chapter is not None else [], page_range=_page_label(page.page_number, page.page_number), excerpt='', expanded_text='', token_count=0, reason='Original PDF page images will be sent directly to the AI handling this turn.', metadata=page_identity)
                    manifest.append({'image_index': len(images), 'evidence_id': item.id, 'source_ingestion_id': source.id, 'physical_page': page.page_number, 'sha256': page.sha256, 'width': page.width, 'height': page.height, 'mime_type': page.mime_type, 'size_bytes': size_bytes})
                    images.append(page.data_url)
                    evidence.append(item)
            if page_offset is not None and manifest and (manifest[-1]['physical_page'] < end):
                next_page_offset = manifest[-1]['physical_page'] - start + 1
    except PdfPageVisualInputError as exc:
        if 'limit' in str(exc).lower():
            raise SourceRangeReadError('Original PDF page images exceed the transfer limit. Select a smaller page range; image quality and original content will be preserved.') from exc
        raise SourceRangeReadError(str(exc)) from exc
    if is_cancelled is not None and is_cancelled():
        raise SourceRangeReadError('Source page reading was cancelled.')
    _seal_original_range_evidence(evidence, owner_user_id=owner_user_id, package_id=package_id, source=source, source_hash=source_hash, catalog_version=catalog_version, source_range=source_range)
    for item in manifest:
        item['source_version'] = evidence[item['image_index']].metadata['source_version']
        item['source_content_hash'] = source_hash
    return SourceRangeReadResult(evidence_items=evidence, catalog_version=catalog_version, source_content_hash=source_hash, source_range=source_range, source_path=path, image_inputs=images, image_manifest=manifest, preserve_image_bytes=True, next_page_offset=next_page_offset, complete=next_page_offset is None)

def _authorized_original_page(page, source_range):
    """Apply the exact same verified pixel boundary as the cloud original reader."""
    from openclass_core.services.source_content_reader import _anchor_clip
    from openclass_core.services.google_sources.config import GoogleSourceError
    try:
        crop = _anchor_clip({}, source_range, page.page_number, page_sha256=page.sha256)
    except GoogleSourceError as exc:
        raise SourceRangeReadError(str(exc)) from exc
    if crop is None:
        return (page, None)
    import base64
    import io
    import math
    from PIL import Image
    with Image.open(io.BytesIO(base64.b64decode(page.data_url.partition(',')[2]))) as original:
        box = (math.ceil(crop[0] * original.width), math.ceil(crop[1] * original.height), math.floor(crop[2] * original.width), math.floor(crop[3] * original.height))
        if box[2] <= box[0] or box[3] <= box[1]:
            raise SourceRangeReadError('The verified original boundary contains no readable pixels.')
        image = original.crop(box)
        output = io.BytesIO()
        image.save(output, format='PNG')
        raw = output.getvalue()
        return (replace(page, data_url='data:image/png;base64,' + base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest(), width=image.width, height=image.height), crop)

def _seal_original_range_evidence(evidence, *, owner_user_id, package_id, source, source_hash, catalog_version, source_range):
    """Only call after this reader's owner/range and before/after byte checks.

    Legacy originals use the same authenticated receipt contract as cloud
    originals. Search/chunk results never pass through this boundary.
    """
    from openclass_core.models import new_id
    from openclass_core.services.source_content_contract import issue_read_receipt
    version = str(source.metadata.get('source_version') or f'original:{source_hash}:catalog:{catalog_version}')
    for item in evidence:
        image_digest = item.metadata.get('page_image_sha256')
        item.metadata.update(source_version=version, source_content_hash=source_hash, provenance={**item.metadata.get('provenance', {}), 'reader': 'authenticated_original_range'}, source_read_request=source_range, read_contract='source_read_v1', input_kind='image' if image_digest else 'text', content_unit_sha256=image_digest or hashlib.sha256(item.expanded_text.encode()).hexdigest())
    issue_read_receipt(items=evidence, owner_user_id=owner_user_id, package_id=package_id, source_id=source.id, source_version=version, content_hash=source_hash, source_range=source_range, authorized_range=source_range, authorization='explicit', grant_id=new_id('originalread'), role='original_range_reader')

def iter_source_range_for_ai(**kwargs):
    """Fresh authorized PDF reads with bounded images and no total page limit."""
    if not is_pdf_source(kwargs['source']):
        yield read_source_range_for_ai(**kwargs)
        return
    offset = 0
    while True:
        result = read_source_range_for_ai(**kwargs, page_offset=offset)
        yield result
        if result.next_page_offset is None:
            return
        if result.next_page_offset <= offset:
            raise SourceRangeReadError('The original page reader did not advance within its authorized range.')
        offset = result.next_page_offset

def source_coordinate_extent(*, owner_user_id: str, package_id: str, source: SourceIngestionRecord, structure: Any, kind: str, container: str='') -> SourceRange:
    """Return the complete authenticated coordinate space for one original source.

    Publication review uses this boundary to calculate the complement of verified
    body ranges without trusting model-provided file sizes or opening arbitrary paths.
    """
    path, _source_hash = _authenticated_structure_source_path(owner_user_id=owner_user_id, package_id=package_id, source=source, structure=structure)
    if kind == 'pdf_pages':
        try:
            from pypdf import PdfReader
        except Exception as exc:
            raise SourceRangeReadError('The PDF reader is not installed in this environment.') from exc
        count = len(PdfReader(str(path)).pages)
        start, end = (1, count)
    elif kind == 'epub_spine':
        with SafeSourceArchive(path) as archive:
            count = len(_epub_spine_items(archive))
        start, end = (0, count - 1)
    elif kind == 'docx_paragraphs':
        try:
            count = len(read_docx_paragraph_blocks(path))
        except OoxmlNavigationError as exc:
            raise SourceRangeReadError('Could not read the native paragraph order of this DOCX.') from exc
        start, end = (0, count - 1)
    elif kind == 'ppt_slides':
        with SafeSourceArchive(path) as archive:
            try:
                count = len(ordered_pptx_slide_parts(archive))
            except OoxmlNavigationError as exc:
                raise SourceRangeReadError('Could not verify the native slide order of this PPTX.') from exc
        start, end = (1, count)
    elif kind == 'sheet_rows':
        if path.suffix.lower() == '.csv':
            with path.open('r', encoding='utf-8-sig', errors='replace', newline='') as handle:
                count = sum((1 for _row in csv.reader(handle)))
        else:
            with SafeSourceArchive(path) as archive:
                names = set(archive.namelist())
                _sheet_name, sheet_path = _xlsx_sheet_path(archive, names, container)
                root = parse_untrusted_xml(archive.read(sheet_path))
                implicit_row = 0
                count = 0
                for row in (node for node in root.iter() if node.tag.endswith('}row')):
                    implicit_row += 1
                    count = max(count, _positive_int(row.attrib.get('r')) or implicit_row)
        start, end = (1, count)
    elif kind == 'text_lines':
        with path.open('r', encoding='utf-8', errors='replace') as handle:
            count = sum((1 for _line in handle))
        start, end = (1, count)
    else:
        raise SourceRangeReadError('Full-document boundaries cannot be calculated for this source range type.')
    if count < 1 or end < start:
        raise SourceRangeReadError('The original file has no content range available for publication review.')
    return SourceRange(kind=kind, start=start, end=end, container=container, display_label=f'{kind}:{start}-{end}')

def read_authenticated_source_ranges(*, owner_user_id: str, package_id: str, source: SourceIngestionRecord, structure: Any, ranges: Sequence[SourceRange]) -> AuthenticatedSourceRangeReadResult:
    """Read bounded ranges from the fingerprinted original file without indexing it."""
    path, source_hash = _authenticated_structure_source_path(owner_user_id=owner_user_id, package_id=package_id, source=source, structure=structure)
    units: list[AuthenticatedSourceTextUnit] = []
    warnings: list[str] = []
    for range_index, source_range in enumerate(ranges):
        payload = _range_payload(source_range)
        _validate_range(payload)
        raw_units, range_warnings = _read_range(path, source=source, source_range=payload)
        warnings.extend(range_warnings)
        units.extend((AuthenticatedSourceTextUnit(text=unit.text, locator=unit.locator, display_label=unit.display_label, range_index=range_index, page_start=unit.page_start, page_end=unit.page_end) for unit in raw_units if _has_any_source_text(unit.text)))
    if _file_sha256(path) != source_hash:
        raise SourceRangeReadError('The source file changed while it was being read for review.')
    return AuthenticatedSourceRangeReadResult(units=units, source_content_hash=source_hash, warnings=list(dict.fromkeys(warnings)))

def _authenticated_structure_source_path(*, owner_user_id: str, package_id: str, source: SourceIngestionRecord, structure: Any) -> tuple[Path, str]:
    if not source_accessible(source, owner_user_id, package_id):
        raise SourceRangeReadError('You do not have permission to read this source.')
    if source.status != 'ready':
        raise SourceRangeReadError('This source is not ready for review yet.')
    if str(getattr(structure, 'owner_user_id', '') or '') != owner_user_id or str(getattr(structure, 'package_id', '') or '') != source.package_id or str(getattr(structure, 'source_ingestion_id', '') or '') != source.id:
        raise SourceRangeReadError('The source catalog does not match the current file identity.')
    source_hash = _content_hash(structure)
    metadata_hash = str(source.metadata.get('content_hash') or '').strip().lower()
    if not _is_sha256(source_hash) or (metadata_hash and metadata_hash != source_hash):
        raise SourceRangeReadError('This source has no verifiable file fingerprint.')
    path = _source_path(source)
    if path is None:
        raise SourceRangeReadError('The original source file could not be found.')
    if _file_sha256(path) != source_hash:
        raise SourceRangeReadError('The source content has changed. The old catalog is no longer available.')
    return (path, source_hash)

def _validate_identity(*, owner_user_id: str, package_id: str, source: SourceIngestionRecord, structure: Any, chapter: SourceChapter, selection: SelectionRef) -> None:
    if not owner_user_id or not package_id:
        raise SourceRangeReadError('This source reference is missing its course or user identity.')
    if not source_accessible(source, owner_user_id, package_id):
        raise SourceRangeReadError('You do not have permission to read this source.')
    if source.status != 'ready':
        raise SourceRangeReadError('This source is not ready to read yet.')
    if str(getattr(structure, 'owner_user_id', '') or '') != owner_user_id or str(getattr(structure, 'package_id', '') or '') != source.package_id or str(getattr(structure, 'source_ingestion_id', '') or '') != source.id:
        raise SourceRangeReadError('The source catalog does not match the current file identity.')
    if chapter.owner_user_id != owner_user_id or chapter.package_id != source.package_id or chapter.source_ingestion_id != source.id:
        raise SourceRangeReadError('The chapter catalog does not match the current file identity.')
    if selection.kind != 'source' or selection.source_ingestion_id != source.id:
        raise SourceRangeReadError('The source reference does not match the current file identity.')
    if not selection.source_chapter_id or selection.source_chapter_id != chapter.id:
        raise SourceRangeReadError('The chapter identifier in this source reference is outdated or modified.')
    mapping_status = str(getattr(chapter, 'mapping_status', '') or '')
    if mapping_status != 'verified':
        raise SourceRangeReadError('The content range for this catalog entry has not been verified and cannot be cited yet.')

def _verified_content_hash(*, source: SourceIngestionRecord, structure: Any, chapter: SourceChapter, selection: SelectionRef) -> str:
    structure_hash = _content_hash(structure)
    chapter_hash = str(getattr(chapter, 'source_content_hash', '') or '').strip().lower()
    selection_hash = str(getattr(selection, 'source_content_hash', '') or '').strip().lower()
    values = [structure_hash, chapter_hash, selection_hash]
    if any((not _is_sha256(value) for value in values)):
        raise SourceRangeReadError('This catalog reference has no verifiable file fingerprint. Rebuild the catalog.')
    if len(set(values)) != 1:
        raise SourceRangeReadError('The source file fingerprint does not match the catalog reference. Select the chapter again.')
    source_metadata_hash = str(source.metadata.get('content_hash') or '').strip().lower()
    if source_metadata_hash and source_metadata_hash != structure_hash:
        raise SourceRangeReadError('The source file fingerprint has changed. Rebuild the catalog.')
    return structure_hash

def _content_hash(value: Any) -> str:
    direct = str(getattr(value, 'source_content_hash', '') or '').strip().lower()
    if direct:
        return direct
    return str(_metadata(value).get('source_content_hash') or '').strip().lower()

def _catalog_version(structure: Any) -> int:
    direct = _positive_int(getattr(structure, 'catalog_version', None))
    return direct or _positive_int(_metadata(structure).get('catalog_version'))

def _range_payload(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        payload = dict(value)
    elif hasattr(value, 'model_dump'):
        payload = dict(value.model_dump(mode='json'))
    else:
        return {}
    payload['kind'] = str(payload.get('kind') or '').strip()
    payload['container'] = str(payload.get('container') or '').strip()
    payload['start_anchor'] = str(payload.get('start_anchor') or '').strip()
    payload['end_anchor'] = str(payload.get('end_anchor') or '').strip()
    payload['path'] = [str(part) for part in payload.get('path') or []]
    payload['display_label'] = str(payload.get('display_label') or '').strip()
    payload['end_inclusive'] = bool(payload.get('end_inclusive', True))
    payload['metadata'] = dict(payload.get('metadata') or {})
    return payload

def _range_identity(payload: dict[str, Any]) -> tuple[Any, ...]:
    metadata = dict(payload.get('metadata') or {})
    return (payload.get('kind'), _normalized_endpoint(payload.get('start')), _normalized_endpoint(payload.get('end')), payload.get('container'), payload.get('start_anchor'), payload.get('end_anchor'), tuple(payload.get('path') or []), bool(payload.get('end_inclusive', True)), _positive_or_zero_int(metadata.get('index_base')))

def _normalized_endpoint(value: Any) -> int | str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return value
    raw = str(value).strip()
    if re.fullmatch('-?\\d+', raw):
        return int(raw)
    return raw

def _validate_range(payload: dict[str, Any]) -> None:
    kind = str(payload.get('kind') or '')
    supported = {'pdf_pages', 'epub_spine', 'docx_paragraphs', 'ppt_slides', 'sheet_rows', 'text_lines', 'dom_anchor', 'structured_path'}
    if kind not in supported:
        raise SourceRangeReadError('This chapter uses an unsupported source range type.')
    if not bool(payload.get('end_inclusive', True)):
        raise SourceRangeReadError('New catalog ranges must use an inclusive end position.')
    if kind in {'pdf_pages', 'docx_paragraphs', 'ppt_slides', 'sheet_rows', 'text_lines'}:
        start = _strict_int(payload.get('start'))
        end = _strict_int(payload.get('end'))
        minimum = 0 if kind == 'docx_paragraphs' else 1
        if start < minimum or end < start:
            raise SourceRangeReadError('This chapter reference contains invalid start or end positions.')
    elif kind == 'epub_spine':
        start = _normalized_endpoint(payload.get('start'))
        end = _normalized_endpoint(payload.get('end'))
        if start is None or end is None:
            raise SourceRangeReadError('This EPUB chapter reference is missing its spine range.')
        if isinstance(start, int) and (not isinstance(end, int) or start < 0 or end < start):
            raise SourceRangeReadError('This EPUB chapter reference contains an invalid spine range.')
    elif kind == 'dom_anchor':
        metadata = dict(payload.get('metadata') or {})
        heading_ordinal = _positive_or_zero_int(metadata.get('heading_ordinal'))
        if not (payload.get('start_anchor') or payload.get('start') or heading_ordinal is not None or payload.get('container')):
            raise SourceRangeReadError('This web chapter reference has no verifiable document range.')

def _read_range(path: Path, *, source: SourceIngestionRecord, source_range: dict[str, Any]) -> tuple[list[_ReadUnit], list[str]]:
    kind = str(source_range['kind'])
    if kind == 'pdf_pages':
        return _read_pdf_pages(path, source_range)
    if kind == 'epub_spine':
        return (_read_epub_spine(path, source_range), [])
    if kind == 'docx_paragraphs':
        return (_read_docx_paragraphs(path, source_range), [])
    if kind == 'ppt_slides':
        return (_read_ppt_slides(path, source_range), [])
    if kind == 'sheet_rows':
        if path.suffix.lower() == '.csv' or source.mime_type.lower() == 'text/csv':
            return (_read_csv_rows(path, source_range), [])
        return (_read_xlsx_rows(path, source_range), [])
    if kind == 'text_lines':
        return (_read_text_lines(path, source_range), [])
    if kind == 'dom_anchor':
        return (_read_dom_anchor(path, source_range), [])
    if kind == 'structured_path':
        return (_read_structured_path(path, source_range), [])
    raise SourceRangeReadError('This chapter uses an unsupported source range type.')

def _read_pdf_pages(path: Path, source_range: dict[str, Any]) -> tuple[list[_ReadUnit], list[str]]:
    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise SourceRangeReadError('The PDF reader is not installed in this environment.') from exc
    start = _strict_int(source_range.get('start'))
    end = _strict_int(source_range.get('end'))
    reader = PdfReader(str(path))
    page_count = len(reader.pages)
    if end > page_count:
        raise SourceRangeReadError('The chapter page range exceeds the actual page count of this PDF.')
    bounded_native_text = _extract_pdf_native_text(path, page_start=start, page_end=end)
    native_units: dict[int, _ReadUnit] = {}
    missing_pages: list[int] = []
    for page_no in range(start, end + 1):
        if bounded_native_text is not None:
            text = bounded_native_text.get(page_no, '').strip()
        else:
            try:
                text = str(reader.pages[page_no - 1].extract_text() or '').strip()
            except Exception:
                text = ''
        if _has_usable_text(text):
            native_units[page_no] = _ReadUnit(text=text, locator=f'pdf:page:{page_no}', display_label=_page_label(page_no, page_no), start=page_no, end=page_no, page_start=page_no, page_end=page_no, mode='on_demand_pdf_native')
        else:
            missing_pages.append(page_no)
    ocr_units: list[_ReadUnit] = []
    unreadable_pages: list[int] = []
    for batch_start, batch_end in _contiguous_batches(missing_pages, max_batch_size=PDF_OCR_BATCH_PAGES):
        layouts = extract_pdf_pages_layout(path, page_start=batch_start, page_end=batch_end, max_pages=batch_end - batch_start + 1)
        layout_pages: set[int] = set()
        for layout in layouts:
            if layout.page_no < batch_start or layout.page_no > batch_end:
                continue
            text = '\n'.join((line.text for line in ordered_ocr_lines(layout.lines) if line.text.strip())).strip()
            if not _has_usable_text(text):
                continue
            layout_pages.add(layout.page_no)
            ocr_units.append(_ReadUnit(text=text, locator=f'pdf:page:{layout.page_no}', display_label=_page_label(layout.page_no, layout.page_no), start=layout.page_no, end=layout.page_no, page_start=layout.page_no, page_end=layout.page_no, mode='on_demand_pdf_ocr'))
        remaining = [page for page in range(batch_start, batch_end + 1) if page not in layout_pages]
        for fallback_start, fallback_end in _contiguous_batches(remaining, max_batch_size=PDF_OCR_BATCH_PAGES):
            fallback = extract_pdf_pages_text(path, page_start=fallback_start, page_end=fallback_end, max_pages=fallback_end - fallback_start + 1)
            if fallback and _has_usable_text(fallback):
                ocr_units.append(_ReadUnit(text=fallback.strip(), locator=f'pdf:pages:{fallback_start}-{fallback_end}', display_label=_page_label(fallback_start, fallback_end), start=fallback_start, end=fallback_end, page_start=fallback_start, page_end=fallback_end, mode='on_demand_pdf_ocr'))
            else:
                unreadable_pages.extend(range(fallback_start, fallback_end + 1))
    units = [*native_units.values(), *ocr_units]
    units.sort(key=lambda item: int(item.page_start or 0))
    units = _merge_pdf_native_units(units)
    warnings = []
    if unreadable_pages:
        warnings.append('No text was found on these selected physical pages: ' + ', '.join((str(page) for page in unreadable_pages)))
    return (units, warnings)

def _extract_pdf_native_text(path: Path, *, page_start: int, page_end: int) -> dict[int, str] | None:
    """Use one bounded Poppler call before falling back to per-page pypdf parsing."""
    try:
        executable = resolve_poppler_root() / 'bin' / 'pdftotext'
        completed = subprocess.run([str(executable), '-f', str(page_start), '-l', str(page_end), '-layout', '-enc', 'UTF-8', str(path), '-'], check=False, capture_output=True, text=True, timeout=PDF_NATIVE_TEXT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError, SourceDocumentToolchainError):
        return None
    if completed.returncode != 0:
        return None
    page_texts = completed.stdout.split('\x0c')
    expected_page_count = page_end - page_start + 1
    if len(page_texts) < expected_page_count:
        return None
    return {page_start + offset: page_texts[offset] for offset in range(expected_page_count)}

def _merge_pdf_native_units(units: Sequence[_ReadUnit]) -> list[_ReadUnit]:
    merged: list[_ReadUnit] = []
    current: list[_ReadUnit] = []
    for unit in units:
        can_merge = bool(unit.mode == 'on_demand_pdf_native' and (not current or (current[-1].mode == unit.mode and current[-1].page_end is not None and (unit.page_start == current[-1].page_end + 1) and (len(current) < PDF_NATIVE_BATCH_PAGES) and (sum((len(item.text) for item in current)) + len(unit.text) <= TEXT_BATCH_CHARS))))
        if current and (not can_merge):
            merged.append(_merge_pdf_batch(current))
            current = []
        if unit.mode == 'on_demand_pdf_native':
            current.append(unit)
        else:
            if current:
                merged.append(_merge_pdf_batch(current))
                current = []
            merged.append(unit)
    if current:
        merged.append(_merge_pdf_batch(current))
    return sorted(merged, key=lambda item: int(item.page_start or 0))

def _merge_pdf_batch(units: Sequence[_ReadUnit]) -> _ReadUnit:
    first = units[0]
    last = units[-1]
    start = int(first.page_start or 1)
    end = int(last.page_end or start)
    text = '\n\n'.join((f'[PDF physical page {unit.page_start}]\n{unit.text}' for unit in units))
    return _ReadUnit(text=text, locator=f'pdf:pages:{start}-{end}', display_label=_page_label(start, end), start=start, end=end, page_start=start, page_end=end, mode='on_demand_pdf_native')

def _read_docx_paragraphs(path: Path, source_range: dict[str, Any]) -> list[_ReadUnit]:
    try:
        paragraphs = read_docx_paragraph_blocks(path)
    except OoxmlNavigationError as exc:
        raise SourceRangeReadError('Could not read the native paragraph order of this DOCX.') from exc
    start = _strict_int(source_range.get('start'))
    end = _strict_int(source_range.get('end'))
    if end >= len(paragraphs):
        raise SourceRangeReadError('The chapter paragraph range exceeds the actual paragraph count of this DOCX.')
    units = [_ReadUnit(text=paragraph.text.strip(), locator=f'docx:paragraph:{paragraph.index}', display_label=f'paragraphs {paragraph.index}-{paragraph.index}', start=paragraph.index, end=paragraph.index) for paragraph in paragraphs if start <= paragraph.index <= end and paragraph.text.strip()]
    return _batch_text_units(units, prefix='docx:paragraphs')

def _read_ppt_slides(path: Path, source_range: dict[str, Any]) -> list[_ReadUnit]:
    start = _strict_int(source_range.get('start'))
    end = _strict_int(source_range.get('end'))
    units: list[_ReadUnit] = []
    with SafeSourceArchive(path) as archive:
        try:
            slide_names = ordered_pptx_slide_parts(archive)
        except OoxmlNavigationError as exc:
            raise SourceRangeReadError('Could not verify the native slide order of this PPTX.') from exc
        if end > len(slide_names):
            raise SourceRangeReadError('The chapter slide range exceeds the actual slide count of this PPTX.')
        for slide_no in range(start, end + 1):
            name = slide_names[slide_no - 1]
            root = parse_untrusted_xml(archive.read(name))
            texts = [str(node.text or '').strip() for node in root.iter() if node.tag.endswith('}t') and str(node.text or '').strip()]
            if texts:
                units.append(_ReadUnit(text='\n'.join(texts), locator=f'pptx:slide:{slide_no}', display_label=f'slide {slide_no}', start=slide_no, end=slide_no))
    return _batch_text_units(units, prefix='pptx:slides')

def _read_csv_rows(path: Path, source_range: dict[str, Any]) -> list[_ReadUnit]:
    start = _strict_int(source_range.get('start'))
    end = _strict_int(source_range.get('end'))
    units: list[_ReadUnit] = []
    with path.open('r', encoding='utf-8-sig', errors='replace', newline='') as handle:
        for row_no, row in enumerate(csv.reader(handle), start=1):
            if row_no > end:
                break
            if row_no < start:
                continue
            text = '\t'.join((cell.strip() for cell in row)).rstrip()
            if text:
                units.append(_ReadUnit(text=text, locator=f'csv:row:{row_no}', display_label=f'row {row_no}', start=row_no, end=row_no))
    return _batch_text_units(units, prefix='csv:rows')

def _read_xlsx_rows(path: Path, source_range: dict[str, Any]) -> list[_ReadUnit]:
    start = _strict_int(source_range.get('start'))
    end = _strict_int(source_range.get('end'))
    container = str(source_range.get('container') or '').strip()
    with SafeSourceArchive(path) as archive:
        names = set(archive.namelist())
        shared = _xlsx_shared_strings(archive, names)
        sheet_name, sheet_path = _xlsx_sheet_path(archive, names, container)
        root = parse_untrusted_xml(archive.read(sheet_path))
        units: list[_ReadUnit] = []
        implicit_row = 0
        for row in (node for node in root.iter() if node.tag.endswith('}row')):
            implicit_row += 1
            row_no = _positive_int(row.attrib.get('r')) or implicit_row
            if row_no > end:
                break
            if row_no < start:
                continue
            values: list[str] = []
            for cell in (node for node in row if node.tag.endswith('}c')):
                cell_type = cell.attrib.get('t', '')
                value_node = next((node for node in cell.iter() if node.tag.endswith('}v')), None)
                if cell_type == 'inlineStr':
                    value = ''.join((str(node.text or '') for node in cell.iter() if node.tag.endswith('}t')))
                else:
                    value = str(value_node.text or '') if value_node is not None else ''
                    if cell_type == 's' and value.isdigit() and (int(value) < len(shared)):
                        value = shared[int(value)]
                values.append(value.strip())
            text = '\t'.join(values).rstrip()
            if text:
                units.append(_ReadUnit(text=text, locator=f'xlsx:sheet:{sheet_name}:row:{row_no}', display_label=f'{sheet_name} row {row_no}', start=row_no, end=row_no))
    return _batch_text_units(units, prefix=f'xlsx:sheet:{sheet_name}:rows')

def _xlsx_shared_strings(archive: SafeSourceArchive, names: set[str]) -> list[str]:
    if 'xl/sharedStrings.xml' not in names:
        return []
    root = parse_untrusted_xml(archive.read('xl/sharedStrings.xml'))
    return [''.join((str(node.text or '') for node in item.iter() if node.tag.endswith('}t'))) for item in root.iter() if item.tag.endswith('}si')]

def _xlsx_sheet_path(archive: SafeSourceArchive, names: set[str], requested_name: str) -> tuple[str, str]:
    workbook_name = 'xl/workbook.xml'
    rels_name = 'xl/_rels/workbook.xml.rels'
    if workbook_name in names and rels_name in names:
        workbook = parse_untrusted_xml(archive.read(workbook_name))
        rels = parse_untrusted_xml(archive.read(rels_name))
        targets = {str(node.attrib.get('Id') or ''): str(node.attrib.get('Target') or '') for node in rels.iter() if node.tag.endswith('}Relationship')}
        sheets: list[tuple[str, str]] = []
        for node in workbook.iter():
            if not node.tag.endswith('}sheet'):
                continue
            name = str(node.attrib.get('name') or '').strip()
            relation_id = next((str(value) for key, value in node.attrib.items() if key.endswith('}id') or key == 'r:id'), '')
            target = targets.get(relation_id, '')
            if not name or not target:
                continue
            normalized = target.lstrip('/')
            if not normalized.startswith('xl/'):
                normalized = posixpath.normpath(posixpath.join('xl', normalized))
            sheets.append((name, normalized))
        if sheets:
            if requested_name:
                match = next((item for item in sheets if item[0] == requested_name), None)
                if match is None:
                    raise SourceRangeReadError('The worksheet named in the catalog does not exist in this XLSX.')
                return match
            return sheets[0]
    fallback = 'xl/worksheets/sheet1.xml'
    if fallback not in names:
        raise SourceRangeReadError('This XLSX has no readable worksheets.')
    return (requested_name or 'Sheet 1', fallback)

def _read_text_lines(path: Path, source_range: dict[str, Any]) -> list[_ReadUnit]:
    start = _strict_int(source_range.get('start'))
    end = _strict_int(source_range.get('end'))
    units: list[_ReadUnit] = []
    with path.open('r', encoding='utf-8', errors='replace') as handle:
        for line_no, line in enumerate(handle, start=1):
            if line_no > end:
                break
            if line_no < start:
                continue
            text = line.rstrip('\r\n')
            if text.strip():
                units.append(_ReadUnit(text=text, locator=f'text:line:{line_no}', display_label=f'line {line_no}', start=line_no, end=line_no))
    return _batch_text_units(units, prefix='text:lines')

def _read_epub_spine(path: Path, source_range: dict[str, Any]) -> list[_ReadUnit]:
    with SafeSourceArchive(path) as archive:
        spine = _epub_spine_items(archive)
        if not spine:
            raise SourceRangeReadError('This EPUB has no verifiable spine order.')
        container = str(source_range.get('container') or '').strip()
        start_raw = _normalized_endpoint(source_range.get('start'))
        end_raw = _normalized_endpoint(source_range.get('end'))
        if isinstance(start_raw, int) and isinstance(end_raw, int):
            if start_raw < 0 or end_raw >= len(spine):
                raise SourceRangeReadError('The chapter spine range exceeds this EPUB.')
            if container and spine[start_raw] != container:
                raise SourceRangeReadError('The chapter spine container does not match the starting entry in this EPUB.')
            selected_names = spine[start_raw:end_raw + 1]
        elif container:
            if container not in spine:
                raise SourceRangeReadError('The chapter spine container does not exist in this EPUB.')
            selected_names = [container]
        else:
            start_name = str(start_raw or '')
            end_name = str(end_raw or '')
            try:
                start_index = spine.index(start_name)
                end_index = spine.index(end_name)
            except ValueError as exc:
                raise SourceRangeReadError('The chapter spine location does not exist in this EPUB.') from exc
            if end_index < start_index:
                raise SourceRangeReadError('The chapter spine range has an invalid start-to-end order.')
            selected_names = spine[start_index:end_index + 1]
        names = set(archive.namelist())
        units: list[_ReadUnit] = []
        for name_index, name in enumerate(selected_names):
            if name not in names:
                raise SourceRangeReadError('The EPUB document named in the catalog does not exist in this file.')
            parser = _AnchoredTextParser()
            parser.feed(archive.read(name).decode('utf-8', errors='replace'))
            text = parser.selected_text(start_anchor=str(source_range.get('start_anchor') or '') if name_index == 0 else '', end_anchor=str(source_range.get('end_anchor') or '') if name_index == len(selected_names) - 1 else '')
            if text.strip():
                units.append(_ReadUnit(text=text, locator=f'epub:{name}', display_label=name, start=name, end=name))
    return _batch_text_units(units, prefix='epub:spine')

def _epub_spine_items(archive: SafeSourceArchive) -> list[str]:
    try:
        container = parse_untrusted_xml(archive.read('META-INF/container.xml'))
    except Exception:
        return []
    rootfile = next((str(node.attrib.get('full-path') or '') for node in container.iter() if node.tag.endswith('rootfile') and node.attrib.get('full-path')), '')
    if not rootfile:
        return []
    try:
        opf = parse_untrusted_xml(archive.read(rootfile))
    except Exception:
        return []
    base = posixpath.dirname(rootfile)
    manifest: dict[str, str] = {}
    spine_ids: list[str] = []
    for node in opf.iter():
        tag = node.tag.split('}')[-1]
        if tag == 'item':
            item_id = str(node.attrib.get('id') or '')
            href = str(node.attrib.get('href') or '')
            if item_id and href:
                manifest[item_id] = posixpath.normpath(posixpath.join(base, href))
        elif tag == 'itemref':
            item_id = str(node.attrib.get('idref') or '')
            if item_id:
                spine_ids.append(item_id)
    return [manifest[item_id] for item_id in spine_ids if item_id in manifest]

def _read_dom_anchor(path: Path, source_range: dict[str, Any]) -> list[_ReadUnit]:
    container = str(source_range.get('container') or '').strip()
    if container and Path(container).name != path.name:
        raise SourceRangeReadError('The web document container in the catalog does not match this file.')
    parser = _AnchoredTextParser()
    parser.feed(path.read_text(encoding='utf-8', errors='replace'))
    start_anchor = str(source_range.get('start_anchor') or source_range.get('start') or '')
    end_anchor = str(source_range.get('end_anchor') or source_range.get('end') or '')
    metadata = dict(source_range.get('metadata') or {})
    heading_ordinal = _positive_or_zero_int(metadata.get('heading_ordinal'))
    end_heading_ordinal = _positive_or_zero_int(metadata.get('end_heading_ordinal'))
    if end_heading_ordinal is None and heading_ordinal is not None and end_anchor:
        end_heading_ordinal = heading_ordinal + 1
    text = parser.selected_text(start_anchor=start_anchor, end_anchor=end_anchor, start_heading_ordinal=heading_ordinal, end_heading_ordinal=end_heading_ordinal)
    return [_ReadUnit(text=text, locator=f'html:anchor:{start_anchor}', display_label=str(source_range.get('display_label') or start_anchor), start=start_anchor, end=end_anchor)]

def _read_structured_path(path: Path, source_range: dict[str, Any]) -> list[_ReadUnit]:
    path_parts = [str(part) for part in source_range.get('path') or []]
    if path.suffix.lower() == '.json':
        try:
            value: Any = json.loads(path.read_text(encoding='utf-8', errors='strict'))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SourceRangeReadError('This JSON file could not be parsed safely.') from exc
        for part in path_parts:
            list_index = _json_list_index(part)
            if isinstance(value, list) and list_index is not None:
                index = list_index
                if index >= len(value):
                    raise SourceRangeReadError('The data path in the catalog does not exist in this JSON.')
                value = value[index]
            elif isinstance(value, dict) and part in value:
                value = value[part]
            else:
                raise SourceRangeReadError('The data path in the catalog does not exist in this JSON.')
        text = json.dumps(value, ensure_ascii=False, indent=2)
    else:
        root = parse_untrusted_xml(path.read_bytes())
        node = root
        for index, part in enumerate(path_parts):
            name, sibling_index = _indexed_xml_segment(part)
            if index == 0 and _local_xml_name(node.tag) == name and (sibling_index == 1):
                continue
            matches = [child for child in node if _local_xml_name(child.tag) == name]
            candidate = matches[sibling_index - 1] if 1 <= sibling_index <= len(matches) else None
            if candidate is None:
                raise SourceRangeReadError('The data path in the catalog does not exist in this XML.')
            node = candidate
        text = '\n'.join((part.strip() for part in node.itertext() if part.strip()))
    locator = 'structured:' + '/'.join(path_parts)
    return [_ReadUnit(text=text, locator=locator, display_label=str(source_range.get('display_label') or '/'.join(path_parts)), start=path_parts[0] if path_parts else None, end=path_parts[-1] if path_parts else None)]

class _AnchoredTextParser(HTMLParser):
    _BLOCK_TAGS = {'article', 'blockquote', 'br', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'p', 'section', 'table', 'tr'}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._length = 0
        self._anchors: dict[str, int] = {}
        self._heading_offsets: list[int] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if normalized in {'script', 'style'}:
            self._ignored_depth += 1
        attrs_map = {key.lower(): value or '' for key, value in attrs}
        if re.fullmatch('h[1-6]', normalized):
            self._heading_offsets.append(self._length)
        for key in ('id', 'name'):
            anchor = attrs_map.get(key, '').lstrip('#')
            if anchor and anchor not in self._anchors:
                self._anchors[anchor] = self._length
        if normalized in self._BLOCK_TAGS:
            self._append('\n')

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in {'script', 'style'} and self._ignored_depth:
            self._ignored_depth -= 1
        if normalized in self._BLOCK_TAGS:
            self._append('\n')

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self._append(data)

    def _append(self, value: str) -> None:
        self._parts.append(value)
        self._length += len(value)

    def selected_text(self, *, start_anchor: str, end_anchor: str, start_heading_ordinal: int | None=None, end_heading_ordinal: int | None=None) -> str:
        text = ''.join(self._parts)
        start_key = start_anchor.lstrip('#')
        end_key = end_anchor.lstrip('#')
        if start_key and start_key in self._anchors:
            start = self._anchors[start_key]
        elif start_heading_ordinal is not None:
            if start_heading_ordinal >= len(self._heading_offsets):
                raise SourceRangeReadError('The heading location in the catalog does not exist in this document.')
            start = self._heading_offsets[start_heading_ordinal]
        elif start_key:
            raise SourceRangeReadError('The starting anchor in the catalog does not exist in this document.')
        else:
            start = 0
        if end_key and end_key in self._anchors:
            end = self._anchors[end_key]
        elif end_heading_ordinal is not None:
            end = self._heading_offsets[end_heading_ordinal] if end_heading_ordinal < len(self._heading_offsets) else len(text)
        elif end_key:
            raise SourceRangeReadError('The ending anchor in the catalog does not exist in this document.')
        else:
            end = len(text)
        if end < start:
            raise SourceRangeReadError('The catalog anchors have an invalid start-to-end order.')
        return _normalize_markup_text(text[start:end])

def _batch_text_units(units: Sequence[_ReadUnit], *, prefix: str) -> list[_ReadUnit]:
    batches: list[_ReadUnit] = []
    current: list[_ReadUnit] = []
    current_chars = 0
    for unit in units:
        if current and (len(current) >= TEXT_BATCH_UNITS or current_chars + len(unit.text) > TEXT_BATCH_CHARS):
            batches.append(_merge_text_batch(current, prefix=prefix))
            current = []
            current_chars = 0
        current.append(unit)
        current_chars += len(unit.text)
    if current:
        batches.append(_merge_text_batch(current, prefix=prefix))
    return batches

def _merge_text_batch(units: Sequence[_ReadUnit], *, prefix: str) -> _ReadUnit:
    first = units[0]
    last = units[-1]
    return _ReadUnit(text='\n'.join((unit.text for unit in units)), locator=f'{prefix}:{first.start}-{last.end}', display_label=first.display_label if len(units) == 1 else f'{first.display_label} - {last.display_label}', start=first.start, end=last.end, mode=first.mode)

def _evidence_from_units(*, source: SourceIngestionRecord, chapter: SourceChapter, units: Sequence[_ReadUnit], source_range: dict[str, Any], catalog_version: int, source_content_hash: str, warnings: list[str]) -> list[RetrievalEvidence]:
    result: list[RetrievalEvidence] = []
    total = len(units)
    for index, unit in enumerate(units, start=1):
        metadata = {'retrieval_mode': unit.mode, 'range_kind': source_range.get('kind'), 'source_range': source_range, 'source_locator': unit.locator, 'range_start': unit.start, 'range_end': unit.end, 'range_end_inclusive': True, 'catalog_version': catalog_version, 'source_content_hash': source_content_hash, 'batch_index': index, 'batch_count': total, 'warnings': warnings}
        if unit.page_start is not None:
            metadata['page_start'] = unit.page_start
        if unit.page_end is not None:
            metadata['page_end_inclusive'] = unit.page_end
        result.append(RetrievalEvidence(source_ingestion_id=source.id, open_notebook_source_id='', source_title=source.title, source_uri=source.source_uri, chapter_id=chapter.id, section_path=chapter.path, page_range=_page_label(unit.page_start, unit.page_end) if unit.page_start is not None and unit.page_end is not None else unit.display_label, chunk_ids=[], excerpt=_compact_text(unit.text, 360), expanded_text=unit.text, relevance_score=chapter.confidence, reason='Read on demand from the original file within the server-verified catalog range.', token_count=_estimate_tokens(unit.text), metadata=metadata))
    return result

def _metadata(value: Any) -> dict[str, Any]:
    metadata = getattr(value, 'metadata', None)
    return dict(metadata) if isinstance(metadata, dict) else {}

def _source_path(source: SourceIngestionRecord) -> Path | None:
    from openclass_core.services.source_ingestion_service import source_local_path
    return source_local_path(source)

def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(block)
    except OSError:
        return ''
    return digest.hexdigest()

def _is_sha256(value: str) -> bool:
    return bool(re.fullmatch('[0-9a-f]{64}', value))

def _has_usable_text(text: str) -> bool:
    meaningful_count = len(_MEANINGFUL_TEXT_RE.findall(text or ''))
    nonempty_lines = [line for line in (text or '').splitlines() if line.strip()]
    return meaningful_count >= 24 or (meaningful_count >= 16 and len(nonempty_lines) >= 3)

def _has_any_source_text(text: str) -> bool:
    return bool((text or '').strip())

def _strict_int(value: Any) -> int:
    normalized = _normalized_endpoint(value)
    if not isinstance(normalized, int) or isinstance(normalized, bool):
        raise SourceRangeReadError('Source range positions must be integers.')
    return normalized

def _positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0

def _positive_or_zero_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None

def _contiguous_batches(values: Iterable[int], *, max_batch_size: int) -> list[tuple[int, int]]:
    batches: list[tuple[int, int]] = []
    start: int | None = None
    previous: int | None = None
    for value in sorted(set(values)):
        if start is None or previous is None or value != previous + 1 or (value - start + 1 > max_batch_size):
            if start is not None and previous is not None:
                batches.append((start, previous))
            start = value
        previous = value
    if start is not None and previous is not None:
        batches.append((start, previous))
    return batches

def _page_label(start: int, end: int) -> str:
    return f'PDF p. {start}' if start == end else f'PDF pp. {start}-{end}'

def _normalize_markup_text(text: str) -> str:
    lines = [re.sub('\\s+', ' ', line).strip() for line in text.splitlines()]
    return '\n'.join((line for line in lines if line)).strip()

def _local_xml_name(tag: str) -> str:
    return tag.split('}')[-1]

def _json_list_index(segment: str) -> int | None:
    match = re.fullmatch('(?:\\[(\\d+)\\]|(\\d+))', segment.strip())
    if match is None:
        return None
    return int(match.group(1) or match.group(2))

def _indexed_xml_segment(segment: str) -> tuple[str, int]:
    match = re.fullmatch('(.+?)\\[(\\d+)\\]', segment.strip())
    if match is None:
        return (segment, 1)
    return (match.group(1), max(1, int(match.group(2))))

def _compact_text(text: str, limit: int) -> str:
    compact = ' '.join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:max(1, limit - 1)].rstrip() + '…'

def _estimate_tokens(text: str) -> int:
    return max(1, len(text.encode('utf-8')))
