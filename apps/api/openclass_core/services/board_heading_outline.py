from __future__ import annotations
import re
from dataclasses import dataclass, field
from markdown_it import MarkdownIt
_CJK_NUMERALS = '〇零一二三四五六七八九十百千万两'
_CIRCLED_NUMERALS = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'

@dataclass
class BoardHeadingNode:
    title: str
    markdown_level: int
    numbering_level: int
    structural_level: int
    heading_order_index: int
    line_start: int
    heading_line_end: int
    subtree_line_end: int = 0
    parent: BoardHeadingNode | None = field(default=None, repr=False)
    children: list[BoardHeadingNode] = field(default_factory=list, repr=False)

    @property
    def heading_path(self) -> list[str]:
        path: list[str] = []
        current: BoardHeadingNode | None = self
        while current is not None:
            path.append(current.title)
            current = current.parent
        return list(reversed(path))

@dataclass(frozen=True)
class BoardHeadingTeachingUnit:
    heading: str
    heading_level: int
    heading_path: list[str]
    parent_heading: str
    heading_order_index: int
    line_start: int
    line_end: int
    has_child_headings: bool
    board_excerpt: str

class BoardHeadingTargetError(ValueError):
    pass

def build_board_heading_teaching_units(board_text: str, *, target_heading: str='') -> tuple[list[BoardHeadingTeachingUnit], list[str]]:
    lines = board_text.splitlines()
    roots = parse_board_heading_outline(board_text)
    all_nodes = list(_walk_preorder(roots))
    if not all_nodes:
        excerpt = board_text.strip()
        if not excerpt:
            return ([], [])
        title = target_heading.strip() or 'Board'
        return ([BoardHeadingTeachingUnit(heading=title, heading_level=0, heading_path=[title], parent_heading='', heading_order_index=0, line_start=0, line_end=len(lines), has_child_headings=False, board_excerpt=excerpt)], [title])
    target = resolve_board_heading_target(all_nodes, target_heading) if target_heading.strip() else None
    if target is not None:
        candidate_nodes = list(_walk_preorder(target.children)) if target.children else [target]
        target_path = target.heading_path
    elif len(roots) == 1 and roots[0].markdown_level == 1 and roots[0].children:
        candidate_nodes = list(_walk_preorder(roots[0].children))
        target_path = roots[0].heading_path
    else:
        candidate_nodes = all_nodes
        target_path = []
    units: list[BoardHeadingTeachingUnit] = []
    for node in candidate_nodes:
        own_line_end = node.children[0].line_start if node.children else node.subtree_line_end
        body_text = '\n'.join(lines[node.heading_line_end:own_line_end]).strip()
        if node.children and (not body_text):
            continue
        excerpt = '\n'.join(lines[node.line_start:own_line_end]).strip()
        if not excerpt:
            continue
        units.append(BoardHeadingTeachingUnit(heading=node.title, heading_level=node.structural_level, heading_path=node.heading_path, parent_heading=node.parent.title if node.parent is not None else '', heading_order_index=node.heading_order_index, line_start=node.line_start, line_end=own_line_end, has_child_headings=bool(node.children), board_excerpt=excerpt))
    return (units, target_path)

def board_heading_outline_payload(board_text: str) -> list[dict[str, object]]:
    roots = parse_board_heading_outline(board_text)
    return [{'heading': node.title, 'heading_path': node.heading_path, 'heading_level': node.structural_level, 'has_child_headings': bool(node.children)} for node in _walk_preorder(roots)]

def parse_board_heading_outline(board_text: str) -> list[BoardHeadingNode]:
    lines = board_text.splitlines()
    nodes: list[BoardHeadingNode] = []
    tokens = MarkdownIt().parse(board_text)
    for index, token in enumerate(tokens):
        if token.type != 'heading_open' or token.map is None:
            continue
        markdown_level = int(token.tag[1]) if token.tag.startswith('h') else 1
        title = tokens[index + 1].content.strip() if index + 1 < len(tokens) else ''
        numbering_level = _numbering_level_hint(title)
        nodes.append(BoardHeadingNode(title=title or f'Section {len(nodes) + 1}', markdown_level=markdown_level, numbering_level=numbering_level, structural_level=markdown_level, heading_order_index=len(nodes), line_start=token.map[0], heading_line_end=token.map[1]))
    roots: list[BoardHeadingNode] = []
    stack: list[BoardHeadingNode] = []
    for node in nodes:
        while stack and (not _heading_can_contain(stack[-1], node)):
            stack.pop()
        if stack:
            node.parent = stack[-1]
            stack[-1].children.append(node)
            node.structural_level = stack[-1].structural_level + 1
        else:
            roots.append(node)
        stack.append(node)
    for index, node in enumerate(nodes):
        node.subtree_line_end = len(lines)
        for candidate in nodes[index + 1:]:
            if candidate.structural_level <= node.structural_level:
                node.subtree_line_end = candidate.line_start
                break
    return roots

def _heading_can_contain(parent: BoardHeadingNode, child: BoardHeadingNode) -> bool:
    """Prefer explicit Markdown depth; use numbering only to refine equal-depth headings."""
    if parent.markdown_level < child.markdown_level:
        return True
    if parent.markdown_level > child.markdown_level:
        return False
    return parent.numbering_level > 0 and child.numbering_level > parent.numbering_level

def resolve_board_heading_target(nodes: list[BoardHeadingNode], target_heading: str) -> BoardHeadingNode:
    target = _normalize_heading(target_heading)
    exact = [node for node in nodes if _normalize_heading(node.title) == target]
    if len(exact) == 1:
        return exact[0]
    compatible = [node for node in nodes if target and (target in _normalize_heading(node.title) or _normalize_heading(node.title) in target)]
    if len(compatible) == 1:
        return compatible[0]
    if not exact and (not compatible):
        raise BoardHeadingTargetError(f'Heading target not found: {target_heading}')
    raise BoardHeadingTargetError(f'Heading target is ambiguous: {target_heading}')

def _walk_preorder(nodes: list[BoardHeadingNode]):
    for node in nodes:
        yield node
        yield from _walk_preorder(node.children)

def _normalize_heading(value: str) -> str:
    return re.sub('\\s+', ' ', value.strip().strip('#').strip()).casefold()

def _numbering_level_hint(title: str) -> int:
    value = title.strip()
    if re.match(f'^[{_CJK_NUMERALS}]+[、.．]\\s*', value):
        return 2
    if re.match(f'^[（(][{_CJK_NUMERALS}]+[）)]\\s*', value):
        return 3
    dotted = re.match('^(\\d+(?:\\.\\d+)+)(?:[.．、)]|\\s)', value)
    if dotted:
        return min(3 + len(dotted.group(1).split('.')), 12)
    if re.match('^\\d+[.．、]\\s*', value):
        return 4
    if re.match('^[（(]\\d+[）)]\\s*', value):
        return 5
    if value[:1] in _CIRCLED_NUMERALS:
        return 6
    return 0
