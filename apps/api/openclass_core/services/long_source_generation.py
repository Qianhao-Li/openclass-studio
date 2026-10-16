"""Generate final board fragments from originals, then assemble without another AI.

The only intermediate model product is a structural control plan. Previously
generated text is never put back into a source context or sent to another writer.
"""
from __future__ import annotations
import base64
from dataclasses import dataclass, field, replace
import hashlib
from itertools import chain
import json
from openclass_core.models import new_id
from openclass_core.services.ai_execution_adapter import BoardGenerationExecutionRequest, StructuredBoardGenerationResult
from openclass_core.services.codex_app_server import CodexTurnCancelledError
from openclass_core.services.original_generation_plan import GenerationPart, OriginalInputCapacity, OriginalInputCapacityError, OriginalSourceDependencyError, is_capacity_failure, plan_original_batch, resolve_original_parts

@dataclass
class OriginalBoardResult:
    turn: StructuredBoardGenerationResult
    content: str
    input_parts: list[dict] = field(default_factory=list)

def _cancelled(callback):
    if callback is not None and callback():
        raise CodexTurnCancelledError('Original-source board generation was cancelled')

def _image_digest(image):
    return hashlib.sha256(base64.b64decode(image.partition(',')[2], validate=True)).hexdigest()

def _unit_ids(batch):
    return [item.id for item in batch.evidence_items]

def _request_for_batch(*, requirement, teaching_plan, content_extent, batch, part, visual_provider=None, visual_plan=None, capacity=None, require_visual_dependencies=True):
    from openclass_core.services.google_sources.turn_evidence import bind_citation_numbers
    scoped = requirement.model_copy(deep=True)
    scoped.source_grounding.reading_notes = []
    scoped.source_grounding.frozen_evidence = bind_citation_numbers(batch.evidence_items)
    ranges = {(item.source_ingestion_id, item.page_range) for item in batch.evidence_items}
    sources = {item.source_ingestion_id for item in batch.evidence_items}
    scoped.source_grounding.confirmed_references = [reference for reference in scoped.source_grounding.confirmed_references if reference.source_ingestion_id in sources and (reference.scope_kind != 'retrieved_range' or (reference.source_ingestion_id, reference.page_range) in ranges)]
    scoped.source_grounding.frozen_visual_evidence = []
    selected_visuals = visual_provider.for_units(set(part.unit_ids)) if visual_provider is not None else []
    available_ids = {item.id for item in batch.evidence_items}
    if require_visual_dependencies and any((not set(visual.metadata.get('dependency_unit_ids', [])) <= available_ids for visual in selected_visuals)):
        raise OriginalSourceDependencyError('A cross-page visual is missing its original source dependencies in this writer input')
    if not require_visual_dependencies:
        selected_visuals = [visual for visual in selected_visuals if visual.metadata.get('source_unit_id') in available_ids]
    manifest = []
    if capacity is not None:
        capacity.check(batch)
    if selected_visuals:
        images, additional = ([], [])
        known_hashes = {_image_digest(image) for image in batch.model_image_inputs}
        for visual in selected_visuals:
            image = visual_provider.image_inputs_for([visual.visual_id])[0]
            digest = _image_digest(image)
            if digest not in known_hashes:
                additional.append(image)
                if capacity is not None:
                    capacity.check(batch, extra_images=additional)
                known_hashes.add(digest)
            images.append(image)
        batch = batch.with_verified_visuals(selected_visuals, images)
        model_images = list(batch.model_image_inputs)
        positions = {_image_digest(image): index for index, image in enumerate(model_images)}
        planned = {item.visual_id: item for item in visual_plan.items}
        for visual in selected_visuals:
            placement = planned.get(visual.visual_id)
            if placement is None:
                raise ValueError('A prepared original visual has no frozen insertion identity')
            manifest.append({'visual_id': visual.visual_id, 'marker': placement.marker, 'kind': visual.kind, 'source_unit_id': visual.metadata.get('source_unit_id'), 'content_hash': visual.content_hash, 'image_index': positions[visual.content_hash], 'source_range': visual.metadata.get('source_range'), 'crop_pixel_bbox': visual.metadata.get('crop_pixel_bbox'), 'visual_group_id': visual.metadata.get('visual_group_id'), 'dependency_unit_ids': visual.metadata.get('dependency_unit_ids', []), 'representation': 'verified_original_visual', 'required': True})
    else:
        model_images = list(getattr(batch, 'model_image_inputs', batch.image_inputs))
    input_manifest = {'representation': 'original_source_input', 'primary_unit_ids': part.unit_ids, 'context_unit_ids': part.context_ids, 'original_heading': part.heading, 'source_units': [{'id': item.id, 'source_id': item.source_ingestion_id, 'source_content_hash': item.metadata.get('source_content_hash'), 'source_version': item.metadata.get('source_version'), 'catalog_version': item.metadata.get('catalog_version'), 'source_range': item.metadata.get('source_range'), 'content_unit_sha256': item.metadata.get('content_unit_sha256') or item.metadata.get('page_image_sha256')} for item in batch.evidence_items], 'image_sha256': [_image_digest(image) for image in model_images], 'image_manifest': batch.image_manifest}
    request = BoardGenerationExecutionRequest(requirement=scoped, teaching_plan=teaching_plan, content_extent=content_extent, image_inputs=model_images, visual_manifest=manifest, preserve_image_bytes=bool(model_images), original_source_manifest=input_manifest)
    return (batch, request)

