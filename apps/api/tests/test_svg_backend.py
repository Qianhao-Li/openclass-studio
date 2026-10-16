import pytest
from openclass_core.services import pdf_backend as pdf


def test_svg_renders_the_authorized_carrier_without_local_ocr():
    raw=b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="80"><rect width="50" height="40" fill="red"/></svg>'
    with pdf.open(stream=raw,filetype='svg') as doc:
        image=doc[0].get_pixmap(matrix=pdf.Matrix(2,2)).image
    assert image.size==(150,120)
    assert image.getpixel((10,10))==(255,0,0)
    assert image.getpixel((140,110))==(255,255,255)


@pytest.mark.parametrize('reference',['file:///etc/passwd','https://untrusted.example/image.png','../private-image.png'])
def test_svg_cannot_expand_original_authorization_to_external_resources(reference):
    raw=f'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><image href="{reference}"/></svg>'.encode()
    with pytest.raises(ValueError,match='not authorized'):
        pdf.open(stream=raw,filetype='svg')
