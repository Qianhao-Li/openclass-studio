export type LessonNavigationItem = {
  id: string;
  package_id: string;
  package_kind: "course" | "standalone";
  title: string;
  current_branch: string | null;
  updated_at: string;
};

export type WorkspaceTabsView = {
  lessons: LessonNavigationItem[];
  active_package_id: string | null;
};
