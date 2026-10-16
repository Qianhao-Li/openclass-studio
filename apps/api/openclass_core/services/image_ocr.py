from __future__ import annotations
import csv
import io
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from functools import cmp_to_key
from pathlib import Path
from typing import Any
VISION_OCR_SCRIPT = Path(__file__).with_name('vision_ocr.swift')
LOCAL_OCR_ENABLED_ENV = 'OPENCLASS_LOCAL_OCR_ENABLED'

def local_ocr_enabled() -> bool:
    """Whether this runtime is explicitly allowed to recognize text locally.

    Local OCR is disabled by default. OpenClass source ingestion may still
    render or resize pages, but text recognition belongs to the selected cloud
    vision model unless an operator deliberately enables this legacy fallback.
    """
    return os.getenv(LOCAL_OCR_ENABLED_ENV, '').strip().lower() in {'1', 'true', 'yes', 'on'}

@dataclass(frozen=True)
class OCRLineLayout:
    text: str
    x: float
    y: float
    width: float = 0.0
    height: float = 0.0

@dataclass(frozen=True)
class OCRPageLayout:
    page_no: int
    lines: list[OCRLineLayout] = field(default_factory=list)

def ordered_ocr_lines(lines: list[OCRLineLayout]) -> list[OCRLineLayout]:
    """Return OCR lines in deterministic human reading order.

    The Vision adapter may append a right-side recognition pass after the main
    pass, so payload order is not a reliable reading order. This mirrors the
    native adapter's column-aware ordering while retaining every line's layout.
    """

    def compare_left_to_right(left: OCRLineLayout, right: OCRLineLayout) -> int:
        if abs(left.x - right.x) > 0.001:
            return -1 if left.x < right.x else 1
        if left.y == right.y:
            return 0
        return -1 if left.y > right.y else 1
    x_sorted = sorted(lines, key=cmp_to_key(compare_left_to_right))
    largest_gap = 0.0
    split_x: float | None = None
    if len(x_sorted) >= 10:
        for previous, current in zip(x_sorted, x_sorted[1:]):
            gap = current.x - previous.x
            if gap > largest_gap:
                largest_gap = gap
                split_x = (current.x + previous.x) / 2

    def top_to_bottom(column: list[OCRLineLayout]) -> list[OCRLineLayout]:

        def compare(left: OCRLineLayout, right: OCRLineLayout) -> int:
            if abs(left.y - right.y) > 0.012:
                return -1 if left.y > right.y else 1
            if left.x == right.x:
                return 0
            return -1 if left.x < right.x else 1
        return sorted(column, key=cmp_to_key(compare))
    if split_x is not None and largest_gap >= 0.18:
        left_column = [line for line in lines if line.x <= split_x]
        right_column = [line for line in lines if line.x > split_x]
        if len(left_column) >= 4 and len(right_column) >= 4:
            return [*top_to_bottom(left_column), *top_to_bottom(right_column)]
    return top_to_bottom(lines)

def _run_vision_ocr_payload(args: list[str], *, timeout: int) -> dict[str, Any] | None:
    if not VISION_OCR_SCRIPT.exists():
        return None
    try:
        result = subprocess.run(['swift', str(VISION_OCR_SCRIPT), *args], capture_output=True, text=True, check=True, timeout=timeout)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    payload = result.stdout.strip()
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None

def _run_vision_ocr(args: list[str], *, timeout: int) -> str | None:
    parsed = _run_vision_ocr_payload(args, timeout=timeout)
    if not parsed:
        return None
    text = str(parsed.get('text') or '').strip()
    return text or None

def _tesseract_languages() -> str:
    return os.getenv('OPENCLASS_TESSERACT_LANGUAGES', 'eng').strip() or 'eng'

def _tesseract_binary() -> str | None:
    configured = os.getenv('OPENCLASS_TESSERACT_BINARY', 'tesseract').strip()
    return shutil.which(configured) if configured else None

def _run_tesseract_text(file_path: Path, *, timeout: int) -> str | None:
    binary = _tesseract_binary()
    if binary is None:
        return None
    try:
        result = subprocess.run([binary, str(file_path), 'stdout', '-l', _tesseract_languages(), '--psm', '6'], capture_output=True, text=True, check=True, timeout=timeout)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    text = result.stdout.strip()
    return text or None

def _parse_tesseract_tsv(payload: str, *, page_no: int) -> OCRPageLayout:
    grouped_words: dict[tuple[int, int, int], list[tuple[int, str, int, int, int, int]]] = {}
    reader = csv.DictReader(io.StringIO(payload), delimiter='\t')
    image_width = 0
    image_height = 0
    for row in reader:
        try:
            level = int(row.get('level') or 0)
            width = int(row.get('width') or 0)
            height = int(row.get('height') or 0)
            image_width = max(image_width, width if level == 1 else 0)
            image_height = max(image_height, height if level == 1 else 0)
            if level != 5:
                continue
            text = str(row.get('text') or '').strip()
            confidence = float(row.get('conf') or -1)
            if not text or confidence < 0:
                continue
            left = int(row.get('left') or 0)
            top = int(row.get('top') or 0)
            key = (int(row.get('block_num') or 0), int(row.get('par_num') or 0), int(row.get('line_num') or 0))
        except (TypeError, ValueError):
            continue
        grouped_words.setdefault(key, []).append((left, text, top, width, height, int(confidence)))
    if image_width < 1 or image_height < 1:
        return OCRPageLayout(page_no=page_no)
    lines: list[OCRLineLayout] = []
    for words in grouped_words.values():
        ordered = sorted(words, key=lambda word: word[0])
        segments: list[list[tuple[int, str, int, int, int, int]]] = []
        for word in ordered:
            previous_right = max((existing[0] + existing[3] for existing in segments[-1])) if segments else None
            if previous_right is None or word[0] - previous_right > image_width * 0.035:
                segments.append([word])
            else:
                segments[-1].append(word)
        for segment in segments:
            left = min((word[0] for word in segment))
            top = min((word[2] for word in segment))
            right = max((word[0] + word[3] for word in segment))
            bottom = max((word[2] + word[4] for word in segment))
            text = ' '.join((word[1] for word in segment)).strip()
            if not text:
                continue
            lines.append(OCRLineLayout(text=text, x=left / image_width, y=1.0 - top / image_height, width=max(0.0, (right - left) / image_width), height=max(0.0, (bottom - top) / image_height)))
    return OCRPageLayout(page_no=page_no, lines=ordered_ocr_lines(lines))

