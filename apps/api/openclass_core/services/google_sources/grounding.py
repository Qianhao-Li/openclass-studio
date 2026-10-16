"""Freeze only original content actually read for an authorized board request."""
from __future__ import annotations
from openclass_core.models import EvidenceBundle, SourceQueryRef, now_iso
from openclass_core.services.source_content_retrieval import retrieve_original_content
from .config import GoogleSourceError
from .service import get_google_source_service

def ground_selected_source(*, owner_user_id, package_id, lesson, source, selection, query, is_cancelled=None, calculation_adapter=None, on_activity=None):
    from openclass_core.services.source_grounded_board import _plan_from_retrieval_bundle, SourceGroundedBoardError
    service = get_google_source_service()
    ref = SourceQueryRef(source_ingestion_id=source.id, source_content_hash=selection.source_content_hash or source.metadata['content_hash'], source_chapter_id=selection.source_chapter_id, catalog_version=selection.catalog_version, source_range=selection.source_range, page_start=selection.source_page_start, page_end=selection.source_page_end)
    try:
        from openclass_core.services.source_content_retrieval import iter_original_content_batches
        from openclass_core.services.original_source_delivery import collect_original_inventory
        evidence, images, manifest, coverage, delivery = collect_original_inventory(iter_original_content_batches(service, owner_user_id=owner_user_id, package_id=package_id, query=query or selection.excerpt, refs=[ref], authorization='explicit', grant_id=f'{lesson.id}:{now_iso()}', role='board_editor', read_complete=True, is_cancelled=is_cancelled), owner_user_id=owner_user_id, package_id=package_id, is_cancelled=is_cancelled, source_store=service.store, structure_store=service.structure_store, google_service=service)
        if not evidence or any((item.get('reason') in {'source_read_budget_exhausted', 'no_verified_original_content'} or item.get('missing') for item in coverage)):
            raise GoogleSourceError('The selected original range was not read completely; the teaching task has not generated a board.')
        pending = {}
        for entry in coverage:
            key = str(entry.get('read_range'))
            pending[key] = not entry.get('complete', False)
        if any(pending.values()):
            raise GoogleSourceError('The selected original source range remains incomplete.')
        bundle = EvidenceBundle(owner_user_id=owner_user_id, package_id=package_id, lesson_id=lesson.id, purpose='board_generation', status='confirmed', query=query or selection.excerpt, evidence_items=evidence, context_text='\n\n'.join((item.expanded_text for item in evidence)), token_count=sum((item.token_count for item in evidence)), confirmed_by_user=True, confirmed_at=now_iso(), metadata={'source_transport': 'original_source_content', 'authorization': 'explicit', 'source_content_hash': source.metadata['content_hash'], 'source_version': source.metadata['source_version'], 'complete': True, 'coverage': coverage, 'image_manifest': manifest, 'source_delivery_contract': 'original_source_delivery_v1'})
        plan = _plan_from_retrieval_bundle(owner_user_id=owner_user_id, lesson=lesson, query=query or selection.excerpt, retrieval_bundle=bundle)
        from .turn_evidence import bind_citation_numbers
        plan.requirement.source_grounding.frozen_evidence = bind_citation_numbers(plan.requirement.source_grounding.frozen_evidence)
        plan.requirement.source_grounding.reading_notes = []
        from dataclasses import replace
        return replace(plan, image_inputs=images, image_manifest=manifest, preserve_image_bytes=bool(images), original_delivery=delivery)
    except (GoogleSourceError, ValueError) as exc:
        raise SourceGroundedBoardError(str(exc)) from exc
