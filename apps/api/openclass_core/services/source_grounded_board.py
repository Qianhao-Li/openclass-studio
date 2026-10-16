from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Callable
from openclass_core.models import AgentActivityEvent, EvidenceBundle, LearningClarificationStatus, LearningRequirementAuxiliaryFactor, LearningRequirementChecklistItem, LearningRequirementKeyFact, LearningRequirementSheet, LearningSourceGrounding, LearningSourceReference, Lesson, RetrievalEvidence, SelectionRef, SourceQueryScope, SourceRange, new_id, now_iso
from openclass_core.services import workspace_state
from openclass_core.services.ai_logging import ai_usage_logger
from openclass_core.services.source_chapter_identity import rebind_stale_source_chapter_selection
from openclass_core.services.source_evidence_store import source_evidence_store
from openclass_core.services.source_range_reader import SourceRangeReadError, is_on_demand_directory_catalog, read_source_range_for_ai, read_verified_source_range
from openclass_core.services.source_pdf_grounding import PDF_SOURCE_INPUT_METHOD, combine_pdf_image_inputs, pdf_image_manifest, prepare_frozen_pdf_source_inputs as _prepare_frozen_pdf_source_inputs
from openclass_core.services.source_ingestion_service import source_local_path
from openclass_core.services.source_page_visual_analysis import SourcePageVisualAnalysisError, analyze_pdf_visual_scope
from openclass_core.services.source_structure_indexer import source_structure_needs_upgrade
from openclass_core.services.source_scope_ocr import SourceScopeOcrError, has_usable_source_text, recover_pdf_scope_evidence
from openclass_core.services.source_structure_store import source_structure_store
from openclass_core.services.source_visual_extraction import CURRENT_SOURCE_VISUAL_INDEX_VERSION
SOURCE_BOARD_TOKEN_BUDGET = 48000
SOURCE_FREEZE_TOKEN_BUDGET = 2147483647
SOURCE_BOARD_EVIDENCE_LIMIT = 64

class SourceGroundedBoardError(RuntimeError):
    """Raised when a user-selected source cannot safely ground a new board."""

@dataclass(frozen=True)
class SourceGroundedBoardPlan:
    requirement: LearningRequirementSheet
    clarification: LearningClarificationStatus
    teaching_plan: str
    image_inputs: list[str] = field(default_factory=list)
    image_manifest: list[dict[str, Any]] = field(default_factory=list)
    preserve_image_bytes: bool = False
    original_delivery: Any | None = None

def prepare_frozen_pdf_source_inputs(*, owner_user_id: str, requirement: LearningRequirementSheet, is_cancelled: Callable[[], bool] | None=None) -> tuple[list[str], list[dict[str, Any]]]:
    try:
        return _prepare_frozen_pdf_source_inputs(owner_user_id=owner_user_id, requirement=requirement, is_cancelled=is_cancelled)
    except SourceRangeReadError as exc:
        raise SourceGroundedBoardError(str(exc)) from exc

def selection_from_source_query_scope(*, owner_user_id: str, lesson: Lesson, scope: SourceQueryScope) -> SelectionRef | None:
    """Recover one authenticated board source selection from a transport QA scope."""
    if scope.mode not in {'chapter', 'source'} or len(scope.refs) != 1:
        return None
    ref = scope.refs[0]
    workspace = workspace_state.load_workspace_for_user(owner_user_id)
    package, _current_lesson = workspace_state.find_lesson_package(workspace, lesson.id)
    source = source_evidence_store.get_source(owner_user_id=owner_user_id, package_id=package.id, source_id=ref.source_ingestion_id)
    if source is None or source.status != 'ready':
        raise SourceGroundedBoardError('This source is not ready to use for board generation yet.')
    actual_hash = str(source.metadata.get('content_hash') or '').strip().lower()
    submitted_hash = ref.source_content_hash.strip().lower()
    if not actual_hash or actual_hash != submitted_hash:
        raise SourceGroundedBoardError('This source has changed. Select the chapter again.')
    if scope.mode == 'source':
        structure = source_structure_store.get_structure(owner_user_id=owner_user_id, package_id=package.id, source_id=source.id)
        if structure is not None and is_on_demand_directory_catalog(structure) and (structure.metadata.get('directory_status') != 'complete'):
            raise SourceGroundedBoardError('The source catalog is still being completed. Cite verified chapters first.')
        return SelectionRef(kind='source', excerpt=f'《{source.title}》', source_ingestion_id=source.id, source_title=source.title, source_uri=source.source_uri, source_content_hash=submitted_hash, source_scope_kind='source')
    if not ref.source_chapter_id:
        return None
    pair = source_structure_store.get_catalog_chapter(owner_user_id=owner_user_id, package_id=package.id, source_id=source.id, chapter_id=ref.source_chapter_id)
    if pair is None:
        raise SourceGroundedBoardError('No verified chapter could be found for this reference. Select it again.')
    structure, chapter = pair
    if chapter.anchor_status != 'verified' and chapter.mapping_status != 'verified':
        raise SourceGroundedBoardError('The chapter range for this source has not been verified.')
    return SelectionRef(kind='source', excerpt=' · '.join((part for part in (source.title, chapter.title) if part)), heading_path=chapter.path, source_ingestion_id=source.id, source_title=source.title, source_uri=source.source_uri, source_chapter_id=chapter.id, source_chapter_number=chapter.number, source_chapter_title=chapter.title, source_page_start=ref.page_start, source_page_end=ref.page_end, source_range=chapter.range, catalog_version=chapter.catalog_version or structure.catalog_version, source_content_hash=submitted_hash, source_scope_kind='chapter')

