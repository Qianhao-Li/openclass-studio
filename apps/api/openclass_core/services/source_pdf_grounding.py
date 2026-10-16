"""Rehydrate authorized original PDF pages from frozen source identities."""
from __future__ import annotations
import json
from collections.abc import Callable, Iterable
from typing import Any
from openclass_core.models import LearningRequirementSheet, SelectionRef
from openclass_core.services.source_evidence_store import source_evidence_store
from openclass_core.services.source_range_reader import MAX_PDF_AI_INPUT_BYTES, SourceRangeReadError, read_source_range_for_ai
from openclass_core.services.source_structure_store import source_structure_store
PDF_SOURCE_INPUT_METHOD = 'original_pdf_page_images'

def pdf_image_manifest(range_read) -> list[dict[str, Any]]:
    return [{**item, 'source_content_hash': range_read.source_content_hash, 'catalog_version': range_read.catalog_version, 'source_range': range_read.source_range} for item in range_read.image_manifest]

def combine_pdf_image_inputs(components: Iterable[tuple[list[str], list[dict[str, Any]]]]) -> tuple[list[str], list[dict[str, Any]]]:
    images: list[str] = []
    manifest: list[dict[str, Any]] = []
    total_bytes = 0
    for component_images, component_manifest in components:
        if len(component_images) != len(component_manifest):
            raise SourceRangeReadError('The original PDF page images do not match the source manifest. Select the source range again.')
        for image, item in zip(component_images, component_manifest, strict=True):
            size = item.get('size_bytes')
            if type(size) is not int or size < 1:
                raise SourceRangeReadError('The original PDF page images have no verifiable size information. Select the source range again.')
            total_bytes += size
            if total_bytes > MAX_PDF_AI_INPUT_BYTES:
                raise SourceRangeReadError('The selected original PDF page images exceed 40 MiB in total. Select fewer pages or reference fewer sources.')
            manifest.append({**item, 'image_index': len(images)})
            images.append(image)
    return (images, manifest)

def _evidence_identity(item) -> tuple:
    metadata = item.metadata
    return (item.source_ingestion_id, item.chapter_id or '', metadata.get('source_content_hash'), metadata.get('catalog_version'), json.dumps(metadata.get('source_range'), sort_keys=True, separators=(',', ':')), metadata.get('page_start'), metadata.get('page_end'), metadata.get('page_image_sha256'))

