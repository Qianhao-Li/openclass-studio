from __future__ import annotations
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
_BODY_FONT = 'Times New Roman'
_EAST_ASIA_FONT = 'Songti SC'
_ACCENT_COLOR = RGBColor(15, 71, 97)

def _set_style_font(style, *, size: float | None=None, bold: bool | None=None, color: RGBColor | None=None) -> None:
    font = style.font
    font.name = _BODY_FONT
    if size is not None:
        font.size = Pt(size)
    if bold is not None:
        font.bold = bold
    if color is not None:
        font.color.rgb = color
    r_pr = style.element.get_or_add_rPr()
    r_fonts = r_pr.rFonts
    if r_fonts is not None:
        r_fonts.set(qn('w:eastAsia'), _EAST_ASIA_FONT)
        r_fonts.set(qn('w:cs'), _BODY_FONT)

def _ensure_paragraph_style(document, style_name: str, base_style: str='Normal'):
    styles = document.styles
    try:
        return styles[style_name]
    except KeyError:
        style = styles.add_style(style_name, WD_STYLE_TYPE.PARAGRAPH)
        style.base_style = styles[base_style]
        return style

def apply_textbook_docx_styles(document) -> None:
    normal = document.styles['Normal']
    _set_style_font(normal, size=11)
    normal.paragraph_format.space_after = Pt(6)
    title = document.styles['Title']
    _set_style_font(title, size=22, bold=True, color=_ACCENT_COLOR)
    title.paragraph_format.space_after = Pt(8)
    for style_name, size, before in (('Heading 1', 18, 18), ('Heading 2', 14, 10), ('Heading 3', 12.5, 8)):
        style = document.styles[style_name]
        _set_style_font(style, size=size, bold=True, color=_ACCENT_COLOR)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(4)
        style.paragraph_format.keep_with_next = True
    body_text = _ensure_paragraph_style(document, 'OpenClass Body Text')
    _set_style_font(body_text, size=11)
    body_text.paragraph_format.space_before = Pt(3)
    body_text.paragraph_format.space_after = Pt(6)
    compact = _ensure_paragraph_style(document, 'OpenClass Compact')
    _set_style_font(compact, size=10.5)
    compact.paragraph_format.space_before = Pt(1)
    compact.paragraph_format.space_after = Pt(1)
    formula = _ensure_paragraph_style(document, 'OpenClass Formula')
    _set_style_font(formula, size=11)
    formula.paragraph_format.space_before = Pt(4)
    formula.paragraph_format.space_after = Pt(6)
