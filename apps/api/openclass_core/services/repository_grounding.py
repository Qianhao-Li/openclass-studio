from __future__ import annotations
import hashlib
from urllib.parse import quote
from openclass_core.models import EvidenceBundle, LearningClarificationStatus, LearningRequirementAuxiliaryFactor, LearningRequirementChecklistItem, LearningRequirementKeyFact, LearningRequirementSheet, LearningSourceGrounding, LearningSourceReference, Lesson, RepositoryFileEntry, RepositoryMapNode, RetrievalEvidence, SelectionRef, SourceIngestionRecord, now_iso
from openclass_core.services import workspace_state
from openclass_core.services.ai_logging import ai_usage_logger
from openclass_core.services.repository_source import RepositorySourceError, read_repository_file_range
from openclass_core.services.repository_store import repository_store
from openclass_core.services.source_evidence_store import source_evidence_store
MAX_REPOSITORY_EVIDENCE_FILES = 64
MAX_REPOSITORY_EVIDENCE_CHARACTERS = 192000
MAX_REPOSITORY_EVIDENCE_LINES_PER_FILE = 400

class RepositoryGroundingError(RuntimeError):
    pass

def resolve_repository_grounded_board_plan(*, owner_user_id: str, lesson: Lesson, selection: SelectionRef, query: str=''):
    from openclass_core.services.source_grounded_board import SourceGroundedBoardPlan
    if not selection.source_ingestion_id or not selection.source_repository_node_id:
        raise RepositoryGroundingError('This repository reference has no verifiable project node. Select it again.')
    workspace = workspace_state.load_workspace_for_user(owner_user_id)
    package, _current_lesson = workspace_state.find_lesson_package(workspace, lesson.id)
    source = source_evidence_store.get_source(owner_user_id=owner_user_id, package_id=package.id, source_id=selection.source_ingestion_id)
    if source is None or source.source_type != 'code_repository' or source.status != 'ready':
        raise RepositoryGroundingError('This repository source is not ready to use for board generation yet.')
    snapshot = repository_store.get_snapshot(owner_user_id=owner_user_id, package_id=package.id, source_id=source.id)
    node = repository_store.get_node(source_id=source.id, node_id=selection.source_repository_node_id)
    if snapshot is None or node is None or (not node.selectable):
        raise RepositoryGroundingError('This project node has no verified source-code range and cannot be cited yet.')
    if selection.source_repository_tree_kind and selection.source_repository_tree_kind != node.tree_kind:
        raise RepositoryGroundingError('The repository reference structure type does not match the current snapshot. Select it again.')
    _validate_snapshot_identity(source=source, selection=selection, snapshot=snapshot)
    evidence = _read_node_evidence(source=source, snapshot=snapshot, node=node)
    if not evidence:
        raise RepositoryGroundingError('This project node has no readable source-code content.')
    label = node.title
    locator = node.path or node.title
    context_text = '\n\n'.join((f"{item.page_range}\nPinned original: {item.metadata['repository_source_url']}\n{item.expanded_text}" for item in evidence))
    bundle = EvidenceBundle(owner_user_id=owner_user_id, package_id=package.id, lesson_id=lesson.id, purpose='board_generation', status='confirmed', query=query.strip() or selection.excerpt, evidence_items=evidence, context_text=context_text, token_count=sum((item.token_count for item in evidence)), confirmed_by_user=True, confirmed_at=now_iso(), metadata={'origin': 'repository_node_selection', 'source_ingestion_id': source.id, 'repository_node_id': node.id, 'repository_tree_kind': node.tree_kind, 'repository_commit_sha': snapshot.resolved_commit_sha, 'repository_snapshot_hash': snapshot.archive_hash, 'repository_manifest_hash': snapshot.manifest_hash})
    source_evidence_store.save_bundle(bundle)
    ai_usage_logger.log_event('repository_source_reference_confirmed', owner_user_id=owner_user_id, package_id=package.id, lesson_id=lesson.id, source_ingestion_id=source.id, repository_node_id=node.id, repository_tree_kind=node.tree_kind, evidence_file_count=len({item.metadata.get('repository_file_id') for item in evidence}), evidence_token_count=bundle.token_count)
    content_hash = hashlib.sha256(context_text.encode('utf-8')).hexdigest()
    reference = LearningSourceReference(evidence_bundle_id=bundle.id, source_ingestion_id=source.id, source_title=source.title, source_chapter_id=node.id, chapter_title=node.title, scope_kind='repository_node', scope_chapter_id=node.id, scope_chapter_title=node.title, section_path=[snapshot.owner, snapshot.name, *([node.path] if node.path else [])], source_locator=locator, page_range=f'{snapshot.resolved_commit_sha[:12]}:{locator}', content_hash=content_hash)
    grounding = LearningSourceGrounding(requested_by_user=True, confirmation_status='confirmed', confirmed_bundle_id=bundle.id, confirmed_at=bundle.confirmed_at, confirmed_references=[reference], frozen_evidence=evidence)
    source_label = f'{source.title} / {label} / {snapshot.resolved_commit_sha[:12]}'
    requirement = LearningRequirementSheet(teaching_type='knowledge_point', learning_content=label, current_level='', target_scenario='', auxiliary_factors=[LearningRequirementAuxiliaryFactor(label='confirmed_repository_source', value=source_label, evidence='repository_node_selection')], theme=label, learning_goal=f'基于已固定版本的仓库节点“{label}”建立可学习的板书。', level='', known_background='', current_questions=[], learning_need_checklist=['已确认仓库版本和源码范围'], target_depth='按所选项目节点的真实源码结构组织讲解。', output_preference='结构化 Markdown 板书', boundary=source_label, board_scope=[source_label], success_criteria='每项源码结论均可追溯到固定提交中的路径和行号。', board_workflow='generate_from_scratch', work_mode='knowledge_board', granularity='source_chapter', source_grounding=grounding)
    clarification = LearningClarificationStatus(progress=100, label='Repository scope confirmed', reason='A verifiable project node from a fixed commit has been selected.', can_start=True, summary=source_label, key_facts=[LearningRequirementKeyFact(label='repository_node', value=source_label, evidence='repository_node_selection', category='learning')], checklist=[LearningRequirementChecklistItem(title='仓库节点', is_clear=True, evidence='repository_node_selection')], work_mode='knowledge_board', granularity='source_chapter', ready_for_board=True)
    return SourceGroundedBoardPlan(requirement=requirement, clarification=clarification, teaching_plan='以固定提交中的已验证源码行作为唯一事实依据，解释项目结构、模块职责、关键流程和学习顺序，并在相关结论旁保留路径与行号。')

