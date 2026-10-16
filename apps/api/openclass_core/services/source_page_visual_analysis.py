from __future__ import annotations
import base64
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal
from pydantic import BaseModel, Field
from openclass_core.models import AgentActivityEvent, SourceChapter, SourceIngestionRecord, SourceStructure, SourceVisualAsset, new_id
from openclass_core.services.image_ocr import OCRPageLayout, extract_pdf_pages_layout
from openclass_core.services.source_visual_storage import persist_source_visual_asset, resolve_source_visual_storage_key
if TYPE_CHECKING:
    from openclass_core.services.ai_execution_adapter import AIExecutionAdapter
VISUAL_POLICY_VERSION = 2
MAX_PREVIEW_PAGES_PER_CALL = 8
DEFAULT_PREVIEW_DPI = 180
DEFAULT_CROP_DPI = 300
MAX_CROP_PIXELS = 24000000

class BoardVisualCandidate(BaseModel):
    page_no: int = Field(ge=1)
    kind: str = Field(default='other', max_length=80)
    bbox: list[float] = Field(default_factory=list, max_length=4)
    caption: str = ''
    nearby_anchor: str = ''
    teaching_reason: str = ''
    related_section: str = ''
    uncertainty_note: str = ''
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

class ExcludedBoardVisualCandidate(BaseModel):
    page_no: int = Field(ge=1)
    kind: str
    reason: str

class BoardVisualToolCall(BaseModel):
    tool: Literal['render_source_pages', 'crop_source_page', 'register_board_visual']
    pages: list[int] = Field(default_factory=list)
    page_no: int | None = Field(default=None, ge=1)
    dpi: int | None = None
    bbox: list[float] = Field(default_factory=list, max_length=4)
    crop_ref: str = ''
    kind: str = 'other'
    caption: str = ''
    nearby_anchor: str = ''
    teaching_reason: str = ''
    related_section: str = ''
    uncertainty_note: str = ''
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

class BoardVisualAgentTurn(BaseModel):
    tool_calls: list[BoardVisualToolCall] = Field(default_factory=list)
    excluded_candidates: list[ExcludedBoardVisualCandidate] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    finished: bool = False

@dataclass(frozen=True)
class _PreparedCrop:
    page_no: int
    bbox: list[float]
    content: bytes
    width: int
    height: int

@dataclass(frozen=True)
class SourcePageVisualAnalysisResult:
    visuals: list[SourceVisualAsset] = field(default_factory=list)
    excluded_candidates: list[dict[str, object]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    page_count: int = 0
    retry_pages: list[int] = field(default_factory=list)
    missing_references: list[str] = field(default_factory=list)

class SourcePageVisualAnalysisError(RuntimeError):
    pass
BOARD_VISUAL_SELECTION_INSTRUCTIONS = '\nYou are the OpenClass Board Agent preparing a source-grounded teaching board. The supplied images\nare physical pages inside a user-confirmed PDF chapter range. Inspect every supplied page and decide\nwhich visual candidates are useful for teaching the current chapter.\n\nYour tools are render_source_pages, crop_source_page, and register_board_visual. Choose the page\nbatches, preview resolution, crop bounds, re-views, and number of calls yourself. Start by asking to\nrender whichever authorized pages you need. A crop result returns an opaque crop_ref; register that\ncrop_ref only after deciding it has teaching value. Set finished when your work for the confirmed\nrange is done. Tool failures are warnings and do not prevent the text board.\n\nRegister concept maps, relationship diagrams, flows, algorithm boxes, model architectures, data\ncharts, tables, and explanatory illustrations when seeing them helps a learner understand the\ncurrent knowledge. By default exclude chapter covers, tables of contents, portraits or author\nphotos, headers, footers, logos, decorative images, duplicates, and scanning noise. Ordinary\nformulas should be written as LaTeX in the board and must not be registered as image crops.\n\nUse contextual teaching value, not a keyword blacklist. A portrait can be useful when the confirmed\nlearning topic is explicitly about that person or history. When a crop boundary is uncertain, use a\nwider bbox so the whole graphic, labels, legend, and caption remain visible, and explain the\nuncertainty. Bboxes use normalized [left, top, right, bottom] coordinates with the page top-left as\norigin. Put rejected candidates in excluded_candidates with a short understandable reason; rejected\ncandidates are diagnostics and will not become board assets. Do not enforce a visual count. You may\nchoose no visual on a page. Never invent a crop_ref or a server path.\n'.strip()

def render_source_pages(source_path: Path, *, authorized_page_start: int, authorized_page_end: int, pages: list[int], dpi: int=DEFAULT_PREVIEW_DPI) -> list[str]:
    """Render authorized PDF pages for Board Agent inspection.

    The caller exposes only opaque source identity to the model. The local path
    stays inside the trusted runtime and is never serialized into prompts.
    """
    if not pages or len(pages) > MAX_PREVIEW_PAGES_PER_CALL:
        raise SourcePageVisualAnalysisError('PDF page render request exceeds the preview budget.')
    if dpi < 72 or dpi > DEFAULT_CROP_DPI:
        raise SourcePageVisualAnalysisError('PDF page render DPI is outside the safe range.')
    if any((page < authorized_page_start or page > authorized_page_end for page in pages)):
        raise SourcePageVisualAnalysisError('PDF page render request is outside the authorized range.')
    return [_render_page_data_url(source_path, page_no=page, dpi=dpi) for page in pages]

def crop_source_page(source_path: Path, *, authorized_page_start: int, authorized_page_end: int, page_no: int, bbox: list[float], dpi: int=DEFAULT_CROP_DPI) -> tuple[bytes, int, int]:
    """Crop one authorized PDF page using normalized coordinates."""
    if page_no < authorized_page_start or page_no > authorized_page_end:
        raise SourcePageVisualAnalysisError('PDF crop request is outside the authorized range.')
    _validate_bbox(bbox)
    if dpi < 72 or dpi > DEFAULT_CROP_DPI:
        raise SourcePageVisualAnalysisError('PDF crop DPI is outside the safe range.')
    try:
        from openclass_core.services import pdf_backend as fitz
        with fitz.open(source_path) as document:
            if page_no > document.page_count:
                raise SourcePageVisualAnalysisError('PDF crop page does not exist.')
            page = document.load_page(page_no - 1)
            left, top, right, bottom = bbox
            clip = fitz.Rect(page.rect.x0 + left * page.rect.width, page.rect.y0 + top * page.rect.height, page.rect.x0 + right * page.rect.width, page.rect.y0 + bottom * page.rect.height)
            width = max(1, round(clip.width * dpi / 72.0))
            height = max(1, round(clip.height * dpi / 72.0))
            if width * height > MAX_CROP_PIXELS:
                raise SourcePageVisualAnalysisError('PDF crop exceeds the pixel budget.')
            pixmap = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), clip=clip, alpha=False)
            return (pixmap.tobytes('png'), pixmap.width, pixmap.height)
    except SourcePageVisualAnalysisError:
        raise
    except Exception as exc:
        raise SourcePageVisualAnalysisError(f'PDF p.{page_no} crop failed.') from exc

