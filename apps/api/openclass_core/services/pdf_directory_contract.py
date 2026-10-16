"""Validate model observations of an original PDF without reading its body.

This contract checks scope, page identities and the consistency of model-reported
heading observations. Passing it does not make those observations independent
proof that the model read a page correctly. The caller must separately establish
that the original PDF, or the authorized original pages, reached the model.
"""
from __future__ import annotations
from collections.abc import Iterable, Mapping
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
PDFPageNumber = Annotated[int, Field(strict=True, ge=1)]
PDFDirectoryRequestedScope = Literal['directory', 'all_authorized_pages']

class PDFHeadingEvidence(BaseModel):
    """A heading the model reports seeing on a delivered physical PDF page."""
    model_config = ConfigDict(extra='forbid')
    node_key: str = Field(min_length=1, max_length=128)
    page: PDFPageNumber
    heading: str = Field(min_length=1, max_length=500)
    role: Literal['directory', 'body']

    @field_validator('node_key', 'heading')
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('Heading evidence must contain a node key and visible heading.')
        return value

class PDFObservedDirectoryRow(BaseModel):
    """An authored directory row that may not have a physical body range yet."""
    model_config = ConfigDict(extra='forbid')
    key: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    number: str = Field(default='', max_length=128)
    parent_key: str = Field(default='', max_length=128)
    directory_page: PDFPageNumber
    printed_page_label: str = Field(default='', max_length=128)

    @field_validator('key', 'title')
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('An observed directory row must have a key and visible title.')
        return value

    @field_validator('parent_key', 'printed_page_label', 'number')
    @classmethod
    def strip_optional_text(cls, value: str) -> str:
        return value.strip()

class PDFPaginationAnchor(BaseModel):
    """A visually observed body-page label, without assuming a global offset."""
    model_config = ConfigDict(extra='forbid')
    physical_page: PDFPageNumber
    printed_page_label: str = Field(min_length=1, max_length=128)
    heading: str = Field(min_length=1, max_length=500)
    node_key: str = Field(default='', max_length=128)

    @field_validator('printed_page_label', 'heading')
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('A pagination anchor must have a visible page label and heading.')
        return value

    @field_validator('node_key')
    @classmethod
    def strip_optional_key(cls, value: str) -> str:
        return value.strip()

class PDFPaginationSegment(BaseModel):
    """A one-to-one page mapping declared from observed pagination metadata."""
    model_config = ConfigDict(extra='forbid')
    physical_start: PDFPageNumber
    physical_end: PDFPageNumber
    printed_start: PDFPageNumber
    printed_end: PDFPageNumber

    @model_validator(mode='after')
    def validate_one_to_one_mapping(self) -> PDFPaginationSegment:
        if self.physical_end < self.physical_start or self.printed_end < self.printed_start:
            raise ValueError('Pagination segment page ranges must not be reversed.')
        if self.physical_end - self.physical_start != self.printed_end - self.printed_start:
            raise ValueError('Pagination segment physical and printed page spans must match.')
        return self

class PDFDirectoryReadReport(BaseModel):
    """Model output; physical page numbers are one-based, never printed labels.

    ``inspected_pages`` includes attempted pages that proved unreadable. A
    directory task may finish from a complete authored directory plus observed
    body anchors; it need not claim to have inspected the entire book.
    """
    model_config = ConfigDict(extra='forbid')
    status: Literal['complete', 'partial', 'unreadable']
    completion_basis: Literal['none', 'authored_directory_and_body_anchors', 'all_authorized_pages'] = 'none'
    inspected_pages: list[PDFPageNumber] = Field(default_factory=list)
    unreadable_pages: list[PDFPageNumber] = Field(default_factory=list)
    unresolved_notes: list[str] = Field(default_factory=list)
    observed_directory_rows: list[PDFObservedDirectoryRow] = Field(default_factory=list)
    directory_pages: list[PDFPageNumber] = Field(default_factory=list)
    directory_complete: bool = Field(default=False, strict=True)
    pagination_anchors: list[PDFPaginationAnchor] = Field(default_factory=list)
    pagination_segments: list[PDFPaginationSegment] = Field(default_factory=list)
    external_directory_keys: list[str] = Field(default_factory=list)
    node_evidence: list[PDFHeadingEvidence] = Field(default_factory=list)
    next_page_requests: list[PDFPageNumber] = Field(default_factory=list)
    revisit_page_requests: list[PDFPageNumber] = Field(default_factory=list)

    @field_validator('unresolved_notes')
    @classmethod
    def validate_notes(cls, values: list[str]) -> list[str]:
        if any((not value.strip() for value in values)):
            raise ValueError('Unresolved notes must describe the remaining issue.')
        return [value.strip() for value in values]

    @field_validator('external_directory_keys')
    @classmethod
    def validate_external_keys(cls, values: list[str]) -> list[str]:
        if any((not value.strip() or len(value.strip()) > 128 for value in values)):
            raise ValueError('External directory keys must be nonempty stable row keys.')
        return [value.strip() for value in values]

