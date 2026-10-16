"""Render unchanged PDF pages for cloud vision without extracting their text.

The reader owns only an in-memory, read-only PDF and ephemeral page images. It
does not call a model, write source/library state, or create history/audit data.
"""
from __future__ import annotations
import base64
import hashlib
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from types import TracebackType
from openclass_core.services import pdf_backend as fitz
DEFAULT_PDF_PAGE_DPI = 180
MAX_PDF_PAGE_DPI = 200
DEFAULT_PDF_DETAIL_DPI = 300
MAX_PDF_DETAIL_DPI = 600
MAX_PDF_PAGE_PIXELS = 12000000
MAX_PDF_PAGES_PER_BATCH = 6
MAX_PDF_PAGE_PNG_BYTES = 32 * 1024 * 1024
MAX_PDF_BATCH_PNG_BYTES = 64 * 1024 * 1024

class PdfPageVisualInputError(RuntimeError):
    """The original PDF cannot safely supply the requested page images."""

class PdfPageBatchTooLargeError(PdfPageVisualInputError):
    """Individually valid page images exceed the total batch byte limit."""

class PdfPageDetailTooLargeError(PdfPageVisualInputError):
    """The requested original-PDF detail exceeds its bounded transfer budget."""

@dataclass(frozen=True)
class PdfPageImage:
    page_number: int
    mime_type: str
    data_url: str
    sha256: str
    width: int
    height: int

