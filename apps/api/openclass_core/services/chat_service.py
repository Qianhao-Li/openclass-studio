from __future__ import annotations
import hashlib
import json
import sqlite3
from collections import OrderedDict
from collections.abc import Callable
from contextlib import nullcontext
from concurrent.futures import Future
from threading import Lock
from time import monotonic, sleep
from uuid import uuid4
from openclass_core.models import AgentActivityEvent, AIModelSelection, BranchRef, ChatRequest, ChatResponse, ConversationTurn, DecisionTrace, SelectionRef
from openclass_core.services import workspace_state
from openclass_core.services.ai_model_catalog import resolve_text_model_selection
from openclass_core.services.chat_turn_gate import TurnGateResult, build_turn_envelope, complete_non_learning_turn, evaluate_turn_gate
from openclass_core.services.codex_chat import process_codex_chat_on_lesson
from openclass_core.services.codex_app_server import CodexTurnCancelledError
from openclass_core.services.existing_board.interaction_session import InteractionSession
from openclass_core.services.existing_board.interaction_workflow import ExistingBoardInteractionReroute
from openclass_core.services.existing_board.workflow import ExistingBoardAnswerThenOfferReroute, process_existing_board_workflow
from openclass_core.services.history import bind_commit_metadata
from openclass_core.services.lesson_title import maybe_generate_lesson_title
from openclass_core.services.native_pi_teaching_agent import has_resumable_native_pi_teaching_task, process_native_pi_teaching_turn
from openclass_core.services.pending_teaching_offer import PENDING_TEACHING_OFFER_METADATA_KEY, PendingTeachingOffer, PendingTeachingOfferDecision, PendingTeachingOfferDecisionResult, decide_pending_teaching_offer, pending_teaching_offer_from_runtime_state
from openclass_core.services.ridoc_chat_context import authoritative_routing_request, bind_ridoc_chat_context, create_ridoc_chat_context, current_context_epoch, current_context_scope, guard_cached_response, request_with_authoritative_conversation, ridoc_completion_guard
from openclass_core.services.ridoc_turns import RidocTurnOutcome, commit_ridoc_turn_outcome
from openclass_core.services.ridoc_v3 import RidocV3Error
from openclass_core.services.ridoc_workspace_store import document_at_history_node, resolve_conversation_branch_parent
from openclass_core.services.teaching_agent_mode import native_pi_teaching_enabled
from openclass_core.services.teaching_policy import assert_board_unchanged, bind_learning_answer, bind_teaching_decision, without_write_controls
from openclass_core.services.teaching_attachments import bind_teaching_attachments, prepare_teaching_attachments
from openclass_core.services.turn_intent import has_explicit_board_interaction_request, has_explicit_board_location_cue, has_explicit_document_mutation_request
_IDEMPOTENCY_CACHE_LIMIT = 256
_PERSISTED_IDEMPOTENCY_WAIT_SECONDS = 30.0
_PERSISTED_IDEMPOTENCY_POLL_SECONDS = 0.02
_idempotency_lock = Lock()
_idempotency_cache: OrderedDict[tuple[str, str, str], tuple[str, ChatResponse]] = OrderedDict()
_idempotency_inflight: dict[tuple[str, str, str], tuple[str, Future[ChatResponse]]] = {}

def _replay_completed_ridoc_turn(store, archive, request, *, user_id, package_id, lesson_id):
    """Replay an authoritative completed transaction without reopening its sources."""
    from openclass_core.models import BoardDecision
    from openclass_core.services.chat.preserved_state import preserved_learning_response_state
    from openclass_core.services.ridoc_v3 import RidocMessageNode
    from openclass_core.services.ridoc_source_citations import source_citations_for_node
    matches = [node for node in archive.nodes.values() if isinstance(node, RidocMessageNode) and (node.lesson_id == lesson_id or node.decision_metadata.get('origin_lesson_id') == lesson_id) and (node.transaction_id == request.turn_id)]
    if not matches:
        return None
    action = request.history_branch_action
    if len(matches) != 1 or matches[0].user_message != request.message.strip():
        raise RidocV3Error('The completed turn identity does not match this request')
    node = matches[0]
    if (node.branch_origin_kind, node.branch_origin_node_id) != (action.kind if action else None, action.source_node_id if action else ''):
        raise RidocV3Error('The completed turn identity belongs to a different history action')
    citations = source_citations_for_node(archive, node)
    package = store.load_package(user_id, package_id=package_id)
    lesson = next((item for item in package.lessons if item.id == node.lesson_id))
    return ChatResponse(chatbot_message=node.assistant_message, source_citations=citations, turn_decision=node.decision_metadata.get('turn_decision'), board_decision=BoardDecision(action='no_change', reason='This completed turn was replayed from the verified course.'), course_package=workspace_state.package_view(package, is_standalone=package.package_kind == 'standalone', resource_lesson_id=node.lesson_id, isolate_lesson_resources=package.package_kind == 'standalone'), ridoc_revision=package.ridoc_revision, **preserved_learning_response_state(lesson))