class PDFDirectoryContractError(ValueError):
    """The read report conflicts with the caller's PDF scope or node ranges."""

def validate_pdf_directory_report(report: PDFDirectoryReadReport, *, page_count: int, delivered_pages: Iterable[int], authorized_pages: Iterable[int], node_ranges: Mapping[str, tuple[int, int]], requested_scope: PDFDirectoryRequestedScope='directory') -> PDFDirectoryReadReport:
    """Check the report against server-known metadata, without opening a file.

    ``delivered_pages`` must describe actual model input, not a model claim.
    ``node_ranges`` contains proposed inclusive body ranges. A directory-page
    observation may be outside its node's body range; a body observation may not.
    Body observations of an unlocated row are retained until its range is known.
    Returning the report does not assign ``mapping_status=verified`` or any
    independent-confidence score.
    """
    if type(page_count) is not int or page_count < 1:
        raise PDFDirectoryContractError('The server must supply a positive PDF page count.')
    if requested_scope not in {'directory', 'all_authorized_pages'}:
        raise PDFDirectoryContractError('Unknown requested PDF directory scope.')
    authorized = _page_set(authorized_pages, page_count=page_count, label='Authorized pages')
    delivered = _page_set(delivered_pages, page_count=page_count, label='Delivered pages')
    if not authorized:
        raise PDFDirectoryContractError('The authorized PDF scope is empty.')
    if not delivered.issubset(authorized):
        raise PDFDirectoryContractError('Delivered pages exceed the authorized PDF scope.')
    inspected = _page_set(report.inspected_pages, page_count=page_count, label='Inspected pages')
    unreadable = _page_set(report.unreadable_pages, page_count=page_count, label='Unreadable pages')
    next_pages = _page_set(report.next_page_requests, page_count=page_count, label='Next page requests')
    revisit_pages = _page_set(report.revisit_page_requests, page_count=page_count, label='Revisit page requests')
    directory_pages = _page_set(report.directory_pages, page_count=page_count, label='Directory pages')
    if not inspected.issubset(delivered):
        raise PDFDirectoryContractError('The model claims to inspect pages that were not delivered.')
    if not unreadable.issubset(inspected):
        raise PDFDirectoryContractError('Unreadable pages must have been inspected.')
    if not next_pages.issubset(authorized):
        raise PDFDirectoryContractError('Next page requests exceed the authorized PDF scope.')
    if next_pages.intersection(delivered):
        raise PDFDirectoryContractError('Next page requests must identify pages not already delivered.')
    if not revisit_pages.issubset(delivered):
        raise PDFDirectoryContractError('Revisit page requests must identify previously delivered pages.')
    if not directory_pages.issubset(inspected):
        raise PDFDirectoryContractError('Directory pages must have been inspected.')
    rows: dict[str, PDFObservedDirectoryRow] = {}
    for row in report.observed_directory_rows:
        if row.key in rows:
            raise PDFDirectoryContractError(f'Duplicate observed directory row key: {row.key}')
        if row.directory_page not in directory_pages or row.directory_page in unreadable:
            raise PDFDirectoryContractError('An observed directory row must identify an inspected, readable directory page.')
        rows[row.key] = row
    for row in rows.values():
        visited = {row.key}
        parent_key = row.parent_key
        while parent_key:
            if parent_key in visited:
                raise PDFDirectoryContractError('Observed directory rows contain a parent cycle.')
            if parent_key not in rows:
                raise PDFDirectoryContractError(f'Observed directory row has an unknown parent: {parent_key}')
            visited.add(parent_key)
            parent_key = rows[parent_key].parent_key
    for key, bounds in node_ranges.items():
        if not key.strip() or len(bounds) != 2:
            raise PDFDirectoryContractError('Each directory node needs a key and an inclusive page range.')
        start, end = bounds
        _page_set((start, end), page_count=page_count, label=f'Range for node {key}', unique=False)
        if end < start:
            raise PDFDirectoryContractError(f'Directory node range is reversed: {key}')
    observed_nodes: set[str] = set()
    for evidence in report.node_evidence:
        if evidence.node_key not in node_ranges and evidence.node_key not in rows:
            raise PDFDirectoryContractError(f'Heading evidence references an unknown node: {evidence.node_key}')
        if evidence.page not in inspected or evidence.page in unreadable:
            raise PDFDirectoryContractError('Heading evidence must come from an inspected, readable page.')
        if evidence.role == 'body':
            if evidence.node_key in node_ranges:
                start, end = node_ranges[evidence.node_key]
                if not start <= evidence.page <= end:
                    raise PDFDirectoryContractError(f'Body heading evidence lies outside its node range: {evidence.node_key}')
        elif rows and evidence.page not in directory_pages:
            raise PDFDirectoryContractError('Directory heading evidence must identify a declared directory page.')
        observed_nodes.add(evidence.node_key)
    for anchor in report.pagination_anchors:
        if anchor.physical_page not in inspected or anchor.physical_page in unreadable:
            raise PDFDirectoryContractError('Pagination anchors must come from an inspected, readable page.')
        if anchor.node_key and anchor.node_key not in node_ranges and (anchor.node_key not in rows):
            raise PDFDirectoryContractError(f'Pagination anchor references an unknown node: {anchor.node_key}')
        if anchor.node_key in node_ranges:
            start, end = node_ranges[anchor.node_key]
            if not start <= anchor.physical_page <= end:
                raise PDFDirectoryContractError(f'Pagination anchor lies outside its node range: {anchor.node_key}')
    _validate_pagination_segments(report.pagination_segments, anchors=report.pagination_anchors, page_count=page_count, authorized_pages=authorized, directory_pages=directory_pages)
    external_keys = set(report.external_directory_keys)
    if len(external_keys) != len(report.external_directory_keys):
        raise PDFDirectoryContractError('Duplicate external directory row key.')
    if not external_keys.issubset(rows):
        raise PDFDirectoryContractError('External directory keys must identify observed directory rows.')
    if external_keys.intersection(node_ranges):
        raise PDFDirectoryContractError('An external directory row cannot also be a located node.')
    if external_keys and (not report.pagination_segments):
        raise PDFDirectoryContractError('External directory rows require verified body pagination segments.')
    if external_keys:
        segments = sorted(report.pagination_segments, key=lambda item: item.physical_start)
        body_anchor_pages = {anchor.physical_page for anchor in report.pagination_anchors if anchor.physical_page not in directory_pages}
        if not body_anchor_pages or segments[0].physical_start != min(body_anchor_pages) or segments[-1].physical_end != page_count:
            raise PDFDirectoryContractError('External directory rows require pagination coverage from the observed body start through the final PDF page.')
        if any((current.physical_start != previous.physical_end + 1 for previous, current in zip(segments, segments[1:]))):
            raise PDFDirectoryContractError('External directory rows require continuous physical body coverage.')
    for key in external_keys:
        printed_page = parse_pdf_printed_page_label(rows[key].printed_page_label)
        if printed_page is None:
            raise PDFDirectoryContractError('An external directory row must have a positive integer printed page label.')
        if any((segment.printed_start <= printed_page <= segment.printed_end for segment in report.pagination_segments)):
            raise PDFDirectoryContractError('An external directory row falls inside an observed body pagination segment.')
    missing_evidence = set(node_ranges).difference(observed_nodes)
    if missing_evidence:
        raise PDFDirectoryContractError('Directory nodes lack observed heading evidence: ' + ', '.join(sorted(missing_evidence)))
    if report.status != 'complete':
        if report.completion_basis != 'none':
            raise PDFDirectoryContractError('An incomplete read cannot declare a completion basis.')
        if report.status == 'unreadable' and (report.node_evidence or rows or report.pagination_anchors):
            raise PDFDirectoryContractError('A wholly unreadable result cannot claim readable headings.')
        return report
    if not node_ranges:
        raise PDFDirectoryContractError('A complete directory must contain an observed node.')
    if unreadable or report.unresolved_notes or next_pages or revisit_pages:
        raise PDFDirectoryContractError('A complete directory cannot have unreadable pages or unresolved work.')
    missing_rows = set(rows).difference(node_ranges).difference(external_keys)
    if missing_rows:
        raise PDFDirectoryContractError('Observed directory rows remain unmapped: ' + ', '.join(sorted(missing_rows)))
    if report.completion_basis == 'none':
        raise PDFDirectoryContractError('A complete directory needs an explicit observation basis.')
    if requested_scope == 'all_authorized_pages' or report.completion_basis == 'all_authorized_pages':
        if inspected != authorized:
            raise PDFDirectoryContractError('The report has not inspected every page in the requested scope.')
    if report.completion_basis == 'authored_directory_and_body_anchors':
        if not rows or not report.directory_complete:
            raise PDFDirectoryContractError('Authored-directory completion needs observed rows and a complete directory-page declaration.')
        if not report.pagination_anchors:
            raise PDFDirectoryContractError('Authored-directory completion needs observed pagination anchors.')
        if not any((anchor.physical_page not in directory_pages for anchor in report.pagination_anchors)):
            raise PDFDirectoryContractError('Authored-directory completion needs a pagination anchor on a body page outside directory pages.')
    return report

