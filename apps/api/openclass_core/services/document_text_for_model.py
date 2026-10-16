from __future__ import annotations
import re
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from openclass_core.services.source_document_toolchain import SourceDocumentToolchainError, resolve_poppler_root
DEFAULT_MAX_DOCUMENT_CHARS = 120000
_PDF_FRONT_PAGES = 40
_PDF_NATIVE_TEXT_TIMEOUT_SECONDS = 45.0

class DocumentTextExtractionError(RuntimeError):
    pass

def extract_document_text_for_model(path: Path, *, max_chars: int=DEFAULT_MAX_DOCUMENT_CHARS) -> str:
    """Turn a source file into text a chat-only model can read.

    Providers without document attachments (notably DeepSeek's public API) receive
    this extracted text in the prompt instead of a binary file part.
    """
    if not path.is_file():
        raise DocumentTextExtractionError('The document input is unavailable.')
    suffix = path.suffix.lower()
    if suffix == '.pdf':
        text = _extract_pdf(path)
    elif suffix == '.epub':
        text = _extract_epub(path)
    elif suffix == '.docx':
        text = _extract_docx(path)
    elif suffix == '.pptx':
        text = _extract_pptx(path)
    elif suffix in {'.txt', '.md', '.markdown', '.csv', '.json', '.xml', '.html', '.htm'}:
        text = _read_text_file(path)
    else:
        text = _read_text_file(path, allow_binary_fallback=True)
    normalized = _normalize_whitespace(text)
    if not normalized:
        raise DocumentTextExtractionError('No readable text could be extracted from the uploaded file for this model.')
    if len(normalized) <= max_chars:
        return normalized
    head = max_chars * 3 // 4
    tail = max_chars - head - 80
    return f'{normalized[:head]}\n\n...[truncated {len(normalized) - max_chars} characters]...\n\n{normalized[-tail:]}'

def _extract_pdf(path: Path) -> str:
    sections: list[str] = [f'SOURCE_FILE\t{path.name}', 'FORMAT\tpdf']
    outline = _pdf_outline_text(path)
    if outline:
        sections.append('NATIVE_PDF_OUTLINE')
        sections.append(outline)
    page_text = _pdf_page_text(path, page_start=1, page_end=_PDF_FRONT_PAGES)
    if page_text:
        sections.append('PDF_TEXT_PAGES')
        sections.append(page_text)
    return '\n\n'.join(sections)

def _pdf_outline_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        return ''
    try:
        reader = PdfReader(str(path))
        outline = reader.outline
    except Exception:
        return ''
    lines: list[str] = []

    def walk(items, level: int=1) -> None:
        if not items:
            return
        for item in items:
            if isinstance(item, list):
                walk(item, level + 1)
                continue
            title = str(getattr(item, 'title', '') or '').strip()
            if not title:
                continue
            page_no = ''
            try:
                page_index = reader.get_destination_page_number(item)
                if isinstance(page_index, int) and page_index >= 0:
                    page_no = str(page_index + 1)
            except Exception:
                page_no = ''
            prefix = '  ' * max(0, level - 1)
            lines.append(f'{prefix}L{level}\t{title}' + (f'\tp.{page_no}' if page_no else ''))
    try:
        walk(outline, 1)
    except Exception:
        return ''
    return '\n'.join(lines[:2000])

def _pdf_page_text(path: Path, *, page_start: int, page_end: int) -> str:
    try:
        executable = resolve_poppler_root() / 'bin' / 'pdftotext'
        completed = subprocess.run([str(executable), '-f', str(page_start), '-l', str(page_end), '-layout', '-enc', 'UTF-8', str(path), '-'], check=False, capture_output=True, text=True, timeout=_PDF_NATIVE_TEXT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError, SourceDocumentToolchainError):
        return _pdf_page_text_pypdf(path, page_start=page_start, page_end=page_end)
    if completed.returncode != 0 or not completed.stdout.strip():
        return _pdf_page_text_pypdf(path, page_start=page_start, page_end=page_end)
    return completed.stdout

