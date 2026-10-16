from __future__ import annotations
import hashlib
import json
import re
import secrets
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence
from openclass_core.models import BoardDocument
from openclass_core.services.ai_logging import ai_usage_logger
from openclass_core.services.board_asset_store import BoardAssetStore, get_board_asset_store
from openclass_core.services.rich_document import rebuild_document_from_content_json
VisualBytesResolver = Callable[[str], tuple[Any, bytes] | bytes | None]
_MARKER_TOKEN_RE = re.compile('\\[\\[OPENCLASS_VISUAL(?:_RECREATED)?_[A-Za-z0-9_-]+_\\d{4}\\]\\]')
_MARKER_RE = re.compile(f'^{_MARKER_TOKEN_RE.pattern}$')
_LINEAR_FLOW_ARROW_RE = re.compile('(?:->|→|⟶|⇒|↓)')
_NON_LINEAR_FLOW_RE = re.compile('(?:<-|←|↑|↙|↘|↖|↗|↔|⇄|⇆)')

class BoardVisualInsertionError(ValueError):
    pass

@dataclass(frozen=True)
class PlannedBoardVisual:
    visual_id: str
    marker: str
    order_index: int
    recreation_marker: str = ''
    before_chunk_id: str = ''
    after_chunk_id: str = ''
    kind: str = 'image'
    caption: str = ''
    source: str = ''
    source_locator: str = ''
    source_ingestion_id: str = ''
    source_chapter_id: str = ''
    page_no: int | None = None
    page_range: str = ''
    slide_no: int | None = None
    sheet_name: str = ''
    mime_type: str = ''
    content_hash: str = ''
    position_hash: str = ''
    table_data: Any = None

@dataclass(frozen=True)
class BoardInsertionPlan:
    nonce: str
    items: tuple[PlannedBoardVisual, ...]

    @property
    def markers(self) -> tuple[str, ...]:
        return tuple((item.marker for item in self.items))

@dataclass
class BoardVisualInsertionResult:
    document: BoardDocument
    applied_visual_ids: list[str] = field(default_factory=list)
    recreated_visual_ids: list[str] = field(default_factory=list)
    original_visual_ids: list[str] = field(default_factory=list)
    appendix_visual_ids: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    asset_ids: list[str] = field(default_factory=list)

def build_board_insertion_plan(confirmed_visuals: Sequence[Any], *, nonce: str | None=None, source_titles: dict[str, str] | None=None, allow_editable_recreation: bool=True) -> BoardInsertionPlan:
    plan_nonce = re.sub('[^A-Za-z0-9_-]', '', nonce or secrets.token_urlsafe(12)) or secrets.token_hex(8)
    items: list[PlannedBoardVisual] = []
    seen: set[str] = set()
    ordered = sorted(confirmed_visuals, key=lambda visual: (_string_value(visual, 'source_ingestion_id', 'source_id'), _int_value(visual, 'order_index', default=0), _string_value(visual, 'visual_id', 'id')))
    for visual in ordered:
        visual_id = _string_value(visual, 'visual_id', 'id')
        if not visual_id or visual_id in seen:
            continue
        anchor_status = _string_value(visual, 'anchor_status')
        if anchor_status and anchor_status != 'verified':
            continue
        seen.add(visual_id)
        index = len(items)
        source_ingestion_id = _string_value(visual, 'source_ingestion_id', 'source_id')
        page_start = _optional_int_value(visual, 'page_no', 'page_start')
        page_end = _optional_int_value(visual, 'page_end')
        page_range = _string_value(visual, 'page_range')
        if not page_range and page_start is not None:
            page_range = str(page_start) if page_end is None or page_end == page_start else f'{page_start}-{page_end}'
        kind = _string_value(visual, 'kind') or 'image'
        table_data = _value(visual, 'table_data')
        supports_editable_recreation = allow_editable_recreation and (not (kind in {'table', 'structured_table', 'native_table'} and table_data is not None))
        items.append(PlannedBoardVisual(visual_id=visual_id, marker=f'[[OPENCLASS_VISUAL_{plan_nonce}_{index:04d}]]', order_index=_int_value(visual, 'order_index', default=index), recreation_marker=f'[[OPENCLASS_VISUAL_RECREATED_{plan_nonce}_{index:04d}]]' if supports_editable_recreation else '', before_chunk_id=_string_value(visual, 'before_chunk_id'), after_chunk_id=_string_value(visual, 'after_chunk_id'), kind=kind, caption=_string_value(visual, 'caption'), source=_string_value(visual, 'source_title', 'source_name', 'source') or (source_titles or {}).get(source_ingestion_id, ''), source_locator=_source_locator(visual), source_ingestion_id=source_ingestion_id, source_chapter_id=_string_value(visual, 'source_chapter_id', 'chapter_id'), page_no=page_start, page_range=page_range, slide_no=_optional_int_value(visual, 'slide_no'), sheet_name=_string_value(visual, 'sheet_name'), mime_type=_string_value(visual, 'mime_type'), content_hash=_string_value(visual, 'asset_hash', 'content_hash', 'image_hash'), position_hash=_string_value(visual, 'position_hash'), table_data=table_data))
    return BoardInsertionPlan(nonce=plan_nonce, items=tuple(items))

