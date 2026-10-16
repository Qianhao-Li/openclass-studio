from __future__ import annotations
import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol
from pydantic import BaseModel, Field
from openclass_core.models import AgentActivityEvent, AIModelSelection, SourceCatalogRun, SourceChapter, SourceIngestionRecord, SourceRange, SourceStructure, SourceStructureQuality, new_id, now_iso
from openclass_core.services.codex_app_server import CodexAppServerTextClient
from openclass_core.services.ai_logging import ai_usage_logger
from openclass_core.services.native_pi_source_agent import generate_native_pi_directory
from openclass_core.services.source_chapter_identity import stable_source_chapter_id
from openclass_core.services.source_codex_catalog import AgentCatalogDirectoryEvidence, AgentCatalogV3, AgentCatalogV3Node, CodexDirectCatalogEvidence, CodexDirectSourceRange, _generate_complete_native_pdf_catalog, coerce_agent_catalog_v3_checkpoint, generate_agent_catalog_turn
from openclass_core.services.source_directory_extractor import CatalogProgressCallback, DirectoryCandidate, DirectoryExtraction, extract_authoritative_native_directory, extract_directory, has_usable_native_body_index
from openclass_core.services.source_ai_directory import AI_DIRECTORY_SCHEMA_VERSION, HYBRID_DIRECTORY_SCHEMA_VERSION, SourceAIDirectoryError, SourceAIDirectoryGenerator, merge_native_and_ai_candidates, native_directory_state, source_ai_directory_generator
from openclass_core.services.source_structure_store import SourceStructureStore, project_pdf_visual_catalog, source_structure_store
CATALOG_SCHEMA_VERSION = 'codex_directory_v1'
PREVIOUS_CATALOG_SCHEMA_VERSION = 'agent_catalog_v3'
LEGACY_CATALOG_SCHEMA_VERSION = 'agent_catalog_v2'
LEGACY_CATALOG_SCHEMA_VERSIONS = (LEGACY_CATALOG_SCHEMA_VERSION, 'ai_directory_v1', 'native_directory_v1', 'hybrid_directory_v1')
AI_CATALOG_SCHEMA_VERSIONS = ()
NATIVE_EPUB_CATALOG_SCHEMA_VERSION = 'native_epub_navigation_v1'
AUTONOMOUS_CATALOG_BUDGET_SECONDS = 15 * 60
MAX_CODEX_BATCH_NODES = 120
MAX_CODEX_BATCH_CHARS = 48000
NORMALIZATION_PROGRESS_START = 64
NORMALIZATION_PROGRESS_END = 80
NUMERIC_CONTAINMENT_RANGE_KINDS = frozenset({'pdf_pages', 'docx_paragraphs', 'ppt_slides', 'sheet_rows', 'text_lines'})
_UNSET_MODEL = object()

class SourceDirectoryProcessingError(RuntimeError):
    pass

class DirectoryNodeDecision(BaseModel):
    local_key: str = Field(min_length=1, max_length=160)
    keep: bool = True
    title: str = Field(default='', max_length=300)
    number: str = Field(default='', max_length=80)
    level: int = Field(default=1, ge=1, le=12)
    reason: str = Field(default='', max_length=300)

class DirectoryBatchDecision(BaseModel):
    batch_hash: str
    decisions: list[DirectoryNodeDecision] = Field(default_factory=list, max_length=MAX_CODEX_BATCH_NODES)

@dataclass(frozen=True)
class DirectoryNormalizationResult:
    candidates: tuple[DirectoryCandidate, ...]
    turn_count: int
    metadata: dict[str, object]

class DirectoryNormalizer(Protocol):

    def normalize(self, *, record: SourceIngestionRecord, candidates: Sequence[DirectoryCandidate], selection: AIModelSelection) -> DirectoryNormalizationResult:
        ...

class CodexDirectoryNormalizer:
    """Run bounded directory-only Codex turns serially.

    The model receives headings and locators only. It never receives the source
    file path or extracted body text, and it cannot alter authoritative ranges.
    """

    def __init__(self, *, user_id: str, progress_callback: CatalogProgressCallback | None=None) -> None:
        self.user_id = user_id
        self.progress_callback = progress_callback

    def normalize(self, *, record: SourceIngestionRecord, candidates: Sequence[DirectoryCandidate], selection: AIModelSelection) -> DirectoryNormalizationResult:
        if not selection.model.strip():
            raise SourceDirectoryProcessingError('A configured text model is required for cataloging.')
        if not candidates:
            return DirectoryNormalizationResult(candidates=(), turn_count=0, metadata={'batch_count': 0})
        batches = _bounded_candidate_batches(candidates)
        normalized: list[DirectoryCandidate] = []
        batch_hashes: list[str] = []
        client = CodexAppServerTextClient(self.user_id)
        for batch_index, batch in enumerate(batches):
            packet = {'schema': CATALOG_SCHEMA_VERSION, 'source': {'id': record.id, 'title': record.title, 'file_name': record.file_name, 'mime_type': record.mime_type}, 'batch_index': batch_index, 'batch_count': len(batches), 'nodes': [_candidate_packet(candidate) for candidate in batch]}
            batch_hash = _hash_json(packet)
            batch_hashes.append(batch_hash)
            response = client.parse(provider=selection.provider, model=selection.model, system_prompt=_directory_system_prompt(), user_prompt='Review this bounded directory-evidence packet. Copy batch_hash exactly and return one decision for every local_key. Do not invent nodes or ranges.\n' + json.dumps({**packet, 'batch_hash': batch_hash}, ensure_ascii=False), schema=DirectoryBatchDecision, allow_live_web_search=False, reasoning_effort=selection.reasoning_effort, service_tier=selection.service_tier, service_tier_is_set='service_tier' in selection.model_fields_set)
            decision = DirectoryBatchDecision.model_validate(response.output_parsed)
            normalized.extend(_apply_batch_decision(batch, decision, expected_hash=batch_hash))
            _report(self.progress_callback, 'normalizing_directory', _normalization_batch_progress(batch_index + 1, len(batches)))
        _validate_locked_navigation_invariants(candidates, normalized)
        return DirectoryNormalizationResult(candidates=tuple(normalized), turn_count=len(batches), metadata={'batch_count': len(batches), 'batch_hashes': batch_hashes, 'execution': 'serial_bounded_turns'})

