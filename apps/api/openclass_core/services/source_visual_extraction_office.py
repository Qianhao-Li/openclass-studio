from __future__ import annotations
import mimetypes
import posixpath
import re
from pathlib import Path
from typing import Any
from openclass_core.services.source_archive import SafeSourceArchive, SourceArchiveError
from openclass_core.services.source_visual_extraction_budget import SourceVisualExtractionBudget, SourceVisualExtractionBudgetError
from openclass_core.services.source_visual_extraction_types import RawSourceVisual, SourceVisualAdapterResult
from openclass_core.services.source_visual_storage import MAX_SOURCE_VISUAL_BYTES
from openclass_core.services.source_xml import SourceXmlError, parse_untrusted_xml
_BLIP_VISUAL_EFFECT_NAMES = frozenset({'alphaBiLevel', 'alphaCeiling', 'alphaFloor', 'alphaInv', 'alphaMod', 'alphaModFix', 'alphaRepl', 'biLevel', 'blur', 'clrChange', 'clrRepl', 'duotone', 'fillOverlay', 'grayscl', 'hsl', 'lum', 'tint'})
_ALTERNATE_IMAGE_REPRESENTATION_NAMES = frozenset({'imgEffect', 'imgLayer', 'imgProps', 'svgBlip'})

def extract_office_visuals(path: Path, *, office_format: str | None=None) -> SourceVisualAdapterResult:
    normalized_format = (office_format or path.suffix.removeprefix('.')).strip().lower()
    budget = SourceVisualExtractionBudget()
    try:
        with SafeSourceArchive(path) as archive:
            if normalized_format == 'docx':
                return _extract_docx(archive, budget=budget)
            if normalized_format == 'pptx':
                return _extract_pptx(archive, budget=budget)
            if normalized_format == 'xlsx':
                return _extract_xlsx(archive, budget=budget)
    except (SourceArchiveError, SourceVisualExtractionBudgetError, SourceXmlError, OSError) as exc:
        return SourceVisualAdapterResult(status='failed', warnings=[f'Office visual parsing failed: {exc}'])
    return SourceVisualAdapterResult(status='ready')

def _extract_docx(archive: SafeSourceArchive, *, budget: SourceVisualExtractionBudget) -> SourceVisualAdapterResult:
    if 'word/document.xml' not in archive.namelist():
        return SourceVisualAdapterResult(status='failed', warnings=['DOCX document.xml is missing.'])
    root = parse_untrusted_xml(archive.read('word/document.xml'))
    relationships = _relationships(archive, 'word/document.xml')
    body = next((node for node in root.iter() if node.tag.endswith('}body')), None)
    if body is None:
        return SourceVisualAdapterResult(status='ready')
    visuals: list[RawSourceVisual] = []
    text_offset = 0
    paragraph_index = 0
    native_order = 0
    native_chart_anchors: list[RawSourceVisual] = []
    image_display_fidelity_issue_count = 0
    native_render_mapping_issue_count = 0
    merged_table_issue_count = 0
    body_blocks = list(body)
    has_rendered_pagination = any((node.tag.endswith('}lastRenderedPageBreak') for node in root.iter()))
    rendered_break_count = 0
    for block_index, block in enumerate(body_blocks):
        if block.tag.endswith('}p'):
            paragraph_text = _node_text(block)
            section_geometry = _docx_section_geometry(body_blocks, block_index)
            drawing_containers = [node for node in block.iter() if node.tag.endswith('}anchor') or node.tag.endswith('}inline')]
            for drawing_index, container in enumerate(drawing_containers):
                page_no = rendered_break_count + _count_nodes_before(block, container, 'lastRenderedPageBreak') + 1 if has_rendered_pagination else None
                bbox, position_reliable, position_metadata = _docx_drawing_bbox(container, section_geometry=section_geometry, page_no=page_no)
                is_floating = container.tag.endswith('}anchor')
                for object_index, (object_kind, object_node) in enumerate(_docx_drawing_objects(container)):
                    common_metadata = {'paragraph_index': paragraph_index, 'drawing_index': drawing_index, 'floating_drawing': is_floating, 'page_position_reliable': position_reliable, 'force_unverified': bool(is_floating and (not position_reliable)), **position_metadata}
                    if object_kind == 'image':
                        budget.reserve_visual_objects()
                        relationship_id = _relationship_attribute(object_node, 'embed')
                        target = relationships.get(relationship_id, '')
                        content = _safe_archive_read(archive, target, image_budget=budget)
                        if not content:
                            continue
                        display_metadata = _ooxml_image_display_metadata(scope=container, blip=object_node, force_unverified=bool(common_metadata['force_unverified']))
                        if display_metadata['office_image_display_transform_reasons']:
                            image_display_fidelity_issue_count += 1
                        visuals.append(RawSourceVisual(kind='image', source_locator=f'docx:paragraph:{paragraph_index}:drawing:{drawing_index}:image:{object_index}', native_order=native_order, content=content, mime_type=_image_mime(target, content), page_no=page_no, paragraph_index=paragraph_index, bbox=bbox, text_offset=text_offset, caption=_doc_properties_caption(container) or paragraph_text, confidence=0.96 if position_reliable else 0.78, metadata={'office_part': target, **common_metadata, **display_metadata}))
                    else:
                        budget.reserve_visual_objects()
                        rendered_mapping_reliable = bool(page_no is not None and position_reliable and (len(bbox) == 4))
                        if not rendered_mapping_reliable:
                            native_render_mapping_issue_count += 1
                        native_chart_anchors.append(RawSourceVisual(kind=object_kind, source_locator=f'docx:paragraph:{paragraph_index}:drawing:{drawing_index}:native-{object_kind}:{object_index}', native_order=native_order, page_no=page_no, paragraph_index=paragraph_index, bbox=bbox, text_offset=text_offset, caption=_doc_properties_caption(container) or paragraph_text, confidence=0.88 if position_reliable else 0.72, metadata={**common_metadata, 'force_unverified': bool(common_metadata['force_unverified'] or not rendered_mapping_reliable), 'rendered_bbox_reliable': rendered_mapping_reliable, 'rendered_mapping_reason': 'page_bbox' if rendered_mapping_reliable else 'inline_or_unresolved_page_bbox'}))
                    native_order += 1
            if paragraph_text:
                text_offset += len(paragraph_text) + 2
            paragraph_index += 1
        elif block.tag.endswith('}tbl'):
            table_paragraphs = list(_docx_table_paragraphs(block))
            table_paragraph_start = paragraph_index
            table_paragraph_end = paragraph_index + max(0, len(table_paragraphs) - 1)
            budget.reserve_visual_objects()
            table_data = _word_table(block)
            if table_data:
                budget.account_table(table_data)
                table_caption = _word_table_caption(block)
                merge_markers = _word_table_merge_markers(block)
                if merge_markers:
                    merged_table_issue_count += 1
                visuals.append(RawSourceVisual(kind='table', source_locator=f'docx:table:{block_index}', native_order=native_order, text_offset=text_offset, table_data=table_data, caption=table_caption, ocr_text=_table_semantic_text(table_data), confidence=0.92, metadata={'office_part': 'word/document.xml', 'block_index': block_index, 'paragraph_start_index': table_paragraph_start, 'paragraph_end_index': table_paragraph_end, **_unrepresented_table_merge_metadata(merge_markers)}))
                native_order += 1
            section_geometry = _docx_section_geometry(body_blocks, block_index)
            for row_index, cell_index, cell_paragraph_index, paragraph in table_paragraphs:
                paragraph_text = _node_text(paragraph)
                drawing_containers = [node for node in paragraph.iter() if node.tag.endswith('}anchor') or node.tag.endswith('}inline')]
                for drawing_index, container in enumerate(drawing_containers):
                    page_no = rendered_break_count + _count_nodes_before(block, container, 'lastRenderedPageBreak') + 1 if has_rendered_pagination else None
                    bbox, position_reliable, position_metadata = _docx_drawing_bbox(container, section_geometry=section_geometry, page_no=page_no)
                    is_floating = container.tag.endswith('}anchor')
                    for object_index, (object_kind, object_node) in enumerate(_docx_drawing_objects(container)):
                        locator_prefix = f'docx:table:{block_index}:row:{row_index}:cell:{cell_index}:paragraph:{cell_paragraph_index}:drawing:{drawing_index}'
                        common_metadata = {'table_block_index': block_index, 'table_row_index': row_index, 'table_cell_index': cell_index, 'cell_paragraph_index': cell_paragraph_index, 'paragraph_start_index': paragraph_index, 'paragraph_end_index': paragraph_index, 'drawing_index': drawing_index, 'floating_drawing': is_floating, 'page_position_reliable': position_reliable, 'force_unverified': bool(is_floating and (not position_reliable)), **position_metadata}
                        if object_kind == 'image':
                            budget.reserve_visual_objects()
                            target = relationships.get(_relationship_attribute(object_node, 'embed'), '')
                            content = _safe_archive_read(archive, target, image_budget=budget)
                            if not content:
                                continue
                            display_metadata = _ooxml_image_display_metadata(scope=container, blip=object_node, force_unverified=bool(common_metadata['force_unverified']))
                            if display_metadata['office_image_display_transform_reasons']:
                                image_display_fidelity_issue_count += 1
                            visuals.append(RawSourceVisual(kind='image', source_locator=f'{locator_prefix}:image:{object_index}', native_order=native_order, content=content, mime_type=_image_mime(target, content), page_no=page_no, paragraph_index=paragraph_index, bbox=bbox, text_offset=text_offset, caption=_doc_properties_caption(container) or paragraph_text, confidence=0.94 if position_reliable else 0.78, metadata={'office_part': target, **common_metadata, **display_metadata}))
                        else:
                            budget.reserve_visual_objects()
                            rendered_mapping_reliable = bool(page_no is not None and position_reliable and (len(bbox) == 4))
                            if not rendered_mapping_reliable:
                                native_render_mapping_issue_count += 1
                            native_chart_anchors.append(RawSourceVisual(kind=object_kind, source_locator=f'{locator_prefix}:native-{object_kind}:{object_index}', native_order=native_order, page_no=page_no, paragraph_index=paragraph_index, bbox=bbox, text_offset=text_offset, caption=_doc_properties_caption(container) or paragraph_text, confidence=0.84 if rendered_mapping_reliable else 0.7, metadata={**common_metadata, 'force_unverified': bool(common_metadata['force_unverified'] or not rendered_mapping_reliable), 'rendered_bbox_reliable': rendered_mapping_reliable, 'rendered_mapping_reason': 'page_bbox' if rendered_mapping_reliable else 'inline_or_unresolved_page_bbox'}))
                        native_order += 1
                if paragraph_text:
                    text_offset += len(paragraph_text) + 2
                paragraph_index += 1
        rendered_break_count += sum((node.tag.endswith('}lastRenderedPageBreak') for node in block.iter()))
    warnings = [*_office_image_display_warnings(image_display_fidelity_issue_count), *_office_native_mapping_warnings(native_render_mapping_issue_count), *_office_merged_table_warnings(merged_table_issue_count)]
    return SourceVisualAdapterResult(visuals=visuals, warnings=warnings, status='partial' if warnings else 'ready', native_chart_count=len(native_chart_anchors), native_chart_anchors=native_chart_anchors)