def derive_board_visual_placements(document: BoardDocument, *, plan: BoardInsertionPlan) -> list[dict[str, str]]:
    """Describe where Codex wrote each confirmed visual marker."""
    nodes = _canonical_content_json(document).get('content')
    if not isinstance(nodes, list):
        return []
    locations = _standalone_marker_locations(nodes)
    placements: list[dict[str, str]] = []
    for item in plan.items:
        marker_choices = [(item.marker, 'original_asset')]
        if item.recreation_marker:
            marker_choices.append((item.recreation_marker, 'editable_recreation'))
        selected = [(location, marker, placement_kind) for marker, placement_kind in marker_choices for location in locations.get(marker, [])]
        if not selected:
            continue
        marker_index, marker, placement_kind = min(selected, key=lambda value: value[0])
        anchor_node = _visual_anchor_node(nodes, marker_index)
        target_text_anchor = _node_plain_text(anchor_node).strip()
        placements.append({'visual_id': item.visual_id, 'marker': marker, 'placement_kind': placement_kind, 'target_text_anchor': target_text_anchor, 'source_before_chunk_id': item.before_chunk_id, 'source_after_chunk_id': item.after_chunk_id, 'reason': 'Codex recreated this verified visual as editable board content.' if placement_kind == 'editable_recreation' else 'Codex placed the confirmed original visual after this board paragraph.'})
    return placements

def validate_original_visual_positions(document: BoardDocument, *, plan: BoardInsertionPlan) -> None:
    """Check required placement before staging assets or accepting final prose."""
    nodes = _canonical_content_json(document).get('content')
    if not isinstance(nodes, list):
        raise BoardVisualInsertionError('The original-visual board has no valid content structure.')
    _validate_original_visual_placements(nodes, plan, derive_board_visual_placements(document, plan=plan), preserved_marker_nodes=Counter())