class SourceDirectoryProcessor:

    def __init__(self, *, store: SourceStructureStore=source_structure_store, normalizer_factory: Callable[[SourceIngestionRecord], DirectoryNormalizer] | None=None, ai_generator: SourceAIDirectoryGenerator | None=None) -> None:
        self.store = store
        self.normalizer_factory = normalizer_factory
        self.ai_generator = ai_generator

    def process(self, *, record: SourceIngestionRecord, path: Path, catalog_model: AIModelSelection | None=None, requested_model: AIModelSelection | None | object=_UNSET_MODEL, progress_callback: CatalogProgressCallback | None=None, activity_callback: Callable[[AgentActivityEvent], None] | None=None, resume_catalog: bool=False, force_ai: bool=False) -> SourceStructure:
        selected_model = requested_model if requested_model is not _UNSET_MODEL else catalog_model
        return self._process_native_then_selected_ai(record=record, path=path, requested_model=selected_model if isinstance(selected_model, AIModelSelection) else None, progress_callback=progress_callback, activity_callback=activity_callback, force_ai=force_ai)

    def process_legacy_catalog(self, *, record: SourceIngestionRecord, path: Path, catalog_model: AIModelSelection | None=None, progress_callback: CatalogProgressCallback | None=None, activity_callback: Callable[[AgentActivityEvent], None] | None=None, resume_catalog: bool=False) -> SourceStructure:
        """Replay the former autonomous pipeline for stored-catalog compatibility."""
        if catalog_model is None:
            raise SourceDirectoryProcessingError('A directory model must be selected.')
        started = time.perf_counter()
        force_content_rebuild = bool(record.metadata.get('force_ai_directory'))
        metadata_hash = str(record.metadata.get('content_hash') or '').strip()
        content_hash = _file_hash(path)
        if not content_hash:
            raise SourceDirectoryProcessingError('The source content fingerprint is unavailable.')
        run = SourceCatalogRun(owner_user_id=record.owner_user_id, package_id=record.package_id, source_ingestion_id=record.id, status='running', model=catalog_model.model, stage_history=['queued', 'content_rebuild_requested' if force_content_rebuild else 'reading_directory_metadata'], metadata={'catalog_schema_version': CATALOG_SCHEMA_VERSION, 'catalog_model_selection': catalog_model.model_dump(mode='json'), 'source_content_hash': content_hash, 'active_catalog_run_id': str(record.metadata.get('active_catalog_run_id') or ''), 'no_full_text_index': True, 'forced_ai_rebuild': force_content_rebuild})
        self.store.save_catalog_run(run)
        extraction: DirectoryExtraction | None = None
        try:
            if metadata_hash and metadata_hash != content_hash:
                raise SourceDirectoryProcessingError('The source file fingerprint no longer matches the uploaded source.')
            warnings: list[str]
            catalog_complete: bool
            execution_metadata: dict[str, object]
            structure_execution_metadata: dict[str, object]
            turn_count: int
            has_authoritative_ranges = False
            if self.normalizer_factory is None:
                native_epub_extraction = None if force_content_rebuild else self._complete_native_epub_extraction(record=record, path=path, progress_callback=progress_callback)
                if native_epub_extraction is not None:
                    return self._publish_native_epub_catalog(record=record, path=path, content_hash=content_hash, extraction=native_epub_extraction, run=run, started=started, progress_callback=progress_callback)
                return self._process_july24_catalog(record=record, path=path, content_hash=content_hash, catalog_model=catalog_model, run=run, started=started, progress_callback=progress_callback, activity_callback=activity_callback, force_content_rebuild=force_content_rebuild)
            else:
                extraction = extract_directory(record, path, progress_callback=progress_callback)
                run = self.store.save_catalog_run(run.model_copy(update={'page_count': extraction.page_count, 'inspected_page_count': extraction.inspected_page_count, 'ocr_page_count': extraction.ocr_page_count, 'stage_history': [*run.stage_history, 'normalizing_directory'], 'metadata': {**run.metadata, 'extraction': extraction.metadata}}))
                _report(progress_callback, 'normalizing_directory', 64)
                normalization = self.normalizer_factory(record).normalize(record=record, candidates=extraction.candidates, selection=catalog_model)
                _validate_locked_navigation_invariants(extraction.candidates, normalization.candidates)
                normalized_candidates = _reclose_normalized_ranges(normalization.candidates, extraction=extraction)
                chapters = _materialize_chapters(record=record, candidates=normalized_candidates, content_hash=content_hash)
                warnings = list(extraction.warnings)
                if not chapters:
                    warnings.append('No citable directory node was found without extracting document body text.')
                catalog_complete = not bool(extraction.metadata.get('navigation_truncated'))
                execution_metadata = {'catalog_authority': 'legacy_explicit_test_injection', 'extraction': extraction.metadata, 'normalization': normalization.metadata}
                structure_execution_metadata = execution_metadata
                turn_count = normalization.turn_count
            validation_stage = 'validating_directory_ranges' if self.normalizer_factory is not None or has_authoritative_ranges else 'validating_directory'
            _report(progress_callback, validation_stage, 92)
            _validate_chapters(chapters)
            verified_count = sum((chapter.mapping_status == 'verified' for chapter in chapters))
            quality = _catalog_quality(chapters, catalog_complete=catalog_complete, directory_only_complete=execution_metadata.get('catalog_task_contract') == 'directory_pages_offset_tree_v1')
            status = 'ready' if chapters else 'linear_only'
            structure = SourceStructure(owner_user_id=record.owner_user_id, package_id=record.package_id, source_ingestion_id=record.id, status=status, strategy='codex_directory_v1', has_verified_toc=bool(chapters) and catalog_complete, quality=quality, chapter_count=len(chapters), chunk_count=0, visual_count=0, visual_index_status='unsupported', visual_index_version=0, confidence=quality.confidence, source_content_hash=content_hash, catalog_schema_version=CATALOG_SCHEMA_VERSION, catalog_model=catalog_model.model, warnings=list(dict.fromkeys(warnings)), metadata={'catalog_pipeline': CATALOG_SCHEMA_VERSION, 'content_hash': content_hash, 'catalog_model_selection': catalog_model.model_dump(mode='json'), **structure_execution_metadata, 'body_text_extracted': False, 'source_chunks_created': False, 'vector_index_created': False, 'visual_index_created': False, 'open_notebook_called': False})
            duration_ms = max(0, round((time.perf_counter() - started) * 1000))
            succeeded_run = run.model_copy(update={'status': 'succeeded', 'turn_count': turn_count, 'chapter_count': len(chapters), 'verified_chapter_count': verified_count, 'verification_rate': verified_count / len(chapters) if chapters else 0.0, 'duration_ms': duration_ms, 'stage_history': [*run.stage_history, validation_stage, 'publishing_catalog', 'succeeded'], 'completed_at': now_iso(), 'metadata': {**run.metadata, **execution_metadata, 'warning_count': len(structure.warnings)}})
            _report(progress_callback, 'publishing_catalog', 97)
            if _file_hash(path) != content_hash:
                raise SourceDirectoryProcessingError('The source file changed while its directory catalog was being built.')
            published = self.store.publish_catalog(structure=structure, chapters=chapters, run=succeeded_run)
            try:
                _report(progress_callback, 'catalog_ready', 99)
            except Exception:
                pass
            return published
        except Exception as exc:
            duration_ms = max(0, round((time.perf_counter() - started) * 1000))
            failed = run.model_copy(update={'status': 'failed', 'duration_ms': duration_ms, 'error': str(exc), 'stage_history': [*run.stage_history, 'failed'], 'completed_at': now_iso(), 'page_count': extraction.page_count if extraction is not None else run.page_count, 'inspected_page_count': extraction.inspected_page_count if extraction is not None else run.inspected_page_count, 'ocr_page_count': extraction.ocr_page_count if extraction is not None else run.ocr_page_count})
            self.store.save_catalog_run(failed)
            if isinstance(exc, SourceDirectoryProcessingError):
                raise
            raise SourceDirectoryProcessingError(str(exc)) from exc

    def _process_native_then_selected_ai(self, *, record: SourceIngestionRecord, path: Path, requested_model: AIModelSelection | None, progress_callback: CatalogProgressCallback | None, activity_callback: Callable[[AgentActivityEvent], None] | None, force_ai: bool) -> SourceStructure:
        """Publish complete native indexes; ask the selected model only for gaps."""
        if not path.is_file():
            raise SourceDirectoryProcessingError('The uploaded source file is unavailable.')
        started = time.perf_counter()
        metadata_hash = str(record.metadata.get('content_hash') or '').strip()
        content_hash = metadata_hash or _file_hash(path)
        if metadata_hash and _file_hash(path) != metadata_hash:
            raise SourceDirectoryProcessingError('The source file fingerprint no longer matches the uploaded source.')
        initial_stage = 'sending_file_to_ai' if force_ai else 'reading_native_directory_index'
        run = SourceCatalogRun(owner_user_id=record.owner_user_id, package_id=record.package_id, source_ingestion_id=record.id, status='running', model=requested_model.model if requested_model is not None else '', stage_history=['queued', initial_stage], metadata={'catalog_schema_version': AI_DIRECTORY_SCHEMA_VERSION, 'catalog_model_selection': requested_model.model_dump(mode='json') if requested_model is not None else None, 'source_content_hash': content_hash, 'active_catalog_run_id': str(record.metadata.get('active_catalog_run_id') or ''), 'processing_mode': 'ai_file_direct_forced' if force_ai else 'native_then_ai', 'force_ai_directory': force_ai, 'no_full_text_index': True})
        self.store.save_catalog_run(run)
        try:
            if force_ai:
                if requested_model is None:
                    raise SourceDirectoryProcessingError('A directory model must be selected before refreshing the catalog with AI.')
                return self._publish_selected_ai_catalog(record=record, path=path, content_hash=content_hash, requested_model=requested_model, run=run, started=started, progress_callback=progress_callback, activity_callback=activity_callback, native_warnings=(), forced=True)
            _report(progress_callback, 'reading_native_directory_index', 35)
            native = extract_authoritative_native_directory(record, path, progress_callback=progress_callback)
            native_state = native_directory_state(native)
            if native_state == 'native_complete':
                return self._publish_native_index_catalog(record=record, content_hash=content_hash, extraction=native, requested_model=requested_model, run=run, started=started, progress_callback=progress_callback)
            if requested_model is None and native_state == 'native_partial':
                return self._publish_native_index_catalog(record=record, content_hash=content_hash, extraction=native, requested_model=None, run=run, started=started, progress_callback=progress_callback, complete=False)
            if requested_model is None:
                raise SourceDirectoryProcessingError('No usable native directory body index was found, and a directory model must be selected before AI cataloging.')
            try:
                return self._publish_selected_ai_catalog(record=record, path=path, content_hash=content_hash, requested_model=requested_model, run=run, started=started, progress_callback=progress_callback, activity_callback=activity_callback, native_warnings=native.warnings, extraction=native)
            except SourceAIDirectoryError as exc:
                if native_state != 'native_partial':
                    raise
                previous = self.store.get_structure(owner_user_id=record.owner_user_id, package_id=record.package_id, source_id=record.id)
                if previous is not None:
                    raise
                return self._publish_native_index_catalog(record=record, content_hash=content_hash, extraction=replace(native, warnings=(*native.warnings, str(exc))), requested_model=requested_model, run=run, started=started, progress_callback=progress_callback, complete=False, model_call_count=exc.model_call_count)
        except Exception as exc:
            self.store.save_catalog_run(run.model_copy(update={'status': 'failed', 'error': str(exc), 'duration_ms': max(1, int((time.perf_counter() - started) * 1000)), 'stage_history': [*run.stage_history, 'failed']}))
            if isinstance(exc, SourceDirectoryProcessingError):
                raise
            raise SourceDirectoryProcessingError(str(exc)) from exc

    def _publish_native_index_catalog(self, *, record: SourceIngestionRecord, content_hash: str, extraction: DirectoryExtraction, requested_model: AIModelSelection | None, run: SourceCatalogRun, started: float, progress_callback: CatalogProgressCallback | None, complete: bool=True, model_call_count: int=0) -> SourceStructure:
        _report(progress_callback, 'materializing_directory', 84)
        chapters = _materialize_chapters(record=record, candidates=extraction.candidates, content_hash=content_hash, catalog_pipeline='native_directory_v1')
        if not chapters:
            raise SourceDirectoryProcessingError('Native directory extraction returned an empty directory.')
        _validate_chapters(chapters)
        quality = _compat_catalog_quality(chapters, diagnostic='The catalog was generated from the native content index.')
        verified_count = sum((chapter.mapping_status == 'verified' for chapter in chapters))
        if not complete:
            quality = quality.model_copy(update={'level': 'partially_verified'})
        directory_source = str(extraction.metadata.get('directory_source') or 'native')
        structure = SourceStructure(owner_user_id=record.owner_user_id, package_id=record.package_id, source_ingestion_id=record.id, status='ready', strategy='native_directory_v1', has_verified_toc=complete, quality=quality, chapter_count=len(chapters), chunk_count=0, visual_count=0, visual_index_status='unsupported', visual_index_version=0, confidence=quality.confidence, source_content_hash=content_hash, catalog_schema_version='native_directory_v1', catalog_model=requested_model.model if requested_model is not None else '', warnings=list(extraction.warnings), metadata={'catalog_pipeline': 'native_directory_v1', 'content_hash': content_hash, 'processing_mode': 'native_only', 'model_call_count': model_call_count, 'no_model_calls': model_call_count == 0, 'native_state': 'native_complete' if complete else 'native_partial', 'directory_status': 'complete' if complete else 'uncertain', 'index_status': 'complete' if complete else 'partial', 'unresolved_node_count': len(chapters) - verified_count, 'directory_source': directory_source, 'catalog_authority': 'native_navigation', 'body_text_extracted': False, 'source_chunks_created': False, 'vector_index_created': False, 'visual_index_created': False, **extraction.metadata})
        _report(progress_callback, 'publishing_catalog', 97)
        return self.store.publish_catalog(structure=structure, chapters=chapters, run=run.model_copy(update={'status': 'succeeded', 'chapter_count': len(chapters), 'verified_chapter_count': verified_count, 'verification_rate': verified_count / len(chapters), 'turn_count': model_call_count, 'duration_ms': max(1, int((time.perf_counter() - started) * 1000)), 'stage_history': [*run.stage_history, 'materializing_directory', 'completed'], 'completed_at': structure.updated_at}))

    def _publish_selected_ai_catalog(self, *, record: SourceIngestionRecord, path: Path, content_hash: str, requested_model: AIModelSelection, run: SourceCatalogRun, started: float, progress_callback: CatalogProgressCallback | None, activity_callback: Callable[[AgentActivityEvent], None] | None, native_warnings: Sequence[str], extraction: DirectoryExtraction | None=None, forced: bool=False) -> SourceStructure:
        _report(progress_callback, 'sending_file_to_ai', 55)
        generator = self.ai_generator or source_ai_directory_generator
        result = generator.generate(record=record, path=path, requested_model=requested_model, progress_callback=progress_callback, extraction=extraction, activity_callback=activity_callback)
        _report(progress_callback, 'validating_directory', 90)
        if _file_hash(path) != content_hash:
            raise SourceDirectoryProcessingError('The source file changed while its directory was being built.')
        is_hybrid = result.strategy == HYBRID_DIRECTORY_SCHEMA_VERSION
        candidates = merge_native_and_ai_candidates(extraction.candidates, result.candidates) if is_hybrid and extraction is not None else result.candidates
        chapters = _materialize_chapters(record=record, candidates=candidates, content_hash=content_hash, catalog_pipeline=result.strategy)
        if not chapters:
            raise SourceDirectoryProcessingError('The selected directory model returned an empty directory.')
        _validate_chapters(chapters)
        diagnostic = 'The native table of contents was preserved. The selected AI fills in missing entries only.' if is_hybrid else 'Catalog refresh requested. The selected AI reads the original file to rebuild it.' if forced else 'No native content index is available. The selected AI generates the catalog.'
        quality = _compat_catalog_quality(chapters, diagnostic=diagnostic)
        processing_mode = 'native_plus_ai' if is_hybrid else 'ai_file_direct_forced' if forced else 'ai_file_direct'
        native_state = 'native_partial' if is_hybrid else 'skipped' if forced else 'native_absent'
        verified_count = sum((chapter.mapping_status == 'verified' for chapter in chapters))
        catalog_complete = verified_count == len(chapters) and (not bool(extraction and extraction.metadata.get('navigation_truncated')))
        if result.catalog_complete is not None:
            catalog_complete = catalog_complete and result.catalog_complete
        if path.suffix.lower() == '.pdf':
            catalog_complete = catalog_complete and (result.input_metadata.get('input_method') == 'original_pdf_page_images' and result.catalog_complete is True)
        if not catalog_complete and quality.level == 'fully_verified':
            quality = quality.model_copy(update={'level': 'partially_verified'})
        structure = SourceStructure(owner_user_id=record.owner_user_id, package_id=record.package_id, source_ingestion_id=record.id, status='ready', strategy=result.strategy, has_verified_toc=catalog_complete, quality=quality, chapter_count=len(chapters), chunk_count=0, visual_count=0, visual_index_status='unsupported', visual_index_version=0, confidence=quality.confidence, source_content_hash=content_hash, catalog_schema_version=result.strategy, catalog_model=result.model_label, warnings=[*native_warnings, *result.warnings], metadata={'catalog_pipeline': result.strategy, 'content_hash': content_hash, 'catalog_model_selection': requested_model.model_dump(mode='json'), 'processing_mode': processing_mode, 'model_call_count': result.model_call_count, 'no_model_calls': False, 'native_state': native_state, 'directory_status': 'complete' if catalog_complete else 'uncertain', 'index_status': 'complete' if verified_count == len(chapters) else 'partial', 'unresolved_node_count': len(chapters) - verified_count, 'forced_ai_rebuild': forced, 'model_label': result.model_label, 'catalog_authority': 'selected_ai_file_output', 'body_text_extracted': False, 'source_chunks_created': False, 'vector_index_created': False, 'visual_index_created': False, **result.input_metadata})
        visual_projection = project_pdf_visual_catalog(structure, chapters)
        if visual_projection is not None:
            structure = structure.model_copy(update={'quality': visual_projection.quality})
        _report(progress_callback, 'publishing_catalog', 97)
        return self.store.publish_catalog(structure=structure, chapters=chapters, run=run.model_copy(update={'status': 'succeeded', 'chapter_count': len(chapters), 'verified_chapter_count': verified_count, 'verification_rate': verified_count / len(chapters), 'turn_count': result.model_call_count, 'duration_ms': max(1, int((time.perf_counter() - started) * 1000)), 'stage_history': [*run.stage_history, 'materializing_directory', 'completed'], 'completed_at': structure.updated_at, 'metadata': {**run.metadata, **result.input_metadata}}))

    @staticmethod
    def _complete_native_epub_extraction(*, record: SourceIngestionRecord, path: Path, progress_callback: CatalogProgressCallback | None) -> DirectoryExtraction | None:
        suffix = Path(record.file_name or path.name).suffix.lower()
        if suffix != '.epub' and record.mime_type.lower() != 'application/epub+zip':
            return None
        extraction = extract_directory(record, path, progress_callback=progress_callback)
        native_count = int(extraction.metadata.get('native_navigation_count') or 0)
        if extraction.metadata.get('format') != 'epub' or native_count <= 0 or int(extraction.metadata.get('spine_count') or 0) <= 0 or extraction.metadata.get('navigation_truncated') or (len(extraction.candidates) != native_count) or any((candidate.mapping_status != 'verified' or candidate.source_range is None or candidate.source_range.kind != 'epub_spine' for candidate in extraction.candidates)):
            return None
        return extraction

    def _publish_native_epub_catalog(self, *, record: SourceIngestionRecord, path: Path, content_hash: str, extraction: DirectoryExtraction, run: SourceCatalogRun, started: float, progress_callback: CatalogProgressCallback | None) -> SourceStructure:
        candidates = tuple((replace(candidate, metadata={**candidate.metadata, 'catalog_authority': 'native_epub_navigation', 'locator_source': 'native_navigation'}) for candidate in extraction.candidates))
        chapters = _materialize_chapters(record=record, candidates=candidates, content_hash=content_hash)
        _validate_chapters(chapters)
        verified_count = sum((chapter.mapping_status == 'verified' for chapter in chapters))
        index_status = 'complete' if verified_count == len(chapters) else 'partial'
        quality = _catalog_quality(chapters, catalog_complete=True)
        native_count = int(extraction.metadata.get('native_navigation_count') or len(chapters))
        completion_reason = 'Imported every authored EPUB NCX/nav entry and resolved its OPF spine and XHTML anchor without extracting document body text.'
        execution_metadata = {'catalog_authority': 'native_epub_navigation', 'catalog_task_contract': NATIVE_EPUB_CATALOG_SCHEMA_VERSION, 'host_directory_transform': 'mechanical_materialization_only', 'native_navigation_count': native_count, 'published_navigation_count': len(chapters), 'spine_count': int(extraction.metadata.get('spine_count') or 0), 'anchor_validation': extraction.metadata.get('anchor_validation'), 'anchor_document_count': int(extraction.metadata.get('anchor_document_count') or 0), 'directory_evidence': [AgentCatalogDirectoryEvidence(kind='native_navigation_exhausted', detail=f'Imported all {native_count} authored EPUB navigation entries.').model_dump(mode='json')], 'work_state': 'satisfied', 'phase': 'terminal', 'directory_status': 'complete', 'index_status': index_status, 'summary': f'Imported {len(chapters)} authored EPUB navigation entries directly.', 'next_plan': '', 'next_action': '', 'stop_reason': '', 'completion_reason': completion_reason, 'directory_gaps': []}
        structure = SourceStructure(owner_user_id=record.owner_user_id, package_id=record.package_id, source_ingestion_id=record.id, status='ready', strategy='epub_navigation', has_verified_toc=bool(chapters), quality=quality, chapter_count=len(chapters), chunk_count=0, visual_count=0, visual_index_status='unsupported', visual_index_version=0, confidence=quality.confidence, source_content_hash=content_hash, catalog_schema_version=CATALOG_SCHEMA_VERSION, catalog_model='', warnings=list(extraction.warnings), metadata={'catalog_pipeline': CATALOG_SCHEMA_VERSION, 'content_hash': content_hash, **execution_metadata, 'unresolved_node_count': len(chapters) - verified_count, 'can_refine': False, 'auto_continuing': False, 'body_text_extracted': False, 'source_chunks_created': False, 'vector_index_created': False, 'visual_index_created': False})
        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        succeeded_run = run.model_copy(update={'status': 'succeeded', 'model': '', 'turn_count': 0, 'chapter_count': len(chapters), 'verified_chapter_count': verified_count, 'verification_rate': verified_count / len(chapters) if chapters else 0.0, 'duration_ms': duration_ms, 'stage_history': [*run.stage_history, 'native_epub_navigation', 'publishing_catalog', 'succeeded'], 'completed_at': now_iso(), 'metadata': {**run.metadata, **execution_metadata}})
        _report(progress_callback, 'publishing_catalog', 97)
        if _file_hash(path) != content_hash:
            raise SourceDirectoryProcessingError('The source file changed while its native EPUB catalog was being built.')
        published = self.store.publish_catalog(structure=structure, chapters=chapters, run=succeeded_run)
        try:
            _report(progress_callback, 'catalog_ready', 99)
        except Exception:
            pass
        return published

    def _process_july24_catalog(self, *, record: SourceIngestionRecord, path: Path, content_hash: str, catalog_model: AIModelSelection, run: SourceCatalogRun, started: float, progress_callback: CatalogProgressCallback | None, activity_callback: Callable[[AgentActivityEvent], None] | None, force_content_rebuild: bool=False) -> SourceStructure:
        """Use authored navigation, otherwise continue native Pi until PDF ranges are complete."""
        reusable_catalog_finder = getattr(self.store, 'find_reusable_verified_catalog', None)
        reusable_catalog = reusable_catalog_finder(owner_user_id=record.owner_user_id, source_content_hash=content_hash, exclude_source_id=record.id) if callable(reusable_catalog_finder) and (not force_content_rebuild) else None
        if reusable_catalog is not None:
            reusable_structure, reusable_chapters = reusable_catalog
            _report(progress_callback, 'reusing_verified_catalog', 92)
            id_map: dict[str, str] = {}
            cloned_chapters: list[SourceChapter] = []
            for chapter in reusable_chapters:
                cloned_id = stable_source_chapter_id(source_ingestion_id=record.id, parent_path=chapter.path[:-1], normalized_number=chapter.normalized_number or chapter.number, title=chapter.title, level=chapter.level, source_locator=chapter.source_locator, order_index=int(chapter.metadata.get('semantic_occurrence', 0) or 0))
                id_map[chapter.id] = cloned_id
                cloned_chapters.append(chapter.model_copy(update={'id': cloned_id, 'owner_user_id': record.owner_user_id, 'package_id': record.package_id, 'source_ingestion_id': record.id, 'parent_id': id_map.get(chapter.parent_id or ''), 'catalog_version': 0, 'metadata': {**chapter.metadata, 'reused_from_source_ingestion_id': reusable_structure.source_ingestion_id, 'reuse_basis': 'exact_source_content_hash'}}))
            _validate_chapters(cloned_chapters)
            reused_structure = reusable_structure.model_copy(update={'id': new_id('structure'), 'owner_user_id': record.owner_user_id, 'package_id': record.package_id, 'source_ingestion_id': record.id, 'catalog_version': 0, 'catalog_updated_at': None, 'created_at': now_iso(), 'updated_at': now_iso(), 'metadata': {**reusable_structure.metadata, 'reused_from_source_ingestion_id': reusable_structure.source_ingestion_id, 'reuse_basis': 'exact_source_content_hash'}})
            succeeded_run = run.model_copy(update={'status': 'succeeded', 'model': '', 'turn_count': 0, 'chapter_count': len(cloned_chapters), 'verified_chapter_count': len(cloned_chapters), 'verification_rate': 1.0, 'duration_ms': max(0, round((time.perf_counter() - started) * 1000)), 'stage_history': [*run.stage_history, 'reused_exact_verified_catalog', 'publishing_catalog', 'succeeded'], 'completed_at': now_iso(), 'metadata': {**run.metadata, 'reused_from_source_ingestion_id': reusable_structure.source_ingestion_id, 'reuse_basis': 'exact_source_content_hash'}})
            published = self.store.publish_catalog(structure=reused_structure, chapters=cloned_chapters, run=succeeded_run)
            _report(progress_callback, 'catalog_ready', 99)
            return published
        _report(progress_callback, 'reading_directory_metadata', 30)
        direct_catalog = None if force_content_rebuild else _generate_complete_native_pdf_catalog(record=record, source_path=path, source_content_hash=content_hash, on_activity=activity_callback)
        native_pdf_catalog = direct_catalog is not None
        discovery_mode = 'authored_navigation' if native_pdf_catalog else 'content_rebuild'
        if direct_catalog is None:
            _report(progress_callback, 'native_pi_content_rebuild', 30)
            direct_catalog = generate_native_pi_directory(record=record, source_path=path, source_content_hash=content_hash, selection=catalog_model, on_activity=activity_callback, discovery_mode=discovery_mode)
        else:
            _report(progress_callback, 'native_pdf_navigation', 72)
        native_pi_total_turn_count = direct_catalog.turn_count
        native_pi_continuation_turns = 0
        stagnant_continuations = 0
        progress_signature = _native_pi_mapping_progress_signature(direct_catalog.audit_metadata)
        if force_content_rebuild and direct_catalog.audit_metadata.get('mapping_status') != 'complete':
            raise SourceDirectoryProcessingError('The refreshed Native Pi directory is not fully citable; the previous catalog was preserved.')
        while not native_pdf_catalog and (not force_content_rebuild) and (path.suffix.lower() == '.pdf') and (direct_catalog.audit_metadata.get('mapping_status') != 'complete'):
            checkpoint = direct_catalog.audit_metadata.get('native_pi_directory_payload')
            if not isinstance(checkpoint, dict):
                raise SourceDirectoryProcessingError('The source Agent returned an incomplete PDF directory without a reusable checkpoint.')
            hierarchy_gap_keys = tuple((str(key) for key in direct_catalog.audit_metadata.get('hierarchy_gap_keys', []) if str(key)))
            unresolved_keys = tuple(dict.fromkeys([*_native_pi_unresolved_node_keys(direct_catalog.chapters), *hierarchy_gap_keys]))
            run = self.store.save_catalog_run(run.model_copy(update={'turn_count': native_pi_total_turn_count, 'stage_history': [*run.stage_history, 'native_pi_mapping_checkpoint'], 'metadata': {**run.metadata, 'native_pi_directory_payload': checkpoint, 'unresolved_node_keys': list(unresolved_keys), 'unresolved_node_count': len(unresolved_keys), 'auto_continuing': True}}))
            if activity_callback is not None:
                total_node_count = len(direct_catalog.chapters)
                verified_node_count = max(0, total_node_count - len(unresolved_keys))
                activity_callback(AgentActivityEvent(turn_id=new_id('catalogprogress'), stage='execute_role', label=f'Content ranges mapped: {verified_node_count}/{total_node_count}; {len(unresolved_keys)} entries remaining', status='running', role='OpenClass', metadata={'kind': 'sourceCatalogProgress', 'source_progress': {'phase': 'continuing_pdf_range_mapping', 'label': 'Completing catalog page mapping', 'detail': f'Mapped {verified_node_count}/{total_node_count} entries; {len(unresolved_keys)} remaining', 'determinate': True, 'completed': verified_node_count, 'total': total_node_count, 'remaining': len(unresolved_keys), 'unit': 'ranges', 'heartbeat_at': now_iso()}}))
            _report(progress_callback, 'continuing_pdf_range_mapping', 84)
            next_catalog = generate_native_pi_directory(record=record, source_path=path, source_content_hash=content_hash, selection=catalog_model, on_activity=activity_callback, previous_catalog=checkpoint, unresolved_node_keys=unresolved_keys, discovery_mode=discovery_mode)
            native_pi_total_turn_count += next_catalog.turn_count
            native_pi_continuation_turns += 1
            next_signature = _native_pi_mapping_progress_signature(next_catalog.audit_metadata)
            stagnant_continuations = stagnant_continuations + 1 if next_signature == progress_signature else 0
            direct_catalog = next_catalog
            progress_signature = next_signature
            if direct_catalog.audit_metadata.get('mapping_status') != 'complete' and stagnant_continuations >= 2:
                raise SourceDirectoryProcessingError('The source Agent could not resolve the remaining PDF directory ranges after two continuation turns without structural progress. The catalog was not published as ready.')
        chapters = list(direct_catalog.chapters)
        execution_metadata = dict(direct_catalog.audit_metadata)
        execution_metadata.update({'source_agent_total_turn_count': native_pi_total_turn_count, 'native_pi_continuation_turn_count': native_pi_continuation_turns, 'auto_continuing': False, 'forced_ai_rebuild': force_content_rebuild, 'native_state': 'skipped' if discovery_mode == 'content_rebuild' else execution_metadata.get('native_state', 'used')})
        task_contract = str(execution_metadata.get('catalog_task_contract') or '')
        if task_contract in {'native_pi_citable_directory_v2', 'native_pi_content_rebuild_v1'}:
            _report(progress_callback, 'calibrating_pdf_p', 72)
            _report(progress_callback, 'mapping_directory_to_pages', 84)
        catalog_stage = 'native_pdf_navigation' if native_pdf_catalog else 'native_pi_content_rebuild' if discovery_mode == 'content_rebuild' else 'native_pi_directory'
        stage_history = [*run.stage_history, catalog_stage]
        has_authoritative_ranges = any((chapter.mapping_status == 'verified' and chapter.range is not None and chapter.catalog_evidence for chapter in chapters))
        stage_history.append('directory_and_ranges_verified' if has_authoritative_ranges else 'directory_list_completed')
        run = self.store.save_catalog_run(run.model_copy(update={'stage_history': stage_history, 'metadata': {**run.metadata, **execution_metadata}}))
        warnings = [] if chapters else ['The source agent returned an empty directory list.']
        if task_contract == 'native_pi_citable_directory_v2' and execution_metadata.get('mapping_status') == 'partial':
            warnings.append(str(execution_metadata.get('mapping_reason') or 'Some PDF ranges could not be proven.'))
        witness_count = _positive_catalog_count(execution_metadata.get('catalog_completeness_witness_node_count'))
        catalog_completeness_proven = path.suffix.lower() != '.pdf' or witness_count >= 2 or task_contract in {'native_pi_directory_list_v1', 'native_pi_citable_directory_v2', 'native_pi_content_rebuild_v1'}
        if chapters and (not catalog_completeness_proven):
            warnings.append('The saved PDF nodes have verified ranges, but no independent whole-directory witness was available; the catalog remains partially trusted.')
        structure_execution_metadata = {key: value for key, value in execution_metadata.items() if key not in {'codex_directory_payload', 'codex_raw_output'}}
        validation_stage = 'validating_directory_ranges' if has_authoritative_ranges else 'validating_directory'
        _report(progress_callback, validation_stage, 92)
        _validate_chapters(chapters)
        verified_count = sum((chapter.mapping_status == 'verified' for chapter in chapters))
        quality = _catalog_quality(chapters, catalog_complete=catalog_completeness_proven, directory_only_complete=execution_metadata.get('catalog_task_contract') in {'directory_pages_offset_tree_v1', 'native_pi_directory_list_v1'})
        is_native_pi_directory = task_contract == 'native_pi_directory_list_v1'
        is_native_pi_citable = task_contract in {'native_pi_citable_directory_v2', 'native_pi_content_rebuild_v1'}
        is_content_rebuild = task_contract == 'native_pi_content_rebuild_v1'
        mapping_complete = bool(chapters) and verified_count == len(chapters)
        index_status = 'partial' if is_native_pi_directory else 'complete' if mapping_complete else 'partial'
        work_state = 'partial' if is_native_pi_citable and (not mapping_complete) else 'satisfied'
        structure = SourceStructure(owner_user_id=record.owner_user_id, package_id=record.package_id, source_ingestion_id=record.id, status='ready' if chapters else 'linear_only', strategy='codex_directory_v1', has_verified_toc=bool(chapters), quality=quality, chapter_count=len(chapters), chunk_count=0, visual_count=0, visual_index_status='unsupported', visual_index_version=0, confidence=quality.confidence, source_content_hash=content_hash, catalog_schema_version=CATALOG_SCHEMA_VERSION, catalog_model='' if native_pdf_catalog else catalog_model.model, warnings=warnings, metadata={'catalog_pipeline': CATALOG_SCHEMA_VERSION, 'content_hash': content_hash, **({} if native_pdf_catalog else {'catalog_model_selection': catalog_model.model_dump(mode='json')}), **structure_execution_metadata, 'work_state': work_state, 'phase': 'terminal', 'directory_status': 'complete' if catalog_completeness_proven else 'uncertain', 'index_status': index_status, 'unresolved_node_count': 0 if is_native_pi_directory else len(chapters) - verified_count, 'can_refine': False, 'auto_continuing': False, 'background_refine_active': False, 'body_text_extracted': False, 'source_chunks_created': False, 'vector_index_created': False, 'visual_index_created': False, 'open_notebook_called': False, 'completion_reason': 'The directory was rebuilt from directly observed body headings and every PDF range was verified.' if is_content_rebuild and mapping_complete else 'The authored directory and every PDF range were mechanically derived from proven P anchors.' if is_native_pi_citable and mapping_complete else 'The authored directory was preserved, but some PDF ranges could not be proven.' if is_native_pi_citable else 'The authored directory list was saved.'})
        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        succeeded_run = run.model_copy(update={'status': 'succeeded', 'model': '' if native_pdf_catalog else catalog_model.model, 'turn_count': native_pi_total_turn_count, 'chapter_count': len(chapters), 'verified_chapter_count': verified_count, 'verification_rate': verified_count / len(chapters) if chapters else 0.0, 'duration_ms': duration_ms, 'stage_history': [*run.stage_history, validation_stage, 'publishing_catalog', 'succeeded'], 'completed_at': now_iso(), 'metadata': {**run.metadata, **execution_metadata, 'warning_count': len(warnings)}})
        _report(progress_callback, 'publishing_catalog', 97)
        if _file_hash(path) != content_hash:
            raise SourceDirectoryProcessingError('The source file changed while its directory catalog was being built.')
        published = self.store.publish_catalog(structure=structure, chapters=chapters, run=succeeded_run)
        try:
            _report(progress_callback, 'catalog_ready', 99)
        except Exception:
            pass
        return published

    def _process_agent_catalog(self, *, record: SourceIngestionRecord, path: Path, content_hash: str, catalog_model: AIModelSelection, run: SourceCatalogRun, started: float, progress_callback: CatalogProgressCallback | None, activity_callback: Callable[[AgentActivityEvent], None] | None, resume_catalog: bool) -> SourceStructure:
        checkpoint = self._agent_catalog_checkpoint(record, path=path) if resume_catalog else None
        _report(progress_callback, 'source_agent_working', 30)
        latest_result = generate_agent_catalog_turn(record=record, source_path=path, source_content_hash=content_hash, selection=catalog_model, initial_catalog=checkpoint, advisory_observations=_agent_catalog_observations(checkpoint), timeout_seconds=AUTONOMOUS_CATALOG_BUDGET_SECONDS, on_activity=activity_callback)
        total_turn_count = latest_result.turn_count
        while latest_result.catalog_payload is not None and latest_result.catalog_payload.get('directory_status') != 'complete' and (latest_result.work_state == 'working'):
            checkpoint = latest_result.catalog_payload
            run = self.store.save_catalog_run(run.model_copy(update={'turn_count': total_turn_count, 'stage_history': [*run.stage_history, 'directory_discovery_checkpoint'], 'metadata': {**run.metadata, 'agent_catalog_payload': checkpoint}}))
            latest_result = generate_agent_catalog_turn(record=record, source_path=path, source_content_hash=content_hash, selection=catalog_model, initial_catalog=checkpoint, advisory_observations=_agent_catalog_observations(checkpoint), timeout_seconds=AUTONOMOUS_CATALOG_BUDGET_SECONDS, on_activity=activity_callback)
            total_turn_count += latest_result.turn_count
        chapters = list(latest_result.chapters)
        _validate_chapters(chapters)
        verified_count = sum((chapter.mapping_status == 'verified' for chapter in chapters))
        checkpoint = latest_result.catalog_payload or _legacy_agent_catalog_payload(chapters, summary=latest_result.summary, next_plan=latest_result.next_plan, stop_reason=latest_result.stop_reason)
        work_state = str(checkpoint['work_state'])
        directory_status = str(checkpoint['directory_status'])
        index_status = str(checkpoint['index_status'])
        if directory_status != 'complete':
            self.store.save_catalog_run(run.model_copy(update={'turn_count': total_turn_count, 'metadata': {**run.metadata, 'agent_catalog_payload': checkpoint}}))
            raise SourceDirectoryProcessingError('The source Agent stopped before the complete authored directory was verified.')
        _publish_agent_catalog_stage_activity(callback=activity_callback, directory_status=directory_status, index_status=index_status, work_state=work_state, chapter_count=len(chapters), verified_count=verified_count, payload=checkpoint)
        quality = _catalog_quality(chapters, catalog_complete=directory_status == 'complete', directory_only_complete=False)
        execution_metadata = dict(latest_result.audit_metadata)
        execution_metadata.update({'agent_catalog_payload': checkpoint, 'work_state': work_state, 'phase': checkpoint['phase'], 'directory_status': directory_status, 'index_status': index_status, 'summary': checkpoint['summary'], 'next_plan': checkpoint['next_plan'], 'next_action': checkpoint['next_action'], 'stop_reason': checkpoint['stop_reason'], 'completion_reason': checkpoint['completion_reason'], 'directory_gaps': checkpoint['directory_gaps'], 'remaining_work': checkpoint.get('remaining_work', []), 'remaining_work_count': len(checkpoint.get('remaining_work', [])), 'snapshot_reason': checkpoint.get('snapshot_reason', 'budget_increment'), 'progress_fingerprint': checkpoint.get('progress_fingerprint', ''), 'no_progress_turns': checkpoint.get('no_progress_turns', 0), 'attempted_action_fingerprints': checkpoint['attempted_action_fingerprints']})
        warnings = [] if chapters else ['The current agent snapshot has no directory nodes yet.']
        unresolved_count = len(chapters) - verified_count
        structure = SourceStructure(owner_user_id=record.owner_user_id, package_id=record.package_id, source_ingestion_id=record.id, status='ready' if chapters else 'linear_only', strategy='codex_directory_v1', has_verified_toc=bool(chapters) and directory_status == 'complete', quality=quality, chapter_count=len(chapters), chunk_count=0, visual_count=0, visual_index_status='unsupported', visual_index_version=0, confidence=quality.confidence, source_content_hash=content_hash, catalog_schema_version=CATALOG_SCHEMA_VERSION, catalog_model=catalog_model.model, warnings=warnings, metadata={'catalog_pipeline': CATALOG_SCHEMA_VERSION, 'content_hash': content_hash, 'catalog_model_selection': catalog_model.model_dump(mode='json'), **execution_metadata, 'work_state': work_state, 'summary': checkpoint['summary'], 'next_plan': checkpoint['next_plan'], 'next_action': checkpoint['next_action'], 'stop_reason': checkpoint['stop_reason'], 'completion_reason': checkpoint['completion_reason'], 'phase': checkpoint['phase'], 'directory_status': directory_status, 'index_status': index_status, 'unresolved_node_count': unresolved_count, 'can_refine': work_state in {'paused', 'partial'}, 'auto_continuing': work_state == 'working', 'background_refine_active': work_state == 'working', 'body_text_extracted': False, 'source_chunks_created': False, 'vector_index_created': False, 'visual_index_created': False})
        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        run_status = 'running' if work_state == 'working' else 'succeeded'
        run = self.store.save_catalog_run(run.model_copy(update={'status': run_status, 'turn_count': total_turn_count, 'chapter_count': len(chapters), 'verified_chapter_count': verified_count, 'verification_rate': verified_count / len(chapters) if chapters else 0.0, 'duration_ms': duration_ms, 'stage_history': [*run.stage_history, 'agent_snapshot_published'], 'completed_at': now_iso() if run_status == 'succeeded' else None, 'metadata': {**run.metadata, **execution_metadata}}))
        if _file_hash(path) != content_hash:
            raise SourceDirectoryProcessingError('The source file changed while its directory catalog was being built.')
        published = self.store.publish_catalog(structure=structure, chapters=chapters, run=run)
        ai_usage_logger.log_event('source_catalog_snapshot_published', owner_user_id=record.owner_user_id, package_id=record.package_id, source_ingestion_id=record.id, catalog_version=published.catalog_version, chapter_count=len(chapters), verified_chapter_count=verified_count, directory_status=directory_status, index_status=index_status, first_catalog_available_ms=max(0, round((time.perf_counter() - started) * 1000)) if published.catalog_version == 1 else None)
        if activity_callback is not None and published.has_verified_toc:
            background_active = work_state == 'working'
            label = f'Catalog version {published.catalog_version} is available; content mapping continues' if background_active else f'Catalog version {published.catalog_version} is complete'
            activity_callback(AgentActivityEvent(turn_id=new_id('catalogpublished'), stage='execute_role', label=label, status='running' if background_active else 'completed', role='OpenClass', metadata={'kind': 'sourceCatalogPublished', 'detail': f'Content ranges: {verified_count}/{len(chapters)}', 'source_progress': {'phase': 'background_catalog_refine' if background_active else 'terminal', 'label': label, 'detail': f'Content ranges: {verified_count}/{len(chapters)}', 'determinate': bool(chapters), 'completed': verified_count, 'total': len(chapters), 'unit': 'ranges', 'catalog_version': published.catalog_version, 'heartbeat_at': now_iso()}}))
        _report(progress_callback, 'catalog_snapshot_available', 45)
        if self.store.catalog_pause_requested(owner_user_id=record.owner_user_id, package_id=record.package_id, source_id=record.id):
            return self._publish_host_pause(structure=published, chapters=chapters, run=run, reason="Paused at the user's request; automatic continuation can resume from this snapshot.")
        return published

    def _agent_catalog_checkpoint(self, record: SourceIngestionRecord, *, path: Path) -> dict[str, object] | None:
        view = self.store.get_structure_view(source=record, chunk_limit=0)
        payload = view.structure.metadata.get('agent_catalog_payload') if view.structure else None
        if isinstance(payload, dict):
            try:
                return coerce_agent_catalog_v3_checkpoint(payload, source_path=path)
            except (TypeError, ValueError):
                pass
        for prior_run in self.store.list_catalog_runs(owner_user_id=record.owner_user_id, package_id=record.package_id, source_id=record.id):
            payload = prior_run.metadata.get('agent_catalog_payload')
            if not isinstance(payload, dict):
                continue
            try:
                return coerce_agent_catalog_v3_checkpoint(payload, source_path=path)
            except (TypeError, ValueError):
                continue
        if not view.chapters:
            return None
        keys_by_id: dict[str, str] = {}
        nodes: list[dict[str, object]] = []
        for index, chapter in enumerate(view.chapters):
            key = str(chapter.metadata.get('agent_catalog_key') or chapter.metadata.get('codex_node_key') or f'legacy.{index + 1}')
            keys_by_id[chapter.id] = key
            nodes.append(AgentCatalogV3Node(key=key, parent_key=keys_by_id.get(chapter.parent_id or ''), number=chapter.number, title=chapter.title, level=chapter.level, source_locator=chapter.source_locator, mapping_status='verified' if chapter.mapping_status == 'verified' and chapter.range else 'unmapped', mapping_reason=str(chapter.metadata.get('mapping_reason') or 'Imported from the previous catalog snapshot.'), source_range=chapter.range.model_dump(mode='json') if chapter.range else None, evidence=[item.model_dump(mode='json') for item in chapter.catalog_evidence], locator_source='legacy_range' if chapter.range else 'unmapped').model_dump(mode='json'))
        return AgentCatalogV3(schema_version='agent_catalog_v3', phase='directory_discovery', directory_status='uncertain', index_status='in_progress' if any((node.get('source_range') for node in nodes)) else 'pending', work_state='working', summary='Imported the previous catalog as a revisable checkpoint.', next_plan='Prove directory completeness without scanning body content.', next_action='inspect authored navigation and bounded directory candidates', stop_reason='', completion_reason='', directory_gaps=['Legacy catalog has no structured directory-completeness evidence.'], nodes=nodes).model_dump(mode='json')

    def _publish_host_pause(self, *, structure: SourceStructure, chapters: list[SourceChapter], run: SourceCatalogRun, reason: str) -> SourceStructure:
        payload = dict(structure.metadata.get('agent_catalog_payload') or {})
        payload.update({'phase': 'terminal', 'work_state': 'paused', 'stop_reason': reason, 'completion_reason': 'Catalog processing was interrupted outside the normal stop rules.'})
        paused = structure.model_copy(update={'metadata': {**structure.metadata, 'agent_catalog_payload': payload, 'work_state': 'paused', 'stop_reason': reason, 'can_refine': True, 'pause_requested': False}})
        paused_run = run.model_copy(update={'status': 'succeeded', 'completed_at': now_iso(), 'stage_history': [*run.stage_history, 'paused']})
        return self.store.publish_catalog(structure=paused, chapters=chapters, run=paused_run)

