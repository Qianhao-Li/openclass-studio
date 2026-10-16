from __future__ import annotations
import base64
import copy
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from openclass_core.models import AgentActivityEvent, AIModelSelection, BoardContentExtent, BoardDecision, BoardDocument, ChatRequest, ChatResponse, ConversationTurn, LearningClarificationStatus, LearningRequirementSheet, RetrievalEvidence, SelectionRef, SourceQueryRef, SourceQueryScope, SourceVisualAsset, SourceVisualEvidence, new_id
from openclass_core.services import workspace_state
from openclass_core.services.source_prompt_projection import coverage_for_model, evidence_for_model, image_manifest_for_model, qa_evidence_for_model, requirement_for_model, scope_for_model
from openclass_core.services.ai_execution_adapter import AIExecutionAdapter, BoardGenerationExecutionRequest, BoardGenerationExecutionResult, BoardGenerationSourceScope, CodexAIExecutionAdapter, build_ai_execution_adapter
from openclass_core.services.ai_model_catalog import build_model_catalog, resolve_text_model_selection, text_model_supports_image_inputs
from openclass_core.services.blank_board_intake import process_blank_board_turn
from openclass_core.services.board_visual_insertion import BoardInsertionPlan, apply_board_insertion_plan, build_board_insertion_plan, derive_board_visual_placements
from openclass_core.services.chat_attachments import PreparedChatAttachments, VerifiedChatAttachment, prepare_chat_attachments, verify_chat_attachments
from openclass_core.services.chat.prompts.blank_board import BOARD_GENERATION_DEVELOPER_INSTRUCTIONS
from openclass_core.services.chat.prompts.existing_board import CODEX_DEVELOPER_INSTRUCTIONS, EXISTING_BOARD_CHAT_INSTRUCTIONS, STRUCTURED_EXISTING_BOARD_INSTRUCTIONS, StructuredExistingBoardTurn as _StructuredExistingBoardTurn
from openclass_core.services.chat.prompts.source_qa import SOURCE_QA_INSTRUCTIONS, SOURCE_VISUAL_ANALYSIS_INSTRUCTIONS, SourceQATurn as _SourceQATurn
from openclass_core.services.chat.response_factory import build_existing_board_response, build_source_qa_response
from openclass_core.services.chat.turn_context import BoardState, board_state as _board_state, conversation_context as _conversation_context, turn_prompt as _turn_prompt
from openclass_core.services.codex_app_server import CodexAppServerError, CodexTurnCancelledError, CodexTurnResult, delete_codex_thread, run_codex_thread_turn
from openclass_core.services.history import commit_operations, current_head_commit
from openclass_core.services.follow_up_suggestions import generate_follow_up_suggestions
from openclass_core.services.lesson_factory import build_requirements
from openclass_core.services.native_pi_teaching_agent import has_resumable_native_pi_teaching_task, process_native_pi_teaching_turn
from openclass_core.services.rich_document import build_document, document_changed, looks_like_html_content, rebuild_document_from_content_json
from openclass_core.services.source_grounded_board import SOURCE_BOARD_TOKEN_BUDGET, SourceGroundedBoardError, prepare_frozen_pdf_source_inputs, resolve_source_query_grounded_board_plan, resolve_source_grounded_board_plan
from openclass_core.services.source_evidence_store import source_evidence_store
from openclass_core.services.source_ingestion_service import source_local_path
from openclass_core.services.source_structure_store import source_structure_store
from openclass_core.services.source_retrieval_service import SourceRetrievalError, SourceRetrievalResult, source_retrieval_service
from openclass_core.services.teaching_agent_mode import native_pi_teaching_enabled
from openclass_core.services.turn_intent import BoardWriteDecision, board_write_decision_trace, board_write_policy_prompt, decide_board_write_action, has_explicit_document_mutation_request, pending_board_write_offer as load_pending_board_write_offer, pending_board_write_offer_after as build_pending_board_write_offer_after
BOARD_FILE_NAME = 'board.md'
DEFAULT_CODEX_MODEL = 'gpt-5.5'
DEFAULT_BOARD_MAX_BYTES = 2 * 1024 * 1024
CODEX_BOARD_GENERATION_TIMEOUT_SECONDS = 300
MAX_FORMULA_IMAGE_DATA_URL_CHARS = 12 * 1024 * 1024
MAX_SOURCE_VISUAL_BYTES = 4 * 1024 * 1024
MAX_SOURCE_VISUALS_PER_BATCH = 8
_PRESERVED_VISUAL_MARKER_RE = re.compile('\\[\\[OPENCLASS_PRESERVED_VISUAL_[0-9a-f]{12}_\\d{4}\\]\\]')

@dataclass(frozen=True)
class CodexBoardGenerationResult:
    """Board turn plus registered/excluded visual decisions and insertion data."""
    turn: BoardGenerationExecutionResult
    insertion_plan: BoardInsertionPlan
    visual_assets: dict[str, tuple[SourceVisualAsset, bytes]]
    registered_visuals: list[dict[str, object]] = field(default_factory=list)
    excluded_visuals: list[dict[str, object]] = field(default_factory=list)
    visual_warnings: list[str] = field(default_factory=list)
    visual_bytes_resolver: Callable | None = None
    source_input_parts: list[dict[str, object]] = field(default_factory=list)
    strict_visual_positions: bool = False

    @property
    def thread_id(self) -> str:
        return self.turn.thread_id

    @property
    def turn_id(self) -> str | None:
        return self.turn.turn_id

    @property
    def final_response(self) -> str:
        return self.turn.final_response

    @property
    def activity(self) -> list[AgentActivityEvent]:
        return self.turn.activity

@dataclass(frozen=True)
class ExistingBoardSourceContext:
    """Backend-verified source material for one existing-board Codex turn."""
    prompt_context: str
    image_inputs: list[str]
    insertion_plan: BoardInsertionPlan
    visual_assets: dict[str, tuple[SourceVisualAsset, bytes]]
    requirement: LearningRequirementSheet
    preserve_image_bytes: bool = False
    original_delivery: object | None = None
_turn_locks_guard = threading.Lock()
_turn_locks: dict[str, threading.Lock] = {}

def codex_workspace_root() -> Path:
    configured = (os.getenv('OPENCLASS_CODEX_WORKSPACE_ROOT') or '').strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path.home() / path
    else:
        path = Path.home() / '.openclass' / 'codex-workspaces'
    return path.resolve()

def _workspace_key(*, user_id: str, lesson_id: str, branch_name: str) -> str:
    identity = '\x00'.join((user_id, lesson_id, branch_name))
    return hashlib.sha256(identity.encode('utf-8')).hexdigest()

def _turn_lock(key: str) -> threading.Lock:
    with _turn_locks_guard:
        return _turn_locks.setdefault(key, threading.Lock())

def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)

def _prepare_workspace(workspace: Path, content_text: str) -> Path:
    encoded_content = content_text.encode('utf-8')
    if len(encoded_content) > _board_max_bytes():
        raise CodexAppServerError('The current board exceeds the configured size limit')
    root = workspace.parent
    root.mkdir(parents=True, exist_ok=True, mode=448)
    if workspace.is_symlink():
        raise CodexAppServerError('Codex board workspace must not be a symbolic link')
    workspace.mkdir(parents=False, exist_ok=True, mode=448)
    try:
        root.chmod(448)
        workspace.chmod(448)
    except OSError:
        pass
    for child in workspace.iterdir():
        if child.name != BOARD_FILE_NAME:
            _remove_path(child)
    board_path = workspace / BOARD_FILE_NAME
    if board_path.is_symlink():
        board_path.unlink()
    board_path.write_bytes(encoded_content)
    try:
        board_path.chmod(384)
    except OSError:
        pass
    return board_path

def _document_for_codex(document: BoardDocument) -> tuple[str, dict[str, dict[str, Any]]]:
    content_json = copy.deepcopy(document.content_json)
    nodes = content_json.get('content') if isinstance(content_json, dict) else None
    if not isinstance(nodes, list):
        return (document.content_text, {})
    preserved: dict[str, dict[str, Any]] = {}
    visual_index = 0

    def replace_visuals(items: list[Any]) -> list[Any]:
        nonlocal visual_index
        replaced: list[Any] = []
        for item in items:
            if not isinstance(item, dict):
                replaced.append(item)
                continue
            if item.get('type') == 'resourceVisualBlock':
                digest = hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()[:12]
                marker = f'[[OPENCLASS_PRESERVED_VISUAL_{digest}_{visual_index:04d}]]'
                visual_index += 1
                preserved[marker] = copy.deepcopy(item)
                replaced.append({'type': 'paragraph', 'content': [{'type': 'text', 'text': marker}]})
                continue
            content = item.get('content')
            if isinstance(content, list):
                item['content'] = replace_visuals(content)
            replaced.append(item)
        return replaced
    content_json['content'] = replace_visuals(nodes)
    serialized = rebuild_document_from_content_json(document, content_json)
    return (serialized.content_text, preserved)

def _restore_preserved_visuals(document: BoardDocument, preserved: dict[str, dict[str, Any]]) -> BoardDocument:
    if not preserved:
        return document
    content_json = copy.deepcopy(document.content_json)
    nodes = content_json.get('content') if isinstance(content_json, dict) else None
    if not isinstance(nodes, list):
        raise CodexAppServerError('Codex board output lost protected visual placeholders')
    counts = {marker: 0 for marker in preserved}

    def restore(items: list[Any]) -> list[Any]:
        restored: list[Any] = []
        for item in items:
            if not isinstance(item, dict):
                restored.append(item)
                continue
            text = _tiptap_plain_text(item).strip()
            if item.get('type') == 'paragraph' and text in preserved:
                counts[text] += 1
                restored.append(copy.deepcopy(preserved[text]))
                continue
            content = item.get('content')
            if isinstance(content, list):
                item['content'] = restore(content)
            restored.append(item)
        return restored
    content_json['content'] = restore(nodes)
    tokens = _PRESERVED_VISUAL_MARKER_RE.findall(document.content_text)
    if set(tokens) != set(preserved) or any((count != 1 for count in counts.values())):
        raise CodexAppServerError('Codex board output altered protected visual placeholders')
    return rebuild_document_from_content_json(document, content_json)

def _tiptap_plain_text(value: Any) -> str:
    if isinstance(value, dict):
        if value.get('type') == 'text':
            return str(value.get('text') or '')
        content = value.get('content')
        if isinstance(content, list):
            return ''.join((_tiptap_plain_text(child) for child in content))
    if isinstance(value, list):
        return ''.join((_tiptap_plain_text(child) for child in value))
    return ''