def _extract_pptx(archive: SafeSourceArchive, *, budget: SourceVisualExtractionBudget) -> SourceVisualAdapterResult:
    slide_names = sorted((name for name in archive.namelist() if re.fullmatch('ppt/slides/slide\\d+\\.xml', name)), key=lambda name: int(re.search('(\\d+)', Path(name).stem).group(1)))
    slide_width, slide_height = _pptx_slide_size(archive)
    visuals: list[RawSourceVisual] = []
    native_order = 0
    native_chart_anchors: list[RawSourceVisual] = []
    image_display_fidelity_issue_count = 0
    merged_table_issue_count = 0
    for slide_no, slide_name in enumerate(slide_names, start=1):
        root = parse_untrusted_xml(archive.read(slide_name))
        relationships = _relationships(archive, slide_name)
        shape_nodes = _pptx_visual_shapes(root, slide_width=slide_width, slide_height=slide_height)
        for shape_index, shape, bbox, transform_reliable in shape_nodes:
            shape_metadata = {'slide': slide_no, 'shape_index': shape_index, 'group_transform_reliable': transform_reliable, 'force_unverified': not transform_reliable}
            if shape.tag.endswith('}grpSp'):
                budget.reserve_visual_objects()
                native_chart_anchors.append(RawSourceVisual(kind='diagram', source_locator=f'pptx:slide:{slide_no}:native-group-diagram:{shape_index}', native_order=native_order, slide_no=slide_no, page_no=slide_no, bbox=bbox, caption=_shape_caption(shape), confidence=0.84 if transform_reliable else 0.68, metadata={**shape_metadata, 'group_shape_diagram': True}))
                native_order += 1
                continue
            table = next((node for node in shape.iter() if node.tag.endswith('}tbl')), None)
            if table is not None:
                budget.reserve_visual_objects()
                table_data = _drawing_table(table)
                if table_data:
                    budget.account_table(table_data)
                    merge_markers = _drawing_table_merge_markers(table)
                    if merge_markers:
                        merged_table_issue_count += 1
                    visuals.append(RawSourceVisual(kind='table', source_locator=f'pptx:slide:{slide_no}:table:{shape_index}', native_order=native_order, slide_no=slide_no, page_no=slide_no, bbox=bbox, caption=_shape_caption(shape), table_data=table_data, confidence=0.96 if transform_reliable else 0.72, metadata={**shape_metadata, **_unrepresented_table_merge_metadata(merge_markers)}))
                    native_order += 1
            for image_index, blip in enumerate((node for node in shape.iter() if node.tag.endswith('}blip'))):
                budget.reserve_visual_objects()
                relationship_id = _relationship_attribute(blip, 'embed')
                target = relationships.get(relationship_id, '')
                content = _safe_archive_read(archive, target, image_budget=budget)
                if not content:
                    continue
                display_metadata = _ooxml_image_display_metadata(scope=shape, blip=blip, force_unverified=bool(shape_metadata['force_unverified']), transform_reliable=transform_reliable)
                if display_metadata['office_image_display_transform_reasons']:
                    image_display_fidelity_issue_count += 1
                visuals.append(RawSourceVisual(kind='image', source_locator=f'pptx:slide:{slide_no}:image:{shape_index}:{image_index}', native_order=native_order, content=content, mime_type=_image_mime(target, content), slide_no=slide_no, page_no=slide_no, bbox=bbox, caption=_shape_caption(shape), confidence=0.96 if transform_reliable else 0.72, metadata={'office_part': target, **shape_metadata, **display_metadata}))
                native_order += 1
            for chart_index, native_node in enumerate((node for node in shape.iter() if node.tag.endswith('}chart') or node.tag.endswith('}relIds'))):
                native_kind = 'diagram' if native_node.tag.endswith('}relIds') else 'chart'
                budget.reserve_visual_objects()
                native_chart_anchors.append(RawSourceVisual(kind=native_kind, source_locator=f'pptx:slide:{slide_no}:native-{native_kind}:{shape_index}:{chart_index}', native_order=native_order, slide_no=slide_no, page_no=slide_no, bbox=bbox, caption=_shape_caption(shape), confidence=0.88 if transform_reliable else 0.7, metadata=shape_metadata))
                native_order += 1
    warnings = [*_office_image_display_warnings(image_display_fidelity_issue_count), *_office_merged_table_warnings(merged_table_issue_count)]
    return SourceVisualAdapterResult(visuals=visuals, warnings=warnings, status='partial' if warnings else 'ready', native_chart_count=len(native_chart_anchors), native_chart_anchors=native_chart_anchors)

