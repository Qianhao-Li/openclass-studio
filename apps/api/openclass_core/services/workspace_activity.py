"""Activity is a read projection, never a second representation of course history."""
from __future__ import annotations
from collections import Counter
from datetime import datetime
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import HTTPException
from openclass_core.services import workspace_state
from openclass_core.services.ridoc_workspace_store import course_store_mode

def workspace_activity(user_id: str, timezone: str) -> dict:
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(400, 'Timezone is not available') from exc
    days: Counter[str] = Counter()
    feed: list[dict] = []

    def track(value: str) -> None:
        if value:
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                days[parsed.astimezone(zone).date().isoformat()] += 1
            except ValueError:
                pass

    def group(package_id: str, package_title: str, standalone: bool, lessons: list[dict]) -> None:
        groups = [[lesson] for lesson in lessons] if standalone else [lessons]
        for items in groups:
            updates = [update for lesson in items for update in lesson['updates']]
            updates.sort(key=lambda item: (item['timestamp'], item['id']), reverse=True)
            if not updates:
                continue
            latest = updates[0]
            identity = f"lesson:{items[0]['id']}" if standalone else f'package:{package_id}'
            feed.append({'id': f'commit-group:{identity}', 'kind': 'commit', 'timestamp': latest['timestamp'], 'actor': items[0]['title'] if standalone else package_title, 'action': f'{len(updates)} learning updates', 'title': 'Recent update history', 'detailTitle': latest['detailTitle'], 'detailBody': latest['detailBody'], 'pills': [package_title, f'{len(items)} course pages', f'{len(updates)} commits'], 'lessonId': latest['lessonId'], 'updates': updates[:20]})
    if course_store_mode() == 'ridoc_required' and (not workspace_state.current_repository_workspace_scope()):
        store = workspace_state.get_ridoc_workspace_store()
        for index in store.list_packages(user_id):
            archive = store.load_archive(user_id, package_id=index.id)
            lessons = []
            for lesson_id in archive.package.lesson_order:
                graph = archive.package.lessons[lesson_id]
                track(graph.created_at)
                track(graph.updated_at)
                updates = []
                for identity in graph.node_order:
                    node = archive.nodes[f'{lesson_id}/{identity}']
                    if node.kind not in {'M', 'C'}:
                        continue
                    track(node.created_at)
                    message = getattr(node, 'label', '') or getattr(node, 'user_message', '') or 'Initial course draft'
                    excerpt = re.sub('\\s+', ' ', message).strip()[:180]
                    updates.append({'id': f'commit:{identity}', 'timestamp': node.created_at, 'title': excerpt, 'detailTitle': f'Branch {node.branch_name}', 'detailBody': excerpt, 'lessonTitle': graph.title, 'lessonId': lesson_id})
                lessons.append({'id': lesson_id, 'title': graph.title, 'updates': updates})
            group(index.id, index.title, index.package_kind == 'standalone', lessons)
    else:
        workspace = workspace_state.load_workspace_for_user(user_id)
        for package in workspace.packages:
            lessons = []
            for lesson in package.lessons:
                track(lesson.created_at)
                track(lesson.updated_at)
                updates = []
                for commit in lesson.history_graph.commits:
                    track(commit.created_at)
                    updates.append({'id': f'commit:{commit.id}', 'timestamp': commit.created_at, 'title': commit.label, 'detailTitle': f'Branch {commit.branch_name}', 'detailBody': commit.message[:180], 'lessonTitle': lesson.title, 'lessonId': lesson.id})
                lessons.append({'id': lesson.id, 'title': lesson.title, 'updates': updates})
            for resource in package.resources:
                track(resource.uploaded_at)
            group(package.id, package.title, workspace_state.is_standalone_package(workspace, package), lessons)
    feed.sort(key=lambda item: (item['timestamp'], item['id']), reverse=True)
    return {'days': dict(days), 'feed': feed}
