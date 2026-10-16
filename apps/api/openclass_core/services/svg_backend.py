"""Bounded SVG rendering without external file/network resource resolution."""
import io
import math
import re
import xml.etree.ElementTree as ET
from PIL import Image
from openclass_core.services.pdf_geometry import Rect
MAX_SVG_BYTES = 10 * 1024 * 1024
MAX_SVG_PIXELS = 12000000

def _dimension(value):
    if value is None:
        return None
    match = re.fullmatch('\\s*([0-9]+(?:\\.[0-9]+)?)(px|pt|pc|in|cm|mm|q)?\\s*', value, re.I)
    if not match:
        return None
    scale = {None: 1, 'px': 1, 'pt': 96 / 72, 'pc': 16, 'in': 96, 'cm': 96 / 2.54, 'mm': 96 / 25.4, 'q': 96 / 101.6}
    return float(match[1]) * scale[match[2].lower() if match[2] else None]

class SvgDocument:
    is_pdf = False
    is_encrypted = False
    needs_pass = False
    is_repaired = False
    page_count = 1

    def __init__(self, content):
        if len(content) > MAX_SVG_BYTES:
            raise ValueError('The SVG exceeds its byte limit')
        text = content.decode('utf-8-sig')
        if re.search('<!DOCTYPE|<!ENTITY|<\\?xml-stylesheet|@import', text, re.I):
            raise ValueError('External SVG resources are not authorized')
        tree = ET.fromstring(text)
        if tree.tag.rsplit('}', 1)[-1] != 'svg':
            raise ValueError('The carrier is not an SVG')
        for node in tree.iter():
            if node.tag.rsplit('}', 1)[-1] in {'foreignObject', 'script'}:
                raise ValueError('The SVG contains unsupported active content')
            for name, value in node.attrib.items():
                if name.rsplit('}', 1)[-1] == 'href' and (not value.startswith('#')) and (not re.fullmatch('data:image/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/=\\s]+', value)):
                    raise ValueError('External SVG resources are not authorized')
            for value in [*node.attrib.values(), node.text or '']:
                if any((not item.strip(' \t\r\n"\'').startswith('#') for item in re.findall('url\\(([^)]+)\\)', value, re.I))):
                    raise ValueError('External SVG resources are not authorized')
        width = _dimension(tree.get('width'))
        height = _dimension(tree.get('height'))
        box = [float(v) for v in re.split('[\\s,]+', tree.get('viewBox', '').strip()) if v]
        if len(box) == 4 and all((math.isfinite(v) for v in box)) and (box[2] > 0) and (box[3] > 0):
            width = width or (height * box[2] / box[3] if height else box[2])
            height = height or width * box[3] / box[2]
        if width is None or height is None or (not 0 < width <= 32767) or (not 0 < height <= 32767) or (width * height > MAX_SVG_PIXELS):
            raise ValueError('The SVG requires a finite viewport within the image budget')
        self.text = text
        self.width = width
        self.height = height
        self.rect = Rect(0, 0, width * 0.75, height * 0.75)

    def __len__(self):
        return 1

    def __getitem__(self, index):
        if index != 0:
            raise ValueError('The SVG has only one page')
        return SvgPage(self)

    def close(self):
        pass

    def convert_to_pdf(self):
        from reportlab.pdfgen.canvas import Canvas
        from reportlab.lib.utils import ImageReader
        output = io.BytesIO()
        canvas = Canvas(output, pagesize=(self.rect.width, self.rect.height))
        image = self[0].get_pixmap(dpi=144, alpha=True).image
        canvas.drawImage(ImageReader(image), 0, 0, self.rect.width, self.rect.height, mask='auto')
        canvas.showPage()
        canvas.save()
        return output.getvalue()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

class SvgPage:

    def __init__(self, document):
        self.document = document
        self.rect = document.rect

    def get_pixmap(self, *, matrix=None, dpi=None, alpha=False, clip=None, **_kwargs):
        if clip is not None:
            raise ValueError('SVG clipping requires an explicit source range adapter')
        scale = dpi / 72 if dpi else matrix.a if matrix else 1
        width = math.ceil(self.rect.width * scale)
        height = math.ceil(self.rect.height * scale)
        if width <= 0 or height <= 0 or width * height > MAX_SVG_PIXELS:
            raise ValueError('The SVG render exceeds its image budget')
        import resvg_py
        from openclass_core.services.pdf_backend import Pixmap
        raw = resvg_py.svg_to_bytes(svg_string=self.document.text, width=width, height=height, background=None if alpha else '#ffffff', resources_dir=None, log_information=False)
        return Pixmap(Image.open(io.BytesIO(raw)).convert('RGBA' if alpha else 'RGB'))