def _extract_xlsx(archive: SafeSourceArchive, *, budget: SourceVisualExtractionBudget) -> SourceVisualAdapterResult:
    shared_strings = _xlsx_shared_strings(archive)
    sheets = _xlsx_sheets(archive)
    visuals: list[RawSourceVisual] = []
    native_order = 0
    native_chart_anchors: list[RawSourceVisual] = []
    image_display_fidelity_issue_count = 0
    for sheet_no, (sheet_name, sheet_path) in enumerate(sheets, start=1):
        if sheet_path not in archive.namelist():
            continue
        root = parse_untrusted_xml(archive.read(sheet_path))
        rendered_page_no = 1 if len(sheets) == 1 and _xlsx_sheet_is_forced_to_single_page(root) else None
        cells, max_column, max_row = _xlsx_cells(root, shared_strings)
        sheet_relationships = _relationships(archive, sheet_path)
        native_tables = _xlsx_native_tables(archive, sheet_root=root, relationships=sheet_relationships)
        for _caption, _reference, bounds in native_tables:
            max_column = max(max_column, bounds[2])
            max_row = max(max_row, bounds[3])
        for table_index, (caption, reference, bounds) in enumerate(native_tables):
            budget.reserve_visual_objects()
            table_data = _xlsx_range_data(cells, bounds)
            if not table_data or not any((any((cell for cell in row)) for row in table_data)):
                continue
            budget.account_table(table_data)
            visuals.append(RawSourceVisual(kind='table', source_locator=f'xlsx:sheet:{sheet_no}:native-table:{table_index}:{reference}', native_order=native_order, sheet_name=sheet_name, bbox=_spreadsheet_range_bbox(bounds, max_column=max_column, max_row=max_row), table_data=table_data, caption=caption or sheet_name, confidence=0.98, metadata={'sheet': sheet_no, 'max_column': max_column, 'max_row': max_row, 'native_table': True, 'table_reference': reference}))
            native_order += 1
        for drawing in (node for node in root.iter() if node.tag.endswith('}drawing')):
            drawing_path = sheet_relationships.get(_relationship_attribute(drawing, 'id'), '')
            if not drawing_path or drawing_path not in archive.namelist():
                continue
            drawing_root = parse_untrusted_xml(archive.read(drawing_path))
            drawing_relationships = _relationships(archive, drawing_path)
            anchors = [node for node in list(drawing_root) if node.tag.rsplit('}', 1)[-1].endswith('Anchor')]
            for anchor_index, anchor in enumerate(anchors):
                bbox = _spreadsheet_anchor_bbox(anchor, max_column=max_column, max_row=max_row)
                for image_index, blip in enumerate((node for node in anchor.iter() if node.tag.endswith('}blip'))):
                    budget.reserve_visual_objects()
                    target = drawing_relationships.get(_relationship_attribute(blip, 'embed'), '')
                    content = _safe_archive_read(archive, target, image_budget=budget)
                    if not content:
                        continue
                    display_metadata = _ooxml_image_display_metadata(scope=anchor, blip=blip)
                    if display_metadata['office_image_display_transform_reasons']:
                        image_display_fidelity_issue_count += 1
                    visuals.append(RawSourceVisual(kind='image', source_locator=f'xlsx:sheet:{sheet_no}:image:{anchor_index}:{image_index}', native_order=native_order, content=content, mime_type=_image_mime(target, content), sheet_name=sheet_name, bbox=bbox, caption=_shape_caption(anchor), confidence=0.9, metadata={'office_part': target, 'sheet': sheet_no, 'max_row': max_row, **display_metadata}))
                    native_order += 1
                for chart_index, _node in enumerate((node for node in anchor.iter() if node.tag.endswith('}chart'))):
                    budget.reserve_visual_objects()
                    native_chart_anchors.append(RawSourceVisual(kind='chart', source_locator=f'xlsx:sheet:{sheet_no}:native-chart:{anchor_index}:{chart_index}', native_order=native_order, page_no=rendered_page_no, sheet_name=sheet_name, bbox=bbox, caption=_shape_caption(anchor), confidence=0.82, metadata={'sheet': sheet_no, 'max_row': max_row, 'rendered_page_mapping': 'single_sheet_fit_to_one_page' if rendered_page_no is not None else 'unverified', 'force_unverified': rendered_page_no is None}))
                    native_order += 1
    warnings = _office_image_display_warnings(image_display_fidelity_issue_count)
    return SourceVisualAdapterResult(visuals=visuals, warnings=warnings, status='partial' if warnings else 'ready', native_chart_count=len(native_chart_anchors), native_chart_anchors=native_chart_anchors)

