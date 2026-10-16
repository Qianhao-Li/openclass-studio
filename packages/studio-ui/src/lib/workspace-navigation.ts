import type { CoursePackage } from "../types";
import type { LessonNavigationItem } from "../types/workspace-navigation";

export function courseNavigationItems(course: CoursePackage): LessonNavigationItem[] {
  const opened = new Set(course.workspace_tab_order);
  return course.lessons.filter((lesson) => opened.has(lesson.id)).map((lesson) => ({
    id: lesson.id, package_id: course.id,
    package_kind: course.is_standalone ? "standalone" : "course",
    title: lesson.title, current_branch: lesson.history_graph.current_branch,
    updated_at: lesson.updated_at,
  }));
}