def _extract_pdf_pages_layout_with_tesseract(file_path: Path, *, page_start: int, page_end: int, max_pages: int) -> list[OCRPageLayout]:
    binary = _tesseract_binary()
    if binary is None:
        return []
    try:
        from openclass_core.services import pdf_backend as fitz
    except Exception:
        return []
    start = max(page_start, 1)
    end = max(page_end, start)
    final_page = min(end, start + max(1, max_pages) - 1)
    layouts: list[OCRPageLayout] = []
    try:
        document = fitz.open(str(file_path))
    except Exception:
        return []
    try:
        final_page = min(final_page, document.page_count)
        with tempfile.TemporaryDirectory(prefix='openclass-tesseract-pdf-') as temp_dir:
            temp_root = Path(temp_dir)
            for page_no in range(start, final_page + 1):
                page = document.load_page(page_no - 1)
                image_path = temp_root / f'page-{page_no}.png'
                page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False).save(str(image_path))
                try:
                    result = subprocess.run([binary, str(image_path), 'stdout', '-l', _tesseract_languages(), '--psm', '3', 'tsv'], capture_output=True, text=True, check=True, timeout=90)
                except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    layouts.append(OCRPageLayout(page_no=page_no))
                    continue
                layouts.append(_parse_tesseract_tsv(result.stdout, page_no=page_no))
    finally:
        document.close()
    return layouts

def extract_image_text(file_path: Path) -> str | None:
    if not local_ocr_enabled() or not file_path.exists():
        return None
    if VISION_OCR_SCRIPT.exists():
        text = _run_vision_ocr([str(file_path)], timeout=90)
        if text:
            return text
    return _run_tesseract_text(file_path, timeout=90)

def extract_pdf_pages_text(file_path: Path, *, page_start: int, page_end: int, max_pages: int=4) -> str | None:
    if not local_ocr_enabled() or not file_path.exists():
        return None
    start = max(page_start, 1)
    end = max(page_end, start)
    pages = max(1, min(max_pages, end - start + 1))
    timeout = max(120, pages * 60)
    if VISION_OCR_SCRIPT.exists():
        text = _run_vision_ocr([str(file_path), str(start), str(end), str(pages)], timeout=timeout)
        if text:
            return text
    layouts = _extract_pdf_pages_layout_with_tesseract(file_path, page_start=start, page_end=end, max_pages=pages)
    text = '\n'.join((line.text for page in layouts for line in ordered_ocr_lines(page.lines))).strip()
    return text or None

def extract_pdf_pages_layout(file_path: Path, *, page_start: int, page_end: int, max_pages: int=12, trailing_column_pass: bool=False) -> list[OCRPageLayout]:
    if not local_ocr_enabled() or not file_path.exists():
        return []
    start = max(page_start, 1)
    end = max(page_end, start)
    pages = max(1, min(max_pages, end - start + 1))
    timeout = max(120, pages * 60)
    args = [str(file_path), str(start), str(end), str(pages)]
    if trailing_column_pass:
        args.append('trailing-column-lines')
    payload = _run_vision_ocr_payload(args, timeout=timeout) if VISION_OCR_SCRIPT.exists() else None
    if not payload:
        return _extract_pdf_pages_layout_with_tesseract(file_path, page_start=start, page_end=end, max_pages=pages)
    page_layouts: list[OCRPageLayout] = []
    raw_pages = payload.get('pages')
    if not isinstance(raw_pages, list):
        return []
    for raw_page in raw_pages:
        if not isinstance(raw_page, dict):
            continue
        try:
            page_no = int(raw_page.get('pageNumber') or 0)
        except (TypeError, ValueError):
            continue
        lines: list[OCRLineLayout] = []
        raw_lines = raw_page.get('lines')
        if isinstance(raw_lines, list):
            for raw_line in raw_lines:
                if not isinstance(raw_line, dict):
                    continue
                text = str(raw_line.get('text') or '').strip()
                if not text:
                    continue
                try:
                    lines.append(OCRLineLayout(text=text, x=float(raw_line.get('x') or 0.0), y=float(raw_line.get('y') or 0.0), width=float(raw_line.get('width') or 0.0), height=float(raw_line.get('height') or 0.0)))
                except (TypeError, ValueError):
                    continue
        page_layouts.append(OCRPageLayout(page_no=page_no, lines=lines))
    return page_layouts