def _board_max_bytes() -> int:
    configured = (os.getenv('OPENCLASS_CODEX_BOARD_MAX_BYTES') or '').strip()
    if not configured:
        return DEFAULT_BOARD_MAX_BYTES
    try:
        value = int(configured)
    except ValueError as exc:
        raise CodexAppServerError('OPENCLASS_CODEX_BOARD_MAX_BYTES must be an integer') from exc
    if value <= 0:
        raise CodexAppServerError('OPENCLASS_CODEX_BOARD_MAX_BYTES must be positive')
    return value

def _watch_board_quota(board_path: Path, *, max_bytes: int, stop_event: threading.Event, quota_exceeded: threading.Event) -> None:
    while not stop_event.wait(0.02):
        try:
            info = board_path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size <= max_bytes:
            continue
        quota_exceeded.set()
        no_follow = getattr(os, 'O_NOFOLLOW', 0)
        descriptor = -1
        try:
            descriptor = os.open(board_path, os.O_WRONLY | no_follow | getattr(os, 'O_CLOEXEC', 0))
            opened = os.fstat(descriptor)
            if stat.S_ISREG(opened.st_mode) and opened.st_size > max_bytes:
                os.ftruncate(descriptor, max_bytes)
        except OSError:
            pass
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return

def _read_validated_board(workspace: Path) -> str:
    entries = list(workspace.iterdir())
    if len(entries) != 1 or entries[0].name != BOARD_FILE_NAME:
        raise CodexAppServerError('Codex board workspace contains an unexpected file')
    board_path = entries[0]
    no_follow = getattr(os, 'O_NOFOLLOW', None)
    if no_follow is None:
        raise CodexAppServerError('This platform cannot safely open the Codex board output')
    flags = os.O_RDONLY | no_follow | getattr(os, 'O_CLOEXEC', 0)
    max_bytes = _board_max_bytes()
    try:
        descriptor = os.open(board_path, flags)
    except OSError as exc:
        raise CodexAppServerError('Codex board output must be a regular file') from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise CodexAppServerError('Codex board output must be a regular file')
        if info.st_size > max_bytes:
            raise CodexAppServerError('Codex board output exceeds the configured size limit')
        with os.fdopen(descriptor, 'rb', closefd=True) as handle:
            descriptor = -1
            raw_content = handle.read(max_bytes + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw_content) > max_bytes:
        raise CodexAppServerError('Codex board output exceeds the configured size limit')
    if [entry.name for entry in workspace.iterdir()] != [BOARD_FILE_NAME]:
        raise CodexAppServerError('Codex board workspace contains an unexpected file')
    try:
        content_text = raw_content.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise CodexAppServerError('Codex board output must be valid UTF-8') from exc
    if looks_like_html_content(content_text):
        raise CodexAppServerError('Codex board output contains HTML instead of Markdown')
    return content_text

def _codex_model(request: ChatRequest, *, user_id: str) -> str:
    if request.text_model is not None and request.text_model.provider == 'openai_codex':
        selected = request.text_model.model.strip()
        if selected:
            return selected
    try:
        return build_model_catalog(user_id).defaults['text'].model
    except Exception:
        pass
    return (os.getenv('OPENAI_CODEX_MODEL') or DEFAULT_CODEX_MODEL).strip() or DEFAULT_CODEX_MODEL

def _text_model_selection(request: ChatRequest, *, user_id: str) -> AIModelSelection:
    return resolve_text_model_selection(request.text_model, user_id=user_id, catalog_builder=build_model_catalog)

def _codex_reasoning_effort(request: ChatRequest) -> str | None:
    selection = request.text_model
    if selection is None or selection.provider != 'openai_codex':
        return None
    normalized = str(selection.reasoning_effort or '').strip()
    return normalized or None

def _codex_service_tier(request: ChatRequest) -> tuple[str | None, bool]:
    selection = request.text_model
    if selection is None or selection.provider != 'openai_codex':
        return (None, False)
    if 'service_tier' not in selection.model_fields_set:
        return (None, False)
    normalized = str(selection.service_tier or '').strip()
    return (normalized or None, True)

def _thread_reference_for_current_branch(lesson) -> tuple[str | None, str | None]:
    from openclass_core.services.ridoc_chat_context import provider_session_reuse_allowed
    if not provider_session_reuse_allowed():
        return (None, None)
    branch_name = lesson.history_graph.current_branch
    commits_by_id = {commit.id: commit for commit in lesson.history_graph.commits}
    pending = [current_head_commit(lesson).id]
    visited: set[str] = set()
    while pending:
        commit_id = pending.pop()
        if commit_id in visited:
            continue
        visited.add(commit_id)
        commit = commits_by_id.get(commit_id)
        if commit is None:
            continue
        metadata = commit.metadata if isinstance(commit.metadata, dict) else {}
        if metadata.get('reset_codex_thread') is True:
            return (None, None)
        if metadata.get('kind') == 'restore_snapshot':
            continue
        thread_id = metadata.get('codex_thread_id')
        if commit.branch_name == branch_name and isinstance(thread_id, str) and thread_id.strip():
            turn_id = metadata.get('codex_turn_id')
            return (thread_id.strip(), turn_id.strip() if isinstance(turn_id, str) and turn_id.strip() else None)
        if commit.branch_name == branch_name:
            pending.extend(commit.parent_ids)
    return (None, None)

def _safe_merge_context_value(value: Any) -> Any:
    blocked_keys = {'local_source_path', 'original_source_path', 'parser_artifacts_path', 'expanded_text', 'context_text'}
    if isinstance(value, dict):
        return {key: _safe_merge_context_value(item) for key, item in value.items() if key not in blocked_keys}
    if isinstance(value, list):
        return [_safe_merge_context_value(item) for item in value]
    return value

def _merge_handoff_context(lesson) -> str:
    head = current_head_commit(lesson)
    metadata = head.metadata if isinstance(head.metadata, dict) else {}
    if metadata.get('reset_codex_thread') is not True:
        return ''
    target_head_id = metadata.get('merge_target_head_commit_id')
    source_head_id = metadata.get('merge_source_head_commit_id')
    if not isinstance(target_head_id, str) or not isinstance(source_head_id, str):
        return ''
    from openclass_core.services.lesson_merge import branch_history_summary
    runtime = {'learning_requirements': lesson.learning_requirements.model_dump(mode='json') if lesson.learning_requirements is not None else None, 'board_task_requirements': lesson.board_task_requirements.model_dump(mode='json') if lesson.board_task_requirements is not None else None}
    payload = {'merge_commit_id': head.id, 'target_branch': metadata.get('merge_target_branch'), 'source_branch': metadata.get('merge_source_branch'), 'target_history': branch_history_summary(lesson, target_head_id), 'source_history': branch_history_summary(lesson, source_head_id), 'merged_runtime': _safe_merge_context_value(runtime), 'chronology_rule': 'The target and source histories are independent labeled lineages. Do not present them as one alternating conversation.'}
    return 'Merged lesson handoff context:\n' + json.dumps(payload, ensure_ascii=False, separators=(',', ':'))

def _formula_image_urls(request: ChatRequest) -> list[str]:
    if request.formula_ink is None:
        return []
    image_data_url = request.formula_ink.image_data_url.strip()
    if not image_data_url.lower().startswith('data:image/') or ';base64,' not in image_data_url[:160].lower() or len(image_data_url) > MAX_FORMULA_IMAGE_DATA_URL_CHARS:
        raise CodexAppServerError('Formula ink must be a bounded base64 image data URL')
    return [image_data_url]

def _discard_uncommitted_thread(thread_id: str, *, user_id: str) -> None:
    try:
        delete_codex_thread(thread_id, user_id=user_id)
    except Exception:
        pass

def _neutral_clarification() -> LearningClarificationStatus:
    return LearningClarificationStatus(progress=0, label='', reason='', missing_items=[], can_start=False, forced_start=False, summary='', next_question='', ready_for_board=False, work_mode=None, granularity=None)

def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()

def _run_frozen_board_generation(*, user_id: str, model: str, requirement: LearningRequirementSheet, teaching_plan: str, content_extent: BoardContentExtent, image_inputs: list[str] | None=None, visual_manifest: list[dict[str, Any]] | None=None, is_cancelled: Callable[[], bool] | None, on_activity: Callable[[AgentActivityEvent], None] | None=None) -> tuple[CodexTurnResult, str]:
    workspace_root = codex_workspace_root()
    workspace_root.mkdir(parents=True, exist_ok=True, mode=448)
    with tempfile.TemporaryDirectory(prefix='blank-board-', dir=workspace_root) as temporary:
        workspace_path = Path(temporary)
        board_path = _prepare_workspace(workspace_path, '')
        quota_stop = threading.Event()
        quota_exceeded = threading.Event()
        quota_monitor = threading.Thread(target=_watch_board_quota, kwargs={'board_path': board_path, 'max_bytes': _board_max_bytes(), 'stop_event': quota_stop, 'quota_exceeded': quota_exceeded}, daemon=True)
        quota_monitor.start()

        def turn_is_cancelled() -> bool:
            return quota_exceeded.is_set() or bool(is_cancelled and is_cancelled())
        payload = {'learning_requirement': requirement_for_model(requirement), 'teaching_plan': teaching_plan, 'content_extent': content_extent, 'visual_manifest': visual_manifest or []}
        try:
            result = run_codex_thread_turn(user_id=user_id, model=model, cwd=workspace_path, user_prompt='Frozen board-generation payload (use frozen_evidence.metadata.citation_numbers as readable [^n] references; never output internal IDs):\n' + json.dumps(payload, ensure_ascii=False, separators=(',', ':')), developer_instructions=BOARD_GENERATION_DEVELOPER_INSTRUCTIONS, thread_id=None, image_urls=image_inputs, timeout_seconds=CODEX_BOARD_GENERATION_TIMEOUT_SECONDS, on_delta=None, on_activity=on_activity, is_cancelled=turn_is_cancelled)
        finally:
            quota_stop.set()
            quota_monitor.join(timeout=0.2)
        if quota_exceeded.is_set():
            _discard_uncommitted_thread(result.thread_id, user_id=user_id)
            raise CodexAppServerError('Codex board output exceeds the configured size limit')
        try:
            content = _read_validated_board(workspace_path)
            if not content.strip():
                raise CodexAppServerError('Board generation completed without writing board.md')
        except Exception:
            _discard_uncommitted_thread(result.thread_id, user_id=user_id)
            raise
        return (result, content)

def _source_visual_image_urls(*, user_id: str, requirement: LearningRequirementSheet) -> list[str]:
    image_urls: list[str] = []
    for evidence in requirement.source_grounding.frozen_visual_evidence:
        if _is_structured_table_evidence(evidence):
            continue
        stored = _read_frozen_source_visual(user_id=user_id, evidence=evidence)
        image_url = _image_data_url(stored)
        if not image_url:
            raise CodexAppServerError(f'Frozen source visual {evidence.visual_id} cannot be safely loaded')
        image_urls.append(image_url)
    return image_urls

