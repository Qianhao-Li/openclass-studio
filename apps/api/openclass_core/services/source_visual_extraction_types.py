from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal
from openclass_core.models import SourceVisualKind, SourceVisualIndexStatus

@dataclass
class RawSourceVisual:
    kind: SourceVisualKind
    source_locator: str
    native_order: int
    content: bytes = b''
    mime_type: str = ''
    page_no: int | None = None
    paragraph_index: int | None = None
    slide_no: int | None = None
    sheet_name: str = ''
    bbox: list[float] = field(default_factory=list)
    text_offset: int | None = None
    caption: str = ''
    ocr_text: str = ''
    table_data: list[list[str]] = field(default_factory=list)
    width: int | None = None
    height: int | None = None
    confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class SourceVisualAdapterResult:
    visuals: list[RawSourceVisual] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status: SourceVisualIndexStatus = 'ready'
    native_chart_count: int = 0
    native_chart_anchors: list[RawSourceVisual] = field(default_factory=list)
VisualAdapterName = Literal['pdf', 'office', 'markup', 'image', 'none']