def _publish_agent_catalog_stage_activity(*, callback: Callable[[AgentActivityEvent], None] | None, directory_status: str, index_status: str, work_state: str, chapter_count: int, verified_count: int, payload: dict[str, object]) -> None:
    if callback is None:
        return
    regimes = payload.get('pagination_regimes')
    regime_count = len(regimes) if isinstance(regimes, list) else 0
    native_count = sum((1 for node in payload.get('nodes', []) if isinstance(node, dict) and node.get('locator_source') == 'native_navigation'))
    stages = (('directory_discovery', 'Confirm catalog boundaries', f'Catalog status: {directory_status} · {chapter_count} entries'), ('page_calibration', 'Calibrate page offsets', 'PDF page targets from native bookmarks' if native_count == chapter_count and chapter_count else f'{regime_count} exact page-offset segments'), ('range_mapping', 'Generate content ranges', f'Mapped {verified_count}/{chapter_count} entries'), ('validation', 'Verify index', f'Index status: {index_status} · Final task status: {work_state}'))
    turn_id = new_id('catalogstage')
    for sequence, (phase, label, detail) in enumerate(stages, start=1):
        callback(AgentActivityEvent(id=f'{turn_id}:{sequence}', turn_id=turn_id, stage='execute_role', label=label, status='completed', role='pi', metadata={'kind': 'sourceCatalogStage', 'detail': detail, 'source_progress': {'phase': phase, 'label': label, 'detail': detail, 'determinate': True}}))