def process_chat_on_lesson(lesson_id: str, request: ChatRequest, *, user_id: str, on_delta: Callable[[str], None] | None=None, on_requirement_update: Callable[[dict[str, object]], None] | None=None, on_agent_activity: Callable[[AgentActivityEvent], None] | None=None, on_document_commit: Callable[[str, str], None] | None=None, is_cancelled: Callable[[], bool] | None=None, commit_metadata: dict[str, object] | None=None, prepared_gate: TurnGateResult | None=None, _ridoc_scoped: bool=False) -> ChatResponse:
    if not _ridoc_scoped and workspace_state.is_ridoc_lesson_for_user(user_id, lesson_id):
        expected_revision = (request.expected_ridoc_revision or '').strip()
        transaction_id = (request.turn_id or '').strip()
        if not expected_revision:
            raise RidocV3Error('RIDOC revision is required; reload before chatting')
        if not (request.session_id or '').strip() or not transaction_id or (not (request.input_event_id or '').strip()):
            raise RidocV3Error('RIDOC chat requires session_id, turn_id, and input_event_id')
        store = workspace_state.get_ridoc_workspace_store()
        package_id = store.package_id_for_lesson(user_id, lesson_id)
        if package_id is None:
            raise RidocV3Error('RIDOC lesson is unavailable')
        context = create_ridoc_chat_context(store, owner_user_id=user_id, package_id=package_id, lesson_id=lesson_id, request=request)
        archive = store.load_archive(user_id, package_id=package_id)
        replay = _replay_completed_ridoc_turn(store, archive, request, user_id=user_id, package_id=package_id, lesson_id=lesson_id)
        if replay is not None:
            context.assert_current()
            return replay
        branch_action = request.history_branch_action
        branch_parent_id: str | None = None
        branch_document = None
        if branch_action is not None:
            source_node, branch_parent_id = resolve_conversation_branch_parent(archive, lesson_id, branch_action.kind, branch_action.source_node_id)
            if branch_action.kind == 'regenerate' and request.message.strip() != source_node.user_message:
                raise RidocV3Error('RIDOC regenerated answers must reuse the original user message')
            branch_document = document_at_history_node(archive, lesson_id, branch_parent_id)
        request = request_with_authoritative_conversation(request, archive, lesson_id, parent_id=branch_parent_id)
        historical_checkpoint = None
        if branch_action is not None:
            from openclass_core.services.ridoc_historical_requirement import HistoricalRequirementCheckpoint
            historical_checkpoint = HistoricalRequirementCheckpoint(store=store, context=context, request=request, document=branch_document, parent_id=branch_parent_id)
        pending_payload = {'session_id': request.session_id, 'input_event_id': request.input_event_id, 'history_branch_action': branch_action.model_dump(mode='json') if branch_action is not None else None}
        if historical_checkpoint is not None:
            pending_payload = historical_checkpoint.preserve(pending_payload)
        store.stage_runtime_turn(owner_user_id=user_id, package_id=package_id, lesson_id=lesson_id, turn_id=transaction_id, state='pending', payload=pending_payload)
        try:
            from openclass_core.services.google_sources.turn_evidence import capture_turn
            from openclass_core.services.staged_board_assets import stage_board_assets
            with bind_ridoc_chat_context(context), workspace_state.bind_ridoc_turn_workspace(user_id, lesson_id, expected_revision=expected_revision) as scope, capture_turn(document_before=branch_document or workspace_state.find_lesson_package(scope.workspace, lesson_id)[1].board_document), historical_checkpoint.bind() if historical_checkpoint is not None else nullcontext(), stage_board_assets(owner_user_id=user_id, archive=archive) as staged_assets:
                if branch_document is not None and branch_parent_id is not None:
                    _package, branch_lesson = workspace_state.find_lesson_package(scope.workspace, lesson_id)
                    pending_branch_name = '__pending_conversation_branch__'
                    branch_lesson.history_graph.branches[pending_branch_name] = BranchRef(name=pending_branch_name, head_commit_id=branch_parent_id, base_commit_id=branch_parent_id, document_cursor_commit_id=branch_parent_id)
                    branch_lesson.history_graph.current_branch = pending_branch_name
                    branch_lesson.board_document = branch_document
                    historical_checkpoint.restore(branch_lesson)
                before_lesson = workspace_state.find_lesson_package(scope.workspace, lesson_id)[1]
                before_document = before_lesson.board_document.model_copy(deep=True)
                before_lesson_title = before_lesson.title
                response = process_chat_on_lesson(lesson_id, request, user_id=user_id, on_delta=on_delta, on_requirement_update=on_requirement_update, on_agent_activity=on_agent_activity, on_document_commit=None, is_cancelled=is_cancelled, commit_metadata=commit_metadata, prepared_gate=prepared_gate, _ridoc_scoped=True)
                package, final_lesson = workspace_state.find_lesson_package(scope.workspace, lesson_id)
                source_lesson = final_lesson
                added_lessons = [item for item in package.lessons if item.id not in archive.package.lessons]
                if len(added_lessons) > 1:
                    raise RidocV3Error('One teaching turn can create at most one destination lesson')
                new_lesson_seed = None
                if added_lessons:
                    if response.turn_decision and response.turn_decision.board_write_opt_out:
                        raise RidocV3Error('This turn forbids creating a new board')
                    from openclass_core.services.rich_document import build_document
                    final_lesson = added_lessons[0]
                    new_lesson_seed = final_lesson.model_copy(deep=True)
                    new_lesson_seed.board_document = build_document(title=final_lesson.title, document_id=final_lesson.board_document.id, content_text='', page_settings=final_lesson.board_document.page_settings)
                result_lesson_id = final_lesson.id
                from openclass_core.services.google_sources.freezing import freeze_citations
                from openclass_core.services.rich_document import document_changed
                if response.turn_decision and response.turn_decision.board_write_opt_out:
                    assert_board_unchanged(before_document, final_lesson.board_document)
                    if before_lesson_title != final_lesson.title:
                        raise RuntimeError('This turn forbids changing the board title; the result was not saved.')
                if is_cancelled is not None and is_cancelled():
                    raise CodexTurnCancelledError('Chat turn was cancelled before freezing evidence')
                guard_document = final_lesson.board_document if document_changed(before_document, final_lesson.board_document.model_copy(update={'title': before_document.title})) else None
                source_evidence, source_assets, frozen_citations = freeze_citations(owner_user_id=user_id, package_id=package_id, lesson_id=result_lesson_id, citations=response.source_citations, document=guard_document)
                source_assets.update(staged_assets.document_payloads(final_lesson.board_document))
                response.source_citations = frozen_citations
                if response.turn_decision and response.turn_decision.board_write_opt_out:
                    assert_board_unchanged(before_document, final_lesson.board_document)
                outcome = RidocTurnOutcome(transaction_id=transaction_id, user_message=request.message, assistant_message=response.chatbot_message, assistant_source='openclass', document=final_lesson.board_document, new_lesson=new_lesson_seed, document_source='ai', document_label='AI document update', evidence_ids=list(source_evidence), resource_refs=list(source_assets), evidence_payloads=source_evidence, asset_payloads=source_assets, decision_metadata={**({'turn_decision': response.turn_decision.model_dump(mode='json')} if response.turn_decision is not None else {}), **({'source_citations': [citation.model_dump(mode='json') for citation in frozen_citations]} if frozen_citations else {})}, package_metadata={'title': package.title, 'summary': package.summary, 'tags': list(package.tags)}, lesson_metadata={'title': final_lesson.title, 'slug': final_lesson.slug, 'summary': final_lesson.summary, 'tags': list(final_lesson.tags)}, branch_action_kind=branch_action.kind if branch_action is not None else None, branch_source_node_id=branch_action.source_node_id if branch_action is not None else '')
                with ridoc_completion_guard(context):
                    if is_cancelled is not None and is_cancelled():
                        raise CodexTurnCancelledError('Chat turn was cancelled before its final commit')
                    commit_ridoc_turn_outcome(store, owner_user_id=user_id, package_id=package_id, lesson_id=lesson_id, expected_revision=expected_revision, outcome=outcome)
                    store.save_lesson_runtime(owner_user_id=user_id, package_id=package_id, lesson=final_lesson)
                    if new_lesson_seed is not None:
                        store.save_lesson_runtime(owner_user_id=user_id, package_id=package_id, lesson=source_lesson)
            committed_package = store.load_package(user_id, package_id=package_id)
            response.course_package = workspace_state.package_view(committed_package, is_standalone=committed_package.package_kind == 'standalone', resource_lesson_id=result_lesson_id, isolate_lesson_resources=committed_package.package_kind == 'standalone')
            response.ridoc_revision = committed_package.ridoc_revision
            if on_document_commit is not None and new_lesson_seed is None:
                from openclass_core.services.history import current_head_commit
                saved_lesson = next((item for item in committed_package.lessons if item.id == lesson_id))
                if document_changed(before_document, saved_lesson.board_document):
                    try:
                        on_document_commit(saved_lesson.board_document.content_text, current_head_commit(saved_lesson).id)
                    except Exception:
                        pass
            store.clear_runtime_turn(owner_user_id=user_id, package_id=package_id, lesson_id=lesson_id, turn_id=transaction_id)
            context.assert_current()
            return response
        except BaseException as exc:
            failed_payload = {'session_id': request.session_id, 'input_event_id': request.input_event_id}
            if historical_checkpoint is not None:
                failed_payload = historical_checkpoint.preserve(failed_payload)
            store.stage_runtime_turn(owner_user_id=user_id, package_id=package_id, lesson_id=lesson_id, turn_id=transaction_id, state='failed', payload=failed_payload, error=str(exc))
            store.audit(owner_user_id=user_id, package_id=package_id, lesson_id=lesson_id, kind='turn_failed', payload={'turn_id': transaction_id, 'error': str(exc)})
            raise
    if not _ridoc_scoped and request.history_branch_action is not None:
        raise RidocV3Error('Conversation regenerate and fork require a RIDOC course')
    if prepared_gate is not None:
        _require_matching_prepared_gate(prepared_gate, lesson_id=lesson_id, request=request, user_id=user_id)
    idempotency_key = _chat_idempotency_key(request, user_id=user_id)
    if idempotency_key is None:
        return _process_chat_on_lesson_once(lesson_id, request, user_id=user_id, on_delta=on_delta, on_requirement_update=on_requirement_update, on_agent_activity=on_agent_activity, on_document_commit=on_document_commit, is_cancelled=is_cancelled, commit_metadata=commit_metadata, prepared_gate=prepared_gate)
    fingerprint = _chat_request_fingerprint(lesson_id, request)
    with _idempotency_lock:
        cached = _idempotency_cache.get(idempotency_key)
        if cached is not None:
            cached_fingerprint, cached_response = cached
            _require_matching_fingerprint(cached_fingerprint, fingerprint)
            _idempotency_cache.move_to_end(idempotency_key)
            return guard_cached_response(cached_response)
        existing = _idempotency_inflight.get(idempotency_key)
        if existing is None:
            future: Future[ChatResponse] = Future()
            _idempotency_inflight[idempotency_key] = (fingerprint, future)
            owns_execution = True
        else:
            inflight_fingerprint, future = existing
            _require_matching_fingerprint(inflight_fingerprint, fingerprint)
            owns_execution = False
    if not owns_execution:
        return guard_cached_response(future.result())
    claim_id = f'chat_claim_{uuid4().hex}'
    persistent_claim_owned = False
    try:
        claim_status, persisted_response = _claim_or_wait_for_persisted_response(idempotency_key=idempotency_key, fingerprint=fingerprint, claim_id=claim_id)
        if claim_status == 'completed':
            if persisted_response is None:
                raise RuntimeError('The persisted chat response is missing.')
            frozen_response = ChatResponse.model_validate(persisted_response)
            with _idempotency_lock:
                _idempotency_inflight.pop(idempotency_key, None)
                _remember_idempotent_response(idempotency_key, fingerprint, frozen_response)
                future.set_result(frozen_response)
            return guard_cached_response(frozen_response)
        persistent_claim_owned = True
        response = _process_chat_on_lesson_once(lesson_id, request, user_id=user_id, on_delta=on_delta, on_requirement_update=on_requirement_update, on_agent_activity=on_agent_activity, on_document_commit=on_document_commit, is_cancelled=is_cancelled, commit_metadata={**(commit_metadata or {}), **_chat_idempotency_metadata(request)}, prepared_gate=prepared_gate)
    except BaseException as exc:
        final_exception = exc
        if persistent_claim_owned:
            try:
                workspace_state.release_chat_input_event(user_id=idempotency_key[0], session_id=idempotency_key[1], input_event_id=idempotency_key[2], fingerprint=fingerprint, claim_id=claim_id)
            except (RuntimeError, ValueError, sqlite3.Error) as release_exc:
                final_exception = release_exc
        with _idempotency_lock:
            _idempotency_inflight.pop(idempotency_key, None)
            future.set_exception(final_exception)
        if final_exception is not exc:
            raise final_exception from exc
        raise
    try:
        frozen_response = _copy_response(response)
        workspace_state.complete_chat_input_event(user_id=idempotency_key[0], session_id=idempotency_key[1], input_event_id=idempotency_key[2], fingerprint=fingerprint, claim_id=claim_id, response=frozen_response.model_dump(mode='json'))
    except BaseException as exc:
        with _idempotency_lock:
            _idempotency_inflight.pop(idempotency_key, None)
            future.set_exception(exc)
        raise
    with _idempotency_lock:
        _idempotency_inflight.pop(idempotency_key, None)
        _remember_idempotent_response(idempotency_key, fingerprint, frozen_response)
        future.set_result(frozen_response)
    return _copy_response(frozen_response)

