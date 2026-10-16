"""Build PDF navigation from original images; cloud search supplies locations only.

The returned checkpoint belongs in a Sources artifact, never runtime metadata or
logs. Publication and cloud-object lifecycle remain the caller's responsibility.
"""
from __future__ import annotations
import hashlib
import time
from openclass_core.models import AIModelSelection, SourceChapter, SourceStructure, SourceStructureQuality
from openclass_core.services.ai_model_catalog import resolve_text_model_selection, text_model_supports_image_inputs
from openclass_core.services.pdf_visual_directory import PDFDirectoryReadLimit, PDFVisualCatalog, _visual_candidates, generate_pdf_visual_directory
from openclass_core.services.source_ai_directory import SourceAIDirectoryError
from openclass_core.services.source_ingestion_jobs import source_task_cancel_requested
from .catalog_fallback import ensure_original_cache
from .catalog_structure import cloud_blocks, heading_number, heading_parts, toc_pages
from .config import GoogleSourceError
from .coverage import validate_inputs, validate_output
PIPELINE = 'source_original_catalog_v1'

def build_original_catalog(service, source, adapted, outputs, *, hints=None, checkpoint=None, require_cloud_layout=True):
    """Return ``(structure, chapters, checkpoint_payload)`` without publishing.

    ``checkpoint`` must be loaded from a hash-verified Sources artifact by the
    caller. Its source identity is checked again before any resumed model call.
    Cancellation, authorization changes and original/layout identity failures
    propagate; bounded or failed model reading returns explicitly partial work.
    """
    if adapted.format != 'pdf':
        raise GoogleSourceError('Original PDF catalog reading requires the PDF format adapter.')
    requested = {key: source.metadata[key] for key in ('catalog_version', 'catalog_model_selection') if key in source.metadata}
    check = service._check_cloud if require_cloud_layout else service._check
    check(source)
    source = service.state.current(source)
    source, path = ensure_original_cache(service, source)
    source = source.model_copy(update={'metadata': {**source.metadata, **requested}})
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != source.metadata['content_hash']:
        raise GoogleSourceError('The original PDF no longer matches this source version.')
    page_count = validate_inputs(raw, adapted)
    if require_cloud_layout:
        if len(outputs) != len(adapted.parts):
            raise GoogleSourceError('Original-page catalog reading requires completed Layout Parser coverage.')
        allowed_documents = set(source.metadata.get('google_documents', []))
        for part, output in zip(adapted.parts, outputs):
            validate_output(part, output)
            if not output.get('processing_complete') or not output.get('layout_verified') or output.get('document_id') not in allowed_documents:
                raise GoogleSourceError('The original page range has no verified Layout Parser completion for this source.')
    identity = {'source_id': source.id, 'source_version': source.metadata['source_version'], 'content_hash': digest}
    if checkpoint and any((checkpoint.get(key) != value for key, value in identity.items())):
        raise GoogleSourceError('The original-reading checkpoint belongs to another source version.')
    if not require_cloud_layout:
        hints = []
    elif hints is None:
        old_report = (checkpoint.get('catalog') or {}).get('read_report', {}) if checkpoint else {}
        if old_report.get('directory_complete'):
            hints = []
            segments = old_report.get('pagination_segments', [])
            offsets = {segment['physical_start'] - segment['printed_start'] for segment in segments}
            if len(offsets) > 1:
                from openclass_core.services.pdf_directory_contract import parse_pdf_printed_page_label
                unresolved = [row for row in old_report.get('observed_directory_rows', []) if (printed := parse_pdf_printed_page_label(row.get('printed_page_label', ''))) is None or sum((segment['printed_start'] <= printed <= segment['printed_end'] for segment in segments)) != 1]
                targets = list(dict.fromkeys([row['title'] for row in [*unresolved[:1], *unresolved[-1:], *unresolved[1:2]]]))[:3]
                for title in targets:
                    hints.extend(service.catalog_search(source, title, outputs=outputs))
        else:
            hints = service.catalog_search(source, 'table of contents 目录', outputs=outputs)
    located_pages = [page for hint in hints if type(hint.get('page_start')) is int and type(hint.get('page_end')) is int for page in range(max(1, hint['page_start']), min(page_count, hint['page_end']) + 1)]
    pages = list(dict.fromkeys([*located_pages, *(toc_pages(cloud_blocks(outputs), adapted.catalog, hints) if require_cloud_layout else [])]))
    saved_directory = (checkpoint.get('catalog') or {}).get('read_report', {}).get('directory_pages', []) if checkpoint else []
    pages = pages or list(saved_directory) or list(range(1, min(page_count, 6) + 1))
    selection = AIModelSelection.model_validate(source.metadata['catalog_model_selection'])
    resolved = resolve_text_model_selection(selection, user_id=source.owner_user_id)
    if not text_model_supports_image_inputs(resolved, user_id=source.owner_user_id):
        raise GoogleSourceError('The selected catalog model cannot read original page images.')
    latest = {**identity, 'source_sha256': digest, 'page_count': page_count, 'catalog': None, 'delivered_pages': [], 'pending_pages': pages, **dict(checkpoint or {})}

    def remember(value):
        retained = {key: latest[key] for key in ('boundary_pages', 'verified_boundaries') if key in latest}
        latest.clear()
        latest.update({**identity, **retained, **value})

    def cancelled():
        if source_task_cancel_requested():
            return True
        check(source)
        return False
    complete, warning, candidates = (False, '', ())
    started = time.monotonic()
    try:
        result = generate_pdf_visual_directory(record=source, path=path, selection=resolved, initial_pages=pages, authorized_pages=set(range(1, page_count + 1)), require_directory_closure=True, max_model_calls=20, max_unique_pages=60, budget_seconds=15 * 60, checkpoint=checkpoint, checkpoint_callback=remember, is_cancelled=cancelled)
        candidates, complete = (result.candidates, result.catalog_complete is True)
        latest['model_call_count'] = result.model_call_count
        if result.model_call_count == 0:
            latest['run_pages'], latest['page_model_calls'] = ([], {})
    except SourceAIDirectoryError as exc:
        if cancelled():
            raise GoogleSourceError('Original-page catalog reading was cancelled.') from None
        latest['model_call_count'] = exc.model_call_count
        latest['last_failure'] = {'kind': type(exc).__name__, 'detail': str(exc).replace(str(path), '[original source]')[:2400]}
        warning = 'Original-page catalog reading stopped before verification completed. Saved observations can be resumed.'
        if isinstance(exc, PDFDirectoryReadLimit):
            warning = f'The automatic original-page {exc.reason} budget was reached. Saved observations can be resumed.'
        if latest.get('catalog'):
            accepted = PDFVisualCatalog.model_validate(latest['catalog'])
            candidates = tuple(_visual_candidates(accepted, digest))
    if candidates and latest.get('catalog'):
        from openclass_core.services.source_pdf_boundaries import verify_pdf_boundaries
        accepted = PDFVisualCatalog.model_validate(latest['catalog'])
        observed_numbers = {row.key: row.number or heading_number(row.title) for row in accepted.read_report.observed_directory_rows}
        boundary_nodes = [node.model_copy(update={'number': observed_numbers.get(node.key, node.number)}) for node in accepted.chapters]
        boundaries, latest = verify_pdf_boundaries(record=source, path=path, nodes=boundary_nodes, checkpoint=latest, selection=resolved, deadline=started + 15 * 60, is_cancelled=cancelled)
        latest['verified_boundaries'] = boundaries
        if len(boundaries) < len(candidates):
            complete = False
            warning = 'Some chapter boundaries still need original-page verification. Verified chapters remain available; saved observations can be resumed.'
    check(source)
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise GoogleSourceError('The original PDF changed while its catalog was being read.')
    structure, chapters = _catalog(source, candidates, latest, complete=complete, warning=warning, catalog_mode='cloud' if require_cloud_layout else 'manual')
    return (structure, chapters, latest or {**identity, 'source_sha256': digest, 'page_count': page_count, 'catalog': None, 'delivered_pages': [], 'pending_pages': pages})