def _legacy_agent_catalog_payload(chapters: Sequence[SourceChapter], *, summary: str, next_plan: str, stop_reason: str) -> dict[str, object]:
    """Keep audited v1 results readable while production defaults to v3."""
    keys_by_id: dict[str, str] = {}
    nodes: list[AgentCatalogV3Node] = []
    for index, chapter in enumerate(chapters):
        key = str(chapter.metadata.get('agent_catalog_key') or chapter.metadata.get('codex_node_key') or f'legacy.{index + 1}')
        keys_by_id[chapter.id] = key
        nodes.append(AgentCatalogV3Node(key=key, parent_key=keys_by_id.get(chapter.parent_id or ''), number=chapter.number, title=chapter.title, level=chapter.level, source_locator=chapter.source_locator, mapping_status='verified' if chapter.range else 'unmapped', mapping_reason=str(chapter.metadata.get('mapping_reason') or 'Imported from a previously audited catalog result.'), source_range=CodexDirectSourceRange(kind=chapter.range.kind, start=chapter.range.start, end=chapter.range.end, container=chapter.range.container, start_anchor=chapter.range.start_anchor, end_anchor=chapter.range.end_anchor, display_label=chapter.range.display_label) if chapter.range else None, evidence=[CodexDirectCatalogEvidence(method=item.method, source_locator=item.source_locator, page_start=item.page_start, page_end=item.page_end, excerpt=item.excerpt, confidence=item.confidence) for item in chapter.catalog_evidence], locator_source='legacy_range' if chapter.range else 'unmapped'))
    all_mapped = bool(nodes) and all((node.source_range is not None for node in nodes))
    return AgentCatalogV3(schema_version='agent_catalog_v3', phase='terminal', directory_status='complete', index_status='complete' if all_mapped else 'partial', work_state='satisfied' if all_mapped else 'partial', summary=summary or 'Imported a previously audited catalog result.', next_plan=next_plan, next_action='', stop_reason=stop_reason or ('' if all_mapped else 'Some legacy nodes have no trusted range.'), completion_reason='Converted trusted stable nodes and ranges without a database migration.', directory_evidence=[AgentCatalogDirectoryEvidence(kind='authored_navigation_exhausted', detail='The previous catalog was already published as a complete authored directory.')], nodes=nodes).model_dump(mode='json')

