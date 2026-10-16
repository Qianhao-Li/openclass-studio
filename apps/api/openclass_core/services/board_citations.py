"""Document-level citation provenance for AI-authored board documents.

A board document is Markdown at rest.  When an authoring turn derives content
from material the learner supplied — an uploaded book, a repository, a linked
page, a transcript — that content carries the ordinary scholarly footnote pair:
an inline ``[^n]`` marker inside the passage that used the material, and one
``[^n]: ...`` definition line in the reference list at the end of the document.

This module owns that pair.  It renumbers markers by first appearance, moves
every definition into a single trailing block, drops markers whose reference was
never written, and reports what it changed so the caller can log a contract
violation.  It knows nothing about subjects, source formats, or languages: a
reference entry is whatever line the authoring turn wrote after the colon.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
CITATION_ANCHOR_PREFIX = 'openclass-ref-'
MAX_CITATION_LABEL_LENGTH = 32
CITATION_MARKER_RE = re.compile(f'\\[\\^([^\\]\\s]{{1,{MAX_CITATION_LABEL_LENGTH}}})\\]')
CITATION_DEFINITION_RE = re.compile(f'^\\[\\^([^\\]\\s]{{1,{MAX_CITATION_LABEL_LENGTH}}})\\]:[ \\t]*(\\S.*)$')
_CODE_FENCE_RE = re.compile('^\\s{0,3}(`{3,}|~{3,})')

def citation_anchor_id(label: str) -> str:
    """The in-document anchor a marker jumps to."""
    return f'{CITATION_ANCHOR_PREFIX}{label}'

@dataclass(frozen=True)
class BoardCitationEntry:
    """One reference-list entry after normalization."""
    label: str
    text: str
    cited: bool

    @property
    def anchor_id(self) -> str:
        return citation_anchor_id(self.label)

@dataclass(frozen=True)
class BoardCitationNormalization:
    """The normalized Markdown plus what the authoring turn got wrong."""
    markdown: str
    entries: tuple[BoardCitationEntry, ...]
    dropped_marker_labels: tuple[str, ...]
    changed: bool
    label_map: dict[str, str] = field(default_factory=dict)

    @property
    def uncited_labels(self) -> tuple[str, ...]:
        return tuple((entry.label for entry in self.entries if not entry.cited))

def _next_fence(current: str, line: str) -> str:
    match = _CODE_FENCE_RE.match(line)
    if match is None:
        return current
    token = match.group(1)[0] * 3
    if not current:
        return token
    return '' if token == current else current

def _collect_definitions(lines: list[str]) -> tuple[dict[str, str], list[str], list[str]]:
    definitions: dict[str, str] = {}
    order: list[str] = []
    body: list[str] = []
    fence = ''
    for line in lines:
        next_fence = _next_fence(fence, line)
        if not fence and (not next_fence):
            match = CITATION_DEFINITION_RE.match(line.strip())
            if match is not None:
                label, text = (match.group(1), match.group(2).strip())
                if label not in definitions:
                    order.append(label)
                definitions[label] = text
                continue
        fence = next_fence
        body.append(line)
    return (definitions, order, body)

def _marker_appearance(body: list[str], definitions: dict[str, str]) -> list[str]:
    appearance: list[str] = []
    fence = ''
    for line in body:
        next_fence = _next_fence(fence, line)
        if fence or next_fence:
            fence = next_fence
            continue
        for match in CITATION_MARKER_RE.finditer(line):
            label = match.group(1)
            if label in definitions and label not in appearance:
                appearance.append(label)
    return appearance

def _rewrite_markers(body: list[str], renumbered: dict[str, str]) -> tuple[list[str], list[str]]:
    dropped: list[str] = []
    rewritten: list[str] = []
    fence = ''
    for line in body:
        next_fence = _next_fence(fence, line)
        if fence or next_fence:
            fence = next_fence
            rewritten.append(line)
            continue

        def replace(match: re.Match[str]) -> str:
            label = match.group(1)
            target = renumbered.get(label)
            if target is None:
                dropped.append(label)
                return ''
            return f'[^{target}]'
        rewritten.append(CITATION_MARKER_RE.sub(replace, line))
    return (rewritten, dropped)

def normalize_board_citations(markdown: str) -> BoardCitationNormalization:
    """Renumber citation markers by first appearance and collect the references.

    Definitions move to a single block at the end of the document in the order
    their markers first appear.  A definition that no marker points at keeps its
    entry after the cited ones; a marker whose definition is missing is removed,
    because a number that jumps nowhere is worse than no number.
    """
    if '[^' not in markdown:
        return BoardCitationNormalization(markdown=markdown, entries=(), dropped_marker_labels=(), changed=False)
    definitions, definition_order, body = _collect_definitions(markdown.splitlines())
    appearance = _marker_appearance(body, definitions)
    renumbered = {label: str(index) for index, label in enumerate(appearance, start=1)}
    for offset, label in enumerate((label for label in definition_order if label not in renumbered), start=len(appearance) + 1):
        renumbered[label] = str(offset)
    rewritten, dropped = _rewrite_markers(body, renumbered)
    entries = tuple((BoardCitationEntry(label=renumbered[label], text=definitions[label], cited=label in appearance) for label in sorted(definition_order, key=lambda item: int(renumbered[item]))))
    body_text = '\n'.join(rewritten).rstrip()
    definition_block = '\n'.join((f'[^{entry.label}]: {entry.text}' for entry in entries))
    if definition_block:
        normalized = f'{body_text}\n\n{definition_block}' if body_text else definition_block
    else:
        normalized = body_text
    return BoardCitationNormalization(markdown=normalized, entries=entries, dropped_marker_labels=tuple(dropped), changed=normalized != markdown, label_map=renumbered)

def validate_board_citation_pairs(markdown: str) -> str | None:
    """Validate a bounded edit without moving or renumbering any untouched text."""
    if '[^' not in markdown:
        return None
    definitions = set()
    markers = set()
    fence = ''
    for line in markdown.splitlines():
        next_fence = _next_fence(fence, line)
        if fence or next_fence:
            fence = next_fence
            continue
        definition = CITATION_DEFINITION_RE.match(line.strip())
        if definition is not None:
            label = definition.group(1)
            if label in definitions:
                return 'citation_definition_conflict'
            definitions.add(label)
        else:
            markers.update((match.group(1) for match in CITATION_MARKER_RE.finditer(line)))
    return 'citation_definition_missing' if markers - definitions else None