def _pdf_page_text_pypdf(path: Path, *, page_start: int, page_end: int) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        return ''
    try:
        reader = PdfReader(str(path))
    except Exception:
        return ''
    chunks: list[str] = []
    last = min(page_end, len(reader.pages))
    for index in range(max(1, page_start) - 1, last):
        try:
            text = reader.pages[index].extract_text() or ''
        except Exception:
            text = ''
        if text.strip():
            chunks.append(f'[page {index + 1}]\n{text}')
    return '\n\n'.join(chunks)

def _extract_epub(path: Path) -> str:
    parts: list[str] = [f'SOURCE_FILE\t{path.name}', 'FORMAT\tepub']
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            for candidate in ('META-INF/container.xml',):
                if candidate in names:
                    parts.append(archive.read(candidate).decode('utf-8', errors='ignore')[:4000])
            toc_names = [name for name in archive.namelist() if name.lower().endswith(('toc.ncx', 'nav.xhtml', 'nav.html'))]
            for name in toc_names[:3]:
                parts.append(f'EPUB_NAV\t{name}')
                parts.append(archive.read(name).decode('utf-8', errors='ignore')[:40000])
            xhtml_names = [name for name in archive.namelist() if name.lower().endswith(('.xhtml', '.html', '.htm'))][:12]
            for name in xhtml_names:
                raw = archive.read(name).decode('utf-8', errors='ignore')
                text = _strip_html(raw)
                if text:
                    parts.append(f'EPUB_CHAPTER\t{name}\n{text[:8000]}')
    except Exception as exc:
        raise DocumentTextExtractionError(f'Failed to read EPUB: {exc}') from exc
    return '\n\n'.join(parts)

def _extract_docx(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read('word/document.xml').decode('utf-8', errors='ignore')
    except Exception as exc:
        raise DocumentTextExtractionError(f'Failed to read DOCX: {exc}') from exc
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise DocumentTextExtractionError(f'Failed to parse DOCX: {exc}') from exc
    texts = [node.text for node in root.iter() if node.tag.endswith('}t') and isinstance(node.text, str) and node.text.strip()]
    return f'SOURCE_FILE\t{path.name}\nFORMAT\tdocx\n\n' + '\n'.join(texts)

def _extract_pptx(path: Path) -> str:
    slides: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            slide_names = sorted((name for name in archive.namelist() if re.fullmatch('ppt/slides/slide\\d+\\.xml', name)))
            for index, name in enumerate(slide_names, start=1):
                xml = archive.read(name).decode('utf-8', errors='ignore')
                root = ET.fromstring(xml)
                texts = [node.text for node in root.iter() if node.tag.endswith('}t') and isinstance(node.text, str) and node.text.strip()]
                if texts:
                    slides.append(f'[slide {index}]\n' + '\n'.join(texts))
    except Exception as exc:
        raise DocumentTextExtractionError(f'Failed to read PPTX: {exc}') from exc
    return f'SOURCE_FILE\t{path.name}\nFORMAT\tpptx\n\n' + '\n\n'.join(slides)

def _read_text_file(path: Path, *, allow_binary_fallback: bool=False) -> str:
    try:
        return path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        if not allow_binary_fallback:
            raise DocumentTextExtractionError(f'Unsupported encoding for text extraction: {path.name}')
        return path.read_text(encoding='utf-8', errors='ignore')

def _strip_html(value: str) -> str:
    no_script = re.sub('(?is)<(script|style).*?>.*?</\\1>', ' ', value)
    return _normalize_whitespace(re.sub('(?is)<[^>]+>', ' ', no_script))

def _normalize_whitespace(value: str) -> str:
    lines = [' '.join(line.split()) for line in value.replace('\r\n', '\n').split('\n')]
    collapsed = '\n'.join((line for line in lines if line))
    return collapsed.strip()