def _agent_catalog_observations(checkpoint: dict[str, object] | None) -> dict[str, object]:
    if not checkpoint:
        return {'node_count': 0, 'max_depth': 0, 'unmapped_node_count': 0, 'note': 'No prior snapshot exists; these observations do not prescribe a starting method.'}
    raw_nodes = checkpoint.get('nodes')
    nodes = raw_nodes if isinstance(raw_nodes, list) else []
    mapped_spans: list[tuple[str, int]] = []
    for raw_node in nodes:
        if not isinstance(raw_node, dict):
            continue
        source_range = raw_node.get('source_range')
        if not isinstance(source_range, dict):
            continue
        start = source_range.get('start')
        end = source_range.get('end')
        if isinstance(start, int) and isinstance(end, int) and (end >= start):
            mapped_spans.append((str(raw_node.get('key') or ''), end - start + 1))
    mapped_spans.sort(key=lambda item: item[1], reverse=True)
    return {'node_count': len(nodes), 'max_depth': max((int(node.get('level') or 0) for node in nodes if isinstance(node, dict)), default=0), 'unmapped_node_count': sum((1 for node in nodes if isinstance(node, dict) and node.get('mapping_status', 'unmapped') != 'verified')), 'largest_mapped_spans': [{'key': key, 'span': span} for key, span in mapped_spans[:8]], 'previous_summary': str(checkpoint.get('summary') or ''), 'previous_next_plan': str(checkpoint.get('next_plan') or ''), 'phase': str(checkpoint.get('phase') or 'directory_discovery'), 'directory_status': str(checkpoint.get('directory_status') or 'uncertain'), 'index_status': str(checkpoint.get('index_status') or 'pending'), 'directory_gaps': checkpoint.get('directory_gaps') or [], 'remaining_work': checkpoint.get('remaining_work') or [], 'snapshot_reason': checkpoint.get('snapshot_reason') or 'budget_increment', 'no_progress_turns': checkpoint.get('no_progress_turns') or 0, 'attempted_action_fingerprints': checkpoint.get('attempted_action_fingerprints') or [], 'note': 'Resume only the typed remaining work; verified nodes are frozen unless conflict resolution is already persisted.'}