def _read_frozen_source_visual(*, user_id: str, evidence: SourceVisualEvidence) -> tuple[SourceVisualAsset, bytes] | None:
    if not evidence.package_id or not evidence.source_ingestion_id or (not evidence.visual_id):
        return None
    stored = source_structure_store.read_visual_bytes(owner_user_id=user_id, package_id=evidence.package_id, source_id=evidence.source_ingestion_id, visual_id=evidence.visual_id)
    if stored is None:
        return None
    visual, content = stored
    if evidence.anchor_status != 'verified' or visual.anchor_status != 'verified' or (not evidence.content_hash) or (visual.content_hash != evidence.content_hash) or (not evidence.position_hash) or (visual.position_hash != evidence.position_hash) or (len(content) > MAX_SOURCE_VISUAL_BYTES):
        return None
    return (visual, content)

def _image_data_url(stored: tuple[SourceVisualAsset, bytes] | None) -> str:
    if stored is None:
        return ''
    visual, content = stored
    mime_type = visual.mime_type.split(';', 1)[0].strip().lower()
    if mime_type not in {'image/png', 'image/jpeg', 'image/webp', 'image/gif'}:
        return ''
    try:
        encoded = base64.b64encode(content).decode('ascii')
    except (TypeError, ValueError):
        return ''
    return f'data:{mime_type};base64,{encoded}'

def _is_structured_table_evidence(evidence: SourceVisualEvidence) -> bool:
    return evidence.kind == 'table' and bool(evidence.table_data)

def _prepare_source_generation_inputs(*, adapter: AIExecutionAdapter, requirement: LearningRequirementSheet, owner_user_id: str, is_cancelled: Callable[[], bool] | None, on_activity: Callable[[AgentActivityEvent], None] | None, include_raster_images: bool=True) -> tuple[LearningRequirementSheet, list[str]]:
    """Retained single-call seam: oversized originals require the segmented writer."""
    from openclass_core.services.long_source_generation import prepare_single_original_input
    prepared, batch = prepare_single_original_input(requirement, owner_user_id=owner_user_id, include_images=include_raster_images, is_cancelled=is_cancelled)
    return (prepared, list(batch.model_image_inputs) if batch is not None else [])

def _prepare_existing_board_source_context(*, owner_user_id: str, lesson, selection: SelectionRef | None, adapter: AIExecutionAdapter, include_raster_images: bool, visual_model_identity: str, is_cancelled: Callable[[], bool] | None, on_activity: Callable[[AgentActivityEvent], None] | None) -> ExistingBoardSourceContext | None:
    """Resolve, freeze, and serialize a structured source reference before Codex runs.

    Raw source chips are intentionally not copied into the prompt.  Only evidence
    resolved against the authenticated package and frozen by the backend reaches
    Codex, so a visible reference can neither be ignored nor spoof its contents.
    """
    plan = resolve_source_grounded_board_plan(owner_user_id=owner_user_id, lesson=lesson, selection=selection, visual_adapter=adapter, visual_model_supports_images=include_raster_images, visual_model_identity=visual_model_identity, is_cancelled=is_cancelled, on_activity=on_activity)
    if plan is None:
        return None
    workspace = workspace_state.load_workspace_for_user(owner_user_id)
    package, _current_lesson = workspace_state.find_lesson_package(workspace, lesson.id)
    scoped_requirement, _scope, _source_document_path, _excluded, _warnings = _load_registered_board_visual_scope(requirement=plan.requirement, owner_user_id=owner_user_id, package_id=package.id, supports_source_visual_tools=getattr(adapter, 'supports_source_visual_tools', False))
    from openclass_core.services.long_source_generation import prepare_single_original_input
    prepared_requirement, original_batch = prepare_single_original_input(scoped_requirement, owner_user_id=owner_user_id, is_cancelled=is_cancelled, include_images=include_raster_images)
    image_inputs = list(original_batch.model_image_inputs) if original_batch else []
    if original_batch is not None:
        from openclass_core.services.teaching_original_sources import register_teaching_original_sources
        from openclass_core.services.original_source_delivery import from_frozen_requirement
        register_teaching_original_sources(from_frozen_requirement(scoped_requirement, owner_user_id, is_cancelled=is_cancelled))
    grounding = prepared_requirement.source_grounding
    insertion_plan = build_board_insertion_plan(grounding.frozen_visual_evidence, source_titles={reference.source_ingestion_id: reference.source_title for reference in grounding.confirmed_references})
    visual_manifest = _visual_manifest_payload(plan=insertion_plan, requirement=prepared_requirement, image_input_visual_ids=[item.visual_id for item in grounding.frozen_visual_evidence if not _is_structured_table_evidence(item)] if image_inputs else [])
    prompt_payload = {'contract': 'Use this backend-verified source range in the current response and any board edit. Do not replace it with board-only continuation or outside knowledge.', 'confirmed_references': [reference.model_dump(mode='json') for reference in grounding.confirmed_references], 'frozen_text_evidence': [{'evidence_id': evidence.id, 'citation_numbers': evidence.metadata.get('citation_numbers', []), 'source_title': evidence.source_title, 'section_path': evidence.section_path, 'page_range': evidence.page_range, 'page_coordinate_system': evidence.metadata.get('page_coordinate_system', ''), 'page_label': evidence.metadata.get('page_label', ''), 'chunk_ids': evidence.chunk_ids, 'text': evidence.expanded_text, 'retrieval_metadata': evidence_for_model([evidence])[0]['metadata']} for evidence in grounding.frozen_evidence], 'visual_manifest': visual_manifest, 'original_pdf_page_images': [entry for entry in original_batch.image_manifest if entry.get('source_range', {}).get('kind') == 'pdf_pages'] if original_batch else [], 'original_source_images': original_batch.image_manifest if original_batch else [], 'visual_contract': 'For every visual_manifest item, follow the marker and recreation rules in the developer instructions. Never invent, crop, or partially reproduce a verified visual.', 'citation_contract': 'Every board passage derived from this material carries a `[^n]` marker and one `[^n]: ...` reference entry at the end of the document, following the citation contract in the developer instructions. Build each entry from the reader-facing fields of confirmed_references and frozen_text_evidence — title, chapter or section, page range, locator — and never from an evidence, chunk, or ingestion identifier. physical_pdf_pages must be labeled PDF pages; never infer printed/book page numbers.'}
    visual_assets: dict[str, tuple[SourceVisualAsset, bytes]] = {}
    evidence_by_id = {item.visual_id: item for item in grounding.frozen_visual_evidence if item.visual_id}
    for item in insertion_plan.items:
        evidence = evidence_by_id.get(item.visual_id)
        if evidence is None or _is_structured_table_evidence(evidence):
            continue
        stored = _read_frozen_source_visual(user_id=owner_user_id, evidence=evidence)
        if stored is not None:
            visual_assets[item.visual_id] = stored
    return ExistingBoardSourceContext(prompt_context=json.dumps(prompt_payload, ensure_ascii=False, separators=(',', ':')), image_inputs=image_inputs, insertion_plan=insertion_plan, visual_assets=visual_assets, requirement=prepared_requirement, preserve_image_bytes=bool(image_inputs), original_delivery=original_batch)

def _uses_original_pdf_pages(requirement: LearningRequirementSheet) -> bool:
    return any((item.metadata.get('input_method') in {'original_pdf_page_images', 'original_source_images'} or item.metadata.get('source_transport') == 'original_pdf_page_images' or (item.metadata.get('read_contract') == 'source_read_v1' and item.metadata.get('input_kind') == 'image') for item in requirement.source_grounding.frozen_evidence))

def _visual_manifest_payload(*, plan: BoardInsertionPlan, requirement: LearningRequirementSheet, image_input_visual_ids: list[str] | None=None) -> list[dict[str, object]]:
    visuals_by_id = {item.visual_id: item for item in requirement.source_grounding.frozen_visual_evidence if item.visual_id}
    source_titles = {reference.source_ingestion_id: reference.source_title for reference in requirement.source_grounding.confirmed_references}
    image_input_indexes = {visual_id: index for index, visual_id in enumerate(image_input_visual_ids or [])}
    manifest: list[dict[str, object]] = []
    for item in plan.items:
        evidence = visuals_by_id.get(item.visual_id)
        manifest.append({'visual_id': item.visual_id, 'marker': item.marker, 'original_marker': item.marker, 'recreation_marker': item.recreation_marker, 'allowed_handling': ['editable_recreation', 'original_asset'] if item.recreation_marker else ['backend_materialization'], 'image_input_index': image_input_indexes.get(item.visual_id), 'order_index': item.order_index, 'kind': item.kind, 'caption': item.caption, 'source_title': source_titles.get(item.source_ingestion_id, ''), 'source_locator': item.source_locator, 'page_start': getattr(evidence, 'page_start', None), 'page_end': getattr(evidence, 'page_end', None), 'slide_no': getattr(evidence, 'slide_no', None), 'sheet_name': getattr(evidence, 'sheet_name', ''), 'before_chunk_id': getattr(evidence, 'before_chunk_id', None), 'after_chunk_id': getattr(evidence, 'after_chunk_id', None), 'extracted_text': getattr(evidence, 'extracted_text', '')[:1200], 'surrounding_text': getattr(evidence, 'surrounding_text', '')[:1200]})
    return manifest

def _generate_blank_board(user_id: str, model: str, requirement: LearningRequirementSheet, teaching_plan: str, content_extent: BoardContentExtent, is_cancelled: Callable[[], bool] | None, on_activity: Callable[[AgentActivityEvent], None] | None=None) -> tuple[CodexBoardGenerationResult, str]:
    adapter = CodexAIExecutionAdapter(owner_user_id=user_id, model=model, board_runner=_run_codex_board_generation, image_analysis_runner=_run_codex_visual_analysis)
    return _generate_blank_board_with_adapter(adapter=adapter, user_id=user_id, requirement=requirement, teaching_plan=teaching_plan, content_extent=content_extent, include_raster_images=True, is_cancelled=is_cancelled, on_activity=on_activity)

