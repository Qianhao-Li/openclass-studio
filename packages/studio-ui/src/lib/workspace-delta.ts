import type {
  CoursePackage,
  DocumentSaveDelta,
  Lesson,
  LessonWorkspaceDelta,
  WorkspaceState,
} from "../types";

export function applyLessonWorkspaceDeltaToPackage(
  coursePackage: CoursePackage,
  delta: LessonWorkspaceDelta
): CoursePackage {
  if (coursePackage.id !== delta.package_id) {
    return coursePackage;
  }

  let lessons = coursePackage.lessons;
  let courseGraph = coursePackage.course_graph;
  if (delta.operation === "create" && delta.created_lesson) {
    lessons = lessons.some((lesson) => lesson.id === delta.created_lesson?.id)
      ? lessons.map((lesson) => (lesson.id === delta.created_lesson?.id ? delta.created_lesson : lesson))
      : [...lessons, delta.created_lesson];
    if (delta.graph_edge && !courseGraph.some((edge) => edge.id === delta.graph_edge?.id)) {
      courseGraph = [...courseGraph, delta.graph_edge];
    }
  } else if (delta.operation === "delete" && delta.deleted_lesson_id) {
    lessons = lessons.filter((lesson) => lesson.id !== delta.deleted_lesson_id);
    courseGraph = courseGraph.filter(
      (edge) =>
        edge.source_lesson_id !== delta.deleted_lesson_id &&
        edge.target_lesson_id !== delta.deleted_lesson_id
    );
  }

  return {
    ...coursePackage,
    storage_kind: delta.storage_kind,
    ridoc_revision: delta.ridoc_revision ?? coursePackage.ridoc_revision,
    lessons,
    course_graph: courseGraph,
    open_lesson_ids: delta.open_lesson_ids,
    active_lesson_id: delta.active_lesson_id ?? null,
    workspace_tab_order: delta.workspace_tab_order,
  };
}

export function applyLessonWorkspaceDeltaToWorkspace(
  workspace: WorkspaceState,
  delta: LessonWorkspaceDelta
): WorkspaceState {
  return {
    ...workspace,
    active_package_id: delta.active_package_id ?? workspace.active_package_id ?? null,
    packages: workspace.packages.map((coursePackage) =>
      applyLessonWorkspaceDeltaToPackage(coursePackage, delta)
    ),
  };
}

export function applyDocumentSaveDeltaToLesson(lesson: Lesson, delta: DocumentSaveDelta): Lesson {
  if (lesson.id !== delta.lesson_id) {
    return lesson;
  }
  const commits = lesson.history_graph.commits.some((commit) => commit.id === delta.latest_commit.id)
    ? lesson.history_graph.commits.map((commit) =>
        commit.id === delta.latest_commit.id ? delta.latest_commit : commit
      )
    : [...lesson.history_graph.commits, delta.latest_commit];
  const currentBranch = lesson.history_graph.branches[delta.current_branch];
  return {
    ...lesson,
    title: delta.storage_kind === "ridoc" ? delta.document.title : lesson.title,
    board_document: delta.document,
    learning_requirements: null,
    board_task_requirements: null,
    updated_at: delta.updated_at,
    history_graph: {
      ...lesson.history_graph,
      current_branch: delta.current_branch,
      commits,
      branches: {
        ...lesson.history_graph.branches,
        [delta.current_branch]: {
          name: delta.current_branch,
          base_commit_id: delta.branch_base_commit_id ?? currentBranch?.base_commit_id ?? delta.branch_head_commit_id,
          created_at: currentBranch?.created_at ?? delta.latest_commit.created_at,
          head_commit_id: delta.branch_head_commit_id,
          document_cursor_commit_id: delta.changed
            ? delta.latest_commit.id
            : currentBranch?.document_cursor_commit_id ?? delta.latest_commit.id,
          document_redo_commit_ids: delta.changed ? [] : currentBranch?.document_redo_commit_ids ?? [],
        },
      },
    },
  };
}

export function applyDocumentSaveDeltaToPackage(
  coursePackage: CoursePackage,
  delta: DocumentSaveDelta
): CoursePackage {
  if (coursePackage.id !== delta.package_id) {
    return coursePackage;
  }
  return {
    ...coursePackage,
    storage_kind: delta.storage_kind,
    ridoc_revision: delta.ridoc_revision ?? coursePackage.ridoc_revision,
    lessons: coursePackage.lessons.map((lesson) => applyDocumentSaveDeltaToLesson(lesson, delta)),
  };
}