def generate_original_board(*, adapter, delivery, requirement, teaching_plan, content_extent='article', capacity=None, visual_provider=None, visual_plan=None, max_document_bytes=2 * 1024 * 1024, is_cancelled=None, on_activity=None, write_original_fragment=None):
    from openclass_core.services.original_source_delivery import bind_original_source_batch, combine_original_batches
    capacity = capacity or OriginalInputCapacity()
    activity, input_parts, completed = ([], [], [])
    last_turn = None

    def write(part, batch, *, fragmented):
        nonlocal last_turn
        _cancelled(is_cancelled)
        prepared, request = _request_for_batch(requirement=requirement, teaching_plan=teaching_plan, content_extent='section' if fragmented else content_extent, batch=batch, part=part, visual_provider=visual_provider, visual_plan=visual_plan, capacity=capacity)
        capacity.check(prepared)
        from openclass_core.services.board_visual_insertion import BoardVisualInsertionError, validate_original_visual_positions
        from openclass_core.services.rich_document import build_document
        selected_ids = {item['visual_id'] for item in request.visual_manifest}
        fragment_visual_plan = replace(visual_plan, items=tuple((item for item in visual_plan.items if item.visual_id in selected_ids))) if visual_plan is not None else None
        for attempt in range(3):
            _cancelled(is_cancelled)
            with bind_original_source_batch(prepared):
                turn, content = write_original_fragment(request, prepared) if write_original_fragment is not None else adapter.generate_board(request, is_cancelled=is_cancelled, on_activity=on_activity)
            _cancelled(is_cancelled)
            if fragment_visual_plan is None:
                break
            try:
                validate_original_visual_positions(build_document(title='', content_text=content), plan=fragment_visual_plan)
            except BoardVisualInsertionError as error:
                if attempt == 2:
                    raise
                request = replace(request, teaching_plan=teaching_plan + '\nThe previous output did not satisfy the original-image placement contract. Generate this final fragment again directly from the attached originals. Place every required visual_manifest marker exactly once as its own paragraph immediately after the relevant explanation. Do not add unknown markers, redraws or an image appendix. Placement validation: ' + str(error))
            else:
                break
        _cancelled(is_cancelled)
        if not content.strip():
            raise RuntimeError('The original-source writer returned an empty final fragment')
        from openclass_core.services.rich_document import looks_like_html_content
        if looks_like_html_content(content):
            raise RuntimeError('The original-source writer returned HTML instead of board Markdown')
        if sum((len(value.encode()) for value in completed)) + len(content.encode()) > max_document_bytes:
            raise RuntimeError('The completed board exceeds the configured document size limit')
        last_turn = turn
        activity.extend(getattr(turn, 'activity', []))
        start = sum((len(value) for value in completed)) + 2 * len(completed)
        input_parts.append({**request.original_source_manifest, 'output_start': start, 'output_end': start + len(content.strip()), 'output_sha256': hashlib.sha256(content.strip().encode()).hexdigest()})
        completed.append(content.strip())
    buffered = []
    iterator = iter(delivery.iter_batches())
    for batch in iterator:
        _cancelled(is_cancelled)
        candidate = combine_original_batches([*buffered, batch])
        try:
            part = GenerationPart(unit_ids=_unit_ids(candidate))
            prepared, _request = _request_for_batch(requirement=requirement, teaching_plan=teaching_plan, content_extent=content_extent, batch=candidate, part=part, visual_provider=visual_provider, visual_plan=visual_plan, capacity=capacity, require_visual_dependencies=False)
            capacity.check(prepared)
        except OriginalInputCapacityError:
            planning_batches = chain(buffered, (batch,), iterator)
            break
        buffered.append(batch)
    else:
        if not buffered:
            raise ValueError('No authorized original units are available for board generation')
        combined = combine_original_batches(buffered)
        try:
            write(GenerationPart(unit_ids=_unit_ids(combined)), combined, fragmented=False)
        except Exception as error:
            if not is_capacity_failure(error):
                raise
            planning_batches = iter(delivery.iter_batches())
            buffered.clear()
        else:
            return _result(last_turn, completed, activity, input_parts)
    unit_sources, unit_order, structural = ({}, [], [])
    for batch in planning_batches:
        _cancelled(is_cancelled)
        ids = _unit_ids(batch)
        if any((identity in unit_sources for identity in ids)):
            raise OriginalSourceDependencyError('Original planning received duplicate source units')
        unit_sources.update(((item.id, item.source_ingestion_id) for item in batch.evidence_items))
        unit_order.extend(ids)

        def plan_bounded(original_batch):
            _cancelled(is_cancelled)
            try:
                capacity.check(original_batch)
                return plan_original_batch(original_batch, adapter=adapter, learning_requirement=requirement, is_cancelled=is_cancelled, on_activity=on_activity)
            except Exception as error:
                if not is_capacity_failure(error):
                    raise
                original_ids = _unit_ids(original_batch)
                if len(original_ids) < 2:
                    raise OriginalSourceDependencyError('One original unit cannot fit the structural planning channel; no summary was substituted') from error
                middle = len(original_ids) // 2
                controls = []
                for subgroup in (original_ids[:middle], original_ids[middle:]):
                    supplied = set()
                    for smaller in delivery.iter_batches(unit_ids=set(subgroup)):
                        supplied.update(_unit_ids(smaller))
                        controls.extend(plan_bounded(smaller))
                    if supplied != set(subgroup):
                        raise OriginalSourceDependencyError('Structural planning did not receive every requested original unit')
                return controls
        structural.extend(plan_bounded(batch))
    buffered.clear()
    parts = resolve_original_parts(structural, unit_sources)
    order = {identity: index for index, identity in enumerate(unit_order)}

    def read_part(part):
        if visual_provider is not None:
            visual_dependencies = {identity for visual in visual_provider.for_units(set(part.unit_ids)) for identity in visual.metadata.get('dependency_unit_ids', [])}
            if not visual_dependencies <= unit_sources.keys():
                raise OriginalSourceDependencyError('A visual dependency exceeds the authorized source inventory')
            part.context_ids = sorted((set(part.context_ids) | visual_dependencies) - set(part.unit_ids), key=order.__getitem__)
        required = set(part.unit_ids) | set(part.context_ids)
        originals = []
        received = set()
        for batch in delivery.iter_batches(unit_ids=required):
            _cancelled(is_cancelled)
            received.update(_unit_ids(batch))
            combined = combine_original_batches([*originals, batch])
            capacity.check(combined)
            originals.append(batch)
        if received != required:
            raise OriginalSourceDependencyError('A final fragment is missing an authorized original dependency')
        return combine_original_batches(originals)

    def execute(part):
        _cancelled(is_cancelled)
        try:
            original = read_part(part)
            write(part, original, fragmented=True)
        except Exception as error:
            if not is_capacity_failure(error):
                raise
            if part.inseparable or len(part.unit_ids) < 2:
                raise OriginalSourceDependencyError("The original units needed together exceed this model channel's actual capacity; no summary or partial board was substituted") from error
            middle = len(part.unit_ids) // 2
            left, right = (part.unit_ids[:middle], part.unit_ids[middle:])
            execute(GenerationPart(left, part.heading, sorted(set(part.context_ids + [right[0]]) - set(left), key=order.__getitem__)))
            execute(GenerationPart(right, part.heading, sorted(set(part.context_ids + [left[-1]]) - set(right), key=order.__getitem__)))
    for part in parts:
        execute(part)
    return _result(last_turn, completed, activity, input_parts)