def _generate_blank_board_with_adapter(*, adapter: AIExecutionAdapter, user_id: str, package_id: str='', requirement: LearningRequirementSheet, teaching_plan: str, content_extent: BoardContentExtent, include_raster_images: bool, is_cancelled: Callable[[], bool] | None, on_activity: Callable[[AgentActivityEvent], None] | None) -> tuple[CodexBoardGenerationResult, str]:
    grounding = requirement.source_grounding
    if grounding.confirmed_references or grounding.frozen_evidence or grounding.frozen_visual_evidence or grounding.reading_notes:
        from openclass_core.services.original_source_delivery import from_frozen_requirement
        from openclass_core.services.original_visual_preparation import prepare_original_visuals
        from openclass_core.services.long_source_generation import generate_original_board
        from openclass_core.services.teaching_original_sources import register_teaching_original_sources
        delivery = from_frozen_requirement(requirement, user_id, is_cancelled=is_cancelled)
        register_teaching_original_sources(delivery)
        if not include_raster_images and any((item.metadata.get('input_kind') == 'image' for item in delivery.evidence_items)):
            raise CodexAppServerError('The selected model cannot read original source images; no text substitute is permitted')
        visuals = prepare_original_visuals(delivery, frozen_requirement=requirement, adapter=adapter, owner_user_id=user_id, package_id=delivery.package_id, is_cancelled=is_cancelled, on_activity=on_activity)
        insertion = build_board_insertion_plan(visuals.evidence, allow_editable_recreation=False, source_titles={reference.source_ingestion_id: reference.source_title for reference in grounding.confirmed_references})
        generated = generate_original_board(adapter=adapter, delivery=delivery, requirement=requirement, teaching_plan=teaching_plan, content_extent=content_extent, visual_provider=visuals, visual_plan=insertion, max_document_bytes=_board_max_bytes(), is_cancelled=is_cancelled, on_activity=on_activity)
        from openclass_core.services.teaching_original_sources import register_teaching_original_dependencies
        register_teaching_original_dependencies(delivery, visual_evidence=visuals.evidence, input_parts=generated.input_parts)
        return (CodexBoardGenerationResult(turn=generated.turn, insertion_plan=insertion, visual_assets={}, visual_bytes_resolver=visuals.read_visual_bytes, registered_visuals=[{'visual_id': item.visual_id, 'source_ingestion_id': item.source_ingestion_id, 'content_hash': item.content_hash, 'position_hash': item.position_hash, 'source_unit_id': item.metadata.get('source_unit_id'), 'visual_review_status': item.metadata.get('visual_review_status')} for item in visuals.evidence], source_input_parts=generated.input_parts, strict_visual_positions=True), generated.content)
    scoped_requirement, source_scope, source_document_path, excluded_visuals, source_visual_warnings = _load_registered_board_visual_scope(requirement=requirement, owner_user_id=user_id, package_id=package_id, supports_source_visual_tools=getattr(adapter, 'supports_source_visual_tools', False))
    prepared_requirement, image_inputs = _prepare_source_generation_inputs(adapter=adapter, requirement=scoped_requirement, owner_user_id=user_id, is_cancelled=is_cancelled, on_activity=on_activity, include_raster_images=include_raster_images)
    insertion_plan = build_board_insertion_plan(prepared_requirement.source_grounding.frozen_visual_evidence, source_titles={reference.source_ingestion_id: reference.source_title for reference in prepared_requirement.source_grounding.confirmed_references})
    visual_manifest = _visual_manifest_payload(plan=insertion_plan, requirement=prepared_requirement, image_input_visual_ids=[item.visual_id for item in prepared_requirement.source_grounding.frozen_visual_evidence if not _is_structured_table_evidence(item)] if image_inputs else [])
    turn, content = adapter.generate_board(BoardGenerationExecutionRequest(requirement=prepared_requirement, teaching_plan=teaching_plan, content_extent=content_extent, image_inputs=image_inputs, visual_manifest=visual_manifest, source_scope=source_scope, source_document_path=source_document_path, preserve_image_bytes=bool(image_inputs) and _uses_original_pdf_pages(prepared_requirement)), is_cancelled=is_cancelled, on_activity=on_activity)
    if is_cancelled is not None and is_cancelled():
        raise CodexTurnCancelledError('Board generation was cancelled')
    if not content.strip():
        raise CodexAppServerError('Board generation completed without content')
    if looks_like_html_content(content):
        raise CodexAppServerError('Board generation returned HTML instead of Markdown')
    if len(content.encode('utf-8')) > _board_max_bytes():
        raise CodexAppServerError('Board generation exceeds the configured size limit')
    evidence_by_id = {item.visual_id: item for item in prepared_requirement.source_grounding.frozen_visual_evidence if item.visual_id}
    visual_assets: dict[str, tuple[SourceVisualAsset, bytes]] = {}
    for item in insertion_plan.items:
        evidence = evidence_by_id.get(item.visual_id)
        if evidence is None or _is_structured_table_evidence(evidence):
            continue
        stored = _read_frozen_source_visual(user_id=user_id, evidence=evidence)
        if stored is not None:
            visual_assets[item.visual_id] = stored
    return (CodexBoardGenerationResult(turn=turn, insertion_plan=insertion_plan, visual_assets=visual_assets, registered_visuals=[item.model_dump(mode='json') for item in prepared_requirement.source_grounding.frozen_visual_evidence], excluded_visuals=excluded_visuals, visual_warnings=source_visual_warnings), content)

def _load_registered_board_visual_scope(*, requirement: LearningRequirementSheet, owner_user_id: str, package_id: str, supports_source_visual_tools: bool) -> tuple[LearningRequirementSheet, BoardGenerationSourceScope | None, Path | None, list[dict[str, object]], list[str]]:
    references = requirement.source_grounding.confirmed_references
    if len(references) != 1:
        return (requirement, None, None, [], [])
    reference = references[0]
    bundle = source_evidence_store.get_bundle(owner_user_id=owner_user_id, bundle_id=requirement.source_grounding.confirmed_bundle_id)
    expected_hash = next((str(item.metadata.get('source_content_hash') or '').strip().lower() for item in requirement.source_grounding.frozen_evidence if item.source_ingestion_id == reference.source_ingestion_id and item.metadata.get('source_content_hash')), '')
    if not expected_hash and bundle is not None:
        expected_hash = str(bundle.metadata.get('source_content_hash') or '').strip().lower()
    source_scope = BoardGenerationSourceScope(source_ingestion_id=reference.source_ingestion_id, source_content_hash=expected_hash, source_chapter_id=reference.source_chapter_id, source_title=reference.source_title, chapter_title=reference.chapter_title, physical_page_start=reference.page_start, physical_page_end=reference.page_end)
    if not package_id:
        return (requirement, source_scope, None, [], [])
    source = source_evidence_store.get_source(owner_user_id=owner_user_id, package_id=package_id, source_id=reference.source_ingestion_id)
    if source is None:
        raise CodexAppServerError('The confirmed source is no longer available.')
    actual_hash = str(source.metadata.get('content_hash') or '').strip().lower()
    if expected_hash and actual_hash != expected_hash:
        raise CodexAppServerError('The confirmed source content changed before board generation.')
    source_document_path = None
    if bundle is not None and bundle.metadata.get('source_transport') == 'direct_original_file':
        source_document_path = source_local_path(source)
        if source_document_path is None:
            raise CodexAppServerError('The original uploaded source file is no longer available for direct AI input.')
    source_scope = BoardGenerationSourceScope(source_ingestion_id=reference.source_ingestion_id, source_content_hash=expected_hash or actual_hash, source_chapter_id=reference.source_chapter_id, source_title=reference.source_title, chapter_title=reference.chapter_title, physical_page_start=reference.page_start, physical_page_end=reference.page_end)
    excluded_visuals: list[dict[str, object]] = []
    visual_warnings: list[str] = []
    if bundle is not None:
        raw_excluded = bundle.metadata.get('visual_excluded_candidates') or []
        if isinstance(raw_excluded, list):
            excluded_visuals = [dict(item) for item in raw_excluded if isinstance(item, dict)]
        raw_warnings = bundle.metadata.get('visual_warnings') or bundle.metadata.get('visual_scope_warnings') or []
        if isinstance(raw_warnings, list):
            visual_warnings = [str(item) for item in raw_warnings if str(item).strip()]
    cache_key = next((factor.value for factor in requirement.auxiliary_factors if factor.label == 'source_visual_scope_cache_key'), '')
    visuals: list[SourceVisualEvidence] = []
    if supports_source_visual_tools and cache_key and reference.source_chapter_id:
        visuals = source_structure_store.scoped_visual_evidence(owner_user_id=owner_user_id, package_id=package_id, source_ingestion_id=reference.source_ingestion_id, chapter_id=reference.source_chapter_id, scope_cache_key=cache_key)
    prepared = requirement.model_copy(deep=True)
    prepared.source_grounding.frozen_visual_evidence = visuals
    return (prepared, source_scope, source_document_path, excluded_visuals, visual_warnings)

def _run_codex_board_generation(user_id: str, model: str, requirement: LearningRequirementSheet, teaching_plan: str, content_extent: BoardContentExtent, image_inputs: list[str], visual_manifest: list[dict[str, Any]], is_cancelled: Callable[[], bool] | None, on_activity: Callable[[AgentActivityEvent], None] | None) -> tuple[CodexTurnResult, str]:
    return _run_frozen_board_generation(user_id=user_id, model=model, requirement=requirement, teaching_plan=teaching_plan, content_extent=content_extent, image_inputs=image_inputs, visual_manifest=visual_manifest, is_cancelled=is_cancelled, on_activity=on_activity)

def _run_codex_visual_analysis(user_id: str, model: str, prompt: str, image_inputs: list[str], is_cancelled: Callable[[], bool] | None, on_activity: Callable[[AgentActivityEvent], None] | None) -> str:
    workspace_root = codex_workspace_root()
    workspace_root.mkdir(parents=True, exist_ok=True, mode=448)
    with tempfile.TemporaryDirectory(prefix='source-visuals-', dir=workspace_root) as temporary:
        workspace_path = Path(temporary)
        board_path = _prepare_workspace(workspace_path, '')
        result = run_codex_thread_turn(user_id=user_id, model=model, cwd=workspace_path, user_prompt=prompt, developer_instructions=SOURCE_VISUAL_ANALYSIS_INSTRUCTIONS, thread_id=None, image_urls=image_inputs, on_delta=None, on_activity=on_activity, is_cancelled=is_cancelled)
        try:
            if board_path.read_text(encoding='utf-8'):
                raise CodexAppServerError('Source visual analysis attempted to edit board.md')
            return result.final_response
        finally:
            _discard_uncommitted_thread(result.thread_id, user_id=user_id)

def _source_query_scope_for_turn(request: ChatRequest, attachments: list[VerifiedChatAttachment]) -> SourceQueryScope | None:
    """Return only a source scope explicitly authorized in the current turn.

    Course-library sources are intentionally absent here. A scope exists only
    when the client submits one or the learner attaches concrete files to this
    message; prior turns and the rest of the course library grant no access.
    """
    if request.source_query_scope is not None:
        return request.source_query_scope
    refs = [SourceQueryRef(source_ingestion_id=item.source.id, source_content_hash=str(item.source.metadata.get('content_hash') or '')) for item in attachments if not item.source.mime_type.startswith('image/') and str(item.source.metadata.get('content_hash') or '').strip()]
    if not refs:
        return None
    return SourceQueryScope(mode='source' if len(refs) == 1 else 'sources', refs=refs)