def _bounded_candidate_batches(candidates: Sequence[DirectoryCandidate]) -> list[list[DirectoryCandidate]]:
    batches: list[list[DirectoryCandidate]] = []
    pending: list[DirectoryCandidate] = []
    pending_chars = 0
    for candidate in candidates:
        candidate_chars = len(json.dumps(_candidate_packet(candidate), ensure_ascii=False))
        if candidate_chars > MAX_CODEX_BATCH_CHARS:
            raise SourceDirectoryProcessingError('One directory node exceeds the bounded Codex packet limit.')
        if pending and (len(pending) >= MAX_CODEX_BATCH_NODES or pending_chars + candidate_chars > MAX_CODEX_BATCH_CHARS):
            batches.append(pending)
            pending = []
            pending_chars = 0
        pending.append(candidate)
        pending_chars += candidate_chars
    if pending:
        batches.append(pending)
    return batches

def _reclose_normalized_ranges(candidates: Sequence[DirectoryCandidate], *, extraction: DirectoryExtraction) -> tuple[DirectoryCandidate, ...]:
    format_name = str(extraction.metadata.get('format') or '')
    maximum_by_kind: dict[str, int] = {}
    if format_name == 'pdf' and extraction.page_count:
        maximum_by_kind['pdf_pages'] = extraction.page_count
    elif format_name == 'epub':
        maximum_by_kind['epub_spine'] = max(0, int(extraction.metadata.get('spine_count') or 0) - 1)
    elif format_name == 'docx':
        maximum_by_kind['docx_paragraphs'] = max(0, int(extraction.metadata.get('paragraph_count') or 0) - 1)
    elif format_name == 'pptx':
        maximum_by_kind['ppt_slides'] = max(1, int(extraction.metadata.get('slide_count') or extraction.page_count or 1))
    elif format_name in {'markdown', 'text'}:
        maximum_by_kind['text_lines'] = max(1, int(extraction.metadata.get('line_count') or 1))
    elif format_name == 'csv':
        maximum_by_kind['sheet_rows'] = max(1, int(extraction.metadata.get('row_count') or 1))
    elif format_name == 'html':
        maximum_by_kind['dom_anchor'] = max((int(candidate.source_range.end) for candidate in extraction.candidates if candidate.source_range is not None and candidate.source_range.kind == 'dom_anchor' and isinstance(candidate.source_range.end, int)), default=0)
    result: list[DirectoryCandidate] = []
    for index, candidate in enumerate(candidates):
        source_range = candidate.source_range
        if source_range is not None and source_range.kind == 'epub_spine' and _is_hierarchy_locked_navigation(candidate):
            result.append(candidate)
            continue
        if source_range is None or source_range.kind not in maximum_by_kind or (not isinstance(source_range.start, int)):
            result.append(candidate)
            continue
        boundary_index = next((following_index for following_index in range(index + 1, len(candidates)) if candidates[following_index].level <= candidate.level), len(candidates))
        boundary = candidates[boundary_index] if boundary_index < len(candidates) else None
        boundary_range = boundary.source_range if boundary is not None else None
        if boundary is not None and (boundary.mapping_status != 'verified' or not _range_is_same_series(source_range, boundary_range) or (not isinstance(boundary_range.start, int)) or (boundary_range.start < source_range.start)):
            result.append(replace(candidate, mapping_status='partial', confidence=min(candidate.confidence, 0.64), metadata={**candidate.metadata, 'range_boundary_status': 'unverified_successor', 'range_boundary_local_key': boundary.local_key}))
            continue
        descendant_start: int | None = None
        descendant_end: int | None = None
        if candidate.mapping_status == 'verified' and _is_numeric_containment_range(source_range):
            descendant_ranges = [descendant.source_range for descendant in candidates[index + 1:boundary_index] if descendant.mapping_status == 'verified' and _is_numeric_containment_range(descendant.source_range) and _range_is_same_series(source_range, descendant.source_range)]
            if descendant_ranges:
                descendant_start = min((int(descendant.start) for descendant in descendant_ranges))
                descendant_end = max((int(descendant.end) for descendant in descendant_ranges))
        next_start = int(boundary_range.start) if boundary_range is not None and isinstance(boundary_range.start, int) else None
        if descendant_start is not None and (descendant_start < int(source_range.start) or (next_start is not None and descendant_end is not None and (descendant_end > next_start))):
            result.append(replace(candidate, mapping_status='partial', confidence=min(candidate.confidence, 0.64), metadata={**candidate.metadata, 'range_boundary_status': 'descendant_precedes_parent' if descendant_start < int(source_range.start) else 'descendant_crosses_successor', **({'range_boundary_local_key': boundary.local_key} if boundary is not None else {})}))
            continue
        maximum = maximum_by_kind[source_range.kind]
        boundary_anchor = boundary_range.start_anchor if boundary_range is not None else ''
        end = maximum if next_start is None else max(source_range.start, next_start if source_range.kind == 'epub_spine' and boundary_anchor else next_start - 1)
        if descendant_end is not None:
            end = max(end, descendant_end)
        updates: dict[str, object] = {'end': end}
        display_label = _normalized_range_label(source_range.kind, source_range.start, end)
        if display_label:
            updates['display_label'] = display_label
        if source_range.kind == 'epub_spine':
            updates['end_anchor'] = boundary_anchor
        elif source_range.kind == 'dom_anchor':
            updates['end_anchor'] = boundary_range.start_anchor if boundary_range is not None else ''
            updates['metadata'] = {**source_range.metadata, 'end_heading_ordinal': next_start if next_start is not None else maximum + 1}
        result.append(replace(candidate, source_range=source_range.model_copy(update=updates)))
    return tuple(result)