def prepare_frozen_pdf_source_inputs(*, owner_user_id: str, requirement: LearningRequirementSheet, is_cancelled: Callable[[], bool] | None=None) -> tuple[list[str], list[dict[str, Any]]]:
    """Read fresh original page images without modifying the frozen requirement.

    The persisted objects contain identity and range metadata only. Image bytes
    are temporary model inputs and are never written back to a bundle or lesson.
    """
    from openclass_core.services.source_content_frozen import prepare_frozen_original_inputs, uses_legacy_original_reader
    original = prepare_frozen_original_inputs(owner_user_id=owner_user_id, requirement=requirement, is_cancelled=is_cancelled)
    grounding = requirement.source_grounding
    evidence = [item for item in grounding.frozen_evidence if (item.metadata.get('source_transport') == PDF_SOURCE_INPUT_METHOD or item.metadata.get('input_method') == PDF_SOURCE_INPUT_METHOD) and (not item.metadata.get('source_read_receipt') or uses_legacy_original_reader(item))]
    if not evidence:
        return original if original is not None else ([], [])
    bundle = source_evidence_store.get_bundle(owner_user_id=owner_user_id, bundle_id=grounding.confirmed_bundle_id)
    if bundle is None or bundle.owner_user_id != owner_user_id or (not bundle.confirmed_by_user) or (bundle.status not in {'confirmed', 'consumed'}) or (grounding.confirmation_status != 'confirmed'):
        raise SourceRangeReadError('The frozen PDF source authorization is no longer valid. Select the source range again.')
    stored_evidence = {item.id: item for item in bundle.evidence_items}
    groups: dict[tuple, list] = {}
    for item in evidence:
        stored = stored_evidence.get(item.id)
        if stored is None or _evidence_identity(stored) != _evidence_identity(item):
            raise SourceRangeReadError('The frozen PDF source identity or page range has been modified. Select the source range again.')
        if 'source_read_receipt' in item.metadata or 'source_read_receipt' in stored.metadata:
            from openclass_core.services.source_content_contract import verified_read_evidence
            if not verified_read_evidence(item) or not verified_read_evidence(stored):
                raise SourceRangeReadError('The frozen PDF original-reading receipt is invalid or modified.')
            for read in (item, stored):
                receipt = read.metadata['source_read_receipt']
                if not uses_legacy_original_reader(read) or receipt.get('owner_user_id') != owner_user_id or receipt.get('package_id') != bundle.package_id:
                    raise SourceRangeReadError('The frozen PDF receipt belongs to another reader or authorization.')
        source_range = item.metadata.get('source_range')
        if not isinstance(source_range, dict) or source_range.get('kind') != 'pdf_pages':
            raise SourceRangeReadError('The frozen PDF source is missing its authorized original-page range. Select it again.')
        matching_reference = next((reference for reference in grounding.confirmed_references if reference.source_ingestion_id == item.source_ingestion_id and (reference.source_chapter_id or '') == (item.chapter_id or '') and (reference.content_hash == item.metadata.get('source_content_hash')) and (reference.page_start == source_range.get('start')) and (reference.page_end == source_range.get('end')) and (reference.evidence_bundle_id == bundle.id)), None)
        if matching_reference is None:
            raise SourceRangeReadError('The frozen original PDF pages have no matching confirmed source range. Select them again.')
        groups.setdefault(_evidence_identity(item)[:5], []).append(item)
    components = [original] if original is not None else []
    for items in groups.values():
        first = items[0]
        metadata = first.metadata
        source = source_evidence_store.get_source(owner_user_id=owner_user_id, package_id=bundle.package_id, source_id=first.source_ingestion_id)
        if source is None or source.status != 'ready':
            raise SourceRangeReadError('The frozen original PDF file is unavailable. Select the source again.')
        chapter = None
        if first.chapter_id:
            pair = source_structure_store.get_catalog_chapter(owner_user_id=owner_user_id, package_id=bundle.package_id, source_id=source.id, chapter_id=first.chapter_id)
            if pair is None:
                raise SourceRangeReadError('The frozen PDF chapter is no longer valid. Select it again.')
            structure, chapter = pair
        else:
            structure = source_structure_store.get_structure(owner_user_id=owner_user_id, package_id=bundle.package_id, source_id=source.id)
            if structure is None:
                raise SourceRangeReadError('The frozen PDF source catalog is no longer valid. Select it again.')
        scope = metadata['source_range']
        selection = SelectionRef(kind='source', excerpt=first.source_title, source_ingestion_id=source.id, source_chapter_id=first.chapter_id, source_content_hash=metadata['source_content_hash'], catalog_version=metadata['catalog_version'], source_range=scope, source_scope_kind='chapter' if first.chapter_id else 'page_range', source_page_start=scope.get('start'), source_page_end=scope.get('end'))
        read = read_source_range_for_ai(owner_user_id=owner_user_id, package_id=bundle.package_id, source=source, structure=structure, chapter=chapter, selection=selection, is_cancelled=is_cancelled)
        frozen_pages = {item.metadata.get('page_start'): item for item in items}
        if len(frozen_pages) != len(items) or set(frozen_pages) != {item['physical_page'] for item in read.image_manifest}:
            raise SourceRangeReadError('The frozen original PDF page manifest is incomplete or inconsistent. Select the source range again.')
        manifest = pdf_image_manifest(read)
        for item in manifest:
            frozen = frozen_pages[item['physical_page']]
            if frozen.metadata.get('page_image_sha256') != item['sha256']:
                raise SourceRangeReadError('The original PDF page images have changed. Select the source range again.')
            item['evidence_id'] = frozen.id
        components.append((read.image_inputs, manifest))
    return combine_pdf_image_inputs(components)