def parse_pdf_printed_page_label(label: str) -> int | None:
    """Parse only explicit positive ASCII integer labels; never infer numbering."""
    if not isinstance(label, str):
        return None
    label = label.strip()
    if not label or not label.isascii() or (not label.isdigit()):
        return None
    try:
        value = int(label)
    except ValueError:
        return None
    return value if value > 0 else None

def map_printed_page_to_physical(printed_page_label: str, segments: Iterable[PDFPaginationSegment]) -> int | None:
    """Map a model-observed label only when a single declared segment covers it.

    The caller must validate the segments against observed body anchors before
    treating the result as a located page. No file content is read here.
    """
    printed_page = parse_pdf_printed_page_label(printed_page_label)
    if printed_page is None:
        return None
    matches = [segment for segment in segments if segment.printed_start <= printed_page <= segment.printed_end]
    if len(matches) > 1:
        raise PDFDirectoryContractError('The printed page label matches multiple pagination segments.')
    if not matches:
        return None
    segment = matches[0]
    return segment.physical_start + printed_page - segment.printed_start

def _validate_pagination_segments(segments: Iterable[PDFPaginationSegment], *, anchors: Iterable[PDFPaginationAnchor], page_count: int, authorized_pages: set[int], directory_pages: set[int]) -> None:
    observed_body_labels = {(anchor.physical_page, parse_pdf_printed_page_label(anchor.printed_page_label)) for anchor in anchors if anchor.physical_page not in directory_pages}
    previous_end = 0
    for segment in sorted(segments, key=lambda item: (item.physical_start, item.physical_end)):
        boundaries = _page_set((segment.physical_start, segment.physical_end), page_count=page_count, label='Pagination segment boundaries', unique=False)
        if not boundaries.issubset(authorized_pages):
            raise PDFDirectoryContractError('Pagination segment boundaries exceed the authorized PDF scope.')
        if segment.physical_start <= previous_end:
            raise PDFDirectoryContractError('Pagination segment physical page ranges overlap.')
        required_labels = {(segment.physical_start, segment.printed_start), (segment.physical_end, segment.printed_end)}
        if not required_labels.issubset(observed_body_labels):
            raise PDFDirectoryContractError('Pagination segment endpoints require exact observed body page labels.')
        previous_end = segment.physical_end

