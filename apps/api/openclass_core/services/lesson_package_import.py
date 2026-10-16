from __future__ import annotations
import re
from copy import deepcopy
from typing import Any, Mapping
from openclass_core.models import BoardDocument, BranchRef, CommitRecord, CoursePackage, Lesson, LessonHistoryGraph, LessonRuntimeSnapshot, PatchOperation, TeachingGuide, WorkspaceState, new_id
from openclass_core.services.board_asset_store import BoardAssetStore, get_board_asset_store
from openclass_core.services.lesson_package_format import RidocArchive, RidocFormatError
_BOARD_ASSET_URL_RE = re.compile('/api/board-assets/(?P<asset_id>basset_[A-Za-z0-9_-]+)/content')

def import_ridoc_archive(*, owner_user_id: str, workspace: WorkspaceState, archive: RidocArchive, asset_store: BoardAssetStore | None=None) -> CoursePackage:
    asset_store = asset_store or get_board_asset_store()
    lesson_payload = archive.graph.get('lesson')
    commits_payload = archive.graph.get('commits')
    branches_payload = archive.graph.get('branches')
    if not isinstance(lesson_payload, dict) or not isinstance(commits_payload, list) or (not isinstance(branches_payload, dict)):
        raise RidocFormatError('RIDOC lesson history is incomplete.')
    new_lesson_id = new_id('lesson')
    new_document_id = new_id('doc')
    commit_id_map = {str(commit['id']): new_id('commit') for commit in commits_payload if isinstance(commit, dict) and isinstance(commit.get('id'), str)}
    if len(commit_id_map) != len(commits_payload):
        raise RidocFormatError('RIDOC commits cannot be remapped safely.')
    asset_id_map: dict[str, str] = {}
    try:
        asset_id_map = _import_assets(owner_user_id=owner_user_id, lesson_id=new_lesson_id, document_id=new_document_id, archive=archive, asset_store=asset_store)
        imported_commits = [_import_commit(raw_commit, archive=archive, document_id=new_document_id, lesson_id=new_lesson_id, commit_id_map=commit_id_map, asset_id_map=asset_id_map) for raw_commit in commits_payload]
        imported_branches = {name: BranchRef(name=name, head_commit_id=_mapped_commit_id(branch.get('head_commit_id'), commit_id_map), base_commit_id=_mapped_commit_id(branch.get('base_commit_id'), commit_id_map), created_at=str(branch.get('created_at') or '')) for name, branch in branches_payload.items() if isinstance(name, str) and isinstance(branch, dict)}
        current_branch = str(archive.graph.get('current_branch') or '')
        if current_branch not in imported_branches:
            raise RidocFormatError('RIDOC current branch could not be imported.')
        head_commit_id = imported_branches[current_branch].head_commit_id
        head_commit = next((commit for commit in imported_commits if commit.id == head_commit_id), None)
        if head_commit is None:
            raise RidocFormatError('RIDOC current branch head is unavailable.')
        runtime = head_commit.runtime_snapshot or LessonRuntimeSnapshot()
        teaching_guide = TeachingGuide.model_validate(lesson_payload.get('teaching_guide'))
        lesson = Lesson(id=new_lesson_id, title=str(lesson_payload.get('title') or archive.manifest.get('lesson', {}).get('title') or 'Imported lesson'), slug=_imported_slug(str(lesson_payload.get('slug') or 'imported-lesson'), new_lesson_id), summary=str(lesson_payload.get('summary') or ''), tags=[str(value) for value in lesson_payload.get('tags') or [] if isinstance(value, str)], board_document=head_commit.snapshot, board_teaching_guide=runtime.board_teaching_guide, board_teaching_progress=runtime.board_teaching_progress, learning_requirements=runtime.learning_requirements, board_task_requirements=runtime.board_task_requirements, teaching_guide=teaching_guide, history_graph=LessonHistoryGraph(branches=imported_branches, commits=imported_commits, current_branch=current_branch), created_at=str(lesson_payload.get('created_at') or archive.manifest.get('exported_at') or ''), updated_at=str(archive.manifest.get('exported_at') or lesson_payload.get('updated_at') or ''))
        if not workspace.packages:
            raise RidocFormatError('RIDOC lesson requires a standalone course pool.')
        standalone = workspace.packages[0]
        standalone.lessons.append(lesson)
        standalone.open_lesson_ids.append(lesson.id)
        standalone.workspace_tab_order.append(lesson.id)
        standalone.active_lesson_id = lesson.id
        workspace.active_package_id = standalone.id
        return standalone
    except Exception:
        asset_store.remove_lesson_references(owner_user_id=owner_user_id, lesson_id=new_lesson_id)
        raise

def rollback_imported_assets(*, owner_user_id: str, lesson_id: str, asset_store: BoardAssetStore | None=None) -> None:
    (asset_store or get_board_asset_store()).remove_lesson_references(owner_user_id=owner_user_id, lesson_id=lesson_id)

