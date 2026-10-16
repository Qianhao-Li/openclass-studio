"""Read-only PDF compatibility surface backed by PDFium and pypdf, without MuPDF."""
from __future__ import annotations
import io
import math
import re
from pathlib import Path
import threading
from pypdf import PdfReader
import pypdfium2 as pdfium
from pypdfium2 import raw
from openclass_core.services.pdf_geometry import Rect, Point, Matrix
from openclass_core.services.pdf_text import observations, text_payload, union
ENGINE = 'pdfium-pypdf-v1'
MAX_RENDER_PIXELS = 32000000
TEXTFLAGS_DICT = 0
TEXTFLAGS_BLOCKS = 0
TEXT_PRESERVE_IMAGES = 4
csRGB = object()
_lock = threading.RLock()

class Pixmap:

    def __init__(self, image):
        self.image = image
    width = property(lambda self: self.image.width)
    height = property(lambda self: self.image.height)
    samples = property(lambda self: self.image.tobytes())
    n = property(lambda self: len(self.image.getbands()))

    def tobytes(self, format='png'):
        output = io.BytesIO()
        self.image.save(output, format=format.upper())
        return output.getvalue()

class Document:

    def __init__(self, content):
        self.content = content
        self._reader = PdfReader(io.BytesIO(content), strict=True)
        self.is_encrypted = self._reader.is_encrypted
        self.needs_pass = self.is_encrypted
        self.is_pdf = True
        self.is_repaired = False
        self.metadata = {'encryption': 'encrypted'} if self.is_encrypted else {str(k).lstrip('/').lower(): str(v) for k, v in (self._reader.metadata or {}).items()}
        with _lock:
            self._document = None if self.is_encrypted else pdfium.PdfDocument(content)
        self._pages = {}
    page_count = property(lambda self: len(self._document) if self._document else 0)

    def __len__(self):
        return self.page_count

    def __iter__(self):
        return (self.load_page(index) for index in range(len(self)))

    def __getitem__(self, index):
        return self.load_page(index)

    def load_page(self, index):
        if type(index) is not int or not 0 <= index < len(self):
            raise ValueError('PDF page is outside the original')
        with _lock:
            if index not in self._pages:
                self._pages[index] = Page(self, index, self._document[index])
            return self._pages[index]

    def get_toc(self, **_kwargs):
        output = []

        def visit(nodes, level):
            for node in nodes:
                if isinstance(node, list):
                    visit(node, level + 1)
                else:
                    number = self._reader.get_destination_page_number(node)
                    if number is not None and 0 <= number < len(self):
                        output.append((level, str(node.title), number + 1))
        visit(self._reader.outline, 1)
        return output

    def extract_image(self, xref):
        for index in range(len(self)):
            for image in self._reader.pages[index].images:
                if image.indirect_reference and image.indirect_reference.idnum == xref:
                    return {'image': image.data, 'ext': image.name.rsplit('.', 1)[-1]}
        raise ValueError('PDF image object is unavailable')

    def close(self):
        with _lock:
            if self._document:
                self._document.close()
                self._document = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

