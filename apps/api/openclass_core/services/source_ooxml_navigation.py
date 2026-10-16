from __future__ import annotations
import posixpath
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote
from openclass_core.services.source_archive import SafeSourceArchive
from openclass_core.services.source_xml import parse_untrusted_xml
if TYPE_CHECKING:
    from docx.document import Document as DocumentObject

class OoxmlNavigationError(ValueError):
    """Raised when an OOXML package cannot provide an authoritative sequence."""

@dataclass(frozen=True)
class DocxParagraphBlock:
    index: int
    text: str
    style_name: str

def read_docx_paragraph_blocks(path: Path, *, include_text: bool | Callable[[str], bool]=True) -> list[DocxParagraphBlock]:
    """Return all Word paragraphs in document XML order, including table cells."""
    try:
        from docx import Document
        from docx.oxml.ns import qn
        from docx.text.paragraph import Paragraph
    except Exception as exc:
        raise OoxmlNavigationError('DOCX navigation dependency is unavailable.') from exc
    with SafeSourceArchive(path):
        pass
    document: DocumentObject = Document(str(path))
    blocks: list[DocxParagraphBlock] = []
    for paragraph_element in document.element.body.iter(qn('w:p')):
        paragraph = Paragraph(paragraph_element, document)
        style_name = (paragraph.style.name if paragraph.style is not None else '') or ''
        should_include_text = include_text(style_name) if callable(include_text) else include_text
        blocks.append(DocxParagraphBlock(index=len(blocks), text=str(paragraph.text or '') if should_include_text else '', style_name=style_name))
    return blocks

def ordered_pptx_slide_parts(archive: SafeSourceArchive, names: set[str] | None=None) -> list[str]:
    """Resolve slide parts from presentation sldId order and relationships."""
    package_names = names if names is not None else set(archive.namelist())
    presentation_name = 'ppt/presentation.xml'
    relationships_name = 'ppt/_rels/presentation.xml.rels'
    if presentation_name not in package_names or relationships_name not in package_names:
        raise OoxmlNavigationError('PPTX presentation order metadata is missing.')
    presentation = parse_untrusted_xml(archive.read(presentation_name))
    relationships = parse_untrusted_xml(archive.read(relationships_name))
    relationship_targets = {str(node.attrib.get('Id') or ''): str(node.attrib.get('Target') or '') for node in relationships.iter() if _local_name(node.tag) == 'Relationship' and str(node.attrib.get('TargetMode') or '').lower() != 'external'}
    slide_parts: list[str] = []
    for node in presentation.iter():
        if _local_name(node.tag) != 'sldId':
            continue
        relationship_id = next((str(value) for key, value in node.attrib.items() if key == 'r:id' or (_local_name(key) == 'id' and 'relationships' in str(key).lower())), '')
        target = relationship_targets.get(relationship_id, '')
        if not relationship_id or not target:
            raise OoxmlNavigationError('PPTX slide order contains an unresolved relationship.')
        slide_part = _normalize_pptx_target(target)
        if slide_part not in package_names:
            raise OoxmlNavigationError('PPTX slide order points to a missing slide part.')
        if slide_part in slide_parts:
            raise OoxmlNavigationError('PPTX slide order contains a duplicate slide part.')
        slide_parts.append(slide_part)
    return slide_parts

def _normalize_pptx_target(target: str) -> str:
    normalized = unquote(target).replace('\\', '/')
    if normalized.startswith('/'):
        normalized = normalized.lstrip('/')
    elif not normalized.startswith('ppt/'):
        normalized = posixpath.normpath(posixpath.join('ppt', normalized))
    else:
        normalized = posixpath.normpath(normalized)
    if not normalized.startswith('ppt/slides/') or not normalized.lower().endswith('.xml') or '..' in normalized.split('/'):
        raise OoxmlNavigationError('PPTX slide relationship has an invalid target.')
    return normalized

def _local_name(tag: object) -> str:
    return str(tag).split('}')[-1]