def validate_pdf_read_continuity(previous: PDFDirectoryReadReport | None, current: PDFDirectoryReadReport, *, previous_node_keys: Iterable[str], current_node_keys: Iterable[str]) -> None:
    """Require incremental reads to retain earlier observations and stable keys.

    Titles and locators may be corrected by a later direct image observation.
    Dropping an earlier node or row is not an implicit correction operation.
    """
    if previous is None:
        return
    if not set(previous.inspected_pages).issubset(current.inspected_pages):
        raise PDFDirectoryContractError('The incremental read report dropped previously inspected pages.')
    previous_rows = {row.key for row in previous.observed_directory_rows}
    current_rows = {row.key for row in current.observed_directory_rows}
    if not previous_rows.issubset(current_rows):
        raise PDFDirectoryContractError('The incremental read report dropped previously observed directory rows.')
    if not set(previous_node_keys).issubset(current_node_keys):
        raise PDFDirectoryContractError('The incremental read report dropped previously located directory nodes.')

def directory_closure_pages(report: PDFDirectoryReadReport, *, page_count: int, page_classifications: Mapping[int, str]) -> list[int]:
    """Pages needed to close an observed authored directory, never RAG coverage.

    A search hit seeds inspection. Every intervening page and both physical
    neighbours must actually be classified before a directory can be complete.
    A boundary at the beginning/end of the file needs no invented neighbour.
    """
    pages = sorted(set(report.directory_pages))
    if not pages:
        return []
    needed = set(range(pages[0], pages[-1] + 1))
    needed.update((page for page in (pages[0] - 1, pages[-1] + 1) if 1 <= page <= page_count))
    return sorted((page for page in needed if page_classifications.get(page) not in {'directory', 'body', 'other'}))