def _relationships(archive: SafeSourceArchive, source_part: str) -> dict[str, str]:
    source_directory = posixpath.dirname(source_part)
    relationship_path = posixpath.join(source_directory, '_rels', f'{posixpath.basename(source_part)}.rels')
    if relationship_path not in archive.namelist():
        return {}
    root = parse_untrusted_xml(archive.read(relationship_path))
    relationships: dict[str, str] = {}
    for node in root.iter():
        if not node.tag.endswith('}Relationship'):
            continue
        if str(node.attrib.get('TargetMode') or '').lower() == 'external':
            continue
        relationship_id = str(node.attrib.get('Id') or '')
        target = str(node.attrib.get('Target') or '')
        normalized = posixpath.normpath(posixpath.join(source_directory, target)).lstrip('/')
        if relationship_id and normalized and (not normalized.startswith('../')):
            relationships[relationship_id] = normalized
    return relationships

def _safe_archive_read(archive: SafeSourceArchive, name: str, *, image_budget: SourceVisualExtractionBudget | None=None) -> bytes:
    if not name or name.startswith('/') or '..' in Path(name).parts:
        return b''
    try:
        content = archive.read(name, max_bytes=MAX_SOURCE_VISUAL_BYTES)
    except KeyError:
        return b''
    if image_budget is not None:
        image_budget.account_image_bytes(len(content))
    return content

def _ooxml_image_display_metadata(*, scope: Any, blip: Any, force_unverified: bool=False, transform_reliable: bool=True) -> dict[str, Any]:
    """Describe whether raw package media is equivalent to the visible Office picture.

    Office stores the original media separately from the DrawingML display layer. Crops,
    masks, rotations, flips, and effects therefore cannot be represented by copying the
    media bytes alone. These objects stay indexed for provenance, but are deliberately
    unverified until a rendered-page crop can be mapped deterministically.
    """
    reasons = _ooxml_image_display_transform_reasons(scope=scope, blip=blip)
    if not transform_reliable and (not any((reason in {'rotation', 'flip'} for reason in reasons))):
        reasons.append('shape_transform_unresolved')
    reasons = list(dict.fromkeys(reasons))
    return {'office_image_display_fidelity': 'unverified' if reasons else 'raw_media_equivalent', 'office_image_display_transform_reasons': reasons, 'force_unverified': bool(force_unverified or reasons)}

def _ooxml_image_display_transform_reasons(*, scope: Any, blip: Any) -> list[str]:
    path = _xml_path_to_node(scope, blip)
    if not path:
        return ['display_layer_unresolved']
    picture = next((node for node in reversed(path) if _xml_local_name(node) == 'pic'), scope)
    blip_fill = next((node for node in reversed(path) if _xml_local_name(node) == 'blipFill'), None)
    reasons: list[str] = []
    if blip_fill is not None:
        source_rectangle = next((node for node in blip_fill.iter() if _xml_local_name(node) == 'srcRect'), None)
        if source_rectangle is not None and _rectangle_has_nonzero_edges(source_rectangle):
            reasons.append('crop')
        fill_rectangle = next((node for node in blip_fill.iter() if _xml_local_name(node) == 'fillRect'), None)
        if fill_rectangle is not None and _rectangle_has_nonzero_edges(fill_rectangle):
            reasons.append('crop')
        if any((_xml_local_name(node) == 'tile' for node in blip_fill.iter())):
            reasons.append('tile')
    for transform in _picture_and_ancestor_group_transforms(path=path, picture=picture):
        transform_reasons = _ooxml_transform_display_reasons(transform)
        reasons.extend(transform_reasons)
    shape_properties = next((node for node in list(picture) if _xml_local_name(node) in {'spPr', 'grpSpPr'}), None)
    if shape_properties is not None:
        for node in shape_properties.iter():
            local_name = _xml_local_name(node)
            if local_name == 'custGeom' or local_name == 'clipPath':
                reasons.append('mask')
            elif local_name == 'prstGeom':
                preset = str(node.attrib.get('prst') or '').strip().lower()
                if preset != 'rect':
                    reasons.append('mask')
            elif local_name in {'effectDag', 'scene3d', 'sp3d'}:
                reasons.append('effect')
            elif local_name == 'effectLst' and list(node):
                reasons.append('effect')
            elif local_name == 'ln' and (not any((_xml_local_name(child) == 'noFill' for child in node.iter()))):
                reasons.append('effect')
    if any((_xml_local_name(node) == 'style' for node in list(picture))):
        reasons.append('effect')
    if any((_xml_local_name(node) in _BLIP_VISUAL_EFFECT_NAMES for node in blip.iter())):
        reasons.append('effect')
    if any((_xml_local_name(node) in _ALTERNATE_IMAGE_REPRESENTATION_NAMES for node in blip.iter())):
        reasons.append('alternate_representation')
    return list(dict.fromkeys(reasons))

def _xml_path_to_node(root: Any, target: Any) -> list[Any]:
    if root is target:
        return [root]
    for child in list(root):
        child_path = _xml_path_to_node(child, target)
        if child_path:
            return [root, *child_path]
    return []

def _xml_local_name(node: Any) -> str:
    return str(node.tag).rsplit('}', 1)[-1]

def _rectangle_has_nonzero_edges(node: Any) -> bool:
    for edge in ('l', 't', 'r', 'b'):
        value = _local_attribute(node, edge)
        if value is None:
            continue
        try:
            if float(value) != 0:
                return True
        except (TypeError, ValueError):
            return True
    return False

def _picture_and_ancestor_group_transforms(*, path: list[Any], picture: Any) -> list[Any]:
    transforms: list[Any] = []
    for ancestor in path:
        local_name = _xml_local_name(ancestor)
        property_names: set[str]
        if ancestor is picture:
            property_names = {'spPr'}
        elif local_name == 'grpSp':
            property_names = {'grpSpPr'}
        else:
            continue
        for properties in (child for child in list(ancestor) if _xml_local_name(child) in property_names):
            transforms.extend((child for child in list(properties) if _xml_local_name(child) == 'xfrm'))
    return transforms

def _ooxml_transform_display_reasons(transform: Any) -> list[str]:
    reasons: list[str] = []
    try:
        rotation = float(_local_attribute(transform, 'rot') or 0)
    except (TypeError, ValueError):
        rotation = 1
    if rotation != 0:
        reasons.append('rotation')
    if any((str(_local_attribute(transform, name) or '').lower() in {'1', 'true', 'on'} for name in ('flipH', 'flipV'))):
        reasons.append('flip')
    return reasons

