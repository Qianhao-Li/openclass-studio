from __future__ import annotations
import html
import re
from collections.abc import Callable
from typing import Any, Literal
from markdown_it import MarkdownIt
from markdown_it.token import Token
_BLOCK_MATH_PLACEHOLDER = '\ue000BLOCKMATH:{index}\ue001'
_INLINE_MATH_PIPE_PLACEHOLDER = '\ue002'
_CHINESE_ORDERED_RE = re.compile('^(\\d+)、\\s+')
_MARKDOWN_HEADING_RE = re.compile('^#{1,6}\\s+')
_MARKDOWN_BULLET_RE = re.compile('^[-*]\\s+')
_MARKDOWN_ORDERED_RE = re.compile('^\\d+[.、]\\s+')
_MARKDOWN_TABLE_ROW_RE = re.compile('^\\s*\\|')
_MARKDOWN_FENCE_RE = re.compile('^```')
_MARKDOWN_BLOCKQUOTE_RE = re.compile('^>\\s?')
_BLOCK_MATH_LINE_RE = re.compile('^(\\\\\\[|\\$\\$)')
_INLINE_MATH_RE = re.compile('\\\\\\[[^\\n]*?\\\\\\]|\\\\\\([^\\n]*?\\\\\\)|\\$\\$[^\\n]*?\\$\\$|\\$(?!\\$)[^\\n$]+?\\$(?!\\$)')

def _is_structural_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return bool(_MARKDOWN_HEADING_RE.match(stripped) or _MARKDOWN_BULLET_RE.match(stripped) or _MARKDOWN_ORDERED_RE.match(stripped) or _MARKDOWN_TABLE_ROW_RE.match(stripped) or _MARKDOWN_FENCE_RE.match(stripped) or _MARKDOWN_BLOCKQUOTE_RE.match(stripped) or _BLOCK_MATH_LINE_RE.match(stripped))

def _normalize_chinese_ordered_lists(text: str) -> str:
    return '\n'.join((_CHINESE_ORDERED_RE.sub('\\1. ', line) for line in text.splitlines()))

def _protect_table_math_pipes(text: str) -> str:

    def protect_line(line: str) -> str:
        if not _MARKDOWN_TABLE_ROW_RE.match(line):
            return line
        return _INLINE_MATH_RE.sub(lambda match: match.group(0).replace('|', _INLINE_MATH_PIPE_PLACEHOLDER), line)
    return '\n'.join((protect_line(line) for line in text.splitlines()))

def _restore_table_math_pipes(value: str) -> str:
    return value.replace(_INLINE_MATH_PIPE_PLACEHOLDER, '|')

def _extract_block_math(text: str, normalize_latex: Callable[[str], str]) -> tuple[str, dict[str, str]]:
    lines = text.splitlines()
    placeholders: dict[str, str] = {}
    output: list[str] = []
    index = 0
    placeholder_index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            output.append(line)
            index += 1
            continue
        extracted = _extract_block_math_at(lines, index, normalize_latex)
        if extracted:
            latex, next_index = extracted
            key = str(placeholder_index)
            placeholders[key] = latex
            output.append(_BLOCK_MATH_PLACEHOLDER.format(index=key))
            output.append('')
            placeholder_index += 1
            index = next_index
            continue
        output.append(line)
        index += 1
    return ('\n'.join(output), placeholders)

def _extract_block_math_at(lines: list[str], index: int, normalize_latex: Callable[[str], str]) -> tuple[str, int] | None:
    line = lines[index].strip()
    delimiters = [('\\[', '\\]'), ('$$', '$$')]
    for opener, closer in delimiters:
        if line.startswith(opener) and line.endswith(closer) and (len(line) > len(opener) + len(closer)):
            latex = line[len(opener):-len(closer)].strip()
            return (normalize_latex(latex), index + 1)
        if line != opener:
            continue
        formula_lines: list[str] = []
        cursor = index + 1
        while cursor < len(lines):
            current = lines[cursor].strip()
            if current == closer:
                latex = '\n'.join(formula_lines).strip()
                if latex:
                    return (normalize_latex(latex), cursor + 1)
                return None
            formula_lines.append(lines[cursor])
            cursor += 1
    return None
_BLOCK_MATH_PLACEHOLDER_RE = re.compile('^\\uE000BLOCKMATH:\\d+\\uE001$')

def _insert_paragraph_breaks(text: str) -> str:
    lines = text.splitlines()
    output: list[str] = []
    previous_was_text = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            output.append('')
            previous_was_text = False
            continue
        if _BLOCK_MATH_PLACEHOLDER_RE.match(stripped):
            output.append(line)
            previous_was_text = False
            continue
        if _is_structural_line(line):
            output.append(line)
            previous_was_text = False
            continue
        if previous_was_text:
            output.append('')
        output.append(line)
        previous_was_text = True
    return '\n'.join(output)

