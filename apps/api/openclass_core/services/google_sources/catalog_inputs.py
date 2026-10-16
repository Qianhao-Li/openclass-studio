"""Reuse immutable processed inputs for catalog rebuilds without reprocessing."""
from __future__ import annotations
import copy
import hashlib
from pathlib import PurePosixPath
from .adapters import AdaptedSource, ProcessingPart, AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, NATIVE_MIMES, TEXT_EXTENSIONS
from .config import GoogleSourceError
from .coverage import validate_output
_MIME_SUFFIXES = {'application/pdf': '.pdf', 'text/plain': '.txt', 'text/html': '.html', **{mime: suffix for suffix, mime in NATIVE_MIMES.items()}}
_LEGACY_FORMATS = {'.doc': 'docx', '.odt': 'docx', '.rtf': 'docx', '.ppt': 'pptx', '.odp': 'pptx', '.xls': 'xlsx', '.ods': 'xlsx'}

def _source_format(source):
    suffix = PurePosixPath(source.file_name).suffix.lower()
    mime = source.mime_type.split(';', 1)[0].strip().lower()
    if source.file_name.endswith('.webbundle.zip'):
        return 'web_snapshot'
    if suffix in _LEGACY_FORMATS:
        return _LEGACY_FORMATS[suffix]
    if suffix in AUDIO_EXTENSIONS or mime.startswith('audio/'):
        return 'audio'
    if suffix == '.pdf' or mime == 'application/pdf':
        return 'pdf'
    if suffix in IMAGE_EXTENSIONS or mime.startswith('image/'):
        return 'image'
    if suffix in NATIVE_MIMES:
        return suffix[1:]
    if suffix == '.epub':
        return 'epub'
    if suffix in {'.csv', '.tsv'}:
        return 'table'
    if suffix in {'.html', '.htm'} or mime == 'text/html':
        return 'html'
    if suffix in TEXT_EXTENSIONS or mime.startswith('text/'):
        return 'text'
    raise GoogleSourceError('This source format has no supported catalog input recovery adapter.')

def rebuild_inputs(service, source, artifact) -> AdaptedSource:
    """Restore the exact GCS bytes previously processed by Google.

    This helper performs reads only. It never invokes adapters, conversions,
    transcription, parsing or model APIs, and never treats chunk text as input.
    The artifact must come from the authenticated source artifact store; its
    binding and each input object's namespace/generation/hash are checked again.
    """
    service._check_cloud(source)
    expected = {'source_id': source.id, 'source_version': source.metadata.get('source_version'), 'content_hash': source.metadata.get('content_hash')}
    if not isinstance(artifact, dict) or any((artifact.get(key) != value for key, value in expected.items())):
        raise GoogleSourceError('The saved catalog inputs belong to a different source version.')
    outputs = artifact.get('parts')
    if not isinstance(outputs, list) or not outputs:
        raise GoogleSourceError('The source has no saved processing inputs to rebuild its catalog.')
    format_name = _source_format(source)
    document_ids = set(source.metadata.get('google_documents', []))
    parts, seen = ([], set())
    for index, output in enumerate(outputs):
        if not isinstance(output, dict):
            raise GoogleSourceError('A saved source processing part is invalid.')
        document_id = output.get('document_id')
        if not document_id or document_id in seen or document_id not in document_ids:
            raise GoogleSourceError("A saved input is missing, duplicated or outside this source's processed documents.")
        seen.add(document_id)
        ref = output.get('input')
        if not isinstance(ref, dict) or output.get('processing_complete') is not True:
            raise GoogleSourceError('A source processing input has not completed verification.')
        mime = str(ref.get('mime_type', '')).split(';', 1)[0].strip().lower()
        if mime not in _MIME_SUFFIXES:
            raise GoogleSourceError('The saved source input has an unsupported processing MIME type.')
        layout = output.get('layout_required')
        if type(layout) is not bool or (mime != 'text/plain' and (not layout)):
            raise GoogleSourceError('The saved source input has no reliable Layout Parser requirement.')
        if layout and output.get('layout_verified') is not True:
            raise GoogleSourceError('A visual source input has not completed Layout Parser.')
        count, offset = (output.get('page_count'), output.get('page_offset'))
        if type(count) is not int or count < 0 or type(offset) is not int or (offset < 0):
            raise GoogleSourceError('The saved source input has invalid physical page coordinates.')
        locator = output.get('locator')
        if not isinstance(locator, dict):
            raise GoogleSourceError('The saved source input has no verified original locator.')
        service._verify_blob_binding(source, ref)
        service._check_cloud(source)
        raw = service.storage.read(ref)
        if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != ref.get('sha256') or str(len(raw)) != str(ref.get('size')):
            raise GoogleSourceError('The saved source input hash or size does not match.')
        name = f'processing-{index + 1}{_MIME_SUFFIXES[mime]}'
        part = ProcessingPart(name=name, mime_type=mime, content=raw, layout_required=layout, page_count=count, page_offset=offset, locator=copy.deepcopy(locator))
        validate_output(part, output)
        if mime == 'application/pdf':
            import io
            from pypdf import PdfReader
            try:
                reader = PdfReader(io.BytesIO(raw))
                valid = not reader.is_encrypted and len(reader.pages) == count and (count > 0)
            except Exception as exc:
                raise GoogleSourceError('The saved PDF processing input cannot be verified.') from exc
            if not valid:
                raise GoogleSourceError('The saved PDF input has a different physical page count.')
        service._check_cloud(source)
        parts.append(part)
    if seen != document_ids:
        raise GoogleSourceError('The saved source artifact omits one or more processed inputs.')
    if format_name == 'pdf':
        expected_page = 0
        for part in parts:
            if part.mime_type != 'application/pdf' or part.page_offset != expected_page or part.locator.get('kind') != 'pdf_pages' or (part.locator.get('start') != expected_page + 1) or (part.locator.get('end') != expected_page + part.page_count):
                raise GoogleSourceError('The saved PDF inputs contain gaps or overlapping original ranges.')
            expected_page += part.page_count
        if artifact.get('page_count') != expected_page:
            raise GoogleSourceError('The saved PDF inputs do not cover the complete original source.')
    if format_name == 'audio' and (len(parts) != 1 or parts[0].mime_type != 'text/plain' or parts[0].locator.get('kind') != 'audio_transcript'):
        raise GoogleSourceError('The saved audio has no complete verified transcription input.')
    tables = artifact.get('tables', {})
    if not isinstance(tables, dict):
        raise GoogleSourceError('The saved source tables have an invalid format.')
    service._check_cloud(source)
    return AdaptedSource(parts=parts, format=format_name, tables=copy.deepcopy(tables))