def _office_image_display_warnings(issue_count: int) -> list[str]:
    if issue_count <= 0:
        return []
    return [f'{issue_count} embedded Office image object(s) use a display crop, transform, mask, or effect that cannot be reproduced from the raw media bytes; they were kept as unverified visual records.']

def _office_native_mapping_warnings(issue_count: int) -> list[str]:
    if issue_count <= 0:
        return []
    return [f'{issue_count} native DOCX chart or diagram object(s) do not expose a deterministic rendered page bounding box and cannot be admitted as verified visual evidence.']

def _office_merged_table_warnings(issue_count: int) -> list[str]:
    if issue_count <= 0:
        return []
    return [f'{issue_count} native Office table(s) contain merged cells whose span semantics cannot be represented by the current editable table matrix; they were kept as unverified visual records.']

def _relationship_attribute(node: Any, local_name: str) -> str:
    for key, value in node.attrib.items():
        if key == local_name or key.endswith(f'}}{local_name}'):
            return str(value)
    return ''

def _node_text(node: Any) -> str:
    return ' '.join(''.join((str(text_node.text or '') for text_node in node.iter() if text_node.tag.endswith('}t'))).split())

def _word_table(table: Any) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in (node for node in table.iter() if node.tag.endswith('}tr')):
        values = [_word_cell_text(cell) for cell in list(row) if cell.tag.endswith('}tc')]
        if values and any(values):
            rows.append(values)
    return rows

def _word_table_merge_markers(table: Any) -> list[str]:
    markers: set[str] = set()
    for cell in (node for node in table.iter() if _xml_local_name(node) == 'tc'):
        cell_properties = next((node for node in list(cell) if _xml_local_name(node) == 'tcPr'), None)
        if cell_properties is None:
            continue
        for property_node in list(cell_properties):
            local_name = _xml_local_name(property_node)
            if local_name == 'gridSpan':
                if _span_attribute_requires_merge(_local_attribute(property_node, 'val')):
                    markers.add('gridSpan')
            elif local_name in {'hMerge', 'vMerge'}:
                markers.add(local_name)
    return sorted(markers)

def _word_cell_text(cell: Any) -> str:
    paragraph_values = [_node_text(paragraph) for paragraph in list(cell) if paragraph.tag.endswith('}p')]
    compact = ' '.join((value for value in paragraph_values if value)).strip()
    return compact or _node_text(cell)

def _docx_table_paragraphs(table: Any) -> list[tuple[int, int, int, Any]]:
    paragraphs: list[tuple[int, int, int, Any]] = []
    rows = [node for node in list(table) if node.tag.endswith('}tr')]
    for row_index, row in enumerate(rows):
        cells = [node for node in list(row) if node.tag.endswith('}tc')]
        for cell_index, cell in enumerate(cells):
            cell_paragraphs = [node for node in cell.iter() if node.tag.endswith('}p')]
            paragraphs.extend(((row_index, cell_index, paragraph_index, paragraph) for paragraph_index, paragraph in enumerate(cell_paragraphs)))
    return paragraphs

def _table_semantic_text(table_data: list[list[str]], *, max_cells: int=64, max_characters: int=1200) -> str:
    """Build bounded positioning context without changing the frozen cell matrix."""
    rows: list[str] = []
    used_cells = 0
    for row in table_data:
        values: list[str] = []
        for cell in row:
            if used_cells >= max_cells:
                break
            compact = ' '.join(str(cell or '').split())
            values.append(compact[:240])
            used_cells += 1
        if values:
            rows.append(' | '.join(values).strip())
        if used_cells >= max_cells:
            break
    return '\n'.join(rows).strip()[:max_characters]

def _drawing_table(table: Any) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in (node for node in table.iter() if node.tag.endswith('}tr')):
        values = [_node_text(cell) for cell in list(row) if cell.tag.endswith('}tc')]
        if values and any(values):
            rows.append(values)
    return rows

def _drawing_table_merge_markers(table: Any) -> list[str]:
    markers: set[str] = set()
    for cell in (node for node in table.iter() if _xml_local_name(node) == 'tc'):
        for attribute_name in ('gridSpan', 'rowSpan'):
            value = _local_attribute(cell, attribute_name)
            if value is not None and _span_attribute_requires_merge(value):
                markers.add(attribute_name)
        for attribute_name in ('hMerge', 'vMerge'):
            value = _local_attribute(cell, attribute_name)
            if value is not None and _boolean_attribute_is_true_or_ambiguous(value):
                markers.add(attribute_name)
    return sorted(markers)

def _span_attribute_requires_merge(value: str | None) -> bool:
    if value is None:
        return True
    try:
        return int(str(value).strip()) != 1
    except (TypeError, ValueError):
        return True