def _is_numeric_containment_range(source_range: SourceRange | None) -> bool:
    return bool(source_range is not None and source_range.kind in NUMERIC_CONTAINMENT_RANGE_KINDS and isinstance(source_range.start, int) and (not isinstance(source_range.start, bool)) and isinstance(source_range.end, int) and (not isinstance(source_range.end, bool)))

def _range_is_same_series(source_range: SourceRange, following_range: SourceRange | None) -> bool:
    if source_range is None or following_range is None:
        return False
    if source_range.kind != following_range.kind:
        return False
    if source_range.kind in {'sheet_rows', 'dom_anchor'}:
        return source_range.container == following_range.container
    return isinstance(following_range.start, int)

def _normalized_range_label(kind: str, start: int, end: int) -> str:
    if kind == 'pdf_pages':
        return f'PDF p. {start}' if start == end else f'PDF pp. {start}-{end}'
    if kind == 'text_lines':
        return f'Line {start}' if start == end else f'Lines {start}-{end}'
    if kind == 'docx_paragraphs':
        return f'Paragraph {start + 1}' if start == end else f'Paragraphs {start + 1}-{end + 1}'
    if kind == 'ppt_slides':
        return f'Slide {start}' if start == end else f'Slides {start}-{end}'
    if kind == 'sheet_rows':
        return f'Row {start}' if start == end else f'Rows {start}-{end}'
    if kind == 'epub_spine':
        return f'EPUB spine {start}' if start == end else f'EPUB spine {start}-{end}'
    return ''

def _candidate_packet(candidate: DirectoryCandidate) -> dict[str, object]:
    source_range = candidate.source_range
    return {'local_key': candidate.local_key, 'title': candidate.title[:300], 'number': candidate.number[:80], 'level': candidate.level, 'order_index': candidate.order_index, 'source_locator': candidate.source_locator[:500], 'source_range': {'kind': source_range.kind, 'start': source_range.start, 'end': source_range.end, 'container': source_range.container[:300], 'display_label': source_range.display_label[:300], 'path_depth': len(source_range.path), 'end_inclusive': source_range.end_inclusive} if source_range is not None else None, 'mapping_status': candidate.mapping_status, 'confidence': candidate.confidence, 'navigation_provenance': candidate.metadata.get('navigation_provenance'), 'hierarchy_locked': bool(candidate.metadata.get('hierarchy_locked')), 'native_level': candidate.metadata.get('native_level'), 'evidence': [{'method': item.method[:120], 'source_locator': item.source_locator[:500], 'page_start': item.page_start, 'page_end': item.page_end, 'excerpt': item.excerpt[:300], 'confidence': item.confidence} for item in candidate.evidence[:4]]}

def _apply_batch_decision(candidates: Sequence[DirectoryCandidate], result: DirectoryBatchDecision, *, expected_hash: str) -> list[DirectoryCandidate]:
    if result.batch_hash != expected_hash:
        raise SourceDirectoryProcessingError('Codex returned decisions for a different directory packet.')
    expected_keys = [candidate.local_key for candidate in candidates]
    decisions_by_key = {decision.local_key: decision for decision in result.decisions}
    if len(decisions_by_key) != len(result.decisions) or set(decisions_by_key) != set(expected_keys):
        raise SourceDirectoryProcessingError('Codex must decide every directory candidate exactly once.')
    normalized: list[DirectoryCandidate] = []
    for candidate in candidates:
        decision = decisions_by_key[candidate.local_key]
        hierarchy_locked = _is_hierarchy_locked_navigation(candidate)
        keep = hierarchy_locked or decision.keep or (not bool(candidate.metadata.get('codex_may_reject')))
        if not keep:
            continue
        title = ' '.join((decision.title or candidate.title).split()).strip()
        if not title:
            raise SourceDirectoryProcessingError('Codex returned a kept directory node without a title.')
        normalized.append(replace(candidate, title=title, number=' '.join((decision.number or candidate.number).split()).strip(), level=int(candidate.metadata['native_level']) if hierarchy_locked else max(1, min(12, decision.level))))
    return normalized

def _is_hierarchy_locked_navigation(candidate: DirectoryCandidate) -> bool:
    native_level = candidate.metadata.get('native_level')
    return bool(candidate.metadata.get('hierarchy_locked') and candidate.metadata.get('navigation_provenance') == 'native' and isinstance(native_level, int) and (not isinstance(native_level, bool)))

def _validate_locked_navigation_invariants(original: Sequence[DirectoryCandidate], normalized: Sequence[DirectoryCandidate]) -> None:
    expected = [candidate for candidate in original if _is_hierarchy_locked_navigation(candidate)]
    if not expected:
        return
    actual = [candidate for candidate in normalized if _is_hierarchy_locked_navigation(candidate)]
    if len(actual) != len(expected):
        raise SourceDirectoryProcessingError('Native navigation normalization changed the number of hierarchy-locked nodes.')
    for before, after in zip(expected, actual, strict=True):
        native_level = int(before.metadata['native_level'])
        if after.local_key != before.local_key or after.order_index != before.order_index or after.source_locator != before.source_locator or (after.source_range != before.source_range) or (after.metadata.get('native_level') != native_level) or (after.level != native_level):
            raise SourceDirectoryProcessingError('Native navigation normalization violated a hierarchy-locked host invariant.')

