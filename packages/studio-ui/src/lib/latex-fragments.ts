const CJK_TEXT = /[\u3400-\u9fff]/;
const STRONG_MATH_SIGNAL =
  /\\(?:begin|end|frac|sqrt|lim|sum|prod|int|sin|cos|tan|ln|log|exp|to|left|right|leftarrow|rightarrow|leftrightarrow|Leftarrow|Rightarrow|Leftrightarrow|Longleftarrow|Longrightarrow|Longleftrightarrow|infty|cdot|times|div|leq?|geq?|approx|neq?|pm|sim|in|notin|mid|subseteq?|supseteq?|cup|cap|mathbb|mathcal|mathfrak|mathbf|boldsymbol|mathrm|operatorname|text|ce|pu|dots|cdots|ldots|vdots|partial|nabla|forall|exists|alpha|beta|gamma|delta|epsilon|varepsilon|zeta|eta|theta|iota|kappa|lambda|mu|xi|pi|rho|varrho|sigma|tau|upsilon|phi|varphi|chi|psi|omega|Gamma|Delta|Theta|Lambda|Xi|Pi|Sigma|Phi|Psi|Omega)(?![A-Za-z])|[_^]|[=<>≤≥≈≠]|[A-Za-z0-9)]\s*(?:[+\-−*/=<>≤≥≈≠±]|→|←)\s*[A-Za-z0-9(\\]|\d+\s*\/\s*\d+|\\[{}]|^\([^()\n]{1,80},[^()\n]{1,80}\)$|^[\[(][A-Za-z0-9α-ωΑ-Ω\\_{}\s+\-−*/=#.,]+,[A-Za-z0-9α-ωΑ-Ω\\_{}\s+\-−*/=#.,]+[\])]$|^[A-Za-z]{1,3}\s*\([A-Za-z0-9α-ωΑ-Ω\\_{}\[\]^()+\-−*/=#·∞→←≤≥≈≠±<>|&:'\s.,]+\)$/;
const DELIMITED_MATH = /\\\[([\s\S]+?)\\\]|\\\((.+?)\\\)|\$\$([\s\S]+?)\$\$|\$(?!\$)([^$\n]+?)\$(?!\$)/g;
const RAW_LATEX_COMMAND =
  /\\(?:begin|end|frac|dfrac|tfrac|sqrt|lim|sum|prod|int|sin|cos|tan|ln|log|exp|to|left|right|leftarrow|rightarrow|leftrightarrow|Leftarrow|Rightarrow|Leftrightarrow|Longleftarrow|Longrightarrow|Longleftrightarrow|infty|cdot|times|div|leq?|geq?|approx|neq?|pm|sim|in|notin|mid|subseteq?|supseteq?|cup|cap|mathbb|mathcal|mathfrak|mathbf|boldsymbol|mathrm|operatorname|text|ce|pu|dots|cdots|ldots|vdots|partial|nabla|forall|exists|alpha|beta|gamma|delta|epsilon|varepsilon|zeta|eta|theta|iota|kappa|lambda|mu|xi|pi|rho|varrho|sigma|tau|upsilon|phi|varphi|chi|psi|omega|Gamma|Delta|Theta|Lambda|Xi|Pi|Sigma|Phi|Psi|Omega)(?![A-Za-z])/g;
const ESCAPED_SET = /\\\{([^{}\n]{1,120})\\\}/g;
const ESCAPED_SET_SIGNAL = /[_^0-9,=<>≤≥≈≠+\-−*/]|\\[A-Za-z]+|[α-ωΑ-Ω]/;
const ORPHAN_MATH_DOLLAR = /(?:(?<=^)|(?<=[\s.,，。；;:：、]))\$(?=$|[\s.,，。；;:：、])/g;
const TRAILING_SENTENCE_MARKS = /[\s.,，。；;:：]+$/;
const LEADING_SENTENCE_MARKS = /^[\s.,，。；;:：]+/;
const LATIN_WORD = /[A-Za-z]+/g;
const NON_FORMULA_LETTER = /[\u00c0-\u024f\u3400-\u9fff]/;
const FORMULA_CHARS = /^[A-Za-z0-9α-ωΑ-Ω\\_{}\[\]^()!+\-−*/=#·∞→←≤≥≈≠±<>|&:;'\s.,]+$/;
const LATEX_ENVIRONMENT = /\\(?:begin|end)\{[A-Za-z*]+\}/g;
const LATEX_TEXT_ARGUMENT = /\\(?:text|operatorname)\{[^{}]*\}/g;
const LATEX_ROMAN_ARGUMENT = /\\mathrm\{[A-Za-z0-9_{}\[\]^()!+\-−*/=#·∞→←≤≥≈≠±<>|&:;'\s.,]+\}/g;
const LATEX_CHEM_ARGUMENT = /\\(?:ce|pu)\{[^{}]*\}/g;
const LATEX_FUNCTIONS = new Set(["lim", "sin", "cos", "tan", "ln", "log", "sqrt", "exp"]);

export type MathSegment = {
  start: number;
  end: number;
  latex: string;
};

export function hasStrongMathSignal(value: string) {
  return STRONG_MATH_SIGNAL.test(value);
}

function latexValidationText(value: string) {
  return value.replace(LATEX_ENVIRONMENT, "").replace(LATEX_TEXT_ARGUMENT, "");
}

function withoutLatexCommands(value: string) {
  return latexValidationText(value)
    .replace(LATEX_ROMAN_ARGUMENT, "")
    .replace(LATEX_CHEM_ARGUMENT, "")
    .replace(/\\[A-Za-z]+/g, "");
}

function hasNonFormulaLetters(value: string) {
  return NON_FORMULA_LETTER.test(withoutLatexCommands(value));
}

function latinWordsAreFormulaLike(value: string) {
  for (const word of withoutLatexCommands(value).matchAll(LATIN_WORD)) {
    const token = word[0];
    if (token.length > 3 && !LATEX_FUNCTIONS.has(token)) {
      return false;
    }
  }
  return true;
}

export function isLikelyDelimitedMath(value: string) {
  const compact = value.trim().replace(TRAILING_SENTENCE_MARKS, "").replace(LEADING_SENTENCE_MARKS, "");
  const validationText = latexValidationText(compact);
  if (
    !compact ||
    !validationText ||
    !FORMULA_CHARS.test(validationText) ||
    hasNonFormulaLetters(compact) ||
    !latinWordsAreFormulaLike(compact)
  ) {
    return false;
  }
  return (
    hasStrongMathSignal(compact) ||
    /^[A-Za-zα-ωΑ-Ω]$/.test(validationText) ||
    /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(validationText)
  );
}

function normalizeLimitSubscript(value: string) {
  return value
    .replaceAll("→", "\\to ")
    .replaceAll("∞", "\\infty")
    .replaceAll("+\\infty", "+\\infty")
    .replaceAll("-\\infty", "-\\infty")
    .replace(/\s+/g, " ")
    .trim();
}

export function normalizeLatex(value: string) {
  let latex = value.trim().replace(TRAILING_SENTENCE_MARKS, "").replace(LEADING_SENTENCE_MARKS, "");

  latex = latex
    .replaceAll("−", "-")
    .replaceAll("→", "\\to ")
    .replaceAll("←", "\\leftarrow ")
    .replaceAll("∞", "\\infty")
    .replaceAll("·", "\\cdot ")
    .replaceAll("≤", "\\le ")
    .replaceAll("≥", "\\ge ")
    .replaceAll("≈", "\\approx ")
    .replaceAll("≠", "\\ne ")
    .replaceAll("±", "\\pm ")
    .replace(/，/g, ",\\quad ")
    .replace(/(?<!\\)\blim_\{([^}]+)\}/g, (_, subscript: string) => `\\lim_{${normalizeLimitSubscript(subscript)}}`)
    .replace(/(?<!\\)\b(sin|cos|tan|ln|log|sqrt|exp)\b/g, "\\$1");

  latex = latex
    .replace(/([A-Za-z]'?\([^()]+\))\s*\/\s*([A-Za-z]'?\([^()]+\))/g, "\\frac{$1}{$2}")
    .replace(
      /(\\(?:sin|cos|tan|ln|log|sqrt|exp)\s+[A-Za-z0-9]+(?:\^\{?[-+\w/]+\}?)?)\s*\/\s*\(([^()]+)\)/g,
      "\\frac{$1}{$2}"
    )
    .replace(
      /(\\(?:sin|cos|tan|ln|log|sqrt|exp)\s+[A-Za-z0-9]+(?:\^\{?[-+\w/]+\}?)?)\s*\/\s*([A-Za-z0-9]+(?:\^\{?[-+\w/]+\}?)?)/g,
      "\\frac{$1}{$2}"
    )
    .replace(/\(([^()]+)\)\s*\/\s*\(([^()]+)\)/g, "\\frac{$1}{$2}")
    .replace(/\(([^()]+)\)\s*\/\s*([A-Za-z0-9]+(?:\^\{?[-+\w/]+\}?)?)/g, "\\frac{$1}{$2}");

  return latex.replace(/\s+/g, " ").trim();
}

export function formulaOnlyLatex(text: string) {
  const compact = text.trim().replace(TRAILING_SENTENCE_MARKS, "");
  const bracketDelimited = compact.match(/^\\\[(.+?)\\\]$/) ?? compact.match(/^\\\((.+?)\\\)$/);

  if (bracketDelimited) {
    return normalizeLatex(bracketDelimited[1]);
  }

  const dollarDelimited = compact.match(/^\$\$?(.+?)\$\$?$/);
  if (dollarDelimited) {
    return isLikelyDelimitedMath(dollarDelimited[1]) ? normalizeLatex(dollarDelimited[1]) : null;
  }

  if (!compact || CJK_TEXT.test(compact) || !hasStrongMathSignal(compact)) {
    return null;
  }

  return isLikelyDelimitedMath(compact) ? normalizeLatex(compact) : null;
}

export function stripOrphanMathDollars(value: string) {
  if (value.split("$").length % 2 === 1 || !hasStrongMathSignal(value)) {
    return value;
  }
  return value.replace(ORPHAN_MATH_DOLLAR, "");
}

function isRawFormulaChar(char: string) {
  return /[A-Za-z0-9α-ωΑ-Ω\\_{}\[\]^()!+\-−*/=#<>≤≥≈≠±|&';→←∞·\s]/.test(char);
}

function trimFragmentBounds(value: string, start: number, end: number) {
  while (start < end && /\s/.test(value[start] ?? "")) {
    start += 1;
  }
  while (end > start && /\s/.test(value[end - 1] ?? "")) {
    end -= 1;
  }
  return { start, end };
}

function rawLatexCandidateMatches(value: string) {
  const candidates: Array<{ index: number; text: string }> = [];
  for (const match of value.matchAll(RAW_LATEX_COMMAND)) {
    candidates.push({ index: match.index ?? 0, text: match[0] });
  }
  for (const match of value.matchAll(ESCAPED_SET)) {
    const body = match[1]?.trim() ?? "";
    if (ESCAPED_SET_SIGNAL.test(body) || /^[A-Za-z]$/.test(body)) {
      candidates.push({ index: match.index ?? 0, text: match[0] });
    }
  }
  return candidates.sort((left, right) => left.index - right.index || right.text.length - left.text.length);
}

function rawLatexSegments(value: string): MathSegment[] {
  const segments: MathSegment[] = [];
  for (const match of rawLatexCandidateMatches(value)) {
    let start = match.index;
    let end = start + match.text.length;
    while (start > 0 && isRawFormulaChar(value[start - 1] ?? "")) {
      start -= 1;
    }
    while (end < value.length && isRawFormulaChar(value[end] ?? "")) {
      end += 1;
    }
    ({ start, end } = trimFragmentBounds(value, start, end));
    if (start >= end) {
      continue;
    }
    const raw = value.slice(start, end);
    if (!isLikelyDelimitedMath(raw)) {
      continue;
    }
    segments.push({ start, end, latex: normalizeLatex(raw) });
  }

  if (!segments.length) {
    return [];
  }

  const merged: MathSegment[] = [];
  for (const segment of segments.sort((left, right) => left.start - right.start || right.end - left.end)) {
    const previous = merged[merged.length - 1];
    if (!previous || segment.start > previous.end) {
      merged.push({
        start: segment.start,
        end: segment.end,
        latex: normalizeLatex(value.slice(segment.start, segment.end)),
      });
      continue;
    }
    const start = Math.min(previous.start, segment.start);
    const end = Math.max(previous.end, segment.end);
    const raw = value.slice(start, end).trim();
    if (isLikelyDelimitedMath(raw)) {
      previous.start = start;
      previous.end = end;
      previous.latex = normalizeLatex(raw);
    } else if (segment.end - segment.start > previous.end - previous.start) {
      previous.start = segment.start;
      previous.end = segment.end;
      previous.latex = normalizeLatex(value.slice(segment.start, segment.end));
    }
  }
  return merged;
}

function delimitedMathSegments(text: string): MathSegment[] {
  const segments: MathSegment[] = [];

  for (const match of text.matchAll(DELIMITED_MATH)) {
    const raw = match[0];
    const rawStart = match.index ?? 0;
    const latex = match[1] ?? match[2] ?? match[3] ?? match[4];

    if (!latex?.trim()) {
      continue;
    }
    if (!isLikelyDelimitedMath(latex)) {
      continue;
    }

    segments.push({
      start: rawStart,
      end: rawStart + raw.length,
      latex: normalizeLatex(latex),
    });
  }

  return segments.sort((left, right) => left.start - right.start);
}

function overlapsDelimited(segment: MathSegment, delimitedSegments: MathSegment[]) {
  return delimitedSegments.some((delimited) => segment.start < delimited.end && segment.end > delimited.start);
}

export function inlineMathSegments(text: string): MathSegment[] {
  const delimitedSegments = delimitedMathSegments(text);
  const rawSegments = rawLatexSegments(text).filter((segment) => !overlapsDelimited(segment, delimitedSegments));
  return [...delimitedSegments, ...rawSegments].sort((left, right) => left.start - right.start);
}