def resolve_source_query_grounded_board_plan(*, owner_user_id: str, lesson: Lesson, scope: SourceQueryScope, query: str, retrieval_bundle: EvidenceBundle | None=None, visual_adapter=None, visual_model_supports_images: bool=False, visual_model_identity: str='', is_cancelled: Callable[[], bool] | None=None, on_activity=None) -> SourceGroundedBoardPlan:
    """Freeze one or many authenticated query scopes into one board plan."""
    if scope.mode == 'all_ready_sources':
        if retrieval_bundle is None:
            raise SourceGroundedBoardError('No verified source content was found for this learning turn.')
        return _plan_from_retrieval_bundle(owner_user_id=owner_user_id, lesson=lesson, query=query, retrieval_bundle=retrieval_bundle)
    component_plans: list[SourceGroundedBoardPlan] = []
    for ref in scope.refs:
        single_scope = SourceQueryScope(mode='source' if scope.mode == 'sources' else scope.mode, refs=[ref])
        selection = selection_from_source_query_scope(owner_user_id=owner_user_id, lesson=lesson, scope=single_scope)
        if selection is None and scope.mode == 'page_range':
            selection = _page_range_selection(owner_user_id=owner_user_id, lesson=lesson, ref=ref)
        if selection is None:
            raise SourceGroundedBoardError('This source reference has no verifiable range. Select it again.')
        plan = resolve_source_grounded_board_plan(owner_user_id=owner_user_id, lesson=lesson, selection=selection, query=query, visual_adapter=visual_adapter, visual_model_supports_images=visual_model_supports_images, visual_model_identity=visual_model_identity, is_cancelled=is_cancelled, on_activity=on_activity)
        if plan is None:
            raise SourceGroundedBoardError('This source reference cannot establish a verifiable range for the board.')
        component_plans.append(replace(plan, image_inputs=[], image_manifest=[], preserve_image_bytes=False) if len(scope.refs) > 1 else plan)
    result = component_plans[0] if len(component_plans) == 1 else _aggregate_source_plans(owner_user_id=owner_user_id, lesson=lesson, query=query, plans=component_plans)
    return result

def _page_range_selection(*, owner_user_id: str, lesson: Lesson, ref) -> SelectionRef:
    workspace = workspace_state.load_workspace_for_user(owner_user_id)
    package, _current_lesson = workspace_state.find_lesson_package(workspace, lesson.id)
    source = source_evidence_store.get_source(owner_user_id=owner_user_id, package_id=package.id, source_id=ref.source_ingestion_id)
    if source is None or source.status != 'ready':
        raise SourceGroundedBoardError('This source is not ready to use for board generation yet.')
    actual_hash = str(source.metadata.get('content_hash') or '').strip().lower()
    if not actual_hash or actual_hash != ref.source_content_hash.strip().lower():
        raise SourceGroundedBoardError('This source has changed. Select the page range again.')
    structure = source_structure_store.get_structure(owner_user_id=owner_user_id, package_id=package.id, source_id=source.id)
    if structure is None or structure.catalog_version < 1:
        raise SourceGroundedBoardError('This source has no verifiable catalog version. Rebuild the catalog.')
    return SelectionRef(kind='source', excerpt=f'《{source.title}》 · {ref.page_start}-{ref.page_end}', source_ingestion_id=source.id, source_title=source.title, source_uri=source.source_uri, source_page_start=ref.page_start, source_page_end=ref.page_end, source_content_hash=actual_hash, catalog_version=structure.catalog_version, source_scope_kind='page_range', source_range={'kind': 'pdf_pages', 'start': ref.page_start, 'end': ref.page_end})