def _process_chat_on_lesson_once(lesson_id, request, **kwargs) -> ChatResponse:
    from openclass_core.services.google_sources.turn_evidence import capture_turn, attach_cited
    owner_user_id = kwargs['user_id']
    initial = workspace_state.get_store().load_lesson_document_context_for_user(owner_user_id, lesson_id)
    before = initial.lesson.board_document.model_copy(deep=True) if initial is not None else None
    from openclass_core.services.teaching_original_sources import bind_teaching_original_sources
    with capture_turn(document_before=before) as capture, bind_teaching_original_sources():
        response = _process_chat_on_lesson_once_impl(lesson_id, request, **kwargs)
        final = workspace_state.get_store().load_lesson_document_context_for_user(owner_user_id, lesson_id) if capture else None
        return attach_cited(response, capture.values(), document_before=before, document_after=final.lesson.board_document if final is not None else None)

def _process_chat_on_lesson_once_impl(lesson_id: str, request: ChatRequest, *, user_id: str, on_delta: Callable[[str], None] | None=None, on_requirement_update: Callable[[dict[str, object]], None] | None=None, on_agent_activity: Callable[[AgentActivityEvent], None] | None=None, on_document_commit: Callable[[str, str], None] | None=None, is_cancelled: Callable[[], bool] | None=None, commit_metadata: dict[str, object] | None=None, prepared_gate: TurnGateResult | None=None) -> ChatResponse:
    lesson_context = workspace_state.get_store().load_lesson_document_context_for_user(user_id, lesson_id)
    if lesson_context is None:
        raise ValueError('Lesson not found')
    initial_lesson = lesson_context.lesson
    initial_document = initial_lesson.board_document.model_copy(deep=True)
    gate = prepared_gate or route_chat_request(lesson_id, request, user_id=user_id, on_agent_activity=on_agent_activity)
    active_interaction = gate.decision.continuation == 'interaction_session'
    if prepared_gate is not None and (not active_interaction):
        active_interaction = _active_interaction_owns_turn(lesson_id, user_id=user_id)
        if active_interaction:
            gate = _gate_for_active_interaction(gate)
    effective_request = without_write_controls(request) if gate.decision.board_write_opt_out else request
    pending_offer_metadata: dict[str, object] = {}
    if not active_interaction:
        pending_offer = pending_teaching_offer_from_runtime_state(workspace_state.load_lesson_runtime_state_for_user(user_id, lesson_id))
        if pending_offer is not None and gate.decision.board_write_opt_out:
            pending_offer_metadata = {PENDING_TEACHING_OFFER_METADATA_KEY: None, 'pending_teaching_offer_transition': 'declined_no_write'}
        elif pending_offer is not None:
            pending_decision = _decide_pending_teaching_offer(gate, request=request, offer=pending_offer, on_agent_activity=on_agent_activity)
            if pending_offer.kind == 'expand_topic' and pending_decision.decision.action == 'accept':
                from openclass_core.services.pending_teaching_offer import request_for_topic_expansion
                effective_request = request_for_topic_expansion(request=request, offer=pending_offer, lesson=initial_lesson)
                gate = TurnGateResult(envelope=gate.envelope, decision=gate.decision.model_copy(update={'intent': 'learning_need', 'learning_flow': 'teach', 'explicit_teaching_request': True, 'board_access': 'state_check_only'}), trace=gate.trace, adapter=gate.adapter, activity=gate.activity)
            else:
                effective_request, gate = _apply_pending_teaching_offer_decision(gate, request=request, offer=pending_offer, result=pending_decision)
            pending_offer_metadata = _pending_teaching_offer_commit_metadata(pending_offer, pending_decision.decision)
    attachment_context = None
    if gate.decision.intent == 'learning_need':
        effective_request, attachment_context = prepare_teaching_attachments(effective_request, user_id=user_id, package_id=lesson_context.package_id, board_empty=not bool(initial_document.content_text.strip()), board_write_opt_out=gate.decision.board_write_opt_out)
    with bind_teaching_decision(gate.decision), bind_teaching_attachments(attachment_context), bind_commit_metadata({**(commit_metadata or {}), **_chat_edit_metadata(effective_request), **pending_offer_metadata}):
        if gate.decision.intent in {'ordinary_chat', 'unclear'}:
            return complete_non_learning_turn(lesson_id, effective_request, gate, user_id=user_id, on_delta=on_delta, on_agent_activity=on_agent_activity, is_cancelled=is_cancelled)
        effective_request, gate = _apply_empty_board_source_learning_policy(lesson_id, effective_request, gate, user_id=user_id)
        downstream_request = _request_with_frozen_text_model(effective_request, gate.envelope.selected_model)
        source_answer_plan = None
        if not active_interaction and (gate.decision.board_write_opt_out or gate.decision.learning_flow == 'none'):
            source_answer_plan = _readonly_source_question_plan(lesson_id, effective_request, user_id=user_id, adapter=gate.adapter, is_cancelled=is_cancelled)
        if active_interaction or (source_answer_plan is None and (bool(initial_lesson.board_document.content_text.strip()) and gate.decision.learning_flow == 'teach' and (effective_request.teaching_action is None) and (effective_request.board_generation_action is None) or _should_use_bounded_existing_board_workflow(lesson_id, effective_request, user_id=user_id))):
            try:
                response = process_existing_board_workflow(lesson_id, effective_request, user_id=user_id, adapter=gate.adapter, selected_model=gate.envelope.selected_model, board_write_opt_out=gate.decision.board_write_opt_out, explicit_teaching_request=gate.decision.explicit_teaching_request, on_delta=on_delta, on_board_task_update=on_requirement_update, on_agent_activity=on_agent_activity, is_cancelled=is_cancelled)
            except ExistingBoardInteractionReroute as reroute:
                return _process_chat_on_lesson_once(lesson_id, reroute.request, user_id=user_id, on_delta=on_delta, on_requirement_update=on_requirement_update, on_agent_activity=on_agent_activity, on_document_commit=on_document_commit, is_cancelled=is_cancelled, commit_metadata={**(commit_metadata or {}), 'interaction_reroute_dispatch_key': reroute.dispatch_key})
            except ExistingBoardAnswerThenOfferReroute as reroute:
                with bind_learning_answer('brief_then_offer' if reroute.allow_teaching_offer else 'read_only', topic=reroute.topic):
                    response = process_codex_chat_on_lesson(lesson_id, _request_with_frozen_text_model(reroute.request, gate.envelope.selected_model), user_id=user_id, on_delta=on_delta, on_requirement_update=on_requirement_update, on_agent_activity=on_agent_activity, on_document_commit=on_document_commit, is_cancelled=is_cancelled)
        else:
            response = process_codex_chat_on_lesson(lesson_id, downstream_request, user_id=user_id, on_delta=on_delta, on_requirement_update=on_requirement_update, on_agent_activity=on_agent_activity, on_document_commit=on_document_commit, is_cancelled=is_cancelled, **{'prepared_source_access': source_answer_plan} if source_answer_plan is not None else {})
    response.turn_decision = gate.decision
    if source_answer_plan is not None:
        source_reason = 'The source-access decision resolved a read-only reference question; source ordinals are not board targets. ' + source_answer_plan.decision.reason
        response.turn_decision = gate.decision.model_copy(update={'board_access': 'forbidden', 'continuation': 'none', 'reason': source_reason})
        response.decision_trace = DecisionTrace(intent_signals=['explicit_readonly_source_question'], matched_rules=['verified_source_qa_without_board_target'], selected_action='learning_need', target_resolver='SourceCatalog', role_executed='source_qa', board_access='forbidden', requirement_effect='preserved', document_changed=False, reason=source_reason)
    response.decision_trace = _merge_decision_trace(gate.trace, response.decision_trace)
    response.agent_activity = _merge_activity(gate.activity, response.agent_activity)
    if gate.decision.board_write_opt_out:
        saved = workspace_state.get_store().load_lesson_document_context_for_user(user_id, lesson_id)
        if saved is not None:
            assert_board_unchanged(initial_document, saved.lesson.board_document)
        return response
    return maybe_generate_lesson_title(lesson_id, downstream_request, response, user_id=user_id)