def _validate_snapshot_identity(*, source: SourceIngestionRecord, selection: SelectionRef, snapshot) -> None:
    expected_commit = str(source.metadata.get('repository_commit_sha') or '')
    expected_archive = str(source.metadata.get('repository_snapshot_hash') or '')
    expected_manifest = str(source.metadata.get('repository_manifest_hash') or '')
    if expected_commit != snapshot.resolved_commit_sha or expected_archive != snapshot.archive_hash or expected_manifest != snapshot.manifest_hash:
        raise RepositoryGroundingError('The repository source identity does not match the pinned snapshot.')
    if selection.source_content_hash and selection.source_content_hash != snapshot.manifest_hash:
        raise RepositoryGroundingError('The repository reference manifest fingerprint is outdated. Select it again.')

def _read_node_evidence(*, source: SourceIngestionRecord, snapshot, node: RepositoryMapNode) -> list[RetrievalEvidence]:
    files = {item.id: item for item in repository_store.files_for_source(source.id)}
    spans: list[tuple[RepositoryFileEntry, int, int, str]] = []
    if node.evidence:
        for item in node.evidence:
            file = files.get(item.file_id)
            if file is not None and file.path == item.path and (file.text_status == 'ready'):
                spans.append((file, item.line_start, item.line_end, item.reason))
    elif node.node_kind in {'root', 'directory'}:
        prefix = f'{node.path}/' if node.path else ''
        for file in files.values():
            if file.text_status == 'ready' and file.path.startswith(prefix):
                spans.append((file, 1, min(file.line_count, MAX_REPOSITORY_EVIDENCE_LINES_PER_FILE), 'selected_repository_directory'))
    evidence: list[RetrievalEvidence] = []
    used_chars = 0
    for file, line_start, line_end, reason in spans[:MAX_REPOSITORY_EVIDENCE_FILES]:
        if used_chars >= MAX_REPOSITORY_EVIDENCE_CHARACTERS:
            break
        line_end = min(line_end, line_start + MAX_REPOSITORY_EVIDENCE_LINES_PER_FILE - 1)
        try:
            text = read_repository_file_range(snapshot=snapshot, file=file, line_start=line_start, line_end=line_end)
        except RepositorySourceError as exc:
            raise RepositoryGroundingError(str(exc)) from exc
        remaining = MAX_REPOSITORY_EVIDENCE_CHARACTERS - used_chars
        if len(text) > remaining:
            selected_lines = text[:remaining].splitlines()
            text = '\n'.join(selected_lines)
            line_end = line_start + max(0, len(selected_lines) - 1)
        if not text.strip():
            continue
        used_chars += len(text)
        evidence.append(RetrievalEvidence(source_ingestion_id=source.id, source_title=source.title, source_uri=source.source_uri, chapter_id=node.id, section_path=[node.title, file.path], page_range=f'{file.path}:L{line_start}-L{line_end}', excerpt=text[:600], expanded_text=text, relevance_score=1.0, reason=reason, token_count=max(1, len(text) // 4), metadata={'repository_commit_sha': snapshot.resolved_commit_sha, 'repository_source_url': f"https://github.com/{quote(snapshot.owner, safe='')}/{quote(snapshot.name, safe='')}/blob/{snapshot.resolved_commit_sha}/{quote(file.path, safe='/')}#L{line_start}-L{line_end}", 'repository_snapshot_hash': snapshot.archive_hash, 'repository_manifest_hash': snapshot.manifest_hash, 'repository_file_id': file.id, 'repository_file_hash': file.content_hash, 'repository_path': file.path, 'line_start': line_start, 'line_end': line_end}))
    return evidence