def apply_board_insertion_plan(document: BoardDocument, *, plan: BoardInsertionPlan, placements: Sequence[Any], owner_user_id: str, lesson_id: str, visual_bytes_resolver: VisualBytesResolver, asset_store: BoardAssetStore | None=None, preserved_document: BoardDocument | None=None, strict_positions: bool=False) -> BoardVisualInsertionResult:
    store = asset_store or get_board_asset_store()
    content_json = _canonical_content_json(document)
    root_nodes = content_json.get('content')
    if not isinstance(root_nodes, list):
        return BoardVisualInsertionResult(document=document)
    plan_by_marker = {marker: item for item in plan.items for marker in (item.marker, item.recreation_marker) if marker}
    preserved_marker_nodes = _preserved_marker_node_counts(preserved_document, current_markers=set(plan_by_marker))
    marker_locations = _standalone_marker_locations(root_nodes)
    document_marker_tokens = {token for node in root_nodes for token in _MARKER_TOKEN_RE.findall(_node_plain_text(node))}
    if strict_positions:
        _validate_original_visual_placements(root_nodes, plan, placements, preserved_marker_nodes=preserved_marker_nodes)
    result = BoardVisualInsertionResult(document=document)
    candidates: list[tuple[PlannedBoardVisual, Any, int | None]] = []
    for item in plan.items:
        marker_choices = [item.marker]
        if item.recreation_marker:
            marker_choices.append(item.recreation_marker)
        located = [(location, marker) for marker in marker_choices for location in marker_locations.get(marker, [])]
        if not located:
            item_marker_tokens = {item.marker, item.recreation_marker} - {''}
            unknown_marker_tokens = document_marker_tokens - set(plan_by_marker)
            if document_marker_tokens & item_marker_tokens or unknown_marker_tokens:
                _record_skip(result, item.visual_id, item.marker, 'placement_missing', lesson_id=lesson_id)
                continue
            candidates.append((item, {'marker': item.marker}, None))
            continue
        marker_index, selected_marker = min(located, key=lambda value: value[0])
        candidates.append((item, {'marker': selected_marker}, marker_index))
    replacements: dict[str, dict[str, Any]] = {}
    recreation_fallback_ids: set[str] = set()
    for item, placement, marker_index in candidates:
        selected_marker = _string_value(placement, 'marker')
        selected_recreation = bool(item.recreation_marker and selected_marker == item.recreation_marker)
        if selected_recreation and marker_index is not None and _editable_recreation_is_structured(root_nodes, marker_index):
            continue
        if selected_recreation:
            recreation_fallback_ids.add(item.visual_id)
        if _is_table_visual(item):
            canonical_table = json.dumps(item.table_data, ensure_ascii=False, separators=(',', ':'), sort_keys=False)
            if item.content_hash and hashlib.sha256(canonical_table.encode('utf-8')).hexdigest() != item.content_hash:
                _record_skip(result, item.visual_id, item.marker, 'table_content_hash_mismatch', lesson_id=lesson_id)
                continue
            table_node = _table_node(item)
            if table_node is None:
                _record_skip(result, item.visual_id, item.marker, 'table_data_invalid', lesson_id=lesson_id)
                continue
            replacements[item.visual_id] = table_node
            continue
        resolved = visual_bytes_resolver(item.visual_id)
        if resolved is None:
            _record_skip(result, item.visual_id, item.marker, 'visual_bytes_unavailable', lesson_id=lesson_id)
            continue
        source_visual, content = resolved if isinstance(resolved, tuple) else (None, resolved)
        if source_visual is not None and (not _resolved_visual_matches_plan(item, source_visual)):
            _record_skip(result, item.visual_id, item.marker, 'resolved_visual_mismatch', lesson_id=lesson_id)
            continue
        if not isinstance(content, bytes) or not content:
            _record_skip(result, item.visual_id, item.marker, 'visual_bytes_invalid', lesson_id=lesson_id)
            continue
        content_hash = hashlib.sha256(content).hexdigest()
        expected_hash = item.content_hash or _string_value(source_visual, 'asset_hash', 'content_hash', 'image_hash')
        if expected_hash and expected_hash != content_hash:
            _record_skip(result, item.visual_id, item.marker, 'visual_content_hash_mismatch', lesson_id=lesson_id)
            continue
        mime_type = _string_value(source_visual, 'mime_type') or item.mime_type or 'image/png'
        try:
            asset = store.put_bytes(owner_user_id=owner_user_id, lesson_id=lesson_id, document_id=document.id, content=content, mime_type=mime_type, file_name=_string_value(source_visual, 'file_name') or f'{item.visual_id}', source_visual_id=item.visual_id)
        except Exception as exc:
            if strict_positions:
                raise
            _record_skip(result, item.visual_id, item.marker, 'board_asset_persist_failed', lesson_id=lesson_id, detail=type(exc).__name__)
            continue
        replacements[item.visual_id] = _resource_visual_node(item, asset.id)
    if strict_positions and set(replacements) != {item.visual_id for item in plan.items}:
        raise BoardVisualInsertionError('A required original visual could not be verified or staged; no board was committed.')
    candidates_by_index = {marker_index: (item, placement) for item, placement, marker_index in candidates if marker_index is not None}
    next_nodes: list[dict[str, Any]] = []
    for node_index, node in enumerate(root_nodes):
        if not isinstance(node, dict):
            continue
        candidate = candidates_by_index.get(node_index)
        if candidate is not None:
            item, placement = candidate
            selected_marker = _string_value(placement, 'marker')
            if item.recreation_marker and selected_marker == item.recreation_marker and (item.visual_id not in recreation_fallback_ids):
                result.applied_visual_ids.append(item.visual_id)
                result.recreated_visual_ids.append(item.visual_id)
                ai_usage_logger.log_event('board_visual_placed', lesson_id=lesson_id, visual_id=item.visual_id, asset_id='', placement_kind='editable_recreation')
                continue
            replacement = replacements.get(item.visual_id)
            if replacement is not None:
                next_nodes.append(replacement)
                result.applied_visual_ids.append(item.visual_id)
                asset_id = _string_value(replacement.get('attrs', {}), 'assetId')
                if asset_id:
                    result.asset_ids.append(asset_id)
                    result.original_visual_ids.append(item.visual_id)
                ai_usage_logger.log_event('board_visual_placed', lesson_id=lesson_id, visual_id=item.visual_id, asset_id=asset_id, placement_kind='editable_table' if _is_table_visual(item) else 'original_asset', fallback_reason='editable_recreation_failed_structure_validation' if item.visual_id in recreation_fallback_ids else '')
            continue
        marker = _standalone_marker(node)
        preserve_unknown_markers = _consume_preserved_marker_node(node, preserved_marker_nodes, current_markers=set(plan_by_marker))
        if marker in plan_by_marker:
            continue
        if marker and _MARKER_RE.fullmatch(marker):
            if preserve_unknown_markers:
                next_nodes.append(node)
            else:
                _record_skip(result, '', marker, 'unknown_marker', lesson_id=lesson_id)
            continue
        cleaned = _strip_visual_markers(node, current_markers=set(plan_by_marker), strip_unknown=not preserve_unknown_markers)
        if cleaned is not None:
            next_nodes.append(cleaned)
    appendix_items = [item for item, _placement, marker_index in candidates if marker_index is None and item.visual_id in replacements]
    if appendix_items:
        next_nodes.append({'type': 'heading', 'attrs': {'level': 2}, 'content': [{'type': 'text', 'text': '本章教学图表'}]})
        for item in appendix_items:
            replacement = replacements[item.visual_id]
            next_nodes.append(replacement)
            result.applied_visual_ids.append(item.visual_id)
            result.appendix_visual_ids.append(item.visual_id)
            asset_id = _string_value(replacement.get('attrs', {}), 'assetId')
            if asset_id:
                result.asset_ids.append(asset_id)
                result.original_visual_ids.append(item.visual_id)
            ai_usage_logger.log_event('board_visual_placed', lesson_id=lesson_id, visual_id=item.visual_id, asset_id=asset_id, placement_kind='chapter_visual_appendix')
    normalized_nodes = next_nodes or [{'type': 'paragraph'}]
    next_json = {**content_json, 'type': 'doc', 'content': normalized_nodes}
    result.document = rebuild_document_from_content_json(document, next_json)
    return result

