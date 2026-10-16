from __future__ import annotations
import json
import re
from dataclasses import dataclass
from pathlib import Path
from openclass_core.models import AgentActivityEvent, now_iso
PROGRESS_PREFIX = 'OPENCLASS_PROGRESS '
_PHASE_BANDS: dict[str, tuple[int, int, str, str]] = {'scan_pages': (30, 55, 'source_codex_scanning_pages', 'Scanning file pages'), 'map_nodes': (55, 75, 'source_codex_mapping_nodes', 'Mapping catalog entries'), 'verify_ranges': (75, 88, 'source_codex_verifying_ranges', 'Verifying chapter ranges'), 'write_catalog': (88, 90, 'source_codex_writing_catalog', 'Writing the complete catalog')}
_JOB_PHASE_RANK = {'source_codex_investigation': 0, 'source_codex_scanning_pages': 1, 'source_codex_mapping_nodes': 2, 'source_codex_verifying_ranges': 3, 'source_codex_writing_catalog': 4}
_LIVE_PHASE_TO_JOB_PHASE = {'directory_discovery': 'source_codex_investigation', 'page_calibration': 'source_codex_mapping_nodes', 'range_mapping': 'source_codex_verifying_ranges', 'validation': 'source_codex_writing_catalog', 'terminal': 'source_codex_writing_catalog', 'background_catalog_refine': 'source_codex_verifying_ranges'}
_UNIT_LABELS = {'pages': 'pages', 'nodes': 'catalog entries', 'ranges': 'chapter ranges', 'spine_items': 'EPUB entries', 'sections': 'sections', 'checks': 'checks', 'artifacts': 'catalog files'}

@dataclass(frozen=True)
class SourceCodexProgressObservation:
    event: AgentActivityEvent
    progress: int
    phase: str