def _aggregate_source_plans(*, owner_user_id: str, lesson: Lesson, query: str, plans: list[SourceGroundedBoardPlan]) -> SourceGroundedBoardPlan:
    workspace = workspace_state.load_workspace_for_user(owner_user_id)
    package, _current_lesson = workspace_state.find_lesson_package(workspace, lesson.id)
    evidence = _dedupe_evidence((item for plan in plans for item in plan.requirement.source_grounding.frozen_evidence))
    image_inputs, image_manifest = ([], [])
    visuals = list({item.visual_id: item for plan in plans for item in plan.requirement.source_grounding.frozen_visual_evidence}.values())
    if not evidence:
        raise SourceGroundedBoardError('No usable content has been extracted from the selected source range yet.')
    bundle = EvidenceBundle(owner_user_id=owner_user_id, package_id=package.id, lesson_id=lesson.id, purpose='board_generation', status='confirmed', query=query, evidence_items=evidence, visual_items=visuals, context_text=_evidence_context_text(evidence), token_count=sum((item.token_count for item in evidence)), confirmed_by_user=True, confirmed_at=now_iso(), metadata={'origin': 'aggregated_source_query_scope', 'component_bundle_ids': [plan.requirement.source_grounding.confirmed_bundle_id for plan in plans], **({'input_method': PDF_SOURCE_INPUT_METHOD, 'image_manifest': image_manifest} if image_inputs else {})})
    source_evidence_store.save_bundle(bundle)
    references = [reference.model_copy(update={'evidence_bundle_id': bundle.id}) for plan in plans for reference in plan.requirement.source_grounding.confirmed_references]
    return _aggregated_plan(query=query, bundle=bundle, references=references, evidence=evidence, visuals=visuals, image_inputs=image_inputs, image_manifest=image_manifest)

def _plan_from_retrieval_bundle(*, owner_user_id: str, lesson: Lesson, query: str, retrieval_bundle: EvidenceBundle) -> SourceGroundedBoardPlan:
    evidence = _dedupe_evidence(retrieval_bundle.evidence_items)
    coverage = retrieval_bundle.metadata.get('coverage')
    if coverage:
        evidence = [item.model_copy(update={'metadata': {**item.metadata, 'source_coverage': coverage}}) for item in evidence]
    from openclass_core.services.source_board_coverage import require_complete_board_evidence
    try:
        require_complete_board_evidence(evidence)
    except ValueError as exc:
        raise SourceGroundedBoardError(str(exc)) from exc
    from openclass_core.services.source_content_contract import verified_read_evidence
    if any((item.metadata.get('source_backend') in {'google', 'local'} and (not verified_read_evidence(item)) for item in evidence)):
        raise SourceGroundedBoardError('Every source input must have a verified original-reading receipt. Search snippets cannot enter a board requirement.')
    if not evidence or not any((has_usable_source_text(item.expanded_text) or (verified_read_evidence(item) and item.metadata.get('content_unit_sha256')) for item in evidence)):
        raise SourceGroundedBoardError('No verified source content was found for this learning turn.')
    workspace = workspace_state.load_workspace_for_user(owner_user_id)
    package, _current_lesson = workspace_state.find_lesson_package(workspace, lesson.id)
    bundle = retrieval_bundle.model_copy(update={'id': new_id('bundle'), 'owner_user_id': owner_user_id, 'package_id': package.id, 'lesson_id': lesson.id, 'purpose': 'board_generation', 'status': 'confirmed', 'query': query, 'evidence_items': evidence, 'context_text': _evidence_context_text(evidence), 'token_count': sum((item.token_count for item in evidence)), 'confirmed_by_user': retrieval_bundle.confirmed_by_user, 'confirmed_at': now_iso(), 'metadata': {**retrieval_bundle.metadata, 'origin': 'all_ready_source_learning'}})
    source_evidence_store.save_bundle(bundle)
    grouped: dict[tuple[str, str, str], list[RetrievalEvidence]] = {}
    for item in evidence:
        grouped.setdefault((item.source_ingestion_id, item.chapter_id, item.page_range), []).append(item)
    references = [LearningSourceReference(evidence_bundle_id=bundle.id, source_ingestion_id=items[0].source_ingestion_id, source_title=items[0].source_title, source_chapter_id=items[0].chapter_id, chapter_title=items[0].section_path[-1] if items[0].section_path else '', scope_kind='retrieved_range', section_path=items[0].section_path, page_range=items[0].page_range, chunk_ids=_dedupe_chunk_ids(items), content_hash=str(items[0].metadata.get('source_content_hash') or _evidence_hash(items))) for items in grouped.values()]
    return _aggregated_plan(query=query, bundle=bundle, references=references, evidence=evidence, visuals=bundle.visual_items)