def _visual_anchor_node(nodes, marker_index):
    return next((node for node in reversed(nodes[:marker_index]) if not _standalone_marker(node) and _node_plain_text(node).strip()), None)

def _validate_original_visual_placements(nodes, plan, placements, *, preserved_marker_nodes):
    locations = _standalone_marker_locations(nodes)
    expected = {item.marker for item in plan.items}
    previous = preserved_marker_nodes.copy()
    for node in nodes:
        if _consume_preserved_marker_node(node, previous, current_markers=expected):
            continue
        unknown = set(_MARKER_TOKEN_RE.findall(_node_plain_text(node))) - expected
        if unknown:
            raise BoardVisualInsertionError('The board contains an unapproved original-visual marker.')
    declarations = {}
    for placement in placements:
        identity = _string_value(placement, 'visual_id')
        if identity in declarations:
            raise BoardVisualInsertionError('An original visual has duplicate placement declarations.')
        declarations[identity] = placement
    if set(declarations) != {item.visual_id for item in plan.items}:
        raise BoardVisualInsertionError('Every required original visual needs a related board placement.')
    for item in plan.items:
        positions = locations.get(item.marker, [])
        if len(positions) != 1:
            raise BoardVisualInsertionError('Each original visual requires exactly one standalone placement marker.')
        placement = declarations[item.visual_id]
        if _string_value(placement, 'marker') != item.marker:
            raise BoardVisualInsertionError('Original visuals cannot be replaced by an editable redraw.')
        expected_anchor = _node_plain_text(_visual_anchor_node(nodes, positions[0])).strip()
        if not expected_anchor or _string_value(placement, 'target_text_anchor') != expected_anchor:
            raise BoardVisualInsertionError('The original visual has no verified adjacent learning-content anchor.')

def _canonical_content_json(document: BoardDocument) -> dict[str, Any]:
    content_json = document.content_json if isinstance(document.content_json, dict) else {}
    content = content_json.get('content')
    if isinstance(content, list) and content:
        return content_json
    from openclass_core.services.rich_document import html_to_tiptap_doc, text_to_tiptap_doc
    if document.content_html.strip():
        return html_to_tiptap_doc(document.content_html)
    return text_to_tiptap_doc(document.content_text)