def _apply_empty_board_source_learning_policy(lesson_id: str, request: ChatRequest, gate: TurnGateResult, *, user_id: str) -> tuple[ChatRequest, TurnGateResult]:
    """Default teaching sequences board preparation without force-freezing it."""
    if gate.decision.board_write_opt_out:
        return (without_write_controls(request), gate)
    if gate.decision.learning_flow == 'none':
        return (request, gate)
    workspace = workspace_state.load_workspace_for_user(user_id)
    _package, lesson = workspace_state.find_lesson_package(workspace, lesson_id)
    if lesson.board_document.content_text.strip():
        return (request, gate)
    post_generation_action = 'auto_explain' if gate.decision.learning_flow == 'teach' else 'stop_after_generation'
    updated_request = request.model_copy(update={'post_generation_action': post_generation_action})
    updated_trace = gate.trace.model_copy(update={'intent_signals': [*gate.trace.intent_signals, f'learning_flow:{gate.decision.learning_flow}'], 'matched_rules': [*gate.trace.matched_rules, 'empty_board_learning_requires_board'], 'target_resolver': 'source_query_scope' if request.source_query_scope else 'learning_requirement', 'sequence_mode': 'board_generation_then_teaching' if gate.decision.learning_flow == 'teach' else 'board_generation_only', 'reason': 'An empty board and a verified source learning request require board generation before any teaching response.'})
    return (updated_request, TurnGateResult(envelope=gate.envelope, decision=gate.decision, trace=updated_trace, adapter=gate.adapter, activity=gate.activity))