def _aggregated_plan(*, query: str, bundle: EvidenceBundle, references: list[LearningSourceReference], evidence: list[RetrievalEvidence], visuals: list, image_inputs: list[str] | None=None, image_manifest: list[dict[str, Any]] | None=None) -> SourceGroundedBoardPlan:
    labels = list(dict.fromkeys((' / '.join((part for part in [ref.source_title, ref.chapter_title, ref.page_range] if part)) for ref in references)))
    source_label = '；'.join((label for label in labels if label))
    grounding = LearningSourceGrounding(requested_by_user=bundle.confirmed_by_user, confirmation_status='confirmed', confirmed_bundle_id=bundle.id, confirmed_at=bundle.confirmed_at, confirmed_references=references, frozen_evidence=evidence, frozen_visual_evidence=visuals, reading_notes=[])
    requirement = LearningRequirementSheet(teaching_type='knowledge_point', learning_content=query.strip() or source_label, current_level='', target_scenario='', theme=query.strip() or source_label, learning_goal='基于本轮已验证资料范围建立一份可学习的聚合板书。', level='', known_background='', current_questions=[], learning_need_checklist=['已确认全部资料范围及来源身份'], target_depth='按各资料与章节分别组织，并呈现它们的关系。', output_preference='结构化 Markdown 板书', boundary=source_label, board_scope=labels, success_criteria='覆盖相关核心概念；冲突信息须并列标明来源，不得合并成无来源结论。', board_workflow='generate_from_scratch', work_mode='knowledge_board', granularity='source_range', source_grounding=grounding)
    clarification = LearningClarificationStatus(progress=100, label='Multiple source ranges confirmed', reason='Source evidence for this turn has been verified and preserved. A combined board can now be generated.', missing_items=[], can_start=True, summary=source_label, work_mode='knowledge_board', granularity='source_range', ready_for_board=True)
    return SourceGroundedBoardPlan(requirement=requirement, clarification=clarification, teaching_plan='仅使用冻结证据生成一份聚合板书；按资料和章节保留来源边界，重复证据只使用一次，冲突内容并列说明各自来源。', image_inputs=image_inputs or [], image_manifest=image_manifest or [], preserve_image_bytes=bool(image_inputs))

def _dedupe_evidence(items) -> list[RetrievalEvidence]:
    return list({item.id: item for item in items}.values())

def _scoped_visual_cache_key(*, source_content_hash: str, catalog_version: int, source_range: dict) -> str:
    payload = {'source_content_hash': source_content_hash, 'catalog_version': catalog_version, 'source_range': source_range, 'extractor_version': CURRENT_SOURCE_VISUAL_INDEX_VERSION}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()

def _direct_original_file_evidence(*, source, structure, chapter, selection: SelectionRef) -> tuple[list[RetrievalEvidence], str, dict[str, Any]]:
    """Authenticate a selected range without reading or transforming source content."""
    source_path = source_local_path(source)
    if source_path is None:
        raise SourceGroundedBoardError('The original source file could not be found and cannot be sent to the AI.')
    expected_hash = str(structure.source_content_hash or source.metadata.get('content_hash') or '').strip().lower()
    if not expected_hash:
        raise SourceGroundedBoardError('This source has no verifiable file fingerprint. Upload it again.')
    digest = hashlib.sha256()
    try:
        with source_path.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(block)
    except OSError as exc:
        raise SourceGroundedBoardError('Could not read the original source file.') from exc
    if digest.hexdigest() != expected_hash:
        raise SourceGroundedBoardError('The source file content has changed. Rebuild the catalog.')
    submitted_hash = (selection.source_content_hash or '').strip().lower()
    if submitted_hash and submitted_hash != expected_hash:
        raise SourceGroundedBoardError('The source file for this chapter reference has changed. Select it again.')
    if chapter.range is None:
        raise SourceGroundedBoardError('This chapter reference has no authoritative source range. Select it again.')
    source_range = chapter.range.model_dump(mode='json')
    if selection.source_range is not None and selection.source_range.model_dump(mode='json') != source_range:
        raise SourceGroundedBoardError('This chapter reference has an outdated or modified source range. Select it again.')
    catalog_version = int(structure.catalog_version or 0)
    if catalog_version < 1 or (selection.catalog_version and selection.catalog_version != catalog_version):
        raise SourceGroundedBoardError('This catalog reference is outdated. Select the chapter again.')
    page_start = _pdf_range_endpoint(source_range, 'start')
    page_end = _pdf_range_endpoint(source_range, 'end')
    evidence = RetrievalEvidence(source_ingestion_id=source.id, source_title=source.title, source_uri=source.source_uri, chapter_id=chapter.id, section_path=chapter.path, page_range=_source_range_display_label(source_range), excerpt='', expanded_text='', relevance_score=chapter.confidence, reason='BoardEditor will read the original uploaded file directly within the authorized range.', token_count=0, metadata={'retrieval_mode': 'direct_original_file', 'source_transport': 'direct_original_file', 'source_range': source_range, 'source_locator': chapter.source_locator, 'catalog_version': catalog_version, 'source_content_hash': expected_hash, 'page_start': page_start, 'page_end': page_end})
    return ([evidence], expected_hash, source_range)

def resolve_source_grounded_board_plan(*, owner_user_id: str, lesson: Lesson, selection: SelectionRef | None, query: str='', visual_adapter=None, visual_model_supports_images: bool=False, visual_model_identity: str='', is_cancelled: Callable[[], bool] | None=None, on_activity=None) -> SourceGroundedBoardPlan | None:
    result = _resolve_original_source_board_plan(owner_user_id=owner_user_id, lesson=lesson, selection=selection, query=query, visual_adapter=visual_adapter, visual_model_supports_images=visual_model_supports_images, visual_model_identity=visual_model_identity, is_cancelled=is_cancelled, on_activity=on_activity)
    return result