def verified_pagination_report(report: PDFDirectoryReadReport) -> tuple[PDFDirectoryReadReport, list[int]]:
    """Project page-number evidence without promoting running headers to chapters.

    Adjacent segments with the same offset are one hypothesis. Only its outer
    endpoints need their own observations; every known internal label must also
    agree. Unsupported hypotheses never suppress unrelated verified segments.
    """
    anchors = {}
    for anchor in report.pagination_anchors:
        label = parse_pdf_printed_page_label(anchor.printed_page_label)
        if label is not None and anchor.physical_page not in report.directory_pages:
            anchors.setdefault(anchor.physical_page, set()).add(label)
    merged = []
    for segment in sorted(report.pagination_segments, key=lambda item: item.physical_start):
        if merged and segment.physical_start <= merged[-1].physical_end + 1 and (segment.physical_start - segment.printed_start == merged[-1].physical_start - merged[-1].printed_start):
            previous = merged[-1]
            end = max(previous.physical_end, segment.physical_end)
            merged[-1] = PDFPaginationSegment(physical_start=previous.physical_start, physical_end=end, printed_start=previous.printed_start, printed_end=previous.printed_start + end - previous.physical_start)
        else:
            merged.append(segment)
    valid, pending = ([], set())
    for segment in merged:
        needed = {page for page, printed in ((segment.physical_start, segment.printed_start), (segment.physical_end, segment.printed_end)) if anchors.get(page) != {printed}}
        needed.update((page for page, labels in anchors.items() if segment.physical_start <= page <= segment.physical_end and labels != {segment.printed_start + page - segment.physical_start}))
        needed.update((page for page in report.directory_pages if segment.physical_start <= page <= segment.physical_end))
        if needed:
            pending.update(needed)
        else:
            valid.append(segment)
    overlaps = {index for index, segment in enumerate(valid) for other_index, other in enumerate(valid) if index != other_index and segment.physical_start <= other.physical_end and (other.physical_start <= segment.physical_end)}
    for index in overlaps:
        pending.update((valid[index].physical_start, valid[index].physical_end))
    valid = [segment for index, segment in enumerate(valid) if index not in overlaps]
    authored = bool(report.observed_directory_rows)
    return (report.model_copy(update={'pagination_segments': valid, 'pagination_anchors': [anchor.model_copy(update={'node_key': ''}) if authored else anchor for anchor in report.pagination_anchors], 'node_evidence': [item for item in report.node_evidence if not authored or item.role == 'directory'], **({'status': 'partial', 'completion_basis': 'none', 'external_directory_keys': []} if pending else {})}), sorted(pending))

def pagination_expansion_pages(report: PDFDirectoryReadReport, *, page_count: int) -> list[int]:
    """Propose original pages for unlocated printed labels, never new mappings."""
    offsets = {segment.physical_start - segment.printed_start for segment in report.pagination_segments}
    if len(offsets) != 1:
        return []
    offset = next(iter(offsets))
    candidates = sorted({printed + offset for row in report.observed_directory_rows if (printed := parse_pdf_printed_page_label(row.printed_page_label)) is not None and (not any((segment.printed_start <= printed <= segment.printed_end for segment in report.pagination_segments))) and (1 <= printed + offset <= page_count)})
    if not candidates:
        return []
    seen = set(report.inspected_pages)
    unseen = [page for page in candidates if page not in seen]
    for page in candidates:
        if page in seen:
            unseen.extend((neighbour for neighbour in (page - 1, page + 1) if 1 <= neighbour <= page_count and neighbour not in seen))
    candidates = sorted(set(unseen))
    if not candidates:
        return []
    return list(dict.fromkeys([candidates[-1], candidates[0], *candidates[1:-1]]))

def _page_set(values: Iterable[int], *, page_count: int, label: str, unique: bool=True) -> set[int]:
    result: set[int] = set()
    for value in values:
        if type(value) is not int or not 1 <= value <= page_count:
            raise PDFDirectoryContractError(f'{label} must use physical PDF pages between 1 and {page_count}.')
        if unique and value in result:
            raise PDFDirectoryContractError(f'{label} contain duplicate page numbers.')
        result.add(value)
    return result