class SourceCodexProgressTracker:
    """Convert auditable Source Codex work into durable, monotonic progress."""

    def __init__(self, source_path: Path) -> None:
        self.source_path = source_path
        self.total_pages = _pdf_page_count(source_path) if source_path.suffix.lower() == '.pdf' else 0
        self.scanned_pages: set[int] = set()
        self.rendered_pages: set[int] = set()
        self.completed_tool_actions = 0
        self.progress = 30
        self.phase = 'source_codex_investigation'
        self.label = 'Source agent is reading the file structure'
        self.visual_page_input = False

    def observe(self, event: AgentActivityEvent) -> SourceCodexProgressObservation:
        if event.metadata.get('kind') == 'pdf_page_images':
            self.visual_page_input = True
            arguments = event.metadata.get('arguments') or {}
            pages = arguments.get('pages', []) if isinstance(arguments, dict) else []
            rendered = {item['physical_page'] for item in pages if isinstance(item, dict) and type(item.get('physical_page')) is int and (1 <= item['physical_page'] <= self.total_pages)}
            self.rendered_pages.update(rendered)
            if event.status == 'completed':
                confirmed = event.metadata.get('inspected_pages', [])
                self.scanned_pages.update((page for page in confirmed if type(page) is int and page in self.rendered_pages))
        if self.visual_page_input:
            self.phase = 'source_codex_scanning_pages'
            self.progress = max(self.progress, min(85, 30 + round(55 * len(self.scanned_pages) / max(1, self.total_pages))))
            self.label = f'AI is reading page images: {len(self.scanned_pages)}/{self.total_pages} pages confirmed'
            source_progress = {'phase': self.phase, 'label': self.label, 'detail': f'{len(self.rendered_pages)} original page images · {len(self.scanned_pages)} pages confirmed by AI · No OCR used', 'progress': self.progress, 'total_pages': self.total_pages, 'pages_scanned': len(self.scanned_pages), 'pages_rendered': len(self.rendered_pages), 'completed_tool_actions': 0, 'heartbeat_at': now_iso()}
            return SourceCodexProgressObservation(event=event.model_copy(update={'metadata': {**event.metadata, 'source_progress': source_progress}}), progress=self.progress, phase=self.phase)
        if event.status == 'completed' and str(event.metadata.get('kind') or '') in {'commandExecution', 'imageView', 'mcpToolCall', 'dynamicToolCall'}:
            self.completed_tool_actions += 1
        command = str(event.metadata.get('command') or '')
        if event.status == 'completed' and str(event.metadata.get('kind') or '') == 'commandExecution' and _command_succeeded(event):
            scanned, rendered = _pages_from_command(command, total_pages=self.total_pages)
            self.scanned_pages.update(scanned)
            self.rendered_pages.update(rendered)
        live_progress = event.metadata.get('source_progress')
        if str(event.metadata.get('kind') or '') in {'sourceCatalogProgress', 'sourceCatalogStage', 'sourceCatalogPublished'} and isinstance(live_progress, dict):
            return self._observe_live_heartbeat(event, live_progress)
        snapshot = _structured_progress(event)
        structured_detail = ''
        if snapshot is not None:
            phase_name, completed, total, unit, structured_detail = snapshot
            band_start, band_end, job_phase, default_label = _PHASE_BANDS[phase_name]
            candidate = band_start + round((band_end - band_start) * completed / total)
            if _JOB_PHASE_RANK[job_phase] >= _JOB_PHASE_RANK[self.phase]:
                self.progress = max(self.progress, min(band_end, candidate))
                self.phase = job_phase
                unit_label = _UNIT_LABELS.get(unit, unit)
                self.label = f'{default_label}: {completed}/{total} {unit_label}'
            else:
                structured_detail = ''
        else:
            self._apply_mechanical_progress(command=command, event=event)
            self.label = self._mechanical_label()
        detail_parts: list[str] = []
        if structured_detail:
            detail_parts.append(structured_detail)
        if self.total_pages and self.scanned_pages:
            detail_parts.append(f'Scanned {len(self.scanned_pages)}/{self.total_pages} pages')
        elif self.total_pages:
            detail_parts.append(f'{self.total_pages} pages in the file')
        if self.rendered_pages:
            detail_parts.append(f'Rendered {len(self.rendered_pages)} pages for review')
        if self.completed_tool_actions and (not any(('tool checks' in part or '工具检查' in part for part in detail_parts))):
            detail_parts.append(f'Completed {self.completed_tool_actions} tool checks')
        source_progress = {'phase': self.phase, 'label': self.label, 'detail': ' · '.join(dict.fromkeys(detail_parts)), 'progress': self.progress, 'pages_scanned': len(self.scanned_pages), 'total_pages': self.total_pages, 'pages_rendered': len(self.rendered_pages), 'completed_tool_actions': self.completed_tool_actions}
        decorated = event.model_copy(update={'metadata': {**event.metadata, 'source_progress': source_progress}})
        return SourceCodexProgressObservation(event=decorated, progress=self.progress, phase=self.phase)

    def _observe_live_heartbeat(self, event: AgentActivityEvent, live_progress: dict[str, object]) -> SourceCodexProgressObservation:
        label = str(live_progress.get('label') or '').strip()
        if label:
            self.label = label
        live_phase = _LIVE_PHASE_TO_JOB_PHASE.get(str(live_progress.get('phase') or ''), self.phase)
        if _JOB_PHASE_RANK.get(live_phase, 0) >= _JOB_PHASE_RANK.get(self.phase, 0):
            self.phase = live_phase
        completed = live_progress.get('completed')
        total = live_progress.get('total')
        determinate = bool(live_progress.get('determinate'))
        if determinate and isinstance(completed, int) and (not isinstance(completed, bool)) and isinstance(total, int) and (not isinstance(total, bool)) and (total > 0):
            candidate = 55 + round(33 * max(0, min(completed, total)) / total)
            self.progress = max(self.progress, min(88, candidate))
        detail_parts = [str(live_progress.get('detail') or '').strip()]
        if self.total_pages and self.scanned_pages:
            detail_parts.append(f'Scanned {len(self.scanned_pages)}/{self.total_pages} pages')
        elif self.total_pages:
            detail_parts.append(f'{self.total_pages} pages in the file')
        if self.rendered_pages:
            detail_parts.append(f'Rendered {len(self.rendered_pages)} pages for review')
        if self.completed_tool_actions and (not any(('tool checks' in part or '工具检查' in part for part in detail_parts))):
            detail_parts.append(f'Completed {self.completed_tool_actions} tool checks')
        source_progress = {**live_progress, 'phase': self.phase, 'label': self.label, 'detail': ' · '.join(dict.fromkeys((part for part in detail_parts if part))), 'progress': self.progress, 'determinate': determinate, 'pages_scanned': len(self.scanned_pages), 'total_pages': self.total_pages, 'pages_rendered': len(self.rendered_pages), 'completed_tool_actions': self.completed_tool_actions}
        decorated = event.model_copy(update={'metadata': {**event.metadata, 'source_progress': source_progress}})
        return SourceCodexProgressObservation(event=decorated, progress=self.progress, phase=self.phase)

    def _apply_mechanical_progress(self, *, command: str, event: AgentActivityEvent) -> None:
        if self.total_pages and self.scanned_pages:
            candidate = 30 + round(25 * len(self.scanned_pages) / self.total_pages)
            self.progress = max(self.progress, min(55, candidate))
            if _JOB_PHASE_RANK[self.phase] <= _JOB_PHASE_RANK['source_codex_scanning_pages']:
                self.phase = 'source_codex_scanning_pages'
        if event.status == 'completed' and str(event.metadata.get('kind') or '') == 'commandExecution' and ('catalog.json' in command) and _command_succeeded(event):
            self.progress = max(self.progress, 89)
            self.phase = 'source_codex_writing_catalog'

    def _mechanical_label(self) -> str:
        if self.phase == 'source_codex_writing_catalog':
            return 'Source agent wrote the catalog and is checking it'
        if self.phase in {'source_codex_mapping_nodes', 'source_codex_verifying_ranges'}:
            return self.label
        if self.total_pages and self.scanned_pages:
            return f'Scanning and checking PDF: {len(self.scanned_pages)}/{self.total_pages} pages'
        if self.completed_tool_actions:
            return f'Source agent is inspecting the file: {self.completed_tool_actions} tool checks completed'
        return 'Source agent is reading the file structure'