def _resolve_original_source_board_plan(*, owner_user_id: str, lesson: Lesson, selection: SelectionRef | None, query: str='', visual_adapter=None, visual_model_supports_images: bool=False, visual_model_identity: str='', is_cancelled: Callable[[], bool] | None=None, on_activity=None) -> SourceGroundedBoardPlan | None:
    """Turn one verified source selection into a frozen-ready blank-board input.

    A structured source click is an explicit learner choice of the material
    boundary.  It is therefore enough to start a knowledge board without
    collecting learner level or target-scenario fields.  This function never
    performs semantic search and never selects a different source on the
    learner's behalf.
    """
    if selection is None or selection.kind != 'source':
        return None
    if selection.source_scope_kind == 'repository_node':
        from openclass_core.services.repository_grounding import RepositoryGroundingError, resolve_repository_grounded_board_plan
        try:
            return resolve_repository_grounded_board_plan(owner_user_id=owner_user_id, lesson=lesson, selection=selection, query=query)
        except RepositoryGroundingError as exc:
            raise SourceGroundedBoardError(str(exc)) from exc
    if not selection.source_ingestion_id:
        raise SourceGroundedBoardError('This source reference has no verifiable chapter location. Select the chapter from the source catalog again.')
    is_whole_source = selection.source_scope_kind == 'source'
    is_page_range = selection.source_scope_kind == 'page_range'
    if is_page_range and selection.source_range is None:
        if selection.source_page_start is None or selection.source_page_end is None or selection.source_page_start < 1 or (selection.source_page_end < selection.source_page_start):
            raise SourceGroundedBoardError('This source reference has no valid page-range boundaries.')
    elif not is_whole_source and (not is_page_range) and (not selection.source_chapter_id):
        raise SourceGroundedBoardError('This source reference has no verifiable chapter location.')
    workspace = workspace_state.load_workspace_for_user(owner_user_id)
    package, _current_lesson = workspace_state.find_lesson_package(workspace, lesson.id)
    source = source_evidence_store.get_source(owner_user_id=owner_user_id, package_id=package.id, source_id=selection.source_ingestion_id)
    if source is None or source.status != 'ready':
        raise SourceGroundedBoardError('This source is not ready to use for board generation yet.')
    submitted_hash = (selection.source_content_hash or '').strip().lower()
    actual_hash = str(source.metadata.get('content_hash') or '').strip().lower()
    if submitted_hash and (not actual_hash or submitted_hash != actual_hash):
        raise SourceGroundedBoardError('This source has changed. Select the referenced range again.')
    google_original_mode = False
    if source.metadata.get('source_backend') in {'google', 'local'}:
        from openclass_core.services.google_sources.grounding import ground_selected_source
        return ground_selected_source(owner_user_id=owner_user_id, package_id=package.id, lesson=lesson, source=source, selection=selection, query=query, is_cancelled=is_cancelled, calculation_adapter=visual_adapter, on_activity=on_activity)
    source_catalog_pipeline = str(source.metadata.get('catalog_pipeline') or source.metadata.get('structure_pipeline') or '')
    if is_whole_source and source_catalog_pipeline in {'native_directory_v1', 'hybrid_directory_v1', 'ai_directory_v1', 'codex_directory_v1'}:
        raise SourceGroundedBoardError('Select a verified chapter range before using this catalog source.')
    if is_whole_source:
        stored_structure = source_structure_store.get_structure(owner_user_id=owner_user_id, package_id=package.id, source_id=source.id)
        if stored_structure is not None and is_on_demand_directory_catalog(stored_structure):
            raise SourceGroundedBoardError('Select a verified chapter range before using this catalog source.')
        from openclass_core.services.open_notebook_source_grounding import OpenNotebookSourceGroundingError, resolve_open_notebook_source_plan
        try:
            plan = resolve_open_notebook_source_plan(owner_user_id=owner_user_id, package_id=package.id, lesson=lesson, source=source, query=query.strip() or selection.excerpt)
        except OpenNotebookSourceGroundingError as exc:
            raise SourceGroundedBoardError(str(exc)) from exc
        return SourceGroundedBoardPlan(requirement=plan.requirement, clarification=plan.clarification, teaching_plan=plan.teaching_plan)
    view = source_structure_store.get_structure_view(source=source, chunk_limit=0)
    if view.structure is None or view.structure.status not in {'ready', 'linear_only'}:
        raise SourceGroundedBoardError('The structure index for this source is not ready yet. Try again shortly.')
    if google_original_mode:
        view.structure = view.structure.model_copy(update={'metadata': {**view.structure.metadata, 'catalog_pipeline': 'native_directory_v1'}})
    uses_on_demand_range = is_on_demand_directory_catalog(view.structure)
    if uses_on_demand_range and is_whole_source:
        raise SourceGroundedBoardError('Select a verified chapter range before using this catalog source.')
    authoritative_chapter = None
    if uses_on_demand_range and (not is_page_range):
        catalog_pair = source_structure_store.get_catalog_chapter(owner_user_id=owner_user_id, package_id=package.id, source_id=source.id, chapter_id=selection.source_chapter_id or '')
        if catalog_pair is None:
            raise SourceGroundedBoardError('No verified catalog range could be found for this reference. Select it again.')
        view.structure, authoritative_chapter = catalog_pair
        if google_original_mode:
            view.structure = view.structure.model_copy(update={'metadata': {**view.structure.metadata, 'catalog_pipeline': 'native_directory_v1'}})
    structure_upgrade_deferred = False if uses_on_demand_range else source_structure_needs_upgrade(view.structure)
    visual_upgrade_deferred = False if uses_on_demand_range else _needs_visual_index_upgrade(source.mime_type, source.file_name, view.structure.metadata)
    if structure_upgrade_deferred or visual_upgrade_deferred:
        ai_usage_logger.log_event('source_structure_upgrade_deferred', owner_user_id=owner_user_id, package_id=package.id, source_ingestion_id=source.id, structure_status=view.structure.status, structure_upgrade_deferred=structure_upgrade_deferred, visual_upgrade_deferred=visual_upgrade_deferred, structure_index_version=view.structure.metadata.get('structure_index_version'), visual_index_version=view.structure.metadata.get('visual_index_version'))
    chapter = authoritative_chapter or next((candidate for candidate in view.chapters if candidate.id == selection.source_chapter_id and (candidate.mapping_status == 'verified' if uses_on_demand_range else candidate.anchor_status == 'verified')), None)
    if chapter is None and uses_on_demand_range and (not is_page_range):
        raise SourceGroundedBoardError('No verified catalog range could be found for this reference. Select it again.')
    if chapter is None and (not is_page_range) and (not uses_on_demand_range):
        rebound = rebind_stale_source_chapter_selection(selection=selection, source_ingestion_id=source.id, chapters=view.chapters)
        if rebound.is_ambiguous:
            raise SourceGroundedBoardError('The source catalog has changed and this reference now matches multiple chapters. Select it again.')
        chapter = rebound.chapter
    if chapter is None and (not is_page_range):
        raise SourceGroundedBoardError('No verified content range could be found for this reference. Select the chapter from the source catalog again.')
    following_chapter = None
    if chapter is not None:
        following_chapter = next((candidate for candidate in sorted(view.chapters, key=lambda item: item.order_index) if candidate.order_index > chapter.order_index and candidate.anchor_status == 'verified'), None)
    range_read = None
    original_delivery = None
    image_inputs: list[str] = []
    image_manifest: list[dict[str, Any]] = []
    pdf_original_pages = bool(source.mime_type.split(';', 1)[0].strip().lower() == 'application/pdf' or source.file_name.lower().endswith('.pdf') or (selection.source_range is not None and selection.source_range.kind == 'pdf_pages'))
    direct_original_file = False
    direct_source_hash = ''
    direct_source_range: dict[str, Any] | None = None
    if pdf_original_pages:
        if not visual_model_supports_images:
            raise SourceGroundedBoardError('The selected model cannot read original PDF page images. Choose a model that supports image input.')
        try:
            from openclass_core.services.source_range_reader import iter_source_range_for_ai
            from openclass_core.services.original_source_delivery import collect_original_inventory
            reads = iter_source_range_for_ai(owner_user_id=owner_user_id, package_id=package.id, source=source, structure=view.structure, chapter=chapter, selection=selection, is_cancelled=is_cancelled)
            first_read = next(reads)
            from itertools import chain
            evidence, image_inputs, image_manifest, _coverage, original_delivery = collect_original_inventory(chain((first_read,), reads), owner_user_id=owner_user_id, package_id=package.id, is_cancelled=is_cancelled)
            range_read = first_read
        except SourceRangeReadError as exc:
            raise SourceGroundedBoardError(str(exc)) from exc
        if not evidence:
            raise SourceGroundedBoardError('The selected PDF range has no authenticated original pages available.')
        evidence = [item.model_copy(update={'metadata': {**item.metadata, 'input_method': PDF_SOURCE_INPUT_METHOD}}) for item in evidence]
    else:
        from openclass_core.services.legacy_original_content import native_selection_range, iter_native_original_batches
        from openclass_core.services.original_source_delivery import collect_original_inventory
        from openclass_core.services.google_sources.config import GoogleSourceError
        from openclass_core.services.source_ingestion_service import source_download_path
        path = source_download_path(source)
        if path is None:
            raise SourceGroundedBoardError('The original source file is unavailable.')
        try:
            direct_source_range = native_selection_range(source=source, structure=view.structure, chapter=chapter, selection=selection, path=path)
            evidence, image_inputs, image_manifest, _coverage, original_delivery = collect_original_inventory(iter_native_original_batches(source=source, structure=view.structure, source_range=direct_source_range, owner_user_id=owner_user_id, package_id=package.id, is_cancelled=is_cancelled), owner_user_id=owner_user_id, package_id=package.id, source_store=source_evidence_store, structure_store=source_structure_store, is_cancelled=is_cancelled)
        except (GoogleSourceError, ValueError) as exc:
            raise SourceGroundedBoardError(str(exc)) from exc
        for item in evidence:
            item.chapter_id = chapter.id
            item.section_path = list(chapter.path)
        direct_source_hash = str(source.metadata['content_hash'])
        if not evidence:
            raise SourceGroundedBoardError('The authorized original range has no deliverable content.')
    visual_evidence = []
    scoped_visual_status = PDF_SOURCE_INPUT_METHOD if pdf_original_pages else 'original_source_content'
    scoped_visual_warnings: list[str] = []
    scoped_visual_excluded: list[dict[str, object]] = []
    scoped_visual_cache_key = ''
    bundle = EvidenceBundle(owner_user_id=owner_user_id, package_id=package.id, lesson_id=lesson.id, purpose='board_generation', status='confirmed', query=selection.excerpt, evidence_items=evidence, visual_items=[] if uses_on_demand_range else visual_evidence, context_text=_evidence_context_text(evidence), token_count=sum((item.token_count for item in evidence)), confirmed_by_user=True, confirmed_at=now_iso(), metadata={'origin': 'structured_source_selection', 'source_ingestion_id': source.id, 'source_chapter_id': chapter.id if chapter else '', 'source_scope_kind': selection.source_scope_kind, 'source_structure_id': view.structure.id, 'catalog_pipeline': view.structure.strategy if uses_on_demand_range else 'legacy_structure', 'catalog_version': range_read.catalog_version if range_read else view.structure.catalog_version, 'source_content_hash': range_read.source_content_hash if range_read else direct_source_hash, 'source_range': range_read.source_range if range_read else direct_source_range, 'source_transport': PDF_SOURCE_INPUT_METHOD if pdf_original_pages else 'direct_original_file' if direct_original_file else 'frozen_evidence', **({'input_method': PDF_SOURCE_INPUT_METHOD, 'image_manifest': image_manifest} if image_inputs else {}), 'visual_scope_cache_key': scoped_visual_cache_key, 'visual_scope_status': scoped_visual_status, 'visual_requested_range': range_read.source_range if range_read else direct_source_range, 'visual_extracted_count': len(visual_evidence), 'visual_scope_warnings': scoped_visual_warnings, 'visual_excluded_candidates': scoped_visual_excluded})
    source_evidence_store.save_bundle(bundle)
    if chapter is not None:
        chapter_number = (chapter.normalized_number or chapter.number).strip()
        chapter_title = chapter.title.strip()
        chapter_label = chapter_title
        if chapter_number and (not (chapter_title == chapter_number or chapter_title.startswith(f'{chapter_number} ') or chapter_title.startswith(f'{chapter_number}\t'))):
            chapter_label = f'{chapter_number} {chapter_title}'.strip()
        chapter_label = chapter_label or chapter.title or source.title
    else:
        chapter_label = evidence[0].page_range or selection.source_page_range or source.title
    reference = LearningSourceReference(evidence_bundle_id=bundle.id, source_ingestion_id=source.id, source_title=source.title, source_chapter_id=chapter.id if chapter else '', chapter_number=chapter.normalized_number or chapter.number if chapter else '', chapter_title=chapter.title if chapter else '', scope_kind='page_range' if is_page_range else 'chapter', scope_chapter_id=chapter.id if chapter else '', scope_chapter_number=chapter.normalized_number or chapter.number if chapter else '', scope_chapter_title=chapter.title if chapter else '', section_path=chapter.path if chapter else evidence[0].section_path, source_locator=chapter.source_locator if chapter else selection.source_locator, page_range=_source_range_display_label(range_read.source_range if range_read else direct_source_range or {}) if range_read or direct_source_range else evidence[0].page_range, page_start=_pdf_range_endpoint(range_read.source_range if range_read else direct_source_range or {}, 'start') if range_read or direct_source_range else selection.source_page_start if is_page_range else chapter.page_start if chapter else None, page_end=_pdf_range_endpoint(range_read.source_range if range_read else direct_source_range or {}, 'end') if range_read or direct_source_range else selection.source_page_end if is_page_range else chapter.page_end if chapter else None, body_start_offset=chapter.body_start_offset if chapter else None, body_end_offset=chapter.body_end_offset if chapter else None, chunk_ids=_dedupe_chunk_ids(evidence), visual_ids=[] if uses_on_demand_range else [item.visual_id for item in visual_evidence], source_structure_id=view.structure.id, source_structure_updated_at=view.structure.updated_at, content_hash=range_read.source_content_hash if pdf_original_pages and range_read is not None else direct_source_hash or _evidence_hash(evidence))
    grounding = LearningSourceGrounding(requested_by_user=True, confirmation_status='confirmed', confirmed_bundle_id=bundle.id, confirmed_at=bundle.confirmed_at, confirmed_references=[reference], frozen_evidence=evidence, frozen_visual_evidence=[] if uses_on_demand_range else visual_evidence)
    source_label = ' / '.join((part for part in [source.title, chapter_label, reference.page_range] if part))
    requirement = LearningRequirementSheet(teaching_type='knowledge_point', learning_content=chapter_label, current_level='', target_scenario='', auxiliary_factors=[LearningRequirementAuxiliaryFactor(label='confirmed_source', value=source_label, evidence='structured_source_selection'), LearningRequirementAuxiliaryFactor(label='source_visual_scope_status', value=scoped_visual_status, evidence='board_agent_visual_diagnostic'), LearningRequirementAuxiliaryFactor(label='source_visual_scope_cache_key', value=scoped_visual_cache_key, evidence='authenticated_source_scope')], theme=chapter_label, learning_goal=f'基于《{source.title}》的所选章节建立可学习的板书。', level='', known_background='', current_questions=[], learning_need_checklist=['已确认资料范围'], target_depth='按资料章节的实际结构组织讲解。', output_preference='结构化 Markdown 板书', boundary=source_label, board_scope=[source_label], success_criteria='覆盖所选资料范围的核心概念、结构关系与必要例证。', board_workflow='generate_from_scratch', work_mode='knowledge_board', granularity='source_range' if is_page_range else 'source_chapter', source_grounding=grounding)
    clarification = LearningClarificationStatus(progress=100, label='Source range confirmed', reason='A verified source chapter has been selected. The board will be generated from that chapter.', missing_items=[], can_start=True, summary=source_label, key_facts=[LearningRequirementKeyFact(label='source_chapter', value=source_label, evidence='structured_source_selection', category='learning')], checklist=[LearningRequirementChecklistItem(title='Source chapter', is_clear=True, evidence='structured_source_selection')], work_mode='knowledge_board', granularity='source_range' if is_page_range else 'source_chapter', ready_for_board=True)
    return SourceGroundedBoardPlan(requirement=requirement, clarification=clarification, teaching_plan=('直接读取附带的原始 PDF 页面图像，并严格限定在冻结的资料范围内；不得使用OCR、文本抽取或另一模型的视觉转写作为原文。' if pdf_original_pages else '直接读取原始上传文件，并严格限定在已验证章节范围内；不得使用主机侧OCR、文本抽取、分块或摘要作为资料正文。' if direct_original_file else '以冻结的资料正文为唯一事实依据，') + '保留章节结构，提炼核心概念、关键关系和必要例证，生成一份可独立学习的板书。' + (' 当前运行时不支持教材视觉工具；继续生成文字板书，并在完成回复中明确说明本轮未处理教材视觉。' if scoped_visual_status == 'unsupported' else ' 将 Board Agent 登记的教学视觉放在相关知识段落附近；位置不确定时放入本章教学图表附录。' if visual_evidence else ''), image_inputs=image_inputs, image_manifest=image_manifest, preserve_image_bytes=bool(image_inputs), original_delivery=original_delivery)