def _materialize_chapters(*, record: SourceIngestionRecord, candidates: Sequence[DirectoryCandidate], content_hash: str, catalog_pipeline: str=CATALOG_SCHEMA_VERSION) -> list[SourceChapter]:
    chapters: list[SourceChapter] = []
    level_stack: list[SourceChapter] = []
    chapter_by_proposal_key: dict[str, SourceChapter] = {}
    semantic_occurrences: Counter[tuple[tuple[str, ...], str, str, int]] = Counter()
    for order_index, candidate in enumerate(candidates):
        level = max(1, candidate.level)
        while level_stack and level_stack[-1].level >= level:
            level_stack.pop()
        explicit_parent_key = str(candidate.metadata.get('attached_native_parent_key') or candidate.metadata.get('proposal_parent_key') or '')
        parent = chapter_by_proposal_key.get(explicit_parent_key) if explicit_parent_key else level_stack[-1] if level_stack else None
        parent_path = parent.path if parent else []
        normalized_number = _normalize_number(candidate.number)
        semantic_key = (tuple((_normalize_label(value) for value in parent_path)), normalized_number, _normalize_label(candidate.title), level)
        occurrence = semantic_occurrences[semantic_key]
        semantic_occurrences[semantic_key] += 1
        chapter_id = stable_source_chapter_id(source_ingestion_id=record.id, parent_path=parent_path, normalized_number=normalized_number, title=candidate.title, level=level, source_locator=candidate.source_locator, order_index=occurrence)
        page_start: int | None = None
        page_end_exclusive: int | None = None
        if candidate.source_range is not None and candidate.source_range.kind == 'pdf_pages' and isinstance(candidate.source_range.start, int) and isinstance(candidate.source_range.end, int):
            page_start = candidate.source_range.start
            page_end_exclusive = candidate.source_range.end + 1
        chapter = SourceChapter(id=chapter_id, owner_user_id=record.owner_user_id, package_id=record.package_id, source_ingestion_id=record.id, parent_id=parent.id if parent else None, number=candidate.number, normalized_number=normalized_number, title=candidate.title, level=level, path=[*parent_path, candidate.title], order_index=order_index, source_locator=candidate.source_locator, body_start_offset=None, body_end_offset=None, page_start=page_start, page_end=page_end_exclusive, anchor_status='verified' if candidate.mapping_status == 'verified' else 'unverified', range=candidate.source_range, mapping_status=candidate.mapping_status, source_content_hash=content_hash, catalog_evidence=list(candidate.evidence), confidence=max(0.0, min(1.0, candidate.confidence)), excerpt=candidate.title, metadata={**candidate.metadata, 'catalog_pipeline': catalog_pipeline, 'semantic_identity_version': 2, 'semantic_occurrence': occurrence, 'legacy_page_end_is_exclusive': True})
        chapters.append(chapter)
        chapter_by_proposal_key[candidate.local_key] = chapter
        proposal_key = str(candidate.metadata.get('proposal_key') or '')
        if proposal_key:
            chapter_by_proposal_key[proposal_key] = chapter
        level_stack.append(chapter)
    return chapters

def _validate_chapters(chapters: Sequence[SourceChapter]) -> None:
    seen_ids: set[str] = set()
    known_chapters: dict[str, SourceChapter] = {}
    previous_order = -1
    for chapter in chapters:
        if chapter.id in seen_ids:
            raise SourceDirectoryProcessingError('Directory chapter ids are not unique.')
        if chapter.parent_id and chapter.parent_id not in known_chapters:
            raise SourceDirectoryProcessingError('A directory child appeared before its parent.')
        if chapter.order_index <= previous_order:
            raise SourceDirectoryProcessingError('Directory order is not strictly increasing.')
        if chapter.mapping_status == 'verified' and chapter.range is None:
            raise SourceDirectoryProcessingError('A verified directory node has no authoritative range.')
        if chapter.range is not None and (not chapter.range.end_inclusive):
            raise SourceDirectoryProcessingError('Authoritative source ranges must have inclusive end bounds.')
        parent = known_chapters.get(chapter.parent_id or '')
        if parent is not None and parent.mapping_status == 'verified' and (chapter.mapping_status == 'verified') and _is_numeric_containment_range(parent.range) and _is_numeric_containment_range(chapter.range) and _range_is_same_series(parent.range, chapter.range) and (int(chapter.range.start) < int(parent.range.start) or int(chapter.range.end) > int(parent.range.end)):
            raise SourceDirectoryProcessingError('A verified directory child range falls outside its verified parent range.')
        seen_ids.add(chapter.id)
        known_chapters[chapter.id] = chapter
        previous_order = chapter.order_index

def _catalog_quality(chapters: Sequence[SourceChapter], *, catalog_complete: bool=True, directory_only_complete: bool=False) -> SourceStructureQuality:
    total = len(chapters)
    verified = sum((chapter.mapping_status == 'verified' for chapter in chapters))
    unverified = total - verified
    ratio = verified / total if total else 0.0
    if total and catalog_complete and (directory_only_complete or verified == total):
        level = 'fully_verified'
    elif verified:
        level = 'partially_verified'
    else:
        level = 'unverified'
    if total and directory_only_complete:
        diagnostics = ['The contents pages, PDF page offsets, and catalog hierarchy passed catalog validation.']
    elif total and (not verified):
        diagnostics = ['The catalog structure has been identified. Content ranges have not been mapped yet.']
    else:
        diagnostics = ['The catalog stores structure and ranges only. Chapter content is read on demand when cited.']
    if not catalog_complete:
        diagnostics.append('Catalog completeness has not been verified, so only partial navigation is available. Automatic continuation stopped at the task limit; verified entries remain individually citable.')
    parent_ids = {chapter.parent_id for chapter in chapters if chapter.parent_id}
    leaf_chapters = [chapter for chapter in chapters if chapter.id not in parent_ids]
    return SourceStructureQuality(evaluator_version=2, level=level, text_readiness='unknown', confidence=1.0 if total and catalog_complete and (directory_only_complete or not verified) else ratio if catalog_complete else min(ratio, 0.9), total_chapter_count=total, verified_chapter_count=verified, unverified_chapter_count=unverified, verified_leaf_count=sum((chapter.mapping_status == 'verified' for chapter in leaf_chapters)), expected_leaf_count=len(leaf_chapters), verified_ratio=ratio, boundary_valid_ratio=ratio, body_coverage_ratio=0.0 if directory_only_complete else ratio, independent_anchor_ratio=ratio, meaningful_characters_per_page=0.0, diagnostics=diagnostics)

def _positive_catalog_count(value: object) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)

def _compat_catalog_quality(chapters: Sequence[SourceChapter], *, diagnostic: str) -> SourceStructureQuality:
    total = len(chapters)
    verified = sum((chapter.mapping_status == 'verified' for chapter in chapters))
    ratio = verified / total if total else 0.0
    parent_ids = {chapter.parent_id for chapter in chapters if chapter.parent_id}
    leaves = [chapter for chapter in chapters if chapter.id not in parent_ids]
    return SourceStructureQuality(evaluator_version=2, level='fully_verified' if total and verified == total else 'partially_verified' if verified else 'unverified', text_readiness='unknown', confidence=ratio if total else 0.0, total_chapter_count=total, verified_chapter_count=verified, unverified_chapter_count=total - verified, verified_leaf_count=sum((chapter.mapping_status == 'verified' for chapter in leaves)), expected_leaf_count=len(leaves), verified_ratio=ratio, boundary_valid_ratio=ratio, body_coverage_ratio=0.0, independent_anchor_ratio=ratio, meaningful_characters_per_page=0.0, diagnostics=[diagnostic])

def _native_pi_unresolved_node_keys(chapters: Sequence[SourceChapter]) -> tuple[str, ...]:
    return tuple((str(chapter.metadata.get('native_pi_key') or chapter.id) for chapter in chapters if chapter.mapping_status != 'verified' or chapter.range is None))

def _native_pi_mapping_progress_signature(audit_metadata: dict[str, object]) -> str:
    """Ignore prose churn and detect only reusable directory/range progress."""
    payload = audit_metadata.get('native_pi_directory_payload')
    if not isinstance(payload, dict):
        return ''
    nodes = payload.get('nodes')
    regimes = payload.get('pagination_regimes')
    structural_payload = {'directory_pages': payload.get('directory_pages'), 'nodes': [{key: node.get(key) for key in ('key', 'parent_key', 'number', 'title', 'level', 'directory_page', 'printed_page', 'pagination_regime_id', 'pdf_file_page', 'observed_label')} for node in nodes if isinstance(node, dict)] if isinstance(nodes, list) else [], 'pagination_regimes': regimes if isinstance(regimes, list) else []}
    return _hash_json(structural_payload)

def _directory_system_prompt() -> str:
    return '\nYou are the single OpenClass file-directory Codex. You review only bounded\nnavigation evidence prepared by the host. Never infer subject knowledge or add\nheadings that are absent from the packet. Keep a node only when it is a genuine\ndocument navigation unit; remove repeated running headers, page numbers, and\ndecorative labels. Preserve local_key exactly, return every candidate exactly\nonce, preserve order, and never change source_range or locator values. You may\nclean a title, normalize its visible number, and correct its hierarchy level\nonly when hierarchy_locked is false. For hierarchy_locked native navigation,\npreserve every node and native_level even when a packet begins below level 1.\nVerified host ranges are authoritative and must be kept. Return schema-valid\nJSON only. Do not use web search.\n'.strip()

def _normalize_number(value: str) -> str:
    normalized = _normalize_label(value).strip('.。')
    parts = [part for part in normalized.split('.') if part]
    if parts and all((part.isdigit() for part in parts)):
        return '.'.join((str(int(part)) for part in parts))
    return normalized

def _normalize_label(value: str) -> str:
    return ' '.join(str(value or '').casefold().split()).strip()

def _hash_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()

def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()

def _report(callback: CatalogProgressCallback | None, phase: str, progress: int) -> None:
    if callback is not None:
        callback(phase, progress)

def _normalization_batch_progress(completed: int, total: int) -> int:
    if total <= 0:
        return NORMALIZATION_PROGRESS_END
    bounded_completed = max(0, min(completed, total))
    span = NORMALIZATION_PROGRESS_END - NORMALIZATION_PROGRESS_START
    return NORMALIZATION_PROGRESS_START + round(span * bounded_completed / total)
source_directory_processor = SourceDirectoryProcessor()