def _pdf_page_count(path: Path) -> int:
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(path)).pages)
    except Exception:
        return 0

def _command_succeeded(event: AgentActivityEvent) -> bool:
    exit_code = event.metadata.get('exit_code')
    return exit_code in (None, 0)

def _structured_progress(event: AgentActivityEvent) -> tuple[str, int, int, str, str] | None:
    kind = str(event.metadata.get('kind') or '')
    command = str(event.metadata.get('command') or '')
    is_commentary = kind == 'commentary'
    is_explicit_progress_command = kind == 'commandExecution' and event.status == 'completed' and _command_succeeded(event) and (PROGRESS_PREFIX in command)
    if not is_commentary and (not is_explicit_progress_command):
        return None
    detail = str(event.metadata.get('detail') or '')
    marker_index = detail.rfind(PROGRESS_PREFIX)
    if marker_index < 0:
        return None
    payload_text = detail[marker_index + len(PROGRESS_PREFIX):].lstrip()
    try:
        payload, _end = json.JSONDecoder().raw_decode(payload_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    phase = str(payload.get('phase') or '')
    if phase not in _PHASE_BANDS:
        return None
    completed = payload.get('completed')
    total = payload.get('total')
    if not isinstance(completed, int) or isinstance(completed, bool) or (not isinstance(total, int)) or isinstance(total, bool) or (total <= 0) or (completed < 0):
        return None
    completed = min(completed, total)
    unit = str(payload.get('unit') or 'checks').strip() or 'checks'
    message = str(payload.get('detail') or '').strip()
    return (phase, completed, total, unit, message)

def _pages_from_command(command: str, *, total_pages: int) -> tuple[set[int], set[int]]:
    if total_pages <= 0 or not command:
        return (set(), set())
    lowered = command.lower()
    uses_text = 'pdftotext' in lowered
    uses_render = 'pdftoppm' in lowered
    if not uses_text and (not uses_render):
        return (set(), set())
    pages: set[int] = set()
    range_matches = re.findall('(?:^|\\s)-f\\s+(\\d+)\\s+-l\\s+(\\d+)(?:\\s|$)', command)
    for start_text, end_text in range_matches:
        start = max(1, int(start_text))
        end = min(total_pages, int(end_text))
        if start <= end:
            pages.update(range(start, end + 1))
    for values in re.findall('for\\s+\\w+\\s+in\\s+([0-9 ]+);', command):
        pages.update((page for token in values.split() if token.isdigit() and 1 <= (page := int(token)) <= total_pages))
    for start_text, end_text in re.findall('seq\\s+(\\d+)\\s+(\\d+)', command):
        start = max(1, int(start_text))
        end = min(total_pages, int(end_text))
        if start <= end:
            pages.update(range(start, end + 1))
    if uses_text and (not pages) and ('source.pdf' in lowered):
        pages.update(range(1, total_pages + 1))
    return (pages, set(pages) if uses_render else set())