def route_chat_request(lesson_id: str, request: ChatRequest, *, user_id: str, on_agent_activity: Callable[[AgentActivityEvent], None] | None=None) -> TurnGateResult:
    """Run the same authoritative, board-free Turn Router used by every chat transport."""
    request = authoritative_routing_request(lesson_id, request, user_id=user_id)
    gate = evaluate_turn_gate(request, lesson_id=lesson_id, user_id=user_id, on_agent_activity=on_agent_activity)
    if _active_interaction_owns_turn(lesson_id, user_id=user_id):
        return _gate_for_active_interaction(gate)
    return gate

def _require_matching_prepared_gate(gate: TurnGateResult, *, lesson_id: str, request: ChatRequest, user_id: str) -> None:
    expected_envelope = build_turn_envelope(request, lesson_id=lesson_id, selected_model=gate.envelope.selected_model)
    if expected_envelope.model_dump(mode='json') != gate.envelope.model_dump(mode='json'):
        raise ValueError('The prepared Turn Router result does not match this chat input.')
    if request.text_model is None:
        return
    requested_model = resolve_text_model_selection(request.text_model, user_id=user_id)
    if requested_model.model_dump(mode='json') != gate.envelope.selected_model.model_dump(mode='json'):
        raise ValueError('The prepared Turn Router result does not match this chat input.')

