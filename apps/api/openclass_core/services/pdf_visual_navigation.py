"""Build navigation ranges from validated AI-observed PDF index metadata only.

The caller validates pagination segments, their visual anchors and external-row
exclusions against the delivered pages before using this helper, and validates
the returned nodes with the complete read report before publishing anything.
This module never opens a source file, renders pages, extracts text or calls AI.
"""
from __future__ import annotations
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from openclass_core.services.pdf_directory_contract import PDFDirectoryContractError, PDFDirectoryReadReport, parse_pdf_printed_page_label, validate_pdf_directory_report
from openclass_core.services.source_ai_directory import AIDirectoryNodeProposal, AIDirectoryProposal

class PDFVisualNavigationError(ValueError):
    """Observed navigation or explicit ranges contain a provable contradiction."""

def derive_outside_body_leaf_keys(report: PDFDirectoryReadReport, *, page_count: int, page_classifications: Mapping[int, str], existing_chapters: Sequence[AIDirectoryNodeProposal]=()) -> list[str]:
    """Resolve only numeric leaf references outside already-validated full body coverage.

    The caller first validates this report against the actual delivered images.
    Original observations remain in the report; no physical range is invented.
    """
    if not report.directory_complete or not report.pagination_segments:
        return []
    by_printed = sorted(report.pagination_segments, key=lambda segment: segment.printed_start)
    if any((right.printed_start <= left.printed_end for left, right in zip(by_printed, by_printed[1:]))):
        return []
    body_start = min((segment.physical_start for segment in report.pagination_segments))
    known_prefix = set(report.directory_pages) | {page for page, kind in page_classifications.items() if kind == 'other' and page in report.inspected_pages}
    if not set(range(1, body_start)).issubset(known_prefix):
        return []
    parents = {row.parent_key for row in report.observed_directory_rows if row.parent_key}
    located = {node.key for node in existing_chapters}
    outside = []
    for row in report.observed_directory_rows:
        printed = parse_pdf_printed_page_label(row.printed_page_label)
        if row.key in parents or row.key in located or printed is None:
            continue
        if not any((segment.printed_start <= printed <= segment.printed_end for segment in report.pagination_segments)):
            outside.append(row.key)
    if not outside:
        return []
    proposed = report.model_copy(update={'status': 'partial', 'completion_basis': 'none', 'external_directory_keys': list(dict.fromkeys([*report.external_directory_keys, *outside]))})
    try:
        validate_pdf_directory_report(proposed, page_count=page_count, delivered_pages=report.inspected_pages, authorized_pages=range(1, page_count + 1), node_ranges={node.key: (node.source_range.start, node.source_range.end) for node in existing_chapters})
    except PDFDirectoryContractError:
        return []
    return outside

@dataclass(frozen=True)
class _Entry:
    key: str
    title: str
    parent_key: str
    printed_label: str
    explicit: AIDirectoryNodeProposal | None

@dataclass(frozen=True)
class _Location:
    start: int | None
    extent: int | None
    complete_tree: bool

