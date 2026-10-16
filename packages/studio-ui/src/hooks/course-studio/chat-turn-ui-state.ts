import type {
  BoardFocusRef,
  BoardTaskRequirementSheet,
  CoursePackage,
  TurnIntent,
} from "../../types";

export function activeLessonIdForAsyncPackage(
  coursePackage: CoursePackage,
  requestLessonId: string,
  currentActiveLessonId: string | null,
  fallbackActiveLessonId?: string | null
) {
  if (
    currentActiveLessonId &&
    currentActiveLessonId !== requestLessonId &&
    coursePackage.workspace_tab_order.includes(currentActiveLessonId)
  ) {
    return currentActiveLessonId;
  }
  return fallbackActiveLessonId;
}

export function mergeCoursePackageForLesson(
  currentPackage: CoursePackage,
  incomingPackage: CoursePackage,
  lessonId: string
): CoursePackage {
  if (currentPackage.id !== incomingPackage.id) {
    return currentPackage;
  }
  const incomingLesson = incomingPackage.lessons.find((lesson) => lesson.id === lessonId);
  if (!incomingLesson) {
    return currentPackage;
  }

  const currentLessons = new Map(currentPackage.lessons.map((lesson) => [lesson.id, lesson]));
  const incomingLessonIds = new Set(incomingPackage.lessons.map((lesson) => lesson.id));
  const lessons = incomingPackage.lessons.map((lesson) =>
    lesson.id === lessonId ? lesson : currentLessons.get(lesson.id) ?? lesson
  );
  currentPackage.lessons.forEach((lesson) => {
    if (!incomingLessonIds.has(lesson.id)) {
      lessons.push(lesson);
    }
  });

  const lessonIds = new Set(lessons.map((lesson) => lesson.id));
  const workspaceTabOrder = [
    ...currentPackage.workspace_tab_order,
    ...incomingPackage.workspace_tab_order.filter(
      (candidateId) => !currentPackage.workspace_tab_order.includes(candidateId)
    ),
  ].filter((candidateId) => lessonIds.has(candidateId));

  return {
    ...incomingPackage,
    lessons,
    workspace_tab_order: workspaceTabOrder,
    active_lesson_id:
      currentPackage.active_lesson_id && lessonIds.has(currentPackage.active_lesson_id)
        ? currentPackage.active_lesson_id
        : incomingPackage.active_lesson_id,
  };
}

export function editorUpdateBelongsToDocument(
  editorDocumentId: string | null,
  targetDocumentId: string
) {
  return editorDocumentId === targetDocumentId;
}

export function resolvedBoardFocusForTurn(
  intent: TurnIntent | null | undefined,
  task: BoardTaskRequirementSheet | null | undefined
): BoardFocusRef | null {
  if (intent === "ordinary_chat" || intent === "unclear") {
    return null;
  }
  if (task?.location_status !== "resolved" || !task.target_location) {
    return null;
  }
  return task.target_location;
}