def _request_with_frozen_text_model(request: ChatRequest, selected_model: AIModelSelection) -> ChatRequest:
    return request.model_copy(update={'text_model': selected_model.model_copy(deep=True)})

def _decide_pending_teaching_offer(gate: TurnGateResult, *, request: ChatRequest, offer: PendingTeachingOffer, on_agent_activity: Callable[[AgentActivityEvent], None] | None) -> PendingTeachingOfferDecisionResult:
    if request.teaching_action == 'restart':
        return PendingTeachingOfferDecisionResult(decision=PendingTeachingOfferDecision(action='accept', reason='The learner used the explicit restart-teaching control.'))
    return decide_pending_teaching_offer(adapter=gate.adapter, request=request, offer=offer, on_activity=on_agent_activity)

def _apply_pending_teaching_offer_decision(gate: TurnGateResult, *, request: ChatRequest, offer: PendingTeachingOffer, result: PendingTeachingOfferDecisionResult) -> tuple[ChatRequest, TurnGateResult]:
    action = result.decision.action
    reason = result.decision.reason.strip()
    effective_request = request
    envelope = gate.envelope
    decision = gate.decision
    trace = gate.trace
    if action == 'accept':
        effective_request = request.model_copy(update={'teaching_action': 'restart'})
        envelope = gate.envelope.model_copy(update={'teaching_action': 'restart', 'explicit_action': 'teaching_restart'})
        decision = gate.decision.model_copy(update={'intent': 'learning_need', 'continuation': 'teaching_sequence', 'relation_to_active': 'continue', 'board_access': 'state_check_only', 'reason': reason})
    elif action == 'decline':
        conversation = [*gate.envelope.conversation]
        if not conversation or conversation[-1].content.strip() != offer.invitation:
            conversation.append(ConversationTurn(role='assistant', content=offer.invitation))
        envelope = gate.envelope.model_copy(update={'conversation': conversation})
        decision = gate.decision.model_copy(update={'intent': 'ordinary_chat', 'continuation': 'none', 'relation_to_active': 'none', 'board_access': 'forbidden', 'reason': reason})
    elif action == 'new_task' and decision.intent == 'learning_need':
        decision = decision.model_copy(update={'continuation': 'none', 'relation_to_active': 'new_task', 'reason': reason})
    trace = trace.model_copy(update={'intent_signals': list(dict.fromkeys([*trace.intent_signals, f'pending_teaching_offer:{action}'])), 'matched_rules': list(dict.fromkeys([*trace.matched_rules, 'pending_teaching_offer_continuation'])), 'selected_action': decision.intent, 'rejected_actions': [intent for intent in ('ordinary_chat', 'learning_need', 'unclear') if intent != decision.intent], 'sequence_mode': 'teaching_sequence' if action == 'accept' else trace.sequence_mode, 'board_access': 'state_check_only' if decision.intent == 'learning_need' else 'forbidden', 'requirement_effect': 'eligible' if decision.intent == 'learning_need' else 'preserved', 'reason': reason})
    return (effective_request, TurnGateResult(envelope=envelope, decision=decision, trace=trace, adapter=gate.adapter, activity=_merge_activity(gate.activity, result.activity)))