def _evidence_context_text(evidence: list[RetrievalEvidence]) -> str:
    return '\n\n'.join(('\n'.join((part for part in [item.source_title, ' > '.join(item.section_path), item.page_range, item.expanded_text] if part)) for item in evidence))

def _dedupe_chunk_ids(evidence: list[RetrievalEvidence]) -> list[str]:
    return list(dict.fromkeys((chunk_id for item in evidence for chunk_id in item.chunk_ids if chunk_id)))

def _evidence_hash(evidence: list[RetrievalEvidence]) -> str:
    content = '\n'.join((item.expanded_text for item in evidence if item.expanded_text))
    return hashlib.sha256(content.encode('utf-8')).hexdigest()

def _pdf_range_endpoint(source_range: dict[str, object], key: str) -> int | None:
    if source_range.get('kind') != 'pdf_pages':
        return None
    value = source_range.get(key)
    return value if isinstance(value, int) and (not isinstance(value, bool)) else None

def _source_range_display_label(source_range: dict[str, object]) -> str:
    display_label = source_range.get('display_label')
    if isinstance(display_label, str) and display_label.strip():
        return display_label.strip()
    if source_range.get('kind') == 'pdf_pages':
        start = _pdf_range_endpoint(source_range, 'start')
        end = _pdf_range_endpoint(source_range, 'end')
        if start is not None and end is not None:
            return f'PDF p. {start}' if start == end else f'PDF pp. {start}-{end}'
    start = source_range.get('start')
    end = source_range.get('end')
    return str(start) if start == end else f'{start}-{end}'

def _needs_visual_index_upgrade(mime_type: str, file_name: str, metadata: dict[str, object]) -> bool:
    normalized_mime = mime_type.lower()
    normalized_name = file_name.lower()
    supported_extensions = ('.pdf', '.docx', '.pptx', '.xlsx', '.epub', '.html', '.htm', '.md', '.markdown', '.csv', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.txt', '.json', '.xml')
    supports_visuals = normalized_mime.startswith(('image/', 'text/')) or normalized_mime in {'application/pdf', 'application/epub+zip', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'application/vnd.openxmlformats-officedocument.presentationml.presentation', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'} or normalized_name.endswith(supported_extensions)
    try:
        version = int(metadata.get('visual_index_version') or 0)
    except (TypeError, ValueError):
        version = 0
    return supports_visuals and version < CURRENT_SOURCE_VISUAL_INDEX_VERSION