def _preprocess_markdown(text: str, normalize_latex: Callable[[str], str]) -> tuple[str, dict[str, str]]:
    normalized = _normalize_chinese_ordered_lists(text)
    normalized = _protect_table_math_pipes(normalized)
    normalized, placeholders = _extract_block_math(normalized, normalize_latex)
    normalized = _insert_paragraph_breaks(normalized)
    return (normalized, placeholders)

def _create_parser() -> MarkdownIt:
    return MarkdownIt('commonmark', {'html': False, 'linkify': False, 'breaks': True}).enable('table')

def _render_block_math_html(latex: str) -> str:
    return f'<div data-type="block-math" data-latex="{html.escape(latex, quote=True)}"></div>'

def _render_block_math_node(latex: str) -> dict[str, Any]:
    return {'type': 'blockMath', 'attrs': {'latex': latex}}

def _inline_token_content(tokens: list[Token], index: int) -> str:
    token = tokens[index]
    if token.type != 'inline':
        return ''
    return _restore_table_math_pipes(token.content)

def _walk_inline_tokens(tokens: list[Token], index: int, inline_html: Callable[[str], str]) -> tuple[str, int]:
    token = tokens[index]
    if token.type != 'inline':
        return ('', index + 1)
    return (inline_html(token.content), index + 1)

def _table_rows_from_tokens(tokens: list[Token], start: int) -> tuple[list[list[str]], int]:
    rows: list[list[str]] = []
    index = start + 1
    current_row: list[str] = []
    while index < len(tokens):
        token = tokens[index]
        if token.type == 'table_close':
            return (rows, index + 1)
        if token.type in {'thead_open', 'thead_close', 'tbody_open', 'tbody_close'}:
            index += 1
            continue
        if token.type == 'tr_open':
            current_row = []
            index += 1
            continue
        if token.type == 'tr_close':
            if current_row:
                rows.append(current_row)
            index += 1
            continue
        if token.type in {'th_open', 'td_open'}:
            cell = _inline_token_content(tokens, index + 1)
            current_row.append(cell)
            index += 3
            continue
        index += 1
    return (rows, index)

def _table_html(rows: list[list[str]], inline_html: Callable[[str], str]) -> str:
    html_rows: list[str] = []
    for row_index, row in enumerate(rows):
        tag = 'th' if row_index == 0 else 'td'
        html_rows.append('<tr>' + ''.join((f'<{tag}>{inline_html(cell)}</{tag}>' for cell in row)) + '</tr>')
    return '<table><tbody>' + ''.join(html_rows) + '</tbody></table>'

def _table_node(rows: list[list[str]], paragraph_node: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    table_rows: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows):
        cell_type = 'tableHeader' if row_index == 0 else 'tableCell'
        table_rows.append({'type': 'tableRow', 'content': [{'type': cell_type, 'content': [paragraph_node(cell)]} for cell in row]})
    return {'type': 'table', 'content': table_rows}

def _render_list_html(tokens: list[Token], start: int, list_type: str, inline_html: Callable[[str], str]) -> tuple[str, int]:
    tag = 'ul' if list_type == 'bullet_list' else 'ol'
    parts: list[str] = [f'<{tag}>']
    index = start + 1
    while index < len(tokens):
        token = tokens[index]
        if token.type == f'{list_type}_close':
            parts.append(f'</{tag}>')
            return (''.join(parts), index + 1)
        if token.type == 'list_item_open':
            inline_content = _inline_token_content(tokens, index + 2)
            parts.append(f'<li>{inline_html(inline_content)}</li>')
            index += 4
            continue
        index += 1
    return (''.join(parts), index)

def _render_list_node(tokens: list[Token], start: int, list_type: str, paragraph_node: Callable[[str], dict[str, Any]]) -> tuple[dict[str, Any], int]:
    items: list[dict[str, Any]] = []
    index = start + 1
    while index < len(tokens):
        token = tokens[index]
        if token.type == f'{list_type}_close':
            return ({'type': list_type, 'content': items}, index + 1)
        if token.type == 'list_item_open':
            inline_content = _inline_token_content(tokens, index + 2)
            items.append({'type': 'listItem', 'content': [paragraph_node(inline_content)]})
            index += 4
            continue
        index += 1
    return ({'type': list_type, 'content': items}, index)

def _replace_block_math_placeholders(value: str, placeholders: dict[str, str]) -> str:
    for key, latex in placeholders.items():
        value = value.replace(_BLOCK_MATH_PLACEHOLDER.format(index=key), latex)
    return value

def _is_block_math_placeholder(line: str, placeholders: dict[str, str]) -> str | None:
    stripped = line.strip()
    for key in placeholders:
        placeholder = _BLOCK_MATH_PLACEHOLDER.format(index=key)
        if stripped == placeholder:
            return key
    return None

