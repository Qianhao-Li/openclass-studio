import pytest

from openclass_core.services.rich_document.core import _normalize_latex


@pytest.mark.parametrize(("source", "expected"), [
    (r"\sigma/\sqrt m", r"\frac{\sigma}{\sqrt m}"),
    (r"1/\sqrt{m}", r"\frac{1}{\sqrt{m}}"),
    (r"(a+b)/\sqrt[3]{m}", r"\frac{a+b}{\sqrt[3]{m}}"),
    (r"a/\frac{b}{c}", r"\frac{a}{\frac{b}{c}}"),
    (r"a/\sqrt{\frac{b}{c}}", r"\frac{a}{\sqrt{\frac{b}{c}}}"),
    (r"a/\sqrt m^2", r"\frac{a}{\sqrt m^2}"),
    (r"a/\mathrm{SE}", r"\frac{a}{\mathrm{SE}}"),
    (r"\frac{\sigma}{\sqrt{m}}", r"\frac{\sigma}{\sqrt{m}}"),
    (r"\begin{aligned} a&=b\\c&=d\end{aligned}", r"\begin{aligned} a&=b\\c&=d\end{aligned}"),
])
def test_fraction_conversion_preserves_complete_tex_arguments(source, expected):
    result = _normalize_latex(source)
    assert result == expected
    assert _normalize_latex(result) == result