def _with_source_retrieval_context(attachments: PreparedChatAttachments, retrieval: SourceRetrievalResult | None) -> PreparedChatAttachments:
    if retrieval is None:
        return attachments
    prompt_context = '\n\n'.join((part for part in (attachments.prompt_context, retrieval.prompt_context) if part))
    return PreparedChatAttachments(prompt_context=prompt_context, image_inputs=[*attachments.image_inputs, *retrieval.image_inputs], metadata=attachments.metadata)

def _process_source_qa_turn(*, lesson_id: str, request: ChatRequest, user_id: str, adapter: AIExecutionAdapter, model_selection: AIModelSelection, initial_lesson, branch_name: str, base_commit_id: str, board_state_before: BoardState, retrieval: SourceRetrievalResult, on_delta: Callable[[str], None] | None, on_agent_activity: Callable[[AgentActivityEvent], None] | None, attachment_image_inputs: list[str] | None=None, attachment_delivery: object | None=None, is_cancelled: Callable[[], bool] | None=None) -> ChatResponse:

    def ensure_active() -> None:
        if is_cancelled is not None and is_cancelled():
            raise CodexTurnCancelledError('Source QA turn was cancelled')
    ensure_active()
    from openclass_core.models import LearningSourceGrounding
    from openclass_core.services.google_sources.turn_evidence import bind_citation_numbers, citation_map
    from openclass_core.services.original_source_delivery import from_grounding
    from openclass_core.services.long_source_generation import generate_original_board
    from openclass_core.services.ai_execution_adapter import StructuredBoardGenerationResult
    from openclass_core.services.teaching_policy import answer_instructions, expansion_offer_metadata, learning_answer_mode
    from openclass_core.services.teaching_attachments import with_turn_attachments
    from openclass_core.services.source_content_contract import SourceRoleContext
    from openclass_core.services.source_qa_citations import render_source_qa_citations
    numbered_evidence = bind_citation_numbers(retrieval.bundle.evidence_items)
    delivery = getattr(retrieval, 'original_delivery', None) or from_grounding(LearningSourceGrounding(frozen_evidence=numbered_evidence), owner_user_id=user_id, is_cancelled=is_cancelled)
    if any((item.metadata.get('input_kind') == 'image' for item in delivery.evidence_items)) and (not text_model_supports_image_inputs(model_selection, user_id=user_id)):
        raise CodexAppServerError('The selected source Q&A model cannot read original images; no text substitute is permitted')
    used_ids = []
    received = {item.id: item for item in numbered_evidence}
    requirement = build_requirements(request.message)
    requirement.source_grounding = LearningSourceGrounding(frozen_evidence=numbered_evidence)

    def answer_original_fragment(part_request, batch):
        from openclass_core.services.original_source_delivery import combine_original_batches
        actual_batch = combine_original_batches([attachment_delivery, batch]) if attachment_delivery is not None else batch
        context = with_turn_attachments(SourceRoleContext('', image_inputs=actual_batch.model_image_inputs, image_manifest=actual_batch.image_manifest, original_delivery=actual_batch))
        actual = context.original_delivery
        if attachment_image_inputs and (not set(attachment_image_inputs) <= set(context.image_inputs)):
            raise CodexAppServerError('Attachments require their authenticated original delivery before source answering')
        evidence = bind_citation_numbers(batch.evidence_items)
        source_ids = {item.id for item in evidence}
        source_manifest = [item for item in actual.image_manifest if item['evidence_id'] in source_ids]
        received.update(((item.id, item) for item in evidence))
        from openclass_core.services.original_generation_plan import OriginalInputCapacity
        OriginalInputCapacity().check(actual)
        from openclass_core.services.original_source_delivery import bind_original_source_batch
        system = SOURCE_QA_INSTRUCTIONS + '\nGenerate only the final answer portion supported by primary_unit_ids in this call. Context IDs are directly attached original dependencies, never prior answers. Do not add outside-knowledge supplements.'
        if learning_answer_mode():
            system += '\n' + answer_instructions()
        payload = {'user_message': request.message, 'source_query_scope': scope_for_model(request.source_query_scope.model_dump(mode='json') if request.source_query_scope is not None else retrieval.bundle.metadata.get('source_query_scope')), 'evidence_bundle_id': retrieval.bundle.id, 'citation_map': citation_map(evidence), 'original_source_manifest': part_request.original_source_manifest, 'coverage': coverage_for_model(actual.coverage), 'original_pdf_page_images': [entry for entry in image_manifest_for_model(source_manifest) if entry.get('source_range', {}).get('kind') == 'pdf_pages'], 'original_source_images': image_manifest_for_model(source_manifest), 'verified_evidence': qa_evidence_for_model(evidence), 'attachment_context': str(context), 'response_contract': _SourceQATurn.model_json_schema()}
        with bind_original_source_batch(actual):
            result = adapter.parse_structured(system_prompt=system, user_prompt=json.dumps(payload, ensure_ascii=False), schema=_SourceQATurn, image_inputs=context.image_inputs, preserve_image_bytes=bool(context.image_inputs), is_cancelled=is_cancelled, on_activity=on_agent_activity)
        answer = _SourceQATurn.model_validate(result.output_parsed)
        render_source_qa_citations(answer.chatbot_message, answer.cited_evidence_ids, evidence, [item for item in retrieval.citations if item.evidence_id in {value.id for value in evidence}])
        used_ids.extend(answer.cited_evidence_ids)
        turn = StructuredBoardGenerationResult(thread_id=new_id('sourceanswer'), turn_id=None, final_response='', activity=list(result.activity))
        return (turn, answer.chatbot_message)
    generated = generate_original_board(adapter=adapter, delivery=delivery, requirement=requirement, teaching_plan='Answer the current source question directly from original inputs.', write_original_fragment=answer_original_fragment, max_document_bytes=_board_max_bytes(), is_cancelled=is_cancelled, on_activity=on_agent_activity)
    ensure_active()
    response = generated.turn
    numbered_evidence = list(received.values())
    output = _SourceQATurn(chatbot_message=generated.content, cited_evidence_ids=list(dict.fromkeys(used_ids)))
    chatbot_message = output.chatbot_message.strip()
    if not chatbot_message:
        raise CodexAppServerError('The source QA role returned no learner-facing response')
    cited_ids = list(dict.fromkeys(output.cited_evidence_ids))
    from openclass_core.services.source_qa_citations import render_source_qa_citations
    try:
        chatbot_message, citations = render_source_qa_citations(chatbot_message, cited_ids, numbered_evidence, retrieval.citations)
    except ValueError as exc:
        raise CodexAppServerError(str(exc)) from exc
    follow_up_suggestions = generate_follow_up_suggestions(adapter=adapter, user_message=request.message, assistant_message=chatbot_message, board_state=board_state_before, workflow_state='source_qa')
    workspace = workspace_state.load_workspace_for_user(user_id)
    package, lesson = workspace_state.find_lesson_package(workspace, lesson_id)
    if lesson.history_graph.current_branch != branch_name or current_head_commit(lesson).id != base_commit_id:
        raise CodexAppServerError('The lesson changed while source QA was working')
    lesson = lesson.model_copy(deep=True)
    runtime = current_head_commit(lesson).runtime_snapshot
    if runtime is not None:
        lesson.learning_requirements = runtime.learning_requirements
        lesson.board_task_requirements = runtime.board_task_requirements
        lesson.board_teaching_guide = runtime.board_teaching_guide
        lesson.board_teaching_progress = runtime.board_teaching_progress
    clarification = _neutral_clarification()
    ensure_active()
    commit_operations(lesson, operations=[], label='Source QA conversation', message='OpenClass answered from a verified source scope.', new_document=lesson.board_document, metadata={'kind': 'source_qa_chat', 'user_message': request.message, 'assistant_message': chatbot_message, 'assistant_message_source': 'source_qa', 'follow_up_suggestions': follow_up_suggestions, 'source_query_scope': retrieval.bundle.metadata.get('source_query_scope'), 'verified_source_bundle_ids': [retrieval.bundle.id], 'verified_source_evidence_ids': cited_ids, 'source_citations': [item.model_dump(mode='json') for item in citations], 'document_changed': False, 'document_write_authorized': False, 'board_state_before': board_state_before, 'board_state_after': board_state_before, 'ai_provider': model_selection.provider, 'ai_model': model_selection.model, 'agent_backend': model_selection.agent_backend, 'agent_activity': [item.model_dump(mode='json') for item in response.activity], 'requirement_changed': False, 'board_task_changed': False, **expansion_offer_metadata(request=request, lesson=lesson, message=chatbot_message)})
    if not workspace_state.save_lesson_for_user_if_head(user_id, lesson, expected_branch_name=branch_name, expected_head_commit_id=base_commit_id):
        raise CodexAppServerError('The lesson changed while source QA was working')
    if on_delta is not None:
        on_delta(chatbot_message)
    workspace = workspace_state.load_workspace_for_user(user_id)
    package, lesson = workspace_state.find_lesson_package(workspace, lesson_id)
    from openclass_core.services.chat.preserved_state import preserved_learning_response_state
    return build_source_qa_response(lesson_title=lesson.title, course_package=workspace_state.package_view_for_lesson(workspace, package, lesson.id), chatbot_message=chatbot_message, citations=citations, follow_up_suggestions=follow_up_suggestions, agent_activity=response.activity, clarification=clarification, preserved_state=preserved_learning_response_state(lesson))