def parse_markdown_to_html(content_text: str, *, inline_html: Callable[[str], str], normalize_latex: Callable[[str], str], code_block_html: Callable[[str | None, str], str], classify_fenced_block: Callable[[str | None, str], Literal['code', 'formula', 'paragraph']], block_paragraph_html: Callable[[str], str | None] | None=None) -> str:
    processed, placeholders = _preprocess_markdown(content_text, normalize_latex)
    parser = _create_parser()
    tokens = parser.parse(processed)
    parts: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type == 'heading_open':
            level = min(int(token.tag[1]), 3)
            inline_content = _inline_token_content(tokens, index + 1)
            parts.append(f'<h{level}>{inline_html(inline_content)}</h{level}>')
            index += 3
            continue
        if token.type == 'paragraph_open':
            inline_content = _inline_token_content(tokens, index + 1)
            placeholder_key = _is_block_math_placeholder(inline_content.strip(), placeholders)
            block_html = block_paragraph_html(inline_content) if block_paragraph_html is not None else None
            if placeholder_key is not None:
                parts.append(_render_block_math_html(placeholders[placeholder_key]))
            elif block_html is not None:
                parts.append(_replace_block_math_placeholders(block_html, placeholders))
            else:
                rendered = inline_html(inline_content)
                rendered = _replace_block_math_placeholders(rendered, placeholders)
                parts.append(f'<p>{rendered}</p>')
            index += 3
            continue
        if token.type == 'fence':
            language = token.info.strip() or None
            if language in {'text', 'txt', 'plain', 'plaintext'}:
                language = None
            code = token.content.rstrip('\n')
            kind = classify_fenced_block(language, code)
            if kind == 'formula':
                parts.append(_render_block_math_html(normalize_latex(code)))
            elif kind == 'code':
                parts.append(code_block_html(language, code))
            else:
                parts.append(f'<p>{inline_html(code)}</p>')
            index += 1
            continue
        if token.type == 'blockquote_open':
            inline_content = _inline_token_content(tokens, index + 2)
            parts.append(f'<blockquote>{inline_html(inline_content)}</blockquote>')
            index += 4
            continue
        if token.type == 'bullet_list_open':
            rendered, index = _render_list_html(tokens, index, 'bullet_list', inline_html)
            parts.append(rendered)
            continue
        if token.type == 'ordered_list_open':
            rendered, index = _render_list_html(tokens, index, 'ordered_list', inline_html)
            parts.append(rendered)
            continue
        if token.type == 'table_open':
            rows, index = _table_rows_from_tokens(tokens, index)
            parts.append(_table_html(rows, inline_html))
            continue
        index += 1
    return '\n'.join(parts) or '<p></p>'

def parse_markdown_to_tiptap(content_text: str, *, inline_nodes: Callable[[str], list[dict[str, Any]]], normalize_latex: Callable[[str], str], code_block_node: Callable[[str | None, str], dict[str, Any]], paragraph_node: Callable[[str], dict[str, Any]], classify_fenced_block: Callable[[str | None, str], Literal['code', 'formula', 'paragraph']], block_paragraph_node: Callable[[str], dict[str, Any] | None] | None=None) -> dict[str, Any]:
    processed, placeholders = _preprocess_markdown(content_text, normalize_latex)
    parser = _create_parser()
    tokens = parser.parse(processed)
    nodes: list[dict[str, Any]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type == 'heading_open':
            level = min(int(token.tag[1]), 3)
            inline_content = _inline_token_content(tokens, index + 1)
            nodes.append({'type': 'heading', 'attrs': {'level': level}, 'content': inline_nodes(inline_content)})
            index += 3
            continue
        if token.type == 'paragraph_open':
            inline_content = _inline_token_content(tokens, index + 1)
            placeholder_key = _is_block_math_placeholder(inline_content.strip(), placeholders)
            block_node = block_paragraph_node(inline_content) if block_paragraph_node is not None else None
            if placeholder_key is not None:
                nodes.append(_render_block_math_node(placeholders[placeholder_key]))
            elif block_node is not None:
                nodes.append(block_node)
            else:
                nodes.append(paragraph_node(inline_content))
            index += 3
            continue
        if token.type == 'fence':
            language = token.info.strip() or None
            if language in {'text', 'txt', 'plain', 'plaintext'}:
                language = None
            code = token.content.rstrip('\n')
            kind = classify_fenced_block(language, code)
            if kind == 'formula':
                nodes.append(_render_block_math_node(normalize_latex(code)))
            elif kind == 'code':
                nodes.append(code_block_node(language, code))
            else:
                nodes.append(paragraph_node(code))
            index += 1
            continue
        if token.type == 'blockquote_open':
            inline_content = _inline_token_content(tokens, index + 2)
            nodes.append({'type': 'blockquote', 'content': [paragraph_node(inline_content)]})
            index += 4
            continue
        if token.type == 'bullet_list_open':
            list_node, index = _render_list_node(tokens, index, 'bulletList', paragraph_node)
            nodes.append(list_node)
            continue
        if token.type == 'ordered_list_open':
            list_node, index = _render_list_node(tokens, index, 'orderedList', paragraph_node)
            nodes.append(list_node)
            continue
        if token.type == 'table_open':
            rows, index = _table_rows_from_tokens(tokens, index)
            nodes.append(_table_node(rows, paragraph_node))
            continue
        index += 1
    return {'type': 'doc', 'content': nodes or [{'type': 'paragraph'}]}