def _standalone_marker_locations(nodes: list[Any]) -> dict[str, list[int]]:
    locations: dict[str, list[int]] = {}
    for index, node in enumerate(nodes):
        marker = _standalone_marker(node)
        if marker:
            locations.setdefault(marker, []).append(index)
    return locations

def _standalone_marker(node: Any) -> str:
    if not isinstance(node, dict) or node.get('type') != 'paragraph':
        return ''
    content = node.get('content')
    if not isinstance(content, list) or len(content) != 1:
        return ''
    child = content[0]
    if not isinstance(child, dict) or child.get('type') != 'text' or child.get('marks'):
        return ''
    text = str(child.get('text') or '').strip()
    return text if _MARKER_RE.fullmatch(text) else ''

def _editable_recreation_is_structured(nodes: list[Any], marker_index: int) -> bool:
    """Accept only a native table or a compact, one-direction linear flow."""
    for node in reversed(nodes[max(0, marker_index - 6):marker_index]):
        if not isinstance(node, dict):
            continue
        if node.get('type') == 'heading':
            return False
        flow_text = _node_plain_text(node).strip()
        if node.get('type') == 'table':
            return True
        if flow_text:
            break
    else:
        return False
    if not flow_text or _NON_LINEAR_FLOW_RE.search(flow_text):
        return False
    arrows = _LINEAR_FLOW_ARROW_RE.findall(flow_text)
    if not arrows or len(arrows) > 8:
        return False
    normalized_directions = {'down' if arrow == '↓' else 'forward' for arrow in arrows}
    if len(normalized_directions) != 1:
        return False
    segments = [part.strip() for part in _LINEAR_FLOW_ARROW_RE.split(flow_text) if part.strip()]
    return 2 <= len(segments) <= 9

def _strip_visual_markers(node: dict[str, Any], *, current_markers: set[str], strip_unknown: bool) -> dict[str, Any] | None:
    cleaned = json.loads(json.dumps(node, ensure_ascii=False))

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if value.get('type') == 'text' and isinstance(value.get('text'), str):
                value['text'] = _MARKER_TOKEN_RE.sub(lambda match: '' if match.group(0) in current_markers or strip_unknown else match.group(0), value['text'])
            content = value.get('content')
            if isinstance(content, list):
                for child in content:
                    walk(child)
                value['content'] = [child for child in content if not (isinstance(child, dict) and child.get('type') == 'text' and (not str(child.get('text') or '')))]
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(cleaned)
    if cleaned.get('type') == 'paragraph' and (not _node_plain_text(cleaned).strip()):
        return None
    return cleaned

def _preserved_marker_node_counts(document: BoardDocument | None, *, current_markers: set[str]) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    if document is None:
        return counts
    content = _canonical_content_json(document).get('content')
    if not isinstance(content, list):
        return counts
    for node in content:
        if not isinstance(node, dict):
            continue
        text = _node_plain_text(node)
        tokens = set(_MARKER_TOKEN_RE.findall(text)) - current_markers
        if tokens:
            counts[str(node.get('type') or ''), text] += 1
    return counts

def _consume_preserved_marker_node(node: dict[str, Any], counts: Counter[tuple[str, str]], *, current_markers: set[str]) -> bool:
    text = _node_plain_text(node)
    if not set(_MARKER_TOKEN_RE.findall(text)) - current_markers:
        return False
    signature = (str(node.get('type') or ''), text)
    if counts[signature] <= 0:
        return False
    counts[signature] -= 1
    return True

def _resolved_visual_matches_plan(item: PlannedBoardVisual, visual: Any) -> bool:
    resolved_id = _string_value(visual, 'visual_id', 'id')
    source_id = _string_value(visual, 'source_ingestion_id', 'source_id')
    before = _string_value(visual, 'before_chunk_id')
    after = _string_value(visual, 'after_chunk_id')
    position_hash = _string_value(visual, 'position_hash')
    return resolved_id == item.visual_id and (not item.source_ingestion_id or source_id == item.source_ingestion_id) and (not item.before_chunk_id or before == item.before_chunk_id) and (not item.after_chunk_id or after == item.after_chunk_id) and (not item.position_hash or position_hash == item.position_hash)

def _node_plain_text(value: Any) -> str:
    if isinstance(value, dict):
        if value.get('type') == 'text':
            return str(value.get('text') or '')
        attrs = value.get('attrs')
        if value.get('type') == 'resourceVisualBlock' and isinstance(attrs, dict):
            return str(attrs.get('caption') or '')
        content = value.get('content')
        if isinstance(content, list):
            return ''.join((_node_plain_text(child) for child in content))
    if isinstance(value, list):
        return ''.join((_node_plain_text(child) for child in value))
    return ''

