"""The current source-teaching turn hands original units to its first explanation."""
from __future__ import annotations
import json
from contextlib import contextmanager
from contextvars import ContextVar
_turn: ContextVar[list | None] = ContextVar('teaching_original_source_deliveries', default=None)
_dependencies: ContextVar[dict | None] = ContextVar('teaching_original_unit_dependencies', default=None)

@contextmanager
def bind_teaching_original_sources():
    current = _turn.get()
    if current is not None:
        yield current
        return
    token = _turn.set([])
    dependency_token = _dependencies.set({})
    try:
        yield _turn.get()
    finally:
        _dependencies.reset(dependency_token)
        _turn.reset(token)

def register_teaching_original_sources(delivery):
    current = _turn.get()
    if current is not None and delivery is not None and (not any((item is delivery for item in current))):
        current.append(delivery)

def register_teaching_original_dependencies(delivery, *, visual_evidence=(), input_parts=()):
    """Retain current-turn raw-source dependencies, never generated prose."""
    dependencies = _dependencies.get()
    if dependencies is None:
        return
    available = {item.id for item in delivery.evidence_items}
    links = {}
    for visual in visual_evidence:
        identities = set(visual.metadata.get('dependency_unit_ids', []))
        for identity in identities:
            links.setdefault(identity, set()).update(identities - {identity})
    for part in input_parts:
        for identity in part.get('primary_unit_ids', []):
            links.setdefault(identity, set()).update(part.get('context_unit_ids', []))
    if not set(links) <= available or any((not values <= available for values in links.values())):
        raise ValueError('Teaching dependencies exceed the current authorized original source units.')
    for identity, values in links.items():
        dependencies.setdefault(identity, set()).update(values)

def first_teaching_original_context(excerpt, *, owner_user_id):
    deliveries = _turn.get() or []
    if not deliveries:
        return None
    from openclass_core.services.google_sources.turn_evidence import original_ids_for_document_excerpt
    from openclass_core.services.original_source_delivery import combine_original_batches, original_context_for_model
    from openclass_core.services.source_content_contract import SourceRoleContext
    from openclass_core.services.original_generation_plan import ORIGINAL_SOURCE_TEXT_BYTE_LIMIT, OriginalInputCapacity
    ids = original_ids_for_document_excerpt(excerpt)
    dependencies = _dependencies.get() or {}
    pending = list(ids)
    while pending:
        for identity in dependencies.get(pending.pop(), ()):
            if identity not in ids:
                ids.add(identity)
                pending.append(identity)
    batches = []
    delivered_ids = set()
    for delivery in deliveries:
        delivery.validate_current()
        available = {item.id for item in delivery.evidence_items}
        chosen = ids & available if ids else available
        if not chosen:
            continue
        for batch in delivery.iter_batches(unit_ids=chosen):
            if batch.owner_user_id != owner_user_id:
                raise ValueError("The first explanation's originals belong to another owner.")
            OriginalInputCapacity().check(combine_original_batches([*batches, batch]))
            batches.append(batch)
            delivered_ids.update((item.id for item in batch.evidence_items))
    if not batches or (ids and (not ids <= delivered_ids)):
        raise ValueError('The first teaching unit has no complete original-source delivery.')
    batch = combine_original_batches(batches)
    context = original_context_for_model(batch, citation_style='numbers')
    text = context if isinstance(context, str) else json.dumps(context, ensure_ascii=False)
    if len(text.encode('utf-8')) > ORIGINAL_SOURCE_TEXT_BYTE_LIMIT:
        raise ValueError("The first teaching unit's original text exceeds the source input capacity; no source summary will be substituted.")
    return SourceRoleContext(text, image_inputs=batch.image_inputs, image_manifest=batch.image_manifest, original_delivery=batch)
