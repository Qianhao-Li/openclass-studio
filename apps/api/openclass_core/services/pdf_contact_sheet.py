"""Derived PDF contact sheets retain explicit original-page/clip mapping upstream."""
import io
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.utils import ImageReader
from openclass_core.services.pdf_geometry import Rect

class ContactSheet:

    def __init__(self):
        self.output = io.BytesIO()
        self.canvas = Canvas(self.output)
        self.pages = []
        self.done = False

    def new_page(self, width=595, height=842):
        if self.pages:
            self.canvas.showPage()
        self.canvas.setPageSize((width, height))
        page = SheetPage(self, Rect(0, 0, width, height))
        self.pages.append(page)
        return page

    def tobytes(self, **_kwargs):
        if not self.done:
            self.canvas.save()
            self.done = True
        return self.output.getvalue()

    def save(self, destination, **kwargs):
        data = self.tobytes(**kwargs)
        if hasattr(destination, 'write'):
            destination.write(data)
        else:
            from pathlib import Path
            Path(destination).write_bytes(data)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def __len__(self):
        return len(self.pages)

class SheetPage:

    def __init__(self, document, rect):
        self.document = document
        self.rect = rect

    def insert_text(self, point, text, fontsize=12, **_kwargs):
        canvas = self.document.canvas
        canvas.setFont('Helvetica', fontsize)
        canvas.drawString(point[0], self.rect.height - point[1], text)

    def show_pdf_page(self, rect, source, page_number, clip=None, keep_proportion=True, **_kwargs):
        rect = Rect(rect)
        page = source.load_page(page_number)
        image = page.get_pixmap(dpi=144, clip=clip).image
        self.document.canvas.drawImage(ImageReader(image), rect.x0, self.rect.height - rect.y1, width=rect.width, height=rect.height, preserveAspectRatio=keep_proportion, mask='auto')
