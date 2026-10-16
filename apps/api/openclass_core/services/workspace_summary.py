"""Read models for shelves. They never stand in for an editable lesson or history."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field
from openclass_core.models import PublicationReview
from openclass_core.services import workspace_state
from openclass_core.services.ridoc_workspace_store import course_store_mode

class PublicationSummary(BaseModel):
    published_at: str
    source_commit_id: str | None = None

class LessonSummary(BaseModel):
    id: str
    title: str
    slug: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    visibility: Literal['private', 'public'] = 'private'
    publication_review: PublicationReview = Field(default_factory=PublicationReview)
    published_version: PublicationSummary | None = None
    created_at: str
    updated_at: str

class PackageSummary(BaseModel):
    id: str
    title: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    visibility: Literal['private', 'public'] = 'private'
    publication_review: PublicationReview = Field(default_factory=PublicationReview)
    published_version: PublicationSummary | None = None
    is_standalone: bool
    lessons: list[LessonSummary]
    active_lesson_id: str | None
    open_lesson_ids: list[str]
    workspace_tab_order: list[str]
    storage_kind: Literal['legacy', 'ridoc']
    ridoc_revision: str | None = None
    package_kind: Literal['course', 'standalone']

class WorkspaceSummary(BaseModel):
    packages: list[PackageSummary]
    active_package_id: str | None

def _publication(value) -> PublicationSummary | None:
    return PublicationSummary(published_at=value.published_at, source_commit_id=getattr(value, 'source_commit_id', None)) if value else None

def workspace_summary(user_id: str) -> WorkspaceSummary:
    if course_store_mode() == 'ridoc_required' and (not workspace_state.current_repository_workspace_scope()):
        store = workspace_state.get_ridoc_workspace_store()
        packages = [PackageSummary(id=item.id, title=item.title, summary=item.summary, tags=item.tags, visibility=item.visibility, is_standalone=item.package_kind == 'standalone', storage_kind='ridoc', ridoc_revision=item.revision, package_kind=item.package_kind, active_lesson_id=item.active_lesson_id, open_lesson_ids=item.open_lesson_ids, workspace_tab_order=item.workspace_tab_order, lessons=[LessonSummary(id=lesson.id, title=lesson.title, slug=lesson.slug, summary=lesson.summary, tags=lesson.tags, created_at=lesson.created_at, updated_at=lesson.updated_at) for lesson in item.lessons]) for item in store.list_packages(user_id)]
        return WorkspaceSummary(packages=packages, active_package_id=store.active_package_id(user_id))
    workspace = workspace_state.load_workspace_for_user(user_id)
    return WorkspaceSummary(packages=[PackageSummary(**{key: getattr(package, key) for key in ('id', 'title', 'summary', 'tags', 'visibility', 'publication_review', 'active_lesson_id', 'open_lesson_ids', 'workspace_tab_order', 'storage_kind', 'ridoc_revision')}, published_version=_publication(package.published_version), is_standalone=workspace_state.is_standalone_package(workspace, package), package_kind='standalone' if workspace_state.is_standalone_package(workspace, package) else 'course', lessons=[LessonSummary(**{key: getattr(lesson, key) for key in ('id', 'title', 'slug', 'summary', 'tags', 'visibility', 'publication_review', 'created_at', 'updated_at')}, published_version=_publication(lesson.published_version)) for lesson in package.lessons]) for package in workspace.packages], active_package_id=workspace.active_package_id)
