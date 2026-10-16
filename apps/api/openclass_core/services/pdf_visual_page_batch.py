"""Apply observations of several original PDF page images as one read.

This module only reconciles model-owned navigation observations. It never opens
source files, calls OCR or a model, or writes source, course, or history state.
"""
from __future__ import annotations
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated
from pydantic import AfterValidator, Field, create_model, model_validator
from openclass_core.services.pdf_directory_contract import PDFDirectoryReadReport, PDFHeadingEvidence, PDFObservedDirectoryRow, PDFPaginationAnchor, parse_pdf_printed_page_label
from openclass_core.services.pdf_page_visual_input import MAX_PDF_PAGES_PER_BATCH
if TYPE_CHECKING:
    from openclass_core.services.pdf_visual_directory import PDFVisualCatalog

def _validate_pages(pages: tuple[int, ...]) -> None:
    if not 1 <= len(pages) <= MAX_PDF_PAGES_PER_BATCH or any((type(page) is not int or page < 1 for page in pages)) or len(set(pages)) != len(pages):
        raise ValueError('A PDF observation batch must identify one to six distinct physical pages.')

def batch_observation_schema(pages: tuple[int, ...], *, require_numbers: bool=False) -> type[PDFVisualCatalog]:
    """Restrict all new observations to the images supplied in this request."""
    _validate_pages(pages)
    return _cached_batch_observation_schema(pages, require_numbers)

@lru_cache(maxsize=32)
def _cached_batch_observation_schema(pages: tuple[int, ...], require_numbers: bool=False) -> type[PDFVisualCatalog]:
    from openclass_core.services.pdf_visual_directory import PDFPageReview, PDFVisualCatalog
    allowed = frozenset(pages)

    def attached_page(value: int) -> int:
        if value not in allowed:
            raise ValueError('An observation must identify an attached physical PDF page.')
        return value
    page_number = Annotated[int, Field(strict=True, ge=min(pages), le=max(pages), json_schema_extra={'enum': list(pages)}), AfterValidator(attached_page)]
    anchor = create_model('PDFBatchPageAnchor', __base__=PDFPaginationAnchor, physical_page=(page_number, ...))
    evidence = create_model('PDFBatchPageHeading', __base__=PDFHeadingEvidence, page=(page_number, ...))
    fields = {'directory_page': (page_number, ...)}
    if require_numbers:
        fields['number'] = (str, Field(..., max_length=128, description='Copy the full visible structural number, such as 2.3.1. Use an empty string only for a visibly unnumbered row, never for an unread number.'))
    row = create_model('PDFBatchPageRow', __base__=PDFObservedDirectoryRow, **fields)
    review = create_model('PDFBatchPageReview', __base__=PDFPageReview, physical_page=(page_number, ...), rows=(list[row], Field(default_factory=list, max_length=2000)))
    report = create_model('PDFBatchReadReport', __base__=PDFDirectoryReadReport, pagination_anchors=(list[anchor], Field(default_factory=list)), node_evidence=(list[evidence], Field(default_factory=list)), observed_directory_rows=(list[PDFObservedDirectoryRow], Field(default_factory=list, max_length=0)))

    @model_validator(mode='after')
    def complete_page_reviews(self):
        reviewed = [item.physical_page for item in self.page_reviews]
        if len(set(reviewed)) != len(reviewed) or set(reviewed) != allowed:
            raise ValueError('The batch must return one page review for every attached physical PDF page.')
        for item in self.page_reviews:
            if any((row.directory_page != item.physical_page for row in item.rows)):
                raise ValueError('Each page review may contain directory rows only from its own attached original page.')
        return self
    return create_model('PDFBatchObservation', __base__=PDFVisualCatalog, read_report=(report, ...), page_review=(type(None), None), page_reviews=(list[review], Field(..., min_length=len(pages), max_length=len(pages))), __validators__={'complete_page_reviews': complete_page_reviews})

def apply_batch_page_reviews(previous: PDFVisualCatalog | None, current: PDFVisualCatalog, pages: list[int]) -> PDFVisualCatalog:
    """Validate each page, then replace the batch's locator observations together."""
    from openclass_core.services.pdf_visual_directory import PDFVisualCatalog, _PageReviewTask, _accumulate_visual_catalog, _prepare_page_review, _validate_directory_page_review
    scoped = batch_observation_schema(tuple(pages)).model_validate(current.model_dump(mode='json'))
    current = PDFVisualCatalog.model_validate(scoped.model_dump(mode='json'))
    attached = set(pages)
    prior = previous.read_report if previous is not None else PDFDirectoryReadReport(status='partial')
    report = current.read_report
    reviews = {review.physical_page: review for review in current.page_reviews}
    if set(report.directory_pages) - set(prior.directory_pages) - attached:
        raise ValueError('A batch may classify only its attached original pages; request other images first.')
    rows: list[PDFObservedDirectoryRow] = []
    for page in pages:
        local = current.model_copy(update={'page_review': reviews[page], 'page_reviews': [], 'read_report': report.model_copy(update={'directory_pages': [value for value in report.directory_pages if value == page or value not in attached], 'pagination_anchors': [anchor for anchor in report.pagination_anchors if anchor.physical_page == page], 'node_evidence': [item for item in report.node_evidence if item.page == page]})})
        _, prepared = _prepare_page_review(previous, local, _PageReviewTask(page, 'page_observation'))
        rows.extend(prepared.read_report.observed_directory_rows)
    directories = [page for page in prior.directory_pages if page not in attached]
    directories.extend((page for page in pages if reviews[page].page_kind == 'directory'))
    retained_anchors = [anchor for anchor in prior.pagination_anchors if anchor.physical_page not in attached]
    anchors = [*retained_anchors, *report.pagination_anchors]
    anchor_pairs = {(anchor.physical_page, parse_pdf_printed_page_label(anchor.printed_page_label)) for anchor in anchors if anchor.physical_page not in directories}
    retained_segments = [segment for segment in prior.pagination_segments if (segment.physical_start, segment.printed_start) in anchor_pairs and (segment.physical_end, segment.printed_end) in anchor_pairs and (not any((segment.physical_start <= page <= segment.physical_end for page in directories)))]
    base = previous.model_copy(update={'read_report': prior.model_copy(update={'directory_pages': directories, 'pagination_anchors': retained_anchors, 'pagination_segments': retained_segments, 'node_evidence': [item for item in prior.node_evidence if not (item.page in attached and item.role == 'body' and (reviews[item.page].page_kind != 'body'))], 'external_directory_keys': prior.external_directory_keys if len(retained_segments) == len(prior.pagination_segments) else []})}) if previous is not None else None
    current = current.model_copy(update={'read_report': report.model_copy(update={'directory_pages': directories, 'observed_directory_rows': rows})})
    result = _accumulate_visual_catalog(base, current)
    for page in pages:
        result = _validate_directory_page_review(result.model_copy(update={'page_review': reviews[page]}), _PageReviewTask(page, 'page_observation'))
    return result.model_copy(update={'page_review': None, 'page_reviews': current.page_reviews})