def build_pdf_visual_navigation(report: PDFDirectoryReadReport, existing_chapters: Sequence[AIDirectoryNodeProposal]=(), *, allow_partial_trees: bool=False) -> list[AIDirectoryNodeProposal]:
    """Return complete located trees; unresolved rows remain in the input report.

    Numeric printed labels require one unambiguous linear pagination segment.
    Previously validated explicit nodes may supply nonnumeric positions or
    disambiguate repeated printed numbering. No labels or parents are invented.
    Missing keys are the local report row keys absent from the returned nodes.
    """
    explicit = AIDirectoryProposal(chapters=list(existing_chapters)).chapters
    if any((node.source_range.kind != 'pdf_pages' or node.source_range.container for node in explicit)):
        raise PDFVisualNavigationError('PDF navigation requires uncontained physical PDF page ranges.')
    if not report.observed_directory_rows:
        return explicit
    explicit_by_key = {node.key: node for node in explicit}
    external = set(report.external_directory_keys)
    entries: dict[str, _Entry] = {}
    all_keys = set()
    for row in report.observed_directory_rows:
        if row.key in all_keys:
            raise PDFVisualNavigationError(f'Duplicate observed directory row key: {row.key}')
        all_keys.add(row.key)
        prior = explicit_by_key.get(row.key)
        if row.key in external:
            printed = parse_pdf_printed_page_label(row.printed_page_label)
            if prior is not None or printed is None or (not report.pagination_segments) or any((segment.printed_start <= printed <= segment.printed_end for segment in report.pagination_segments)):
                raise PDFVisualNavigationError(f'External directory exclusion conflicts with known navigation: {row.key}')
            continue
        if prior is not None and (prior.parent_key != row.parent_key or prior.title != row.title):
            raise PDFVisualNavigationError(f'An explicit node conflicts with its observed row identity: {row.key}')
        entries[row.key] = _Entry(row.key, row.title, row.parent_key, row.printed_page_label, prior)
    if external - all_keys:
        raise PDFVisualNavigationError('An external directory exclusion references an unknown row.')
    for node in sorted(explicit, key=lambda item: item.order_index):
        if node.key not in entries:
            entries[node.key] = _Entry(node.key, node.title, node.parent_key, '', node)
    children: dict[str, list[str]] = {'': []}
    for key, entry in entries.items():
        children.setdefault(key, [])
        if entry.parent_key and entry.parent_key not in entries:
            raise PDFVisualNavigationError(f'An observed directory row has an unavailable parent: {key}')
        children.setdefault(entry.parent_key, []).append(key)
    locations: dict[str, _Location] = {}
    visiting: set[str] = set()

    def locate(key: str, depth: int=1) -> _Location:
        if key in visiting:
            raise PDFVisualNavigationError('Observed directory rows contain a parent cycle.')
        if depth > 12:
            raise PDFVisualNavigationError('The observed directory exceeds the supported tree depth.')
        if key in locations:
            return locations[key]
        visiting.add(key)
        entry = entries[key]
        descendants = [locate(child, depth + 1) for child in children[key]]
        printed = parse_pdf_printed_page_label(entry.printed_label)
        matching = [segment for segment in report.pagination_segments if printed is not None and segment.printed_start <= printed <= segment.printed_end]
        starts = [(segment.physical_start + printed - segment.printed_start, segment.physical_end) for segment in matching]
        prior = entry.explicit
        selected = starts[0] if len(starts) == 1 else None
        if len(starts) > 1 and prior is not None:
            compatible = [item for item in starts if item[0] == prior.source_range.start]
            if len(compatible) != 1:
                raise PDFVisualNavigationError(f'An explicit node conflicts with ambiguous pagination: {key}')
            selected = compatible[0]
        if selected is not None:
            start, extent = selected
            if prior is not None and prior.source_range.start != start:
                raise PDFVisualNavigationError(f'An explicit start conflicts with the pagination segment: {key}')
        elif prior is not None:
            if printed is not None and any((segment.physical_start <= prior.source_range.start <= segment.physical_end for segment in report.pagination_segments)):
                raise PDFVisualNavigationError(f'An explicit printed label conflicts with the pagination segment: {key}')
            start, extent = (prior.source_range.start, prior.source_range.end)
        elif not entry.printed_label and descendants and all((item.start is not None for item in descendants)):
            start = min((item.start for item in descendants if item.start is not None))
            extent = None
        else:
            start, extent = (None, None)
        known_extents = [item.extent for item in descendants if item.extent is not None]
        if extent is not None:
            known_extents.append(extent)
        extent = max(known_extents) if known_extents else None
        if start is not None and any((item.start is not None and item.start < start for item in descendants)):
            raise PDFVisualNavigationError(f'An observed child starts before its parent: {key}')
        result = _Location(start, extent, start is not None and all((item.complete_tree for item in descendants)))
        locations[key] = result
        visiting.remove(key)
        return result
    for key in entries:
        locate(key)
    for siblings in children.values():
        starts = [locations[key].start for key in siblings if locations[key].start is not None]
        if starts != sorted(starts):
            raise PDFVisualNavigationError('Observed sibling order contradicts their physical start pages.')

    def build_tree(key: str, *, level: int, parent_end: int | None, next_start: int | None, unknown_next: bool=False) -> list[AIDirectoryNodeProposal] | None:
        entry, location = (entries[key], locations[key])
        if location.start is None or (not location.complete_tree and (not allow_partial_trees)):
            return None
        if unknown_next and entry.explicit is None:
            return None
        if next_start is None and parent_end is None and (not report.directory_complete) and (entry.explicit is None):
            return None
        bounds = [bound for bound in (parent_end, next_start, location.extent) if bound is not None]
        if not bounds:
            return None
        end = min(bounds)
        if entry.explicit is not None:
            if entry.explicit.source_range.end > end:
                raise PDFVisualNavigationError(f'An explicit range exceeds its observed navigation boundary: {key}')
            end = entry.explicit.source_range.end
        if end < location.start:
            raise PDFVisualNavigationError(f'An observed node lies outside its parent or sibling boundary: {key}')
        descendants: list[AIDirectoryNodeProposal] = []
        siblings = children[key]
        for index, child in enumerate(siblings):
            child_next = locations[siblings[index + 1]].start if index + 1 < len(siblings) else None
            subtree = build_tree(child, level=level + 1, parent_end=end, next_start=child_next, unknown_next=index + 1 < len(siblings) and child_next is None)
            if subtree is None:
                if allow_partial_trees:
                    continue
                return None
            descendants.extend(subtree)
        node = AIDirectoryNodeProposal(key=key, parent_key=entry.parent_key, title=entry.title, number=entry.explicit.number if entry.explicit is not None else '', level=level, order_index=0, source_range={'kind': 'pdf_pages', 'start': location.start, 'end': end, 'container': ''}, evidence_excerpt=entry.explicit.evidence_excerpt if entry.explicit is not None else entry.title)
        return [node, *descendants]
    output: list[AIDirectoryNodeProposal] = []
    roots = children['']
    for index, key in enumerate(roots):
        next_start = locations[roots[index + 1]].start if index + 1 < len(roots) else None
        subtree = build_tree(key, level=1, parent_end=None, next_start=next_start, unknown_next=index + 1 < len(roots) and next_start is None)
        if subtree is not None:
            output.extend(subtree)
        else:
            root_keys: set[str] = set()
            pending = [key]
            while pending:
                descendant = pending.pop()
                root_keys.add(descendant)
                pending.extend(children[descendant])
            output.extend((node for node in explicit if node.key in root_keys))
    output = [node.model_copy(update={'order_index': index}) for index, node in enumerate(output)]
    return AIDirectoryProposal(chapters=output).chapters