def _process_structured_existing_board_turn(*, lesson_id: str, request: ChatRequest, user_id: str, adapter: AIExecutionAdapter, model_selection: AIModelSelection, initial_lesson, branch_name: str, base_commit_id: str, board_state_before: BoardState, board_write_decision: BoardWriteDecision, pending_write_offer: dict[str, str] | None, source_context: ExistingBoardSourceContext | None, attachment_context: str, image_inputs: list[str], on_delta: Callable[[str], None] | None, on_agent_activity: Callable[[AgentActivityEvent], None] | None, is_cancelled: Callable[[], bool] | None, preserve_image_bytes: bool=False, original_delivery: object | None=None) -> ChatResponse:
    if is_cancelled is not None and is_cancelled():
        raise CodexTurnCancelledError('Board turn was cancelled')
    original_delivery = original_delivery or getattr(source_context, 'original_delivery', None)
    codex_board_text, preserved_visuals = _document_for_codex(initial_lesson.board_document)
    verified_context = '\n\n'.join((part for part in (source_context.prompt_context if source_context is not None else '', attachment_context, _merge_handoff_context(initial_lesson)) if part))
    document_write_authorized = board_write_decision.action in {'edit_now', 'confirm_offered_write'}
    turn_payload = {'board_markdown': codex_board_text, 'board_write_policy': board_write_policy_prompt(board_write_decision, pending_write_offer), 'conversation': [turn.model_dump(mode='json') for turn in request.conversation], 'selection': request.selection.model_dump(mode='json') if request.selection is not None else None, 'formula_latex': request.formula_ink.source_latex if request.formula_ink is not None else None, 'verified_context': verified_context, 'user_message': request.message}
    chatbot_streamed = False
    complete_text = getattr(adapter, 'complete_text', None)
    if not document_write_authorized and callable(complete_text):

        def publish_chat_delta(delta: str) -> None:
            nonlocal chatbot_streamed
            if not delta or on_delta is None:
                return
            chatbot_streamed = True
            on_delta(delta)
        response = complete_text(system_prompt=EXISTING_BOARD_CHAT_INSTRUCTIONS, user_prompt=json.dumps(turn_payload, ensure_ascii=False), image_inputs=image_inputs, **{'source_delivery': original_delivery} if original_delivery is not None else {}, is_cancelled=is_cancelled, on_activity=on_agent_activity, on_text_delta=publish_chat_delta, **{'preserve_image_bytes': True} if preserve_image_bytes else {})
        chatbot_message = response.output_text.strip()
        candidate_markdown = codex_board_text
    else:
        response = adapter.parse_structured(system_prompt=STRUCTURED_EXISTING_BOARD_INSTRUCTIONS, user_prompt=json.dumps({**turn_payload, 'response_contract': _StructuredExistingBoardTurn.model_json_schema()}, ensure_ascii=False), schema=_StructuredExistingBoardTurn, image_inputs=image_inputs, **{'source_delivery': original_delivery} if original_delivery is not None else {}, on_activity=on_agent_activity, **{'preserve_image_bytes': True} if preserve_image_bytes else {}, **{'is_cancelled': is_cancelled} if is_cancelled is not None else {})
        output = _StructuredExistingBoardTurn.model_validate(response.output_parsed)
        chatbot_message = output.chatbot_message.strip()
        candidate_markdown = output.board_markdown
    if is_cancelled is not None and is_cancelled():
        raise CodexTurnCancelledError('Board turn was cancelled')
    execution_source = 'pi' if model_selection.agent_backend == 'pi' else model_selection.provider
    if not chatbot_message:
        raise RuntimeError(f'{execution_source} completed without a learner-facing response')
    if document_write_authorized:
        if looks_like_html_content(candidate_markdown):
            raise CodexAppServerError(f'{execution_source} board output contains HTML instead of Markdown')
        if len(candidate_markdown.encode('utf-8')) > _board_max_bytes():
            raise CodexAppServerError(f'{execution_source} board output exceeds the configured size limit')
    else:
        candidate_markdown = codex_board_text
    current_document = initial_lesson.board_document
    if candidate_markdown == codex_board_text:
        next_document = current_document
    else:
        rebuilt_document = build_document(title=current_document.title, content_text=candidate_markdown, document_id=current_document.id, page_settings=current_document.page_settings)
        next_document = _restore_preserved_visuals(rebuilt_document, preserved_visuals)
        if source_context is not None and source_context.insertion_plan.items:
            placements = derive_board_visual_placements(next_document, plan=source_context.insertion_plan)

            def resolve_visual_bytes(visual_id: str):
                return source_context.visual_assets.get(visual_id)
            visual_result = apply_board_insertion_plan(next_document, plan=source_context.insertion_plan, placements=placements, owner_user_id=user_id, lesson_id=initial_lesson.id, visual_bytes_resolver=resolve_visual_bytes, preserved_document=current_document)
            next_document = visual_result.document
    changed = document_changed(current_document, next_document)
    follow_up_suggestions = generate_follow_up_suggestions(adapter=adapter, user_message=request.message, assistant_message=chatbot_message, board_state='non_empty', workflow_state='board_changed' if changed else 'conversation') if document_write_authorized else []
    pending_write_offer_after = build_pending_board_write_offer_after(board_write_decision, question=request.message, content=chatbot_message)
    workspace = workspace_state.load_workspace_for_user(user_id)
    package, lesson = workspace_state.find_lesson_package(workspace, lesson_id)
    if lesson.history_graph.current_branch != branch_name or current_head_commit(lesson).id != base_commit_id:
        raise CodexAppServerError(f'The lesson changed while {execution_source} was working')
    clarification = _neutral_clarification()
    if is_cancelled is not None and is_cancelled():
        raise CodexTurnCancelledError('Board turn was cancelled')
    lesson.board_teaching_guide = None
    lesson.board_teaching_progress = None
    lesson.learning_requirements = None
    lesson.board_task_requirements = None
    commit_operations(lesson, operations=[], label=f'{execution_source} document update' if changed else f'{execution_source} conversation', message=f'{execution_source} completed the user turn.', new_document=next_document, metadata={'kind': 'board_document_edit' if changed else 'basic_chat', 'user_message': request.message, 'assistant_message': chatbot_message, 'assistant_message_source': execution_source, 'follow_up_suggestions': follow_up_suggestions, 'interaction_mode': request.interaction_mode, 'selection': request.selection.model_dump(mode='json') if request.selection is not None else None, 'verified_source_reference_used': source_context is not None, 'document_changed': changed, 'document_write_authorized': document_write_authorized, 'pending_board_write_offer_after': pending_write_offer_after, 'board_state_before': board_state_before, 'board_state_after': _board_state(next_document.content_text), 'document_hash_before': _text_hash(current_document.content_text), 'document_hash_after': _text_hash(next_document.content_text), 'ai_provider': model_selection.provider, 'ai_model': model_selection.model, 'agent_backend': model_selection.agent_backend, 'agent_activity': [event.model_dump(mode='json') for event in response.activity], 'active_requirement_sheet_after': None, 'active_board_task_sheet_after': None, 'learning_clarification_after': clarification.model_dump(mode='json'), 'requirement_cleared': True, 'board_task_cleared': True, 'decision_trace': board_write_decision_trace(board_write_decision, document_write_authorized=document_write_authorized, document_changed=changed)})
    if not workspace_state.save_lesson_for_user_if_head(user_id, lesson, expected_branch_name=branch_name, expected_head_commit_id=base_commit_id):
        raise CodexAppServerError(f'The lesson changed while {execution_source} was working')
    if changed:
        from openclass_core.services.google_sources.turn_evidence import remember_document_numbering
        remember_document_numbering(candidate_markdown)
    if on_delta is not None and (not chatbot_streamed):
        on_delta(chatbot_message)
    workspace = workspace_state.load_workspace_for_user(user_id)
    package, lesson = workspace_state.find_lesson_package(workspace, lesson_id)
    return build_existing_board_response(lesson_title=lesson.title, course_package=workspace_state.package_view_for_lesson(workspace, package, lesson.id), chatbot_message=chatbot_message, follow_up_suggestions=follow_up_suggestions, agent_activity=response.activity, clarification=clarification, changed=changed, no_change_reason=board_write_decision.reason, changed_reason='The user authorized a board change and the selected agent changed the document.')

