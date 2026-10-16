import io
from reportlab.pdfgen.canvas import Canvas
from PIL import Image
from openclass_core.services import pdf_backend as pdf
import pytest


def original_pdf():
    output=io.BytesIO();canvas=Canvas(output,pagesize=(300,200))
    canvas.drawString(30,150,"Visible native text")
    canvas.bookmarkPage("first");canvas.addOutlineEntry("First page","first")
    canvas.save();return output.getvalue()


def test_pdfium_reads_original_renders_pages_and_preserves_geometry():
    content=original_pdf()
    with pdf.open(stream=content,filetype="pdf") as document:
        assert document.page_count==1
        assert document.get_toc()==[(1,"First page",1)]
        page=document[0]
        assert page.rect==pdf.Rect(0,0,300,200)
        assert "Visible native text" in page.get_text()
        traces=page.get_texttrace()
        assert traces and all(span["type"]==0 for span in traces)
        image=Image.open(io.BytesIO(page.get_pixmap(dpi=144).tobytes("png")))
        assert image.size==(600,400)
        assert page.get_pixmap(dpi=144,clip=pdf.Rect(0,0,150,100)).image.size==(300,200)
        assert content==document.content


def test_derived_contact_sheet_can_render_a_bounded_original_page():
    with pdf.open(stream=original_pdf(),filetype="pdf") as original:
        with pdf.open() as sheet:
            page=sheet.new_page(width=400,height=300)
            page.insert_text((10,20),"PDF 1")
            page.show_pdf_page(pdf.Rect(50,30,350,230),original,0)
            result=sheet.tobytes()
    with pdf.open(stream=result,filetype="pdf") as document:
        assert "PDF 1" in document[0].get_text()
        assert document[0].get_images()


def test_oversized_pdf_page_fails_before_native_bitmap_allocation(monkeypatch):
    from pypdf import PdfWriter
    output=io.BytesIO();writer=PdfWriter();writer.add_blank_page(width=10_000_000,height=10_000_000);writer.write(output)
    with pdf.open(stream=output.getvalue(),filetype='pdf') as document:
        monkeypatch.setattr(document[0]._page,'render',lambda **_:pytest.fail('The oversized page must not allocate a native bitmap'))
        with pytest.raises(ValueError,match='rendering budget'):document[0].get_pixmap()
