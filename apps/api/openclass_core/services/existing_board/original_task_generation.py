"""Original-source generation inside an already authorized BoardTask.

The task projection is a role input, not a first-board requirement run. This
module prepares final fragments and temporary visuals only; existing task and
RIDOC transaction owners retain all write authority.
"""
from __future__ import annotations
from dataclasses import dataclass
import json
from pydantic import BaseModel, ConfigDict, Field
from openclass_core.models import LearningSourceGrounding
from openclass_core.services.board_visual_insertion import apply_board_insertion_plan, build_board_insertion_plan, derive_board_visual_placements
from openclass_core.services.original_delivery_group import OriginalDeliveryGroup
from openclass_core.services.original_source_delivery import original_context_for_model

class OriginalBoardTaskInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    board_task: dict
    learning_content: str
    learning_goal: str
    output_preference: str
    content_extent: str
    source_grounding: LearningSourceGrounding = Field(default_factory=LearningSourceGrounding)

class OriginalTaskFragment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = ''
    markdown: str

@dataclass
class OriginalTaskPreparation:
    delivery: object
    requirement: OriginalBoardTaskInput
    visuals: object
    insertion_plan: object

    def apply_visuals(self, document, *, lesson_id):
        if not self.insertion_plan.items:
            return document
        return apply_board_insertion_plan(document, plan=self.insertion_plan, placements=derive_board_visual_placements(document, plan=self.insertion_plan), owner_user_id=self.delivery.owner_user_id, lesson_id=lesson_id, visual_bytes_resolver=self.visuals.read_visual_bytes, strict_positions=True).document

@dataclass
class OriginalTaskResult:
    title: str
    markdown: str
    activity: list
    preparation: OriginalTaskPreparation
    input_parts: list

def prepare_task_originals(*, source_context, safe_task, adapter, on_activity=None, is_cancelled=None):
    from openclass_core.services.original_visual_preparation import prepare_original_visuals
    from openclass_core.services.teaching_original_sources import register_teaching_original_sources, register_teaching_original_dependencies
    original = getattr(source_context, 'original_delivery', None)
    if original is None:
        raise ValueError('A source-backed BoardTask requires actual original-source delivery.')
    delivery = OriginalDeliveryGroup([original])
    requirement = OriginalBoardTaskInput(board_task=safe_task, learning_content=str(safe_task['question_or_topic']), learning_goal=str(safe_task['question_or_topic']), output_preference=str(safe_task.get('special_interaction_requirements') or 'none'), content_extent=str(safe_task['content_extent']), source_grounding=LearningSourceGrounding(frozen_evidence=delivery.evidence_items))
    visuals = prepare_original_visuals(delivery, frozen_requirement=requirement, adapter=adapter, owner_user_id=delivery.owner_user_id, package_id=delivery.package_id, on_activity=on_activity, is_cancelled=is_cancelled)
    insertion = build_board_insertion_plan(visuals.evidence, allow_editable_recreation=False, source_titles={item.source_ingestion_id: item.source_title for item in delivery.evidence_items})
    register_teaching_original_sources(delivery)
    register_teaching_original_dependencies(delivery, visual_evidence=visuals.evidence)
    return OriginalTaskPreparation(delivery, requirement, visuals, insertion)

def generate_task_from_originals(*, adapter, source_context, safe_task, system_prompt, current_board_markdown=None, approved_target=None, citation_style='numbers', on_activity=None, is_cancelled=None):
    from openclass_core.services.long_source_generation import generate_original_board
    preparation = prepare_task_originals(source_context=source_context, safe_task=safe_task, adapter=adapter, on_activity=on_activity, is_cancelled=is_cancelled)
    titles = []

    def write_fragment(request, batch):
        payload = {'board_task': safe_task, 'verified_source_context': original_context_for_model(batch, citation_style=citation_style), 'original_source_manifest': request.original_source_manifest, 'visual_manifest': request.visual_manifest, 'generation_instruction': request.teaching_plan, 'content_extent': request.content_extent}
        if current_board_markdown is not None:
            payload['current_board_markdown'] = current_board_markdown
        if approved_target is not None:
            payload['approved_target'] = approved_target
        response = adapter.parse_structured(system_prompt=system_prompt + '\nGenerate only the final content for primary_unit_ids using the attached originals. context_unit_ids are dependencies, not additional output sections. Place each visual_manifest marker exactly once as a standalone paragraph directly after its relevant explanation. Do not redraw images or append an image gallery. Existing board text is the edit target, never a replacement for the cited originals. Do not fabricate source details from the task fields. ' + ('Cite exact [evidence_ID] markers.' if citation_style == 'identity' else 'Use supplied citation_numbers as [^n] markers.'), user_prompt=json.dumps(payload, ensure_ascii=False, separators=(',', ':')), schema=OriginalTaskFragment, image_inputs=request.image_inputs, source_delivery=batch, preserve_image_bytes=bool(request.image_inputs), allow_live_web_search=False, on_activity=on_activity, is_cancelled=is_cancelled)
        output = OriginalTaskFragment.model_validate(response.output_parsed)
        if output.title.strip():
            titles.append(output.title.strip())
        return (response, output.markdown)
    result = generate_original_board(adapter=adapter, delivery=preparation.delivery, requirement=preparation.requirement, teaching_plan='Execute this frozen BoardTask within its authorized destination and extent.', content_extent=safe_task['content_extent'], visual_provider=preparation.visuals, visual_plan=preparation.insertion_plan, write_original_fragment=write_fragment, on_activity=on_activity, is_cancelled=is_cancelled)
    from openclass_core.services.teaching_original_sources import register_teaching_original_dependencies
    register_teaching_original_dependencies(preparation.delivery, input_parts=result.input_parts)
    title = titles[0] if titles else str(safe_task['question_or_topic']).strip()[:200]
    return OriginalTaskResult(title, result.content, list(result.turn.activity), preparation, result.input_parts)

def bounded_task_original_inputs(preparation, *, citation_style='identity'):
    from openclass_core.services.long_source_generation import _request_for_batch
    from openclass_core.services.original_generation_plan import GenerationPart, OriginalInputCapacity
    from openclass_core.services.original_source_delivery import combine_original_batches
    from openclass_core.services.source_content_contract import SourceRoleContext
    capacity = OriginalInputCapacity()
    batches = []
    for batch in preparation.delivery.iter_batches():
        combined = combine_original_batches([*batches, batch])
        capacity.check(combined)
        batches.append(batch)
    if not batches:
        raise ValueError('The authorized mutation has no original input.')
    combined = combine_original_batches(batches)
    combined, request = _request_for_batch(requirement=preparation.requirement, teaching_plan='Execute the bounded BoardTask.', content_extent=preparation.requirement.content_extent, batch=combined, part=GenerationPart(unit_ids=[item.id for item in combined.evidence_items]), visual_provider=preparation.visuals, visual_plan=preparation.insertion_plan, capacity=capacity)
    context = SourceRoleContext(json.dumps(original_context_for_model(combined, citation_style=citation_style), ensure_ascii=False), image_inputs=combined.model_image_inputs, image_manifest=combined.image_manifest, original_delivery=combined)
    return (context, request.visual_manifest)