def register_board_visual(*, source: SourceIngestionRecord, structure: SourceStructure, chapter: SourceChapter, page_no: int, candidate: BoardVisualCandidate, content: bytes, width: int, height: int, scope_cache_key: str, model_identity: str, extracted_text: str, order_index: int) -> SourceVisualAsset:
    """Persist one Board Agent-approved teaching visual."""
    storage_key, content_hash = persist_source_visual_asset(content, mime_type='image/png')
    position_payload = json.dumps({'page': page_no, 'bbox': [round(value, 6) for value in candidate.bbox]}, sort_keys=True, separators=(',', ':'))
    position_hash = hashlib.sha256(position_payload.encode('utf-8')).hexdigest()
    identity = f"{source.id}:{source.metadata.get('content_hash', '')}:{position_hash}:{content_hash}"
    visual_id = f'sourcevisual_{hashlib.sha256(identity.encode()).hexdigest()[:24]}'
    return SourceVisualAsset(id=visual_id, owner_user_id=source.owner_user_id, package_id=source.package_id, source_ingestion_id=source.id, structure_id=structure.id, structure_version=structure.visual_index_version, chapter_id=chapter.id, kind=_stored_kind(candidate.kind), source_locator=f'pdf:page:{page_no}:board-visual:{order_index}', page_start=page_no, page_end=page_no, bbox=[round(value, 6) for value in candidate.bbox], caption=candidate.caption.strip(), extracted_text=extracted_text, surrounding_text=candidate.nearby_anchor.strip(), anchor_status='verified', mime_type='image/png', asset_path=str(resolve_source_visual_storage_key(storage_key)), storage_key=storage_key, order_index=order_index, content_hash=content_hash, position_hash=position_hash, width=width, height=height, confidence=candidate.confidence, metadata={'scope_cache_key': scope_cache_key, 'visual_policy_version': VISUAL_POLICY_VERSION, 'analysis_model': model_identity, 'region_kind': candidate.kind, 'source_range_verified': True, 'teaching_reason': candidate.teaching_reason.strip(), 'related_section': candidate.related_section.strip(), 'uncertainty_note': candidate.uncertainty_note.strip(), 'board_render_policy': 'original_capture_required'})