def _pending_teaching_offer_commit_metadata(offer: PendingTeachingOffer, decision: PendingTeachingOfferDecision) -> dict[str, object]:
    preserve_offer = decision.action == 'unrelated'
    return {PENDING_TEACHING_OFFER_METADATA_KEY: offer.model_dump(mode='json') if preserve_offer else None, 'pending_teaching_offer_id': offer.offer_id, 'pending_teaching_offer_transition': decision.action}

def _readonly_source_question_plan(lesson_id, request, *, user_id, adapter, is_cancelled=None):
    """Resolve source intent before interpreting its chapter number as a board target."""
    references = [*([request.selection] if request.selection is not None else []), *request.selections]
    if references or request.source_query_scope is not None or request.attachments or (request.formula_ink is not None) or (request.teaching_action is not None) or (request.board_task_confirmation is not None) or (request.board_generation_action is not None) or (request.interaction_mode == 'direct_edit'):
        return None
    if has_explicit_document_mutation_request(request.message) or has_explicit_board_interaction_request(request.message):
        return None
    from openclass_core.services.source_retrieval_preferences import auto_source_retrieval_enabled
    if not auto_source_retrieval_enabled(user_id, workspace_state.get_store().path):
        return None
    workspace = workspace_state.load_workspace_for_user(user_id)
    package, lesson = workspace_state.find_lesson_package(workspace, lesson_id)
    if not lesson.board_document.content_text.strip():
        return None
    from openclass_core.services.source_retrieval_policy import decide_source_access
    plan = decide_source_access(owner_user_id=user_id, package_id=package.id, message=request.message, adapter=adapter, is_cancelled=is_cancelled)
    if plan.mode != 'none' and plan.decision.explicit_source_request and plan.decision.readonly_source_question:
        return plan
    return None

def _should_use_bounded_existing_board_workflow(lesson_id: str, request: ChatRequest, *, user_id: str) -> bool:
    if request.formula_ink is not None or request.attachments:
        return False
    if request.source_query_scope is not None:
        return False
    if request.teaching_action is not None:
        return False
    if request.board_task_confirmation is not None:
        return True
    if request.board_generation_action is not None:
        return True
    if request.interaction_mode == 'direct_edit':
        return True
    references = [*([request.selection] if request.selection is not None else []), *request.selections]
    if any((reference.kind == 'source' for reference in references)):
        return False
    workspace = workspace_state.load_workspace_for_user(user_id)
    _package, lesson = workspace_state.find_lesson_package(workspace, lesson_id)
    if not lesson.board_document.content_text.strip():
        return False
    has_board_selection = any((reference.kind == 'board' for reference in references))
    if has_board_selection:
        return True
    if has_explicit_document_mutation_request(request.message, has_board_selection=has_board_selection):
        return True
    if has_explicit_board_location_cue(request.message, has_board_selection=has_board_selection):
        return True
    if has_explicit_board_interaction_request(request.message):
        return True
    if _active_incomplete_board_task(lesson_id, user_id=user_id) is not None:
        return True
    return False

def _active_incomplete_board_task(lesson_id: str, *, user_id: str) -> dict[str, object] | None:
    runtime_state = workspace_state.load_lesson_runtime_state_for_user(user_id, lesson_id)
    if runtime_state is None:
        return None
    task = runtime_state.runtime_snapshot.board_task_requirements
    if task is None:
        return None
    return {'requested_action': task.requested_action, 'location_status': task.location_status, 'task_id': task.task_id}

def _active_interaction_owns_turn(lesson_id: str, *, user_id: str) -> bool:
    runtime_state = workspace_state.load_lesson_runtime_state_for_user(user_id, lesson_id)
    if runtime_state is None:
        return False
    task = runtime_state.runtime_snapshot.board_task_requirements
    if task is None or task.requested_action != 'interact' or task.interaction_session is None:
        return False
    session = InteractionSession.model_validate(task.interaction_session)
    return session.current_state == 'active' and session.source_board_task_id == task.task_id