def _import_commit(raw_commit: Mapping[str, Any], *, archive: RidocArchive, document_id: str, lesson_id: str, commit_id_map: Mapping[str, str], asset_id_map: Mapping[str, str]) -> CommitRecord:
    original_commit_id = str(raw_commit.get('id') or '')
    snapshot_ref = raw_commit.get('snapshot_ref')
    snapshot_payload = archive.snapshots.get(snapshot_ref) if isinstance(snapshot_ref, str) else None
    if not isinstance(snapshot_payload, dict) or not isinstance(snapshot_payload.get('document'), dict):
        raise RidocFormatError('RIDOC commit snapshot is unavailable during import.')
    document_payload = _rewrite_import_value(deepcopy(snapshot_payload['document']), document_id=document_id, lesson_id=lesson_id, commit_id_map=commit_id_map, asset_id_map=asset_id_map)
    document_payload['id'] = document_id
    runtime_payload = _rewrite_import_value(deepcopy(snapshot_payload.get('runtime_snapshot')), document_id=document_id, lesson_id=lesson_id, commit_id_map=commit_id_map, asset_id_map=asset_id_map)
    metadata = _rewrite_import_value(deepcopy(raw_commit.get('metadata') or {}), document_id=document_id, lesson_id=lesson_id, commit_id_map=commit_id_map, asset_id_map=asset_id_map)
    metadata['ridoc_origin'] = {'document_id': archive.manifest.get('document_id'), 'commit_id': original_commit_id, 'branch_name': raw_commit.get('branch_name'), 'spec_version': archive.manifest.get('spec_version')}
    metadata['ridoc_imported'] = True
    metadata['ridoc_source_mode'] = archive.manifest.get('source_mode')
    metadata['ridoc_warnings'] = archive.manifest.get('warnings') or []
    return CommitRecord(id=_mapped_commit_id(original_commit_id, commit_id_map), label=str(raw_commit.get('label') or 'Imported history'), message=str(raw_commit.get('message') or 'Imported from RIDOC'), branch_name=str(raw_commit.get('branch_name') or 'main'), created_at=str(raw_commit.get('created_at') or archive.manifest.get('exported_at') or ''), parent_ids=[_mapped_commit_id(parent_id, commit_id_map) for parent_id in raw_commit.get('parent_ids') or []], operations=[PatchOperation.model_validate(operation) for operation in raw_commit.get('operations') or []], snapshot=BoardDocument.model_validate(document_payload), runtime_snapshot=LessonRuntimeSnapshot.model_validate(runtime_payload) if isinstance(runtime_payload, dict) else None, metadata=metadata)

def _import_assets(*, owner_user_id: str, lesson_id: str, document_id: str, archive: RidocArchive, asset_store: BoardAssetStore) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in archive.manifest.get('asset_index') or []:
        if not isinstance(item, dict):
            continue
        original_id = item.get('original_id')
        path = item.get('path')
        content = archive.assets.get(path) if isinstance(path, str) else None
        if not isinstance(original_id, str) or content is None:
            raise RidocFormatError('RIDOC embedded asset is unavailable during import.')
        record = asset_store.put_bytes(owner_user_id=owner_user_id, lesson_id=lesson_id, document_id=document_id, content=content, mime_type=str(item.get('mime_type') or 'application/octet-stream'), file_name=str(item.get('file_name') or 'asset'))
        result[original_id] = record.id
    return result

def _rewrite_import_value(value: Any, *, document_id: str, lesson_id: str, commit_id_map: Mapping[str, str], asset_id_map: Mapping[str, str]) -> Any:
    if isinstance(value, dict):
        rewritten = {key: _rewrite_import_value(item, document_id=document_id, lesson_id=lesson_id, commit_id_map=commit_id_map, asset_id_map=asset_id_map) for key, item in value.items()}
        if isinstance(rewritten.get('assetId'), str):
            rewritten['assetId'] = asset_id_map.get(rewritten['assetId'], rewritten['assetId'])
        if isinstance(rewritten.get('asset_id'), str):
            rewritten['asset_id'] = asset_id_map.get(rewritten['asset_id'], rewritten['asset_id'])
        return rewritten
    if isinstance(value, list):
        return [_rewrite_import_value(item, document_id=document_id, lesson_id=lesson_id, commit_id_map=commit_id_map, asset_id_map=asset_id_map) for item in value]
    if not isinstance(value, str):
        return value
    if value in commit_id_map:
        return commit_id_map[value]
    if value in asset_id_map:
        return asset_id_map[value]
    rewritten = _BOARD_ASSET_URL_RE.sub(lambda match: f"/api/board-assets/{asset_id_map.get(match.group('asset_id'), match.group('asset_id'))}/content", value)
    if rewritten.startswith('lesson_'):
        return lesson_id
    if rewritten.startswith('doc_'):
        return document_id
    return rewritten

def _mapped_commit_id(value: object, mapping: Mapping[str, str]) -> str:
    if not isinstance(value, str) or value not in mapping:
        raise RidocFormatError('RIDOC history contains an unmappable commit reference.')
    return mapping[value]

def _imported_slug(value: str, lesson_id: str) -> str:
    normalized = re.sub('[^a-z0-9-]+', '-', value.lower()).strip('-') or 'imported-lesson'
    return f'{normalized}-{lesson_id[-8:]}'