def analyze_pdf_visual_scope(*, source: SourceIngestionRecord, structure: SourceStructure, chapter: SourceChapter, source_path: Path, page_start: int, page_end: int, scope_cache_key: str, model_identity: str, adapter: AIExecutionAdapter, is_cancelled: Callable[[], bool] | None=None, on_activity=None) -> SourcePageVisualAnalysisResult:
    """Let the selected Board Agent choose and register teaching visuals.

    This function deliberately has no coverage audit, expected-count gate, or
    fixed retry chain. Invalid individual candidates become warnings while the
    remaining teaching visuals continue to registration.
    """
    if page_start < 1 or page_end < page_start:
        raise SourcePageVisualAnalysisError('The verified PDF visual range is invalid.')
    layouts = extract_pdf_pages_layout(source_path, page_start=page_start, page_end=page_end, max_pages=page_end - page_start + 1)
    layouts_by_page = {layout.page_no: layout for layout in layouts}
    activity_turn_id = new_id('boardvisual')
    visuals: list[SourceVisualAsset] = []
    excluded: list[dict[str, object]] = []
    warnings: list[str] = []
    prepared_crops: dict[str, _PreparedCrop] = {}
    viewed_pages: set[int] = set()
    tool_results: list[dict[str, object]] = []
    image_inputs: list[str] = []
    page_count = page_end - page_start + 1
    max_agent_turns = max(12, (page_count + MAX_PREVIEW_PAGES_PER_CALL - 1) // MAX_PREVIEW_PAGES_PER_CALL * 4 + 4)
    finished = False
    for turn_index in range(max_agent_turns):
        _check_cancelled(is_cancelled)
        response = adapter.parse_structured(system_prompt=BOARD_VISUAL_SELECTION_INSTRUCTIONS, user_prompt=json.dumps({'task': 'select_teaching_visuals_for_board', 'source_reference': 'opaque_confirmed_source', 'authorized_physical_page_range': [page_start, page_end], 'viewed_pages': sorted(viewed_pages), 'pending_crop_refs': {crop_ref: {'page_no': crop.page_no, 'bbox': crop.bbox, 'width': crop.width, 'height': crop.height} for crop_ref, crop in prepared_crops.items()}, 'registered_visual_ids': [item.id for item in visuals], 'previous_tool_results': tool_results, 'transport_limits': {'max_images_per_turn': MAX_PREVIEW_PAGES_PER_CALL, 'max_render_dpi': DEFAULT_CROP_DPI}}, ensure_ascii=False), schema=BoardVisualAgentTurn, image_inputs=image_inputs, on_activity=on_activity)
        agent_turn = BoardVisualAgentTurn.model_validate(response.output_parsed)
        excluded.extend((item.model_dump(mode='json') for item in agent_turn.excluded_candidates))
        warnings.extend((note for note in agent_turn.notes if note.strip()))
        next_tool_results: list[dict[str, object]] = []
        next_image_inputs: list[str] = []
        for call_index, call in enumerate(agent_turn.tool_calls):
            try:
                if call.tool == 'render_source_pages':
                    rendered = render_source_pages(source_path, authorized_page_start=page_start, authorized_page_end=page_end, pages=call.pages, dpi=call.dpi or DEFAULT_PREVIEW_DPI)
                    if len(next_image_inputs) + len(rendered) > MAX_PREVIEW_PAGES_PER_CALL:
                        raise SourcePageVisualAnalysisError('PDF page render request exceeds the per-turn image budget.')
                    first_index = len(next_image_inputs)
                    next_image_inputs.extend(rendered)
                    viewed_pages.update(call.pages)
                    next_tool_results.append({'tool': call.tool, 'pages': call.pages, 'dpi': call.dpi or DEFAULT_PREVIEW_DPI, 'image_input_indexes': list(range(first_index, first_index + len(rendered)))})
                elif call.tool == 'crop_source_page':
                    if call.page_no is None:
                        raise SourcePageVisualAnalysisError('PDF crop page is required.')
                    content, width, height = crop_source_page(source_path, authorized_page_start=page_start, authorized_page_end=page_end, page_no=call.page_no, bbox=call.bbox, dpi=call.dpi or DEFAULT_CROP_DPI)
                    crop_ref = f'crop_{turn_index + 1:04d}_{call_index + 1:02d}'
                    prepared_crops[crop_ref] = _PreparedCrop(page_no=call.page_no, bbox=list(call.bbox), content=content, width=width, height=height)
                    next_image_inputs.append('data:image/png;base64,' + base64.b64encode(content).decode('ascii'))
                    next_tool_results.append({'tool': call.tool, 'crop_ref': crop_ref, 'page_no': call.page_no, 'bbox': call.bbox, 'image_input_index': len(next_image_inputs) - 1})
                else:
                    crop = prepared_crops.get(call.crop_ref)
                    if crop is None:
                        raise SourcePageVisualAnalysisError('Board visual registration references an unknown crop.')
                    candidate = BoardVisualCandidate(page_no=crop.page_no, kind=call.kind, bbox=crop.bbox, caption=call.caption, nearby_anchor=call.nearby_anchor, teaching_reason=call.teaching_reason, related_section=call.related_section, uncertainty_note=call.uncertainty_note, confidence=call.confidence)
                    if not candidate.teaching_reason.strip():
                        raise SourcePageVisualAnalysisError('Board visual registration requires a teaching reason.')
                    if any((item.page_start == crop.page_no and [round(value, 6) for value in item.bbox] == [round(value, 6) for value in crop.bbox] for item in visuals)):
                        raise SourcePageVisualAnalysisError('Duplicate Board visual registration was skipped.')
                    visual = register_board_visual(source=source, structure=structure, chapter=chapter, page_no=crop.page_no, candidate=candidate, content=crop.content, width=crop.width, height=crop.height, scope_cache_key=scope_cache_key, model_identity=model_identity, extracted_text=_text_inside_region(layouts_by_page.get(crop.page_no), crop.bbox), order_index=len(visuals))
                    visuals.append(visual)
                    prepared_crops.pop(call.crop_ref, None)
                    next_tool_results.append({'tool': call.tool, 'crop_ref': call.crop_ref, 'visual_id': visual.id, 'board_marker': f'[OPENCLASS_VISUAL:{visual.id}]'})
            except Exception as exc:
                warnings.append(f'{call.tool} failed: {exc}')
                next_tool_results.append({'tool': call.tool, 'status': 'warning', 'reason': str(exc)})
        tool_results = next_tool_results
        image_inputs = next_image_inputs
        if agent_turn.finished:
            finished = True
            break
        if not agent_turn.tool_calls:
            warnings.append('Board Agent stopped without requesting another visual tool.')
            break
    if not finished:
        warnings.append('Board Agent visual work ended at the runtime safety budget.')
    _publish_activity(on_activity, AgentActivityEvent(turn_id=activity_turn_id, stage='verify', label=f'Board Agent registered {len(visuals)} teaching visuals', status='completed', role='BoardWriter', metadata={'kind': 'board_visual_selection_completed', 'page_count': page_count, 'viewed_page_count': len(viewed_pages), 'registered_visual_count': len(visuals), 'excluded_candidate_count': len(excluded), 'warning_count': len(warnings)}))
    return SourcePageVisualAnalysisResult(visuals=visuals, excluded_candidates=excluded, warnings=warnings, page_count=page_count)

def _validate_bbox(bbox: list[float]) -> None:
    if len(bbox) != 4 or not all((0.0 <= value <= 1.0 for value in bbox)):
        raise SourcePageVisualAnalysisError('PDF crop coordinates are invalid.')
    left, top, right, bottom = bbox
    width, height = (right - left, bottom - top)
    if width < 0.015 or height < 0.015 or width * height > 0.95:
        raise SourcePageVisualAnalysisError('PDF crop coordinates are unsafe.')

def _render_page_data_url(path: Path, *, page_no: int, dpi: int) -> str:
    try:
        from openclass_core.services import pdf_backend as fitz
        with fitz.open(path) as document:
            if page_no > document.page_count:
                raise SourcePageVisualAnalysisError('PDF render page does not exist.')
            page = document.load_page(page_no - 1)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=False)
            content = pixmap.tobytes('png')
    except SourcePageVisualAnalysisError:
        raise
    except Exception as exc:
        raise SourcePageVisualAnalysisError(f'PDF p.{page_no} page render failed.') from exc
    return 'data:image/png;base64,' + base64.b64encode(content).decode('ascii')

def _text_inside_region(layout: OCRPageLayout | None, bbox: list[float]) -> str:
    if layout is None:
        return ''
    left, top, right, bottom = bbox
    lines: list[str] = []
    for line in layout.lines:
        line_left = line.x
        line_top = 1.0 - line.y - line.height / 2.0
        line_right = line.x + line.width
        line_bottom = line_top + line.height
        if line_right >= left and line_left <= right and (line_bottom >= top) and (line_top <= bottom):
            lines.append(line.text)
    return '\n'.join(lines)[:8000]

def _stored_kind(kind: str) -> str:
    if kind == 'table':
        return 'table'
    if kind in {'concept_map', 'relationship_diagram', 'flowchart', 'algorithm_box', 'model_architecture', 'data_chart'}:
        return 'diagram'
    return 'image'

def _check_cancelled(is_cancelled: Callable[[], bool] | None) -> None:
    if is_cancelled is not None and is_cancelled():
        raise SourcePageVisualAnalysisError('Board visual selection was cancelled.')

def _publish_activity(on_activity, event: AgentActivityEvent) -> None:
    if on_activity is not None:
        on_activity(event)