def _result(last_turn, completed, activity, input_parts):
    if last_turn is None:
        raise RuntimeError('No complete original-grounded board fragment was generated')
    return OriginalBoardResult(StructuredBoardGenerationResult(thread_id=getattr(last_turn, 'thread_id', None) or new_id('originalboard'), turn_id=getattr(last_turn, 'turn_id', None), final_response='', activity=activity), '\n\n'.join(completed), input_parts)

def prepare_single_original_input(requirement, *, owner_user_id, include_images=True, is_cancelled=None, capacity=None):
    """Prepare one bounded original input; never invoke a summarizing role."""
    from openclass_core.services.original_source_delivery import from_frozen_requirement, combine_original_batches
    from openclass_core.services.google_sources.turn_evidence import bind_citation_numbers
    prepared = requirement.model_copy(deep=True)
    grounding = prepared.source_grounding
    if not (grounding.confirmed_references or grounding.frozen_evidence or grounding.frozen_visual_evidence or grounding.reading_notes):
        return (prepared, None)
    delivery = from_frozen_requirement(requirement, owner_user_id, is_cancelled=is_cancelled)
    capacity = capacity or OriginalInputCapacity()
    batches = []
    for batch in delivery.iter_batches():
        _cancelled(is_cancelled)
        combined = combine_original_batches([*batches, batch])
        capacity.check(combined)
        if combined.image_inputs and (not include_images):
            raise ValueError('The selected model cannot read the original source images')
        batches.append(batch)
    if not batches:
        raise ValueError('The selected source has no authenticated original carrier')
    batch = combine_original_batches(batches)
    grounding.reading_notes = []
    grounding.frozen_evidence = bind_citation_numbers(batch.evidence_items)
    grounding.frozen_visual_evidence = []
    return (prepared, batch)
