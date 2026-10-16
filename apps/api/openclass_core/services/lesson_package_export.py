from __future__ import annotations
from pathlib import Path
from typing import Iterable
from openclass_core.models import EvidenceBundle, Lesson
from openclass_core.services.board_asset_store import BoardAssetStore, get_board_asset_store
from openclass_core.services.lesson_package_format import RidocAsset, build_ridoc_archive, write_ridoc
from openclass_core.services.lesson_package_source_pages import collect_referenced_source_pages
from openclass_core.services.source_evidence_store import SourceEvidenceStore, source_evidence_store

def export_lesson_ridoc(*, owner_user_id: str, package_id: str, lesson: Lesson, target_path: Path, source_mode: str='evidence', evidence_store: SourceEvidenceStore | None=None, asset_store: BoardAssetStore | None=None) -> Path:
    evidence_store = evidence_store or source_evidence_store
    asset_store = asset_store or get_board_asset_store()
    evidence_ids = referenced_evidence_bundle_ids(lesson)
    bundles: list[EvidenceBundle] = []
    missing_ids: list[str] = []
    if source_mode == 'evidence':
        for bundle_id in evidence_ids:
            bundle = evidence_store.get_bundle(owner_user_id=owner_user_id, bundle_id=bundle_id)
            if bundle is None:
                missing_ids.append(bundle_id)
            else:
                bundles.append(bundle)
    source_pages = []
    source_page_warnings: list[str] = []
    if source_mode == 'evidence':
        source_pages, source_page_warnings = collect_referenced_source_pages(owner_user_id=owner_user_id, package_id=package_id, lesson=lesson, evidence_bundles=bundles, evidence_store=evidence_store)
    assets = _lesson_board_assets(owner_user_id=owner_user_id, lesson_id=lesson.id, asset_store=asset_store)
    archive = build_ridoc_archive(lesson, evidence_bundles=bundles, missing_evidence_ids=missing_ids, assets=assets, source_pages=source_pages, source_page_warnings=source_page_warnings, source_mode=source_mode)
    return write_ridoc(archive, target_path)

def referenced_evidence_bundle_ids(lesson: Lesson) -> list[str]:
    result: list[str] = []

    def add(values: Iterable[object]) -> None:
        for value in values:
            if isinstance(value, str) and value and (value not in result):
                result.append(value)
    for commit in lesson.history_graph.commits:
        metadata_ids = commit.metadata.get('verified_source_bundle_ids')
        if isinstance(metadata_ids, list):
            add(metadata_ids)
        runtime = commit.runtime_snapshot
        requirement = runtime.learning_requirements if runtime is not None else None
        if requirement is None:
            continue
        grounding = requirement.source_grounding
        add([grounding.confirmed_bundle_id])
        add((reference.evidence_bundle_id for reference in grounding.confirmed_references))
    return result

def _lesson_board_assets(*, owner_user_id: str, lesson_id: str, asset_store: BoardAssetStore) -> list[RidocAsset]:
    result: list[RidocAsset] = []
    seen: set[str] = set()
    for reference in asset_store.references_for_lesson(owner_user_id=owner_user_id, lesson_id=lesson_id):
        if reference.asset_id in seen:
            continue
        stored = asset_store.read_bytes(reference.asset_id, owner_user_id)
        if stored is None:
            continue
        record, content = stored
        result.append(RidocAsset(original_id=record.id, mime_type=record.mime_type, file_name=record.file_name, content=content))
        seen.add(reference.asset_id)
    return result