def _resource_visual_node(item: PlannedBoardVisual, asset_id: str) -> dict[str, Any]:
    caption = item.caption or '资料图示'
    return {'type': 'resourceVisualBlock', 'attrs': {'marker': item.marker, 'visualId': item.visual_id, 'assetId': asset_id, 'caption': caption, 'source': item.source, 'sourceTitle': item.source, 'sourceLocator': item.source_locator, 'sourceIngestionId': item.source_ingestion_id, 'sourceChapterId': item.source_chapter_id, 'pageNo': item.page_no, 'pageRange': item.page_range, 'slideNo': item.slide_no, 'sheetName': item.sheet_name, 'kind': item.kind, 'recreationKind': 'original', 'recreationStatus': 'original_only', 'recreationConfidence': '1.00', 'recreationNote': '', 'recreationHtml': '', 'originalSrc': '', 'originalAlt': caption, 'originalInitiallyCollapsed': False}}

def _is_table_visual(item: PlannedBoardVisual) -> bool:
    return item.kind in {'table', 'structured_table', 'native_table'} and item.table_data is not None

def _table_node(item: PlannedBoardVisual) -> dict[str, Any] | None:
    table_data = item.table_data
    rows = table_data.get('rows') if isinstance(table_data, dict) else table_data
    if not isinstance(rows, list) or not rows:
        return None
    normalized_rows: list[list[str]] = []
    for raw_row in rows:
        cells = raw_row.get('cells') if isinstance(raw_row, dict) else raw_row
        if not isinstance(cells, list) or not cells:
            continue
        normalized_rows.append([str(cell.get('text') if isinstance(cell, dict) and 'text' in cell else cell or '') for cell in cells])
    if not normalized_rows:
        return None
    width = max((len(row) for row in normalized_rows))
    content: list[dict[str, Any]] = []
    for row_index, row in enumerate(normalized_rows):
        cells: list[dict[str, Any]] = []
        for value in row + [''] * (width - len(row)):
            text_node = [{'type': 'text', 'text': value}] if value else []
            cells.append({'type': 'tableHeader' if row_index == 0 else 'tableCell', 'attrs': {'colspan': 1, 'rowspan': 1, 'colwidth': None}, 'content': [{'type': 'paragraph', 'content': text_node}]})
        content.append({'type': 'tableRow', 'content': cells})
    return {'type': 'table', 'attrs': {'sourceVisualId': item.visual_id, 'sourceIngestionId': item.source_ingestion_id, 'sourceChapterId': item.source_chapter_id, 'sourceTitle': item.source, 'sourceLocator': item.source_locator, 'pageNo': item.page_no, 'pageRange': item.page_range, 'caption': item.caption}, 'content': content}

def _record_skip(result: BoardVisualInsertionResult, visual_id: str, marker: str, reason: str, *, lesson_id: str, detail: str='') -> None:
    payload = {'visual_id': visual_id, 'marker': marker, 'reason': reason}
    if detail:
        payload['detail'] = detail
    result.skipped.append(payload)
    ai_usage_logger.log_event('board_visual_placement_skipped', lesson_id=lesson_id, **payload)

def _value(value: Any, *names: str) -> Any:
    if isinstance(value, dict):
        for name in names:
            if name in value:
                return value[name]
        return None
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return None

def _string_value(value: Any, *names: str) -> str:
    item = _value(value, *names)
    return str(item).strip() if item is not None else ''

def _int_value(value: Any, *names: str, default: int=0) -> int:
    item = _value(value, *names)
    try:
        return int(item)
    except (TypeError, ValueError):
        return default

def _optional_int_value(value: Any, *names: str) -> int | None:
    item = _value(value, *names)
    if item is None or item == '':
        return None
    try:
        return int(item)
    except (TypeError, ValueError):
        return None

def _source_locator(visual: Any) -> str:
    explicit = _string_value(visual, 'source_locator', 'locator')
    if explicit:
        return explicit
    page = _value(visual, 'page_no')
    slide = _value(visual, 'slide_no')
    sheet = _string_value(visual, 'sheet_name')
    if page is not None:
        return f'page:{page}'
    if slide is not None:
        return f'slide:{slide}'
    if sheet:
        return f'sheet:{sheet}'
    return ''