def process_codex_chat_on_lesson(lesson_id: str, request: ChatRequest, *, user_id: str, on_delta: Callable[[str], None] | None=None, on_requirement_update: Callable[[dict[str, object]], None] | None=None, on_agent_activity: Callable[[AgentActivityEvent], None] | None=None, on_document_commit: Callable[[str, str], None] | None=None, is_cancelled: Callable[[], bool] | None=None, prepared_source_access=None) -> ChatResponse:
    from openclass_core.services.teaching_policy import current_teaching_decision, default_board_teaching, learning_answer_mode
    with _turn_lock(user_id):
        initial_workspace = workspace_state.load_workspace_for_user(user_id)
        initial_package, initial_lesson = workspace_state.find_lesson_package(initial_workspace, lesson_id)
        initial_package.active_lesson_id = initial_lesson.id
        branch_name = initial_lesson.history_graph.current_branch
        base_commit_id = current_head_commit(initial_lesson).id
        board_state_before = _board_state(initial_lesson.board_document.content_text)
        model_selection = _text_model_selection(request, user_id=user_id)
        selected_model = model_selection.model
        selected_model_supports_images = text_model_supports_image_inputs(model_selection, user_id=user_id)
        visual_model_identity = ':'.join((part for part in (model_selection.provider, model_selection.model, model_selection.access_method or '') if part))
        adapter = build_ai_execution_adapter(model_selection, owner_user_id=user_id, board_runner=_run_codex_board_generation, image_analysis_runner=_run_codex_visual_analysis, supports_source_visual_tools=selected_model_supports_images)
        verified_attachments = verify_chat_attachments(owner_user_id=user_id, package_id=initial_package.id, attachments=request.attachments)
        prepared_attachments = prepare_chat_attachments(attachments=verified_attachments)
        from openclass_core.services.original_attachment_delivery import original_attachment_batch
        attachment_delivery = original_attachment_batch(verified_attachments, prepared_attachments, owner_user_id=user_id, package_id=initial_package.id)
        source_query_scope = _source_query_scope_for_turn(request, verified_attachments)
        from openclass_core.services.source_retrieval_policy import decide_source_access, SourceAccessPlan, source_access_context
        source_access_plan = prepared_source_access or SourceAccessPlan()
        source_plan_attachment_offset = 0
        has_explicit_source_selection = any((item is not None and item.kind == 'source' for item in [request.selection, *request.selections]))
        if prepared_source_access is None and source_query_scope is None and (not has_explicit_source_selection):
            source_access_plan = decide_source_access(owner_user_id=user_id, package_id=initial_package.id, message=request.message, adapter=adapter, is_cancelled=is_cancelled)
        for event in source_access_plan.activity:
            if on_agent_activity is not None:
                on_agent_activity(event)
        if current_teaching_decision() is None and native_pi_teaching_enabled() and (board_state_before == 'empty' or has_resumable_native_pi_teaching_task(user_id=user_id, lesson_id=lesson_id)):
            return process_native_pi_teaching_turn(lesson_id=lesson_id, request=request.model_copy(update={'source_query_scope': source_query_scope}), source_access_mode='none' if source_access_plan.clarification else source_access_plan.mode, source_access_refs=source_access_plan.refs, user_id=user_id, model=model_selection, on_delta=on_delta, on_activity=on_agent_activity, on_document_commit=on_document_commit, is_cancelled=is_cancelled)
        if source_access_plan.mode != 'none':
            from dataclasses import replace
            context = source_access_context(source_access_plan, owner_user_id=user_id, package_id=initial_package.id, lesson_id=lesson_id, query=request.message, is_cancelled=is_cancelled)
            source_plan_attachment_offset = len(prepared_attachments.image_inputs)
            if source_access_plan.image_inputs and (not selected_model_supports_images):
                raise CodexAppServerError('The selected model cannot read original source images. Choose an image-capable model; search snippets cannot replace them.')
            prepared_attachments = replace(prepared_attachments, prompt_context='\n\n'.join((part for part in (prepared_attachments.prompt_context, context) if part)), image_inputs=[*prepared_attachments.image_inputs, *source_access_plan.image_inputs])
        source_board_plan = None
        source_board_requested = bool(board_state_before == 'empty' and source_query_scope is not None and (request.board_generation_action == 'start' or default_board_teaching() or has_explicit_document_mutation_request(request.message, has_board_selection=False)))
        if source_board_requested and source_query_scope is not None:
            try:
                all_ready_retrieval = source_retrieval_service.retrieve(owner_user_id=user_id, package_id=initial_package.id, lesson_id=lesson_id, query=request.message, scope=source_query_scope, calculation_adapter=adapter, is_cancelled=is_cancelled) if source_query_scope.mode == 'all_ready_sources' else None
                source_board_plan = resolve_source_query_grounded_board_plan(owner_user_id=user_id, lesson=initial_lesson, scope=source_query_scope, query=request.message, retrieval_bundle=all_ready_retrieval.bundle if all_ready_retrieval else None, visual_adapter=adapter, visual_model_supports_images=selected_model_supports_images, visual_model_identity=visual_model_identity, is_cancelled=is_cancelled, on_activity=on_agent_activity)
            except (SourceGroundedBoardError, SourceRetrievalError) as exc:
                raise CodexAppServerError(str(exc)) from exc
            request = request.model_copy(update={'source_query_scope': None})
            source_query_scope = None
        source_retrieval = None
        if source_query_scope is not None:
            try:
                source_retrieval = source_retrieval_service.retrieve(owner_user_id=user_id, package_id=initial_package.id, lesson_id=lesson_id, query=request.message, scope=source_query_scope, calculation_adapter=adapter, is_cancelled=is_cancelled)
                if source_retrieval.image_inputs and (not selected_model_supports_images):
                    raise CodexAppServerError('The selected model does not support original PDF page images. Choose an image-capable model. OCR and text extraction will not be used as fallbacks.')
            except SourceRetrievalError as exc:
                raise CodexAppServerError(str(exc)) from exc
            prepared_attachments = _with_source_retrieval_context(prepared_attachments, source_retrieval)
        readonly_source_question = bool(prepared_source_access is not None and source_access_plan.decision.readonly_source_question)
        if readonly_source_question:
            if source_access_plan.grounding is None:
                raise CodexAppServerError(source_access_plan.clarification or 'No verified evidence is available for this source question')
            bundle = source_evidence_store.get_bundle(owner_user_id=user_id, bundle_id=source_access_plan.grounding.confirmed_bundle_id)
            if bundle is None or bundle.package_id != initial_package.id:
                raise CodexAppServerError('Verified source question evidence is unavailable')
            from openclass_core.services.google_sources.turn_evidence import to_citations
            source_retrieval = SourceRetrievalResult(bundle=bundle, citations=to_citations(bundle.evidence_items), prompt_context=prepared_attachments.prompt_context, image_inputs=source_access_plan.image_inputs, image_manifest=source_access_plan.image_manifest, preserve_image_bytes=bool(source_access_plan.image_inputs))
        source_qa_is_read_only = (readonly_source_question or not has_explicit_document_mutation_request(request.message, has_board_selection=bool(request.selection is not None and request.selection.kind == 'board'))) and (not (board_state_before == 'empty' and default_board_teaching()))
        if source_retrieval is not None and source_qa_is_read_only and (request.board_generation_action is None) and (request.teaching_action is None):
            return _process_source_qa_turn(lesson_id=lesson_id, request=request, user_id=user_id, adapter=adapter, model_selection=model_selection, initial_lesson=initial_lesson, branch_name=branch_name, base_commit_id=base_commit_id, board_state_before=board_state_before, retrieval=source_retrieval, on_delta=on_delta, on_agent_activity=on_agent_activity, attachment_delivery=attachment_delivery, attachment_image_inputs=prepared_attachments.image_inputs[:-len(source_retrieval.image_inputs)] if source_retrieval.image_inputs else prepared_attachments.image_inputs, is_cancelled=is_cancelled)
        if learning_answer_mode() is not None and (board_state_before == 'empty' or request.teaching_action is None):
            from openclass_core.services.readonly_learning import answer_without_board
            return answer_without_board(lesson_id=lesson_id, request=request, user_id=user_id, adapter=adapter, verified_context=prepared_attachments.prompt_context, image_inputs=prepared_attachments.image_inputs, preserve_image_bytes=bool(source_access_plan.image_inputs), on_delta=on_delta, on_activity=on_agent_activity, is_cancelled=is_cancelled)
        if board_state_before == 'empty':
            uses_structured_adapter = model_selection.agent_backend == 'pi' or model_selection.provider == 'deepseek'

            def generate_with_selected_adapter(selected_user_id, _selected_model, requirement, teaching_plan, content_extent, selected_is_cancelled, selected_on_activity):
                return _generate_blank_board_with_adapter(adapter=adapter, user_id=selected_user_id, package_id=initial_package.id, requirement=requirement, teaching_plan=teaching_plan, content_extent=content_extent, include_raster_images=bool(selected_model_supports_images and _uses_original_pdf_pages(requirement)), is_cancelled=selected_is_cancelled, on_activity=selected_on_activity)
            return process_blank_board_turn(lesson=initial_lesson, request=request, user_id=user_id, model=selected_model, provider=model_selection.provider, adapter=adapter, conversation_text='\n\n'.join((item for item in (_conversation_context(request.conversation), _merge_handoff_context(initial_lesson), prepared_attachments.prompt_context) if item)), on_delta=on_delta, on_requirement_update=on_requirement_update, on_agent_activity=on_agent_activity, on_document_commit=on_document_commit, is_cancelled=is_cancelled, generate_board=generate_with_selected_adapter if uses_structured_adapter else _generate_blank_board, discard_generated_thread=(lambda _thread_id: None) if uses_structured_adapter else lambda thread_id: _discard_uncommitted_thread(thread_id, user_id=user_id), source_plan=source_board_plan, source_grounding=source_access_plan.grounding, visual_model_supports_images=selected_model_supports_images, visual_model_identity=visual_model_identity)
        from openclass_core.services.auto_board_teaching import continue_board_teaching, start_board_teaching
        from openclass_core.services.board_teaching_turn_decision import BoardTeachingDecisionResult, decide_board_teaching_turn
        teaching_decision = BoardTeachingDecisionResult()
        if request.teaching_action is None:
            teaching_decision = decide_board_teaching_turn(owner_user_id=user_id, lesson_id=lesson_id, adapter=adapter, user_message=request.message, has_selection=request.selection is not None)
        natural_teaching_action = teaching_decision.decision.action
        if request.teaching_action is not None or natural_teaching_action != 'none':
            if request.teaching_action is None and natural_teaching_action == 'start':
                teaching_result = start_board_teaching(owner_user_id=user_id, lesson_id=lesson_id, adapter=adapter, target_heading=teaching_decision.decision.target_heading, user_message=request.message, on_delta=on_delta, on_activity=on_agent_activity, is_cancelled=is_cancelled)
            else:
                restart = request.teaching_action == 'restart' if request.teaching_action is not None else natural_teaching_action == 'restart'
                teaching_result = continue_board_teaching(owner_user_id=user_id, lesson_id=lesson_id, adapter=adapter, restart=restart, user_message=request.message, on_delta=on_delta, on_activity=on_agent_activity, is_cancelled=is_cancelled)
            teaching_activity = [*teaching_decision.activity, *teaching_result.activity]
            for event in teaching_activity:
                if on_agent_activity is not None:
                    on_agent_activity(event)
            if teaching_result.chatbot_message and on_delta is not None and (not teaching_result.chatbot_streamed):
                on_delta(teaching_result.chatbot_message)
            workspace = workspace_state.load_workspace_for_user(user_id)
            package, lesson = workspace_state.find_lesson_package(workspace, lesson_id)
            return ChatResponse(chatbot_message=teaching_result.chatbot_message, follow_up_suggestions=teaching_result.follow_up_suggestions, agent_activity=teaching_activity, learning_requirement_sheet=build_requirements(lesson.title), active_requirement_sheet=None, learning_clarification=_neutral_clarification(), board_task_sheet=teaching_result.board_task, active_board_task_sheet=None, board_task_run_id=teaching_result.board_task_run_id, board_task_version_id=teaching_result.board_task_version_id, board_task_phase='consumed' if teaching_result.status == 'succeeded' else 'not_executed', board_task_questions=[], board_decision=BoardDecision(action='no_change', reason='The Board AI authorized a bounded section explanation.'), requirement_cleared=True, board_document_operation_status='none', teaching_progress=teaching_result.progress, auto_teaching_operation_status=teaching_result.status, auto_teaching_operation_failure_reason=teaching_result.failure_reason, course_package=workspace_state.package_view_for_lesson(workspace, package, lesson.id))
        pending_write_offer = load_pending_board_write_offer(initial_lesson)
        board_write_decision = decide_board_write_action(message=request.message, interaction_mode=request.interaction_mode, has_pending_offer=pending_write_offer is not None, has_board_selection=bool(request.selection is not None and request.selection.kind == 'board'))
        source_context = None
        if request.selection is not None and request.selection.kind == 'source':
            try:
                source_context = _prepare_existing_board_source_context(owner_user_id=user_id, lesson=initial_lesson, selection=request.selection, adapter=adapter, include_raster_images=selected_model_supports_images, visual_model_identity=visual_model_identity, is_cancelled=is_cancelled, on_activity=on_agent_activity)
            except SourceGroundedBoardError as exc:
                raise CodexAppServerError(str(exc)) from exc
        attachment_context = prepared_attachments.prompt_context
        if source_access_plan.image_inputs:
            source_image_offset = len(source_context.image_inputs if source_context is not None else []) + source_plan_attachment_offset
            attachment_context += '\nAuthoritative original source image positions in the final attachment list (replace bundle-local positions):\n' + json.dumps([{**item, 'image_index': int(item['image_index']) + source_image_offset} for item in source_access_plan.image_manifest], ensure_ascii=False)
        if source_retrieval is not None and source_retrieval.image_inputs:
            image_offset = len(source_context.image_inputs if source_context is not None else []) + len(prepared_attachments.image_inputs) - len(source_retrieval.image_inputs)
            attachment_context += '\nOriginal source images (zero-based positions in the complete attachment list):\n' + json.dumps([{**item, 'image_index': int(item['image_index']) + image_offset} for item in source_retrieval.image_manifest], ensure_ascii=False)
        original_delivery = None
        deliveries = [value for value in (getattr(source_context, 'original_delivery', None), attachment_delivery, getattr(source_access_plan, 'original_delivery', None), getattr(source_retrieval, 'original_delivery', None)) if value is not None]
        if deliveries:
            from dataclasses import replace
            from openclass_core.services.source_content_contract import SourceRoleContext
            from openclass_core.services.original_delivery_group import OriginalDeliveryGroup
            from openclass_core.services.original_role_context import materialize_original_context
            actual_context = materialize_original_context(SourceRoleContext('', original_delivery=OriginalDeliveryGroup(deliveries)))
            original_delivery = actual_context.original_delivery
            attachment_context = str(actual_context)
            prepared_attachments = replace(prepared_attachments, image_inputs=actual_context.image_inputs)
            if source_context is not None:
                source_context = replace(source_context, prompt_context='', image_inputs=[], original_delivery=original_delivery)
        if model_selection.agent_backend == 'pi' or model_selection.provider == 'deepseek':
            return _process_structured_existing_board_turn(lesson_id=lesson_id, request=request, user_id=user_id, adapter=adapter, model_selection=model_selection, initial_lesson=initial_lesson, branch_name=branch_name, base_commit_id=base_commit_id, board_state_before=board_state_before, board_write_decision=board_write_decision, pending_write_offer=pending_write_offer, source_context=source_context, attachment_context=attachment_context, image_inputs=(source_context.image_inputs if source_context is not None else []) + prepared_attachments.image_inputs + _formula_image_urls(request), on_delta=on_delta, on_agent_activity=on_agent_activity, is_cancelled=is_cancelled, preserve_image_bytes=bool(source_context is not None and source_context.preserve_image_bytes or (source_retrieval is not None and source_retrieval.preserve_image_bytes) or source_access_plan.image_inputs), original_delivery=original_delivery)
        codex_model = selected_model
        prior_thread_id, prior_turn_id = _thread_reference_for_current_branch(initial_lesson)
        workspace_key = _workspace_key(user_id=user_id, lesson_id=lesson_id, branch_name=branch_name)
        workspace_root = codex_workspace_root()
        workspace_root.mkdir(parents=True, exist_ok=True, mode=448)
        with tempfile.TemporaryDirectory(prefix=f'{workspace_key[:16]}-', dir=workspace_root) as temporary_workspace:
            workspace_path = Path(temporary_workspace)
            codex_board_text, preserved_visuals = _document_for_codex(initial_lesson.board_document)
            board_path = _prepare_workspace(workspace_path, codex_board_text)
            verified_context = '\n\n'.join((item for item in (source_context.prompt_context if source_context is not None else '', attachment_context, _merge_handoff_context(initial_lesson)) if item))
            user_prompt = _turn_prompt(request, is_new_thread=prior_thread_id is None, board_state=board_state_before, verified_source_context=verified_context, board_write_decision=board_write_decision, pending_board_write_offer=pending_write_offer)
            codex_reasoning_effort = _codex_reasoning_effort(request)
            codex_service_tier, codex_service_tier_is_set = _codex_service_tier(request)
            quota_stop = threading.Event()
            quota_exceeded = threading.Event()
            quota_monitor = threading.Thread(target=_watch_board_quota, kwargs={'board_path': board_path, 'max_bytes': _board_max_bytes(), 'stop_event': quota_stop, 'quota_exceeded': quota_exceeded}, daemon=True)
            quota_monitor.start()

            def turn_is_cancelled() -> bool:
                return quota_exceeded.is_set() or bool(is_cancelled and is_cancelled())
            result = None
            try:
                from contextlib import nullcontext
                from openclass_core.services.original_source_delivery import bind_original_source_batch
                original_scope = bind_original_source_batch(original_delivery) if original_delivery is not None else nullcontext()
                with original_scope:
                    result = run_codex_thread_turn(user_id=user_id, model=codex_model, cwd=workspace_path, user_prompt=user_prompt, fallback_user_prompt=_turn_prompt(request, is_new_thread=True, board_state=board_state_before, verified_source_context=verified_context, board_write_decision=board_write_decision, pending_board_write_offer=pending_write_offer) if prior_thread_id is not None else user_prompt, developer_instructions=CODEX_DEVELOPER_INSTRUCTIONS, thread_id=prior_thread_id, last_turn_id=prior_turn_id, image_urls=(source_context.image_inputs if source_context is not None else []) + prepared_attachments.image_inputs + _formula_image_urls(request), on_delta=on_delta, on_activity=on_agent_activity, is_cancelled=turn_is_cancelled, reasoning_effort=codex_reasoning_effort, service_tier=codex_service_tier, service_tier_is_set=codex_service_tier_is_set)
            except CodexTurnCancelledError as exc:
                if quota_exceeded.is_set():
                    raise CodexAppServerError('Codex board output exceeds the configured size limit') from exc
                raise
            finally:
                quota_stop.set()
                quota_monitor.join(timeout=0.2)
            if quota_exceeded.is_set():
                if result is not None:
                    _discard_uncommitted_thread(result.thread_id, user_id=user_id)
                raise CodexAppServerError('Codex board output exceeds the configured size limit')
            assert result is not None
            try:
                codex_content = _read_validated_board(workspace_path)
                workspace = workspace_state.load_workspace_for_user(user_id)
                package, lesson = workspace_state.find_lesson_package(workspace, lesson_id)
                package.active_lesson_id = lesson.id
                if lesson.history_graph.current_branch != branch_name:
                    raise CodexAppServerError('The lesson branch changed while Codex was working')
                if current_head_commit(lesson).id != base_commit_id:
                    raise CodexAppServerError('The lesson changed while Codex was working')
                current_document = lesson.board_document
                document_change_attempted = codex_content != codex_board_text
                document_write_authorized = board_write_decision.action in {'edit_now', 'confirm_offered_write'}
                unauthorized_document_change_blocked = document_change_attempted and (not document_write_authorized)
                if not document_change_attempted or not document_write_authorized:
                    next_document = current_document
                else:
                    rebuilt_document = build_document(title=current_document.title, content_text=codex_content, document_id=current_document.id, page_settings=current_document.page_settings)
                    next_document = _restore_preserved_visuals(rebuilt_document, preserved_visuals)
                    if source_context is not None and source_context.insertion_plan.items:
                        placements = derive_board_visual_placements(next_document, plan=source_context.insertion_plan)

                        def resolve_visual_bytes(visual_id: str):
                            return source_context.visual_assets.get(visual_id)
                        visual_result = apply_board_insertion_plan(next_document, plan=source_context.insertion_plan, placements=placements, owner_user_id=user_id, lesson_id=lesson.id, visual_bytes_resolver=resolve_visual_bytes, preserved_document=current_document)
                        next_document = visual_result.document
                changed = document_changed(current_document, next_document)
                follow_up_suggestions = generate_follow_up_suggestions(adapter=CodexAIExecutionAdapter(owner_user_id=user_id, model=codex_model), user_message=request.message, assistant_message=result.final_response, board_state=board_state_before, workflow_state='board_changed' if changed else 'conversation')
                lesson.board_teaching_guide = None
                lesson.board_teaching_progress = None
                lesson.learning_requirements = None
                lesson.board_task_requirements = None
                clarification = _neutral_clarification()
                pending_write_offer_after = build_pending_board_write_offer_after(board_write_decision, question=request.message, content=result.final_response)
                metadata = {'kind': 'board_document_edit' if changed else 'basic_chat', 'user_message': request.message, 'assistant_message': result.final_response, 'assistant_message_source': 'codex', 'follow_up_suggestions': follow_up_suggestions, 'interaction_mode': request.interaction_mode, 'selection': request.selection.model_dump(mode='json') if request.selection is not None else None, 'chat_attachments': prepared_attachments.metadata, 'verified_source_reference_used': source_context is not None, 'verified_source_bundle_ids': [reference.evidence_bundle_id for reference in source_context.requirement.source_grounding.confirmed_references] if source_context is not None else [], 'verified_source_chapter_ids': [reference.source_chapter_id for reference in source_context.requirement.source_grounding.confirmed_references if reference.source_chapter_id] if source_context is not None else [], 'verified_source_evidence_ids': [evidence.id for evidence in source_context.requirement.source_grounding.frozen_evidence] if source_context is not None else [], 'document_changed': changed, 'document_change_attempted': document_change_attempted, 'document_write_authorized': document_write_authorized, 'unauthorized_document_change_blocked': unauthorized_document_change_blocked, 'pending_board_write_offer_after': pending_write_offer_after, 'board_state_before': board_state_before, 'board_state_after': _board_state(next_document.content_text), 'document_hash_before': _text_hash(current_document.content_text), 'document_hash_after': _text_hash(next_document.content_text), 'codex_thread_id': result.thread_id, 'codex_turn_id': result.turn_id, 'codex_parent_thread_id': result.parent_thread_id, 'codex_replaced_stale_thread_id': result.replaced_stale_thread_id, 'codex_model': codex_model, 'codex_reasoning_effort': codex_reasoning_effort, 'codex_service_tier': codex_service_tier, 'codex_service_tier_is_set': codex_service_tier_is_set, 'codex_branch': branch_name, 'codex_base_commit_id': base_commit_id, 'agent_activity': [event.model_dump(mode='json') for event in result.activity], 'active_requirement_sheet_after': None, 'active_board_task_sheet_after': None, 'learning_clarification_after': clarification.model_dump(mode='json'), 'requirement_cleared': True, 'board_task_cleared': True, 'decision_trace': board_write_decision_trace(board_write_decision, document_write_authorized=document_write_authorized, document_changed=changed)}
                commit_operations(lesson, operations=[], label='Codex document update' if changed else 'Codex conversation', message='Codex completed the user turn.', new_document=next_document, metadata=metadata)
                saved = workspace_state.save_lesson_for_user_if_head(user_id, lesson, expected_branch_name=branch_name, expected_head_commit_id=base_commit_id)
                if not saved:
                    raise CodexAppServerError('The lesson changed while Codex was working')
                if changed:
                    from openclass_core.services.google_sources.turn_evidence import remember_document_numbering
                    remember_document_numbering(codex_content)
                workspace = workspace_state.load_workspace_for_user(user_id)
                package, lesson = workspace_state.find_lesson_package(workspace, lesson_id)
                return build_existing_board_response(lesson_title=lesson.title, course_package=workspace_state.package_view_for_lesson(workspace, package, lesson.id), chatbot_message=result.final_response, follow_up_suggestions=follow_up_suggestions, agent_activity=result.activity, clarification=clarification, changed=changed, no_change_reason=board_write_decision.reason, changed_reason='The user authorized a board change and Codex changed the document.')
            except Exception:
                _discard_uncommitted_thread(result.thread_id, user_id=user_id)
                raise

def document_ai_edit_request(lesson_id: str, instruction: str, selection_text: str | None, conversation: list[ConversationTurn], *, user_id: str) -> ChatResponse:
    selection = SelectionRef(kind='board', excerpt=selection_text, location_kind='target_range') if selection_text else None
    return process_codex_chat_on_lesson(lesson_id, ChatRequest(message=instruction, interaction_mode='direct_edit', selection=selection, conversation=conversation), user_id=user_id)