def _gate_for_active_interaction(gate: TurnGateResult) -> TurnGateResult:
    reason = 'A persisted active interaction session owns this input until it exits or classifies the input as a new task.'
    return TurnGateResult(envelope=gate.envelope, decision=gate.decision.model_copy(update={'intent': 'learning_need', 'continuation': 'interaction_session', 'relation_to_active': 'continue', 'board_access': 'state_check_only', 'reason': reason}), trace=gate.trace.model_copy(update={'intent_signals': list(dict.fromkeys([*gate.trace.intent_signals, 'active_interaction_session'])), 'matched_rules': list(dict.fromkeys([*gate.trace.matched_rules, 'active_interaction_session_ownership'])), 'selected_action': 'learning_need', 'rejected_actions': ['ordinary_chat', 'unclear'], 'sequence_mode': 'interaction_session', 'board_access': 'state_check_only', 'requirement_effect': 'eligible', 'reason': reason}), adapter=gate.adapter, activity=gate.activity)

def _merge_decision_trace(gate_trace: DecisionTrace, workflow_trace: DecisionTrace | None) -> DecisionTrace:
    if workflow_trace is None:
        return gate_trace.model_copy(update={'role_executed': 'workflow'})
    return workflow_trace.model_copy(update={'intent_signals': list(dict.fromkeys([*gate_trace.intent_signals, *workflow_trace.intent_signals])), 'matched_rules': list(dict.fromkeys([*gate_trace.matched_rules, *workflow_trace.matched_rules]))})

def _chat_idempotency_key(request: ChatRequest, *, user_id: str) -> tuple[str, str, str] | None:
    if request.session_id is None or request.input_event_id is None:
        return None
    return (user_id, request.session_id, request.input_event_id)

def _chat_request_fingerprint(lesson_id: str, request: ChatRequest) -> str:
    payload = {'lesson_id': lesson_id, 'request': request.model_dump(mode='json')}
    context_epoch = current_context_epoch()
    if context_epoch is not None:
        payload['ridoc_context_epoch'] = context_epoch
        payload['ridoc_context_scope'] = current_context_scope()
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()

def _chat_idempotency_metadata(request: ChatRequest) -> dict[str, object]:
    return {'chat_idempotency_key': f'{request.session_id}:{request.input_event_id}', 'chat_session_id': request.session_id or '', 'chat_input_event_id': request.input_event_id or '', 'chat_turn_id': request.turn_id or ''}

def _require_matching_fingerprint(expected: str, actual: str) -> None:
    if expected != actual:
        raise ValueError('The chat input event id was reused for a different request.')

def _claim_or_wait_for_persisted_response(*, idempotency_key: tuple[str, str, str], fingerprint: str, claim_id: str) -> tuple[str, dict[str, object] | None]:
    deadline = monotonic() + _PERSISTED_IDEMPOTENCY_WAIT_SECONDS
    while True:
        status, response = workspace_state.claim_chat_input_event(user_id=idempotency_key[0], session_id=idempotency_key[1], input_event_id=idempotency_key[2], fingerprint=fingerprint, claim_id=claim_id)
        if status != 'waiting':
            return (status, response)
        if monotonic() >= deadline:
            raise TimeoutError('The original chat input event is still running.')
        sleep(_PERSISTED_IDEMPOTENCY_POLL_SECONDS)

def _remember_idempotent_response(idempotency_key: tuple[str, str, str], fingerprint: str, response: ChatResponse) -> None:
    _idempotency_cache[idempotency_key] = (fingerprint, response)
    _idempotency_cache.move_to_end(idempotency_key)
    while len(_idempotency_cache) > _IDEMPOTENCY_CACHE_LIMIT:
        _idempotency_cache.popitem(last=False)

def _copy_response(response: ChatResponse) -> ChatResponse:
    return ChatResponse.model_validate(response.model_dump(mode='json'))

def _clear_idempotency_state_for_tests() -> None:
    with _idempotency_lock:
        _idempotency_cache.clear()
        _idempotency_inflight.clear()

def _chat_edit_metadata(request: ChatRequest) -> dict[str, object]:
    if not request.chat_edit_source_commit_id:
        return {}
    return {'chat_edit_source_commit_id': request.chat_edit_source_commit_id, 'chat_edit_base_commit_id': request.chat_edit_base_commit_id, 'chat_edit_original_message': request.chat_edit_original_message or ''}

def document_ai_edit_request(lesson_id: str, instruction: str, selection_text: str | None, conversation: list[ConversationTurn], *, user_id: str) -> ChatResponse:
    selection = SelectionRef(kind='board', excerpt=selection_text, location_kind='target_range') if selection_text else None
    return process_chat_on_lesson(lesson_id, ChatRequest(message=instruction, interaction_mode='direct_edit', selection=selection, conversation=conversation), user_id=user_id)

def _merge_activity(first: list[AgentActivityEvent], second: list[AgentActivityEvent]) -> list[AgentActivityEvent]:
    merged: dict[str, AgentActivityEvent] = {}
    order: list[str] = []
    for event in [*first, *second]:
        if event.id not in merged:
            order.append(event.id)
        merged[event.id] = event
    return [merged[event_id] for event_id in order]