class PdfPageVisualReader:

    def __init__(self, path: Path, expected_sha256: str | None=None, *, is_cancelled: Callable[[], bool] | None=None, dpi: int=DEFAULT_PDF_PAGE_DPI) -> None:
        if type(dpi) is not int or not 72 <= dpi <= MAX_PDF_PAGE_DPI:
            raise PdfPageVisualInputError('PDF page DPI must be between 72 and 200.')
        self._path = Path(path)
        self._is_cancelled = is_cancelled
        self._dpi = dpi
        self._document: fitz.Document | None = None
        self._check_cancelled()
        try:
            original = self._path.read_bytes()
        except OSError:
            raise PdfPageVisualInputError('The original PDF is unavailable.') from None
        self.source_sha256 = hashlib.sha256(original).hexdigest()
        if expected_sha256 is not None and expected_sha256.lower() != self.source_sha256:
            raise PdfPageVisualInputError('The original PDF hash does not match.')
        self._check_cancelled()
        try:
            document = fitz.open(stream=original, filetype='pdf')
        except Exception:
            raise PdfPageVisualInputError('The original PDF is invalid or unreadable.') from None
        try:
            if document.is_encrypted or document.needs_pass or bool((document.metadata or {}).get('encryption')):
                raise PdfPageVisualInputError('Encrypted PDFs cannot supply page images.')
            if not document.is_pdf or document.page_count < 1 or document.is_repaired:
                raise PdfPageVisualInputError('The original PDF is invalid or unreadable.')
            self.page_count = document.page_count
            self._check_cancelled()
            self._verify_source_hash()
        except BaseException:
            document.close()
            raise
        self._document = document

    def __enter__(self) -> PdfPageVisualReader:
        self._require_document()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None) -> None:
        try:
            if exc_type is None:
                self._verify_source_hash()
        finally:
            self.close()

    def close(self) -> None:
        if self._document is not None:
            self._document.close()
            self._document = None

    def render_pages(self, page_numbers: Iterable[int], *, max_pages_per_batch: int=MAX_PDF_PAGES_PER_BATCH, is_cancelled: Callable[[], bool] | None=None) -> list[PdfPageImage]:
        document = self._require_document()
        self._check_cancelled(is_cancelled)
        if type(max_pages_per_batch) is not int or not 1 <= max_pages_per_batch <= MAX_PDF_PAGES_PER_BATCH:
            raise PdfPageVisualInputError('PDF page batches must contain at most six pages.')
        requested = list(islice(page_numbers, max_pages_per_batch + 1))
        if not requested or len(requested) > max_pages_per_batch:
            raise PdfPageVisualInputError('The requested PDF page batch is empty or too large.')
        if any((type(number) is not int or not 1 <= number <= self.page_count for number in requested)):
            raise PdfPageVisualInputError('A requested physical page is outside the PDF.')
        if len(set(requested)) != len(requested):
            raise PdfPageVisualInputError('A PDF page batch must not repeat a physical page.')
        self._verify_source_hash()
        images: list[PdfPageImage] = []
        total_png_bytes = 0
        for number in requested:
            self._check_cancelled(is_cancelled)
            try:
                page = document.load_page(number - 1)
                width = math.ceil(page.rect.width * self._dpi / 72)
                height = math.ceil(page.rect.height * self._dpi / 72)
                if width <= 0 or height <= 0 or width * height > MAX_PDF_PAGE_PIXELS:
                    raise PdfPageVisualInputError('The PDF page exceeds the image size limit.')
                pixmap = page.get_pixmap(dpi=self._dpi, colorspace=fitz.csRGB, alpha=False, annots=True)
                self._check_cancelled(is_cancelled)
                if pixmap.width * pixmap.height > MAX_PDF_PAGE_PIXELS:
                    raise PdfPageVisualInputError('The PDF page exceeds the image size limit.')
                png = pixmap.tobytes('png')
                if len(png) > MAX_PDF_PAGE_PNG_BYTES:
                    raise PdfPageVisualInputError('The PDF page exceeds the image byte limit.')
                total_png_bytes += len(png)
                if total_png_bytes > MAX_PDF_BATCH_PNG_BYTES:
                    raise PdfPageBatchTooLargeError('The PDF page batch exceeds the image byte limit.')
                images.append(PdfPageImage(page_number=number, mime_type='image/png', data_url='data:image/png;base64,' + base64.b64encode(png).decode('ascii'), sha256=hashlib.sha256(png).hexdigest(), width=pixmap.width, height=pixmap.height))
            except PdfPageVisualInputError:
                raise
            except Exception:
                raise PdfPageVisualInputError('The original PDF page could not be rendered.') from None
        self._check_cancelled(is_cancelled)
        self._verify_source_hash()
        return images

    def render_region(self, page_number: int, region: list[float], *, dpi: int=DEFAULT_PDF_DETAIL_DPI, max_png_bytes: int=MAX_PDF_PAGE_PNG_BYTES, is_cancelled: Callable[[], bool] | None=None) -> PdfPageImage:
        """Render an original PDF region directly, in rotated whole-page coordinates.

        This does not crop or enlarge a previously rendered page PNG. A clipped
        pixmap preserves the PDF's cropbox, rotation and annotations while keeping
        allocation proportional to the requested region.
        """
        document = self._require_document()
        self._check_cancelled(is_cancelled)
        if type(page_number) is not int or not 1 <= page_number <= self.page_count:
            raise PdfPageVisualInputError('A requested physical page is outside the PDF.')
        if not isinstance(region, list) or len(region) != 4 or any((type(value) not in (int, float) or not math.isfinite(value) for value in region)) or (not 0 <= region[0] < region[2] <= 1) or (not 0 <= region[1] < region[3] <= 1):
            raise PdfPageVisualInputError('The original PDF region requires ordered normalized coordinates.')
        if type(dpi) is not int or not DEFAULT_PDF_DETAIL_DPI <= dpi <= MAX_PDF_DETAIL_DPI:
            raise PdfPageVisualInputError('PDF detail DPI must be between 300 and 600.')
        if type(max_png_bytes) is not int or max_png_bytes < 1:
            raise PdfPageDetailTooLargeError('The original PDF detail exceeds the image byte limit.')
        self._verify_source_hash()
        try:
            page = document.load_page(page_number - 1)
            bounds = page.rect
            clip = fitz.Rect(bounds.x0 + region[0] * bounds.width, bounds.y0 + region[1] * bounds.height, bounds.x0 + region[2] * bounds.width, bounds.y0 + region[3] * bounds.height)
            width = math.ceil(clip.x1 * dpi / 72) - math.floor(clip.x0 * dpi / 72)
            height = math.ceil(clip.y1 * dpi / 72) - math.floor(clip.y0 * dpi / 72)
            if width <= 0 or height <= 0 or width * height > MAX_PDF_PAGE_PIXELS:
                raise PdfPageDetailTooLargeError('The original PDF detail exceeds the image size limit. Read a smaller region.')
            self._check_cancelled(is_cancelled)
            pixmap = page.get_pixmap(dpi=dpi, clip=clip, colorspace=fitz.csRGB, alpha=False, annots=True)
            self._check_cancelled(is_cancelled)
            if pixmap.width * pixmap.height > MAX_PDF_PAGE_PIXELS:
                raise PdfPageDetailTooLargeError('The original PDF detail exceeds the image size limit.')
            png = pixmap.tobytes('png')
            if len(png) > min(max_png_bytes, MAX_PDF_PAGE_PNG_BYTES):
                raise PdfPageDetailTooLargeError('The original PDF detail exceeds the image byte limit. Read a smaller region.')
            self._verify_source_hash()
            self._check_cancelled(is_cancelled)
            return PdfPageImage(page_number=page_number, mime_type='image/png', data_url='data:image/png;base64,' + base64.b64encode(png).decode('ascii'), sha256=hashlib.sha256(png).hexdigest(), width=pixmap.width, height=pixmap.height)
        except PdfPageVisualInputError:
            raise
        except Exception:
            raise PdfPageVisualInputError('The original PDF region could not be rendered.') from None

    def _require_document(self) -> fitz.Document:
        if self._document is None:
            raise PdfPageVisualInputError('The PDF page reader is closed.')
        return self._document

    def _check_cancelled(self, extra: Callable[[], bool] | None=None) -> None:
        if self._is_cancelled is not None and self._is_cancelled() or (extra is not None and extra()):
            raise PdfPageVisualInputError('PDF page rendering was cancelled.')

    def _verify_source_hash(self) -> None:
        digest = hashlib.sha256()
        try:
            with self._path.open('rb') as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(chunk)
        except OSError:
            raise PdfPageVisualInputError('The original PDF is unavailable.') from None
        if digest.hexdigest() != self.source_sha256:
            raise PdfPageVisualInputError('The original PDF changed while reading its pages.')
