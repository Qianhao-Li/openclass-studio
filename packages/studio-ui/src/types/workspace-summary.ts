import type { CoursePackage, Lesson } from ".";

type PublicationSummary = { published_at: string; source_commit_id?: string | null };
export type LessonSummary = Pick<Lesson, "id" | "title" | "slug" | "summary" | "tags" | "visibility" | "publication_review" | "created_at" | "updated_at"> & {
  published_version?: PublicationSummary | null;
};
export type PackageSummary = Pick<CoursePackage, "id" | "title" | "summary" | "tags" | "visibility" | "publication_review" | "is_standalone" | "active_lesson_id" | "open_lesson_ids" | "workspace_tab_order" | "storage_kind" | "ridoc_revision" | "package_kind"> & {
  lessons: LessonSummary[];
  published_version?: PublicationSummary | null;
};
export type WorkspaceSummary = { packages: PackageSummary[]; active_package_id?: string | null };
