from __future__ import annotations
import re
from collections.abc import Callable
LatexFragment = tuple[int, int, str]
_RAW_LATEX_COMMAND_RE = re.compile('\\\\(?:begin|end|frac|dfrac|tfrac|sqrt|lim|sum|prod|int|sin|cos|tan|ln|log|exp|to|left|right|leftarrow|rightarrow|leftrightarrow|Leftarrow|Rightarrow|Leftrightarrow|Longleftarrow|Longrightarrow|Longleftrightarrow|infty|cdot|times|div|leq?|geq?|approx|neq?|pm|sim|in|notin|mid|subseteq?|supseteq?|cup|cap|mathbb|mathcal|mathfrak|mathbf|boldsymbol|mathrm|operatorname|text|ce|pu|dots|cdots|ldots|vdots|partial|nabla|forall|exists|alpha|beta|gamma|delta|epsilon|varepsilon|zeta|eta|theta|iota|kappa|lambda|mu|xi|pi|rho|varrho|sigma|tau|upsilon|phi|varphi|chi|psi|omega|Gamma|Delta|Theta|Lambda|Xi|Pi|Sigma|Phi|Psi|Omega)(?![A-Za-z])')
_ESCAPED_SET_RE = re.compile('\\\\\\{(?P<body>[^{}\\n]{1,120})\\\\\\}')
_ESCAPED_SET_SIGNAL_RE = re.compile('[_^0-9,=<>≤≥≈≠+\\-−*/]|\\\\[A-Za-z]+|[α-ωΑ-Ω]')
_ORPHAN_MATH_DOLLAR_RE = re.compile('(?:(?<=^)|(?<=[\\s.,，。；;:：、]))\\$(?=$|[\\s.,，。；;:：、])')
_RAW_FORMULA_CHAR_RE = re.compile("[A-Za-z0-9α-ωΑ-Ω\\\\_{}\\[\\]^()!+\\-−*/=#<>≤≥≈≠±|&';→←∞·\\s]")

def strip_orphan_math_dollars(value: str, *, has_math_signal: Callable[[str], bool]) -> str:
    if value.count('$') % 2 == 0 or not has_math_signal(value):
        return value
    return _ORPHAN_MATH_DOLLAR_RE.sub('', value)

def _trim_fragment_bounds(value: str, start: int, end: int) -> tuple[int, int]:
    while start < end and value[start].isspace():
        start += 1
    while end > start and value[end - 1].isspace():
        end -= 1
    return (start, end)

def _is_raw_formula_char(char: str) -> bool:
    return bool(_RAW_FORMULA_CHAR_RE.fullmatch(char))

def _raw_latex_candidate_matches(value: str) -> list[re.Match[str]]:
    candidates = list(_RAW_LATEX_COMMAND_RE.finditer(value))
    for match in _ESCAPED_SET_RE.finditer(value):
        body = match.group('body').strip()
        if _ESCAPED_SET_SIGNAL_RE.search(body) or re.fullmatch('[A-Za-z]', body):
            candidates.append(match)
    return sorted(candidates, key=lambda item: (item.start(), -item.end()))

def find_raw_latex_fragments(value: str, *, is_likely_math: Callable[[str], bool], normalize_latex: Callable[[str], str]) -> list[LatexFragment]:
    fragments: list[LatexFragment] = []
    for match in _raw_latex_candidate_matches(value):
        start = match.start()
        end = match.end()
        while start > 0 and _is_raw_formula_char(value[start - 1]):
            start -= 1
        while end < len(value) and _is_raw_formula_char(value[end]):
            end += 1
        start, end = _trim_fragment_bounds(value, start, end)
        if start >= end:
            continue
        raw = value[start:end]
        if not is_likely_math(raw):
            continue
        fragments.append((start, end, normalize_latex(raw)))
    if not fragments:
        return []
    merged: list[LatexFragment] = []
    for start, end, _latex in sorted(fragments, key=lambda item: (item[0], -item[1])):
        if not merged or start > merged[-1][1]:
            merged.append((start, end, normalize_latex(value[start:end])))
            continue
        previous_start, previous_end, _previous_latex = merged[-1]
        next_start = min(previous_start, start)
        next_end = max(previous_end, end)
        raw = value[next_start:next_end].strip()
        if is_likely_math(raw):
            merged[-1] = (next_start, next_end, normalize_latex(raw))
        elif end - start > previous_end - previous_start:
            merged[-1] = (start, end, normalize_latex(value[start:end]))
    return merged