def _boolean_attribute_is_true_or_ambiguous(value: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {'0', 'false', 'off', 'no'}:
        return False
    return True

def _unrepresented_table_merge_metadata(markers: list[str]) -> dict[str, Any]:
    if not markers:
        return {}
    return {'force_unverified': True, 'table_merge_semantics': 'unrepresented', 'table_merge_markers': markers}

def _docx_drawing_objects(container: Any) -> list[tuple[str, Any]]:
    objects: list[tuple[str, Any]] = []
    for node in container.iter():
        if node.tag.endswith('}blip'):
            objects.append(('image', node))
        elif node.tag.endswith('}chart'):
            objects.append(('chart', node))
        elif node.tag.endswith('}relIds'):
            objects.append(('diagram', node))
    return objects

def _docx_section_geometry(body_blocks: list[Any], block_index: int) -> tuple[float, float, float | None, float | None, float | None, float | None] | None:
    section_properties = None
    for candidate in body_blocks[block_index:]:
        section_properties = next((node for node in candidate.iter() if node.tag.endswith('}sectPr')), None)
        if section_properties is not None:
            break
    if section_properties is None:
        return None
    page_size = next((node for node in section_properties if node.tag.endswith('}pgSz')), None)
    if page_size is None:
        return None
    try:
        page_width = float(_local_attribute(page_size, 'w') or 0) * 635.0
        page_height = float(_local_attribute(page_size, 'h') or 0) * 635.0
    except (TypeError, ValueError):
        return None
    if page_width <= 0 or page_height <= 0:
        return None
    page_margins = next((node for node in section_properties if node.tag.endswith('}pgMar')), None)
    margins: list[float | None] = [None, None, None, None]
    if page_margins is not None:
        for index, name in enumerate(('left', 'right', 'top', 'bottom')):
            try:
                value = _local_attribute(page_margins, name)
                margins[index] = float(value) * 635.0 if value is not None else None
            except (TypeError, ValueError):
                margins[index] = None
    return (page_width, page_height, margins[0], margins[1], margins[2], margins[3])

def _docx_drawing_bbox(container: Any, *, section_geometry: tuple[float, float, float | None, float | None, float | None, float | None] | None, page_no: int | None) -> tuple[list[float], bool, dict[str, Any]]:
    if not container.tag.endswith('}anchor'):
        return ([], False, {'position_mode': 'inline'})
    metadata: dict[str, Any] = {'position_mode': 'floating'}
    if section_geometry is None or page_no is None:
        return ([], False, metadata)
    extent = next((node for node in list(container) if node.tag.endswith('}extent')), None)
    if extent is None:
        return ([], False, metadata)
    try:
        width = float(extent.attrib.get('cx', 0))
        height = float(extent.attrib.get('cy', 0))
    except (TypeError, ValueError):
        return ([], False, metadata)
    if width <= 0 or height <= 0:
        return ([], False, metadata)
    page_width, page_height, margin_left, margin_right, margin_top, margin_bottom = section_geometry
    simple_position_enabled = str(container.attrib.get('simplePos') or '').lower() in {'1', 'true', 'on'}
    if simple_position_enabled:
        simple_position = next((node for node in list(container) if node.tag.endswith('}simplePos')), None)
        if simple_position is None:
            return ([], False, metadata)
        try:
            x = float(simple_position.attrib.get('x', ''))
            y = float(simple_position.attrib.get('y', ''))
        except (TypeError, ValueError):
            return ([], False, metadata)
        metadata['position_reference'] = 'simplePos'
    else:
        horizontal = next((node for node in list(container) if node.tag.endswith('}positionH')), None)
        vertical = next((node for node in list(container) if node.tag.endswith('}positionV')), None)
        if horizontal is None or vertical is None:
            return ([], False, metadata)
        horizontal_reference = str(horizontal.attrib.get('relativeFrom') or '')
        vertical_reference = str(vertical.attrib.get('relativeFrom') or '')
        metadata.update({'position_h_relative_from': horizontal_reference, 'position_v_relative_from': vertical_reference})
        x = _docx_axis_position(horizontal, reference=horizontal_reference, page_extent=page_width, margin_before=margin_left, margin_after=margin_right, object_extent=width, horizontal=True)
        y = _docx_axis_position(vertical, reference=vertical_reference, page_extent=page_height, margin_before=margin_top, margin_after=margin_bottom, object_extent=height, horizontal=False)
        if x is None or y is None:
            return ([], False, metadata)
    left = max(0.0, min(page_width, x))
    top = max(0.0, min(page_height, y))
    right = max(0.0, min(page_width, x + width))
    bottom = max(0.0, min(page_height, y + height))
    if right <= left or bottom <= top:
        return ([], False, metadata)
    return ([round(left / page_width, 6), round(top / page_height, 6), round(right / page_width, 6), round(bottom / page_height, 6)], True, metadata)

def _docx_axis_position(node: Any, *, reference: str, page_extent: float, margin_before: float | None, margin_after: float | None, object_extent: float, horizontal: bool) -> float | None:
    if reference == 'page':
        origin = 0.0
        reference_extent = page_extent
    elif reference == 'margin':
        if margin_before is None or margin_after is None:
            return None
        origin = margin_before
        reference_extent = page_extent - margin_before - margin_after
    else:
        return None
    if reference_extent <= 0:
        return None
    offset = next((child for child in list(node) if child.tag.endswith('}posOffset')), None)
    if offset is not None:
        try:
            return origin + float(offset.text or '')
        except (TypeError, ValueError):
            return None
    align = next((child for child in list(node) if child.tag.endswith('}align')), None)
    alignment = str(align.text or '').strip().lower() if align is not None else ''
    leading, trailing = ('left', 'right') if horizontal else ('top', 'bottom')
    if alignment == leading:
        return origin
    if alignment == 'center':
        return origin + (reference_extent - object_extent) / 2.0
    if alignment == trailing:
        return origin + reference_extent - object_extent
    return None

def _count_nodes_before(root: Any, target: Any, local_name: str) -> int:
    count = 0
    for node in root.iter():
        if node is target:
            break
        if node.tag.endswith(f'}}{local_name}'):
            count += 1
    return count

def _local_attribute(node: Any, local_name: str) -> str | None:
    for key, value in node.attrib.items():
        if key == local_name or key.endswith(f'}}{local_name}'):
            return str(value)
    return None

def _doc_properties_caption(node: Any) -> str:
    properties = next((child for child in node.iter() if child.tag.endswith('}docPr')), None)
    if properties is None:
        return ''
    return str(properties.attrib.get('descr') or properties.attrib.get('title') or '').strip()[:500]

def _word_table_caption(table: Any) -> str:
    caption = next((node for node in table.iter() if node.tag.endswith('}tblCaption')), None)
    return str(_local_attribute(caption, 'val') or '').strip()[:500] if caption is not None else ''

def _pptx_slide_size(archive: SafeSourceArchive) -> tuple[float, float]:
    default = (12192000.0, 6858000.0)
    if 'ppt/presentation.xml' not in archive.namelist():
        return default
    root = parse_untrusted_xml(archive.read('ppt/presentation.xml'))
    size = next((node for node in root.iter() if node.tag.endswith('}sldSz')), None)
    if size is None:
        return default
    try:
        return (float(size.attrib.get('cx', default[0])), float(size.attrib.get('cy', default[1])))
    except (TypeError, ValueError):
        return default

def _pptx_visual_shapes(root: Any, *, slide_width: float, slide_height: float) -> list[tuple[int, Any, list[float], bool]]:
    shape_tree = next((node for node in root.iter() if node.tag.endswith('}spTree')), None)
    if shape_tree is None:
        return []
    results: list[tuple[int, Any, list[float], bool]] = []

    def visit(children: list[Any], parent_transform: tuple[float, float, float, float] | None) -> None:
        for child in children:
            local_name = child.tag.rsplit('}', 1)[-1]
            if local_name == 'grpSp':
                contains_shape_content = any((item is not child and item.tag.rsplit('}', 1)[-1] in {'sp', 'cxnSp'} for item in child.iter()))
                if contains_shape_content:
                    bbox, reliable = _pptx_group_bbox(child, parent_transform=parent_transform, slide_width=slide_width, slide_height=slide_height)
                    results.append((len(results), child, bbox, reliable))
                    continue
                group_transform = _pptx_group_transform(child, parent_transform=parent_transform)
                group_children = [item for item in list(child) if item.tag.rsplit('}', 1)[-1] in {'pic', 'graphicFrame', 'grpSp'}]
                visit(group_children, group_transform)
                continue
            if local_name not in {'pic', 'graphicFrame'}:
                continue
            bbox, reliable = _pptx_shape_bbox(child, parent_transform=parent_transform, slide_width=slide_width, slide_height=slide_height)
            results.append((len(results), child, bbox, reliable))
    visit(list(shape_tree), (1.0, 1.0, 0.0, 0.0))
    return results

def _pptx_group_bbox(group: Any, *, parent_transform: tuple[float, float, float, float] | None, slide_width: float, slide_height: float) -> tuple[list[float], bool]:
    if parent_transform is None:
        return ([], False)
    properties = next((node for node in list(group) if node.tag.endswith('}grpSpPr')), None)
    transform = next((node for node in list(properties) if node.tag.endswith('}xfrm')), None) if properties is not None else None
    if transform is None or _ooxml_transform_is_rotated_or_flipped(transform):
        return ([], False)
    offset = next((node for node in list(transform) if node.tag.endswith('}off')), None)
    extent = next((node for node in list(transform) if node.tag.endswith('}ext')), None)
    if offset is None or extent is None:
        return ([], False)
    try:
        x = float(offset.attrib.get('x', 0))
        y = float(offset.attrib.get('y', 0))
        width = float(extent.attrib.get('cx', 0))
        height = float(extent.attrib.get('cy', 0))
    except (TypeError, ValueError):
        return ([], False)
    return _pptx_bbox_from_parent_coordinates(x=x, y=y, width=width, height=height, parent_transform=parent_transform, slide_width=slide_width, slide_height=slide_height)

def _pptx_group_transform(group: Any, *, parent_transform: tuple[float, float, float, float] | None) -> tuple[float, float, float, float] | None:
    if parent_transform is None:
        return None
    properties = next((node for node in list(group) if node.tag.endswith('}grpSpPr')), None)
    transform = next((node for node in list(properties) if node.tag.endswith('}xfrm')), None) if properties is not None else None
    if transform is None or _ooxml_transform_is_rotated_or_flipped(transform):
        return None
    offset = next((node for node in list(transform) if node.tag.endswith('}off')), None)
    extent = next((node for node in list(transform) if node.tag.endswith('}ext')), None)
    child_offset = next((node for node in list(transform) if node.tag.endswith('}chOff')), None)
    child_extent = next((node for node in list(transform) if node.tag.endswith('}chExt')), None)
    if offset is None or extent is None or child_offset is None or (child_extent is None):
        return None
    try:
        x = float(offset.attrib.get('x', 0))
        y = float(offset.attrib.get('y', 0))
        width = float(extent.attrib.get('cx', 0))
        height = float(extent.attrib.get('cy', 0))
        child_x = float(child_offset.attrib.get('x', 0))
        child_y = float(child_offset.attrib.get('y', 0))
        child_width = float(child_extent.attrib.get('cx', 0))
        child_height = float(child_extent.attrib.get('cy', 0))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0 or child_width <= 0 or (child_height <= 0):
        return None
    local_scale_x = width / child_width
    local_scale_y = height / child_height
    local_transform = (local_scale_x, local_scale_y, x - child_x * local_scale_x, y - child_y * local_scale_y)
    return _compose_axis_transform(parent_transform, local_transform)

def _pptx_shape_bbox(node: Any, *, parent_transform: tuple[float, float, float, float] | None, slide_width: float, slide_height: float) -> tuple[list[float], bool]:
    if parent_transform is None:
        return ([], False)
    transform = next((child for child in node.iter() if child.tag.endswith('}xfrm')), None)
    if transform is None or _ooxml_transform_is_rotated_or_flipped(transform):
        return ([], False)
    offset = next((child for child in list(transform) if child.tag.endswith('}off')), None)
    extent = next((child for child in list(transform) if child.tag.endswith('}ext')), None)
    if offset is None or extent is None:
        return ([], False)
    try:
        x = float(offset.attrib.get('x', 0))
        y = float(offset.attrib.get('y', 0))
        width = float(extent.attrib.get('cx', 0))
        height = float(extent.attrib.get('cy', 0))
    except (TypeError, ValueError):
        return ([], False)
    return _pptx_bbox_from_parent_coordinates(x=x, y=y, width=width, height=height, parent_transform=parent_transform, slide_width=slide_width, slide_height=slide_height)

def _pptx_bbox_from_parent_coordinates(*, x: float, y: float, width: float, height: float, parent_transform: tuple[float, float, float, float], slide_width: float, slide_height: float) -> tuple[list[float], bool]:
    if width <= 0 or height <= 0 or slide_width <= 0 or (slide_height <= 0):
        return ([], False)
    scale_x, scale_y, translate_x, translate_y = parent_transform
    left = translate_x + x * scale_x
    top = translate_y + y * scale_y
    right = translate_x + (x + width) * scale_x
    bottom = translate_y + (y + height) * scale_y
    normalized = [round(max(0.0, min(1.0, left / slide_width)), 6), round(max(0.0, min(1.0, top / slide_height)), 6), round(max(0.0, min(1.0, right / slide_width)), 6), round(max(0.0, min(1.0, bottom / slide_height)), 6)]
    return (normalized, normalized[2] > normalized[0] and normalized[3] > normalized[1])

def _compose_axis_transform(parent: tuple[float, float, float, float], child: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    parent_scale_x, parent_scale_y, parent_translate_x, parent_translate_y = parent
    child_scale_x, child_scale_y, child_translate_x, child_translate_y = child
    return (parent_scale_x * child_scale_x, parent_scale_y * child_scale_y, parent_translate_x + child_translate_x * parent_scale_x, parent_translate_y + child_translate_y * parent_scale_y)

def _ooxml_transform_is_rotated_or_flipped(transform: Any) -> bool:
    try:
        rotation = float(transform.attrib.get('rot', 0) or 0)
    except (TypeError, ValueError):
        return True
    flip_horizontal = str(transform.attrib.get('flipH') or '').lower() in {'1', 'true', 'on'}
    flip_vertical = str(transform.attrib.get('flipV') or '').lower() in {'1', 'true', 'on'}
    return rotation != 0 or flip_horizontal or flip_vertical

def _shape_caption(node: Any) -> str:
    properties = next((child for child in node.iter() if child.tag.endswith('}cNvPr') or child.tag.endswith('}docPr')), None)
    if properties is None:
        return ''
    return str(properties.attrib.get('descr') or properties.attrib.get('title') or properties.attrib.get('name') or '').strip()[:500]

def _xlsx_shared_strings(archive: SafeSourceArchive) -> list[str]:
    if 'xl/sharedStrings.xml' not in archive.namelist():
        return []
    root = parse_untrusted_xml(archive.read('xl/sharedStrings.xml'))
    return [_node_text(node) for node in root.iter() if node.tag.endswith('}si')]

def _xlsx_sheets(archive: SafeSourceArchive) -> list[tuple[str, str]]:
    fallback_paths = sorted((name for name in archive.namelist() if re.fullmatch('xl/worksheets/sheet\\d+\\.xml', name)), key=lambda name: int(re.search('(\\d+)', Path(name).stem).group(1)))
    if 'xl/workbook.xml' not in archive.namelist():
        return [(f'Sheet {index}', path) for index, path in enumerate(fallback_paths, start=1)]
    relationships = _relationships(archive, 'xl/workbook.xml')
    root = parse_untrusted_xml(archive.read('xl/workbook.xml'))
    sheets: list[tuple[str, str]] = []
    for index, node in enumerate((item for item in root.iter() if item.tag.endswith('}sheet')), start=1):
        relationship_id = _relationship_attribute(node, 'id')
        path = relationships.get(relationship_id, '')
        if path:
            sheets.append((str(node.attrib.get('name') or f'Sheet {index}'), path))
    return sheets or [(f'Sheet {index}', path) for index, path in enumerate(fallback_paths, start=1)]

def _xlsx_sheet_is_forced_to_single_page(root: Any) -> bool:
    setup_properties = next((node for node in root.iter() if node.tag.endswith('}pageSetUpPr')), None)
    page_setup = next((node for node in root.iter() if node.tag.endswith('}pageSetup')), None)
    if setup_properties is None or page_setup is None:
        return False
    fit_enabled = str(setup_properties.attrib.get('fitToPage') or '').lower() in {'1', 'true', 'on'}
    try:
        fit_width = int(page_setup.attrib.get('fitToWidth', 0) or 0)
        fit_height = int(page_setup.attrib.get('fitToHeight', 0) or 0)
    except (TypeError, ValueError):
        return False
    return fit_enabled and fit_width == 1 and (fit_height == 1)

def _xlsx_cells(root: Any, shared_strings: list[str]) -> tuple[dict[tuple[int, int], str], int, int]:
    cells: dict[tuple[int, int], str] = {}
    max_column = 0
    max_row = 0
    for fallback_row, row in enumerate((node for node in root.iter() if node.tag.endswith('}row')), start=1):
        try:
            row_index = int(row.attrib.get('r') or fallback_row)
        except (TypeError, ValueError):
            row_index = fallback_row
        for fallback_column, cell in enumerate((node for node in row if node.tag.endswith('}c')), start=1):
            reference = str(cell.attrib.get('r') or '')
            column = _column_number(reference) or fallback_column
            cell_type = str(cell.attrib.get('t') or '')
            if cell_type == 'inlineStr':
                value = _node_text(cell)
            else:
                value_node = next((node for node in cell.iter() if node.tag.endswith('}v')), None)
                value = str(value_node.text or '') if value_node is not None else ''
                if cell_type == 's' and value.isdigit() and (int(value) < len(shared_strings)):
                    value = shared_strings[int(value)]
            cells[row_index, column] = value.strip()
            max_column = max(max_column, column)
            max_row = max(max_row, row_index)
    return (cells, max(1, max_column), max(1, max_row))

def _xlsx_native_tables(archive: SafeSourceArchive, *, sheet_root: Any, relationships: dict[str, str]) -> list[tuple[str, str, tuple[int, int, int, int]]]:
    tables: list[tuple[str, str, tuple[int, int, int, int]]] = []
    for node in (item for item in sheet_root.iter() if item.tag.endswith('}tablePart')):
        target = relationships.get(_relationship_attribute(node, 'id'), '')
        content = _safe_archive_read(archive, target)
        if not content:
            continue
        table_root = parse_untrusted_xml(content)
        reference = str(table_root.attrib.get('ref') or '').strip()
        bounds = _xlsx_range_bounds(reference)
        if bounds is None:
            continue
        caption = str(table_root.attrib.get('displayName') or table_root.attrib.get('name') or '').strip()[:500]
        tables.append((caption, reference, bounds))
    return tables

def _xlsx_range_bounds(reference: str) -> tuple[int, int, int, int] | None:
    parts = reference.replace('$', '').split(':', 1)
    if len(parts) == 1:
        parts.append(parts[0])
    parsed: list[tuple[int, int]] = []
    for part in parts:
        match = re.fullmatch('([A-Za-z]+)([1-9]\\d*)', part.strip())
        if not match:
            return None
        parsed.append((_column_number(match.group(1)), int(match.group(2))))
    start_column, start_row = parsed[0]
    end_column, end_row = parsed[1]
    if start_column <= 0 or start_row <= 0 or end_column < start_column or (end_row < start_row):
        return None
    cell_count = (end_column - start_column + 1) * (end_row - start_row + 1)
    if end_column > 16384 or end_row > 1048576 or cell_count > 100000:
        return None
    return (start_column, start_row, end_column, end_row)

def _xlsx_range_data(cells: dict[tuple[int, int], str], bounds: tuple[int, int, int, int]) -> list[list[str]]:
    start_column, start_row, end_column, end_row = bounds
    return [[cells.get((row, column), '') for column in range(start_column, end_column + 1)] for row in range(start_row, end_row + 1)]

def _spreadsheet_range_bbox(bounds: tuple[int, int, int, int], *, max_column: int, max_row: int) -> list[float]:
    start_column, start_row, end_column, end_row = bounds
    width = max(1, max_column)
    height = max(1, max_row)
    return [round((start_column - 1) / width, 6), round((start_row - 1) / height, 6), round(min(1.0, end_column / width), 6), round(min(1.0, end_row / height), 6)]

def _column_number(reference: str) -> int:
    match = re.match('([A-Za-z]+)', reference)
    if not match:
        return 0
    value = 0
    for character in match.group(1).upper():
        value = value * 26 + ord(character) - ord('A') + 1
    return value

def _spreadsheet_anchor_bbox(anchor: Any, *, max_column: int, max_row: int) -> list[float]:
    points: list[tuple[int, int]] = []
    for marker in (node for node in anchor if node.tag.endswith('}from') or node.tag.endswith('}to')):
        column_node = next((node for node in marker if node.tag.endswith('}col')), None)
        row_node = next((node for node in marker if node.tag.endswith('}row')), None)
        try:
            points.append((int(column_node.text or 0), int(row_node.text or 0)))
        except (AttributeError, TypeError, ValueError):
            continue
    if not points:
        return []
    start = points[0]
    end = points[-1] if len(points) > 1 else (start[0] + 1, start[1] + 1)
    width = max(1, max(max_column, end[0] + 1))
    height = max(1, max(max_row, end[1] + 1))
    return [round(start[0] / width, 6), round(start[1] / height, 6), round(min(1.0, (end[0] + 1) / width), 6), round(min(1.0, (end[1] + 1) / height), 6)]

def _image_mime(name: str, content: bytes) -> str:
    guessed = mimetypes.guess_type(name)[0] or ''
    if guessed in {'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/svg+xml', 'image/tiff', 'image/bmp'}:
        return guessed
    if content.startswith(b'\x89PNG'):
        return 'image/png'
    if content.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if content.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if content.startswith(b'RIFF') and content[8:12] == b'WEBP':
        return 'image/webp'
    if b'<svg' in content[:1024].lower():
        return 'image/svg+xml'
    return 'application/octet-stream'
