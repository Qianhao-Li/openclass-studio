from __future__ import annotations
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile
_WORD_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
_MATH_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/math'
_NS = {'w': _WORD_NS, 'm': _MATH_NS}
_RAW_LATEX_RE = re.compile('\\\\(?:frac|dfrac|tfrac|begin|end|left|right|xi|Phi|Delta|sum|int|lim|varepsilon|epsilon)\\b')
_RAW_MARKERS = ('\\(', '\\[', '$$', 'displaystyle', 'begincases', 'a ^ x', 'Phi(x)')
_HTML_RE = re.compile('</?(?:h[1-6]|p|span|div|table|thead|tbody|tr|td|th|ul|ol|li|strong|em)\\b', re.I)
_SCRIPT_GLYPHS = set('₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ᵃᵇᶜᵈᵉᶠᵍⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻ')

class DocxExportQualityError(ValueError):
    pass

@dataclass(frozen=True)
class DocxExportQualityReport:
    raw_latex_markers: tuple[str, ...]
    html_markers: tuple[str, ...]
    plaintext_formula_markers: tuple[str, ...]
    omml_count: int
    display_omml_count: int
    table_count: int

    @property
    def passed(self) -> bool:
        return not self.raw_latex_markers and (not self.html_markers) and (not self.plaintext_formula_markers)

def _document_xml(path: Path) -> str:
    with ZipFile(path) as archive:
        return archive.read('word/document.xml').decode('utf-8', errors='replace')

def inspect_docx_export(path: Path) -> DocxExportQualityReport:
    xml_text = _document_xml(path)
    root = ET.fromstring(xml_text.encode('utf-8'))
    visible_text = ''.join((node.text or '' for node in root.findall('.//w:t', _NS)))
    raw_latex = [marker for marker in _RAW_MARKERS if marker in xml_text]
    raw_latex.extend(sorted(set(_RAW_LATEX_RE.findall(xml_text))))
    html_markers = sorted(set(_HTML_RE.findall(visible_text)))
    formula_plaintext: list[str] = []
    if any((glyph in visible_text for glyph in _SCRIPT_GLYPHS)) and any((op in visible_text for op in ('∫', '∑', 'lim'))):
        formula_plaintext.append('unicode-script-formula-text')
    return DocxExportQualityReport(raw_latex_markers=tuple(raw_latex), html_markers=tuple(html_markers), plaintext_formula_markers=tuple(formula_plaintext), omml_count=len(root.findall('.//m:oMath', _NS)), display_omml_count=len(root.findall('.//m:oMathPara', _NS)), table_count=len(root.findall('.//w:tbl', _NS)))

def assert_docx_export_quality(path: Path) -> DocxExportQualityReport:
    report = inspect_docx_export(path)
    if not report.passed:
        problems = []
        if report.raw_latex_markers:
            problems.append(f"raw LaTeX markers: {', '.join(report.raw_latex_markers)}")
        if report.html_markers:
            problems.append(f"HTML markers: {', '.join(report.html_markers)}")
        if report.plaintext_formula_markers:
            problems.append(f"plaintext formula markers: {', '.join(report.plaintext_formula_markers)}")
        raise DocxExportQualityError('; '.join(problems))
    return report