class Page:

    def __init__(self, document, index, page):
        self.parent = document
        self.number = index
        self._page = page
        self._text = None
        self.unit = float(document._reader.pages[index].get('/UserUnit', 1))
        if not math.isfinite(self.unit) or not 0 < self.unit <= 75000:
            raise ValueError('Invalid PDF user unit')
        width, height = page.get_size()
        self.rect = Rect(0, 0, width * self.unit, height * self.unit)
        self.rotation = page.get_rotation()
        self.first_annot = True if raw.FPDFPage_GetAnnotCount(page.raw) > 0 else None

    def _observations(self):
        if self._text is None:
            self._text = observations(self)
        return self._text

    def get_text(self, kind='text', *, clip=None, sort=False, **_kwargs):
        with _lock:
            return text_payload(self._observations()[0], kind, clip=Rect(clip) if clip is not None else None, sort=sort)

    def get_textbox(self, rect):
        return self.get_text(clip=rect, sort=True)

    def search_for(self, query, **_kwargs):
        if not query:
            return []
        with _lock:
            chars = self._observations()[0]
            text = ''.join((char['text'] for char in chars))
            pattern = '\\s+'.join((re.escape(part) for part in query.split()))
            return [union((char['bbox'] for char in chars[match.start():match.end()] if char['bbox'] is not None)) for match in re.finditer(pattern, text, re.IGNORECASE)]

    def get_texttrace(self):
        with _lock:
            return self._observations()[1]

    def _objects(self):
        output = []
        bounds = self._page.get_bbox()
        for obj in self._page.get_objects(max_depth=1):
            box = obj.get_bounds()
            output.append((obj.type, Rect(*[value * self.unit for value in (box[0] - bounds[0], bounds[3] - box[3], box[2] - bounds[0], bounds[3] - box[1])])))
        return output

    def get_bboxlog(self):
        with _lock:
            return [({1: 'fill-text', 2: 'fill-path', 3: 'fill-image', 4: 'fill-shade', 5: 'fill-path'}.get(kind, 'fill-path'), tuple(box)) for kind, box in self._objects()]

    def get_drawings(self):
        with _lock:
            return [{'rect': box} for kind, box in self._objects() if kind in {2, 4, 5}]

    def get_images(self, full=False):
        images = []
        for image in self.parent._reader.pages[self.number].images:
            if image.indirect_reference:
                images.append((image.indirect_reference.idnum, 0, image.image.width, image.image.height, 8, 'DeviceRGB', '', image.name, '', 0))
        return images

    def get_image_rects(self, xref):
        from PIL import Image
        original = Image.open(io.BytesIO(self.parent.extract_image(xref)['image'])).convert('RGB')
        digest = __import__('hashlib').sha256(original.tobytes()).digest()
        result = []
        with _lock:
            bounds = self._page.get_bbox()
            for obj in self._page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE]):
                bitmap = obj.get_bitmap()
                try:
                    image = bitmap.to_pil().convert('RGB')
                finally:
                    bitmap.close()
                if image.size != original.size or __import__('hashlib').sha256(image.tobytes()).digest() != digest:
                    continue
                box = obj.get_bounds()
                result.append(Rect(*[value * self.unit for value in (box[0] - bounds[0], bounds[3] - box[3], box[2] - bounds[0], bounds[3] - box[1])]))
        return result

    def get_pixmap(self, *, dpi=None, matrix=None, clip=None, alpha=False, annots=True, **_kwargs):
        scale = float(dpi) / 72 if dpi is not None else matrix.a if matrix else 1
        if scale <= 0:
            raise ValueError('PDF scale must be positive')
        if not math.isfinite(scale) or self.rect.width * self.rect.height * scale * scale > MAX_RENDER_PIXELS:
            raise ValueError('The PDF page exceeds the verified rendering budget.')
        region = Rect(clip) & self.rect if clip is not None else self.rect
        if region.is_empty:
            raise ValueError('The requested PDF region is empty')
        effective = scale * self.unit * (1 - 1e-12)
        ceil = lambda value: math.ceil(value - 1e-09)
        floor = lambda value: math.floor(value + 1e-09)
        pixels = (floor(region.x0 * scale), ceil(self.rect.height * scale) - ceil(region.y1 * scale), ceil(self.rect.width * scale) - ceil(region.x1 * scale), floor(region.y0 * scale))
        crop = tuple((max(0, value - 1e-07) / effective for value in pixels))
        with _lock:
            bitmap = self._page.render(scale=effective, crop=crop, draw_annots=annots)
            try:
                image = bitmap.to_pil().convert('RGBA' if alpha else 'RGB').copy()
            finally:
                bitmap.close()
        return Pixmap(image)

def open(filename=None, *, stream=None, filetype=None):
    if filename is None and stream is None:
        from openclass_core.services.pdf_contact_sheet import ContactSheet
        return ContactSheet()
    content = bytes(stream) if stream is not None else Path(filename).read_bytes()
    if filetype == 'svg' or (filetype is None and filename is not None and (Path(filename).suffix.lower() == '.svg')):
        from openclass_core.services.svg_backend import SvgDocument
        return SvgDocument(content)
    if filetype not in {None, 'pdf'} or not content.startswith(b'%PDF-'):
        raise ValueError('This adapter requires an original PDF carrier')
    return Document(content)