def _catalog(source, candidates, checkpoint, *, complete, warning, catalog_mode='cloud'):
    digest = source.metadata['content_hash']
    version = int(source.metadata.get('catalog_version', 1))
    report = (checkpoint.get('catalog') or {}).get('read_report', {})
    rows = report.get('observed_directory_rows', [])
    excluded = set(report.get('external_directory_keys', []))
    by_key = {str(candidate.metadata['proposal_key']): candidate for candidate in candidates}
    entries = {row['key']: row for row in rows if row['key'] not in excluded}
    for key, candidate in by_key.items():
        entries.setdefault(key, {'key': key, 'title': candidate.title, 'parent_key': candidate.metadata.get('proposal_parent_key', ''), 'printed_page_label': ''})
    ids = {key: 'sourcechapter_' + hashlib.sha256(f'{source.id}:{digest}:{version}:{key}'.encode()).hexdigest()[:24] for key in entries}
    chapters = []
    page_hashes = checkpoint.get('page_image_sha256', {})
    boundaries = checkpoint.get('verified_boundaries', {})
    for index, (key, row) in enumerate(entries.items()):
        candidate = by_key.get(key)
        number = row.get('number') or heading_number(row['title'])
        normalized_number = (heading_number(f'{number} Title') or number).rsplit('.', 1)[-1]
        _, body_title = heading_parts(row['title'])
        original_heading = row['title'] if heading_number(row['title']) or not number else f"{number} {row['title']}"
        ancestry, parent = ([], row.get('parent_key', ''))
        while parent:
            if parent not in entries or parent in ancestry or parent == key or (len(ancestry) >= 12):
                raise GoogleSourceError('The observed original-page directory has an invalid parent graph.')
            ancestry.insert(0, parent)
            parent = entries[parent].get('parent_key', '')
        source_range = candidate.source_range if candidate else None
        boundary = boundaries.get(key)
        if source_range is not None:
            evidence_pages = set(candidate.metadata.get('visual_evidence_pages', []))
            for segment in report.get('pagination_segments', []):
                if segment['physical_start'] <= source_range.end and segment['physical_end'] >= source_range.start:
                    evidence_pages.update((segment['physical_start'], segment['physical_end']))
            source_range = source_range.model_copy(update={**({'start': boundary['start'], 'end': boundary['end']} if boundary else {}), 'metadata': {**source_range.metadata, 'source_pipeline': PIPELINE, 'source_version': source.metadata['source_version'], 'source_content_hash': digest, 'evidence_method': 'original_page_visual', 'boundary_precision': 'page_range', **(boundary or {}), 'page_image_hashes': {str(page): page_hashes[str(page)] for page in sorted(evidence_pages) if str(page) in page_hashes}}})
        chapters.append(SourceChapter(id=ids[key], owner_user_id=source.owner_user_id, package_id=source.package_id, source_ingestion_id=source.id, parent_id=ids.get(row.get('parent_key')), title=body_title, number=number, normalized_number=normalized_number, level=len(ancestry) + 1, path=[*(heading_parts(entries[parent]['title'])[1] for parent in ancestry), body_title], order_index=index, page_start=source_range.start if source_range else None, page_end=source_range.end if source_range else None, range=source_range, anchor_status='verified' if source_range and boundary else 'unverified', mapping_status='verified' if source_range and boundary else 'unmapped', source_content_hash=digest, catalog_version=version, catalog_evidence=list(candidate.evidence) if candidate else [], metadata={'origin': 'original_page_visual', 'source_pipeline': PIPELINE, 'original_row_key': key, 'ordinal': int(normalized_number) if normalized_number.isdigit() else None, 'original_heading': original_heading, 'locator_source': 'original_page_regions' if boundary else 'original_page_ranges', 'directory_page': row.get('directory_page'), 'printed_page_label': row.get('printed_page_label', '')}))
    verified = sum((chapter.mapping_status == 'verified' for chapter in chapters))
    complete = complete and bool(chapters) and (verified == len(chapters))
    status = 'ready' if complete else 'partial' if chapters else 'unavailable'
    pending = checkpoint.get('pending_pages', [])
    directory_discovered = bool(report.get('directory_complete') and report.get('directory_pages'))
    gaps = ['Some original directory or body boundary observations are still required.'] if not complete else []
    structure = SourceStructure(owner_user_id=source.owner_user_id, package_id=source.package_id, source_ingestion_id=source.id, status='ready' if chapters else 'linear_only', strategy='ai_directory_v1', has_verified_toc=complete, chapter_count=len(chapters), source_content_hash=digest, catalog_version=version, catalog_schema_version=PIPELINE, catalog_model=source.metadata['catalog_model_selection']['model'], quality=SourceStructureQuality(level='fully_verified' if complete else 'partially_verified' if verified else 'unverified', total_chapter_count=len(chapters), verified_chapter_count=verified, unverified_chapter_count=len(chapters) - verified, verified_ratio=verified / len(chapters) if chapters else 0), warnings=[warning] if warning else [], metadata={'source_backend': source.metadata.get('source_backend'), 'catalog_mode': catalog_mode, 'catalog_pipeline': PIPELINE, 'catalog_label': 'Original-page table of contents', 'catalog_status': status, 'directory_status': 'complete' if directory_discovered or complete else 'uncertain', 'directory_pages': report.get('directory_pages', []), 'directory_gaps': gaps, 'pending_pages': pending, 'work_state': 'satisfied' if complete else 'paused', 'phase': 'terminal' if complete else 'range_mapping' if directory_discovered else 'page_calibration', 'can_refine': not complete, 'stop_reason': warning, 'model_call_count': checkpoint.get('model_call_count', 0), 'numbering_status': 'complete' if set(report.get('directory_pages', [])) <= set(checkpoint.get('number_reviewed_pages', [])) and (not checkpoint.get('number_unresolved_keys')) else 'pending', 'original_pages_read': len(checkpoint.get('delivered_pages', []))})
    return (structure, chapters)
