import type { CoursePackage } from "../types";
import { routeWithQuery } from "./course-routes";

// One-use handoff from an acknowledged open, scoped to the verified user and
// explicit destination. This is not a cache of future editable versions.
let handoff: { userId: string; lessonId: string; course: CoursePackage; expires: number } | null = null;
let locationSequence = 0;
let studioLocation: { pathname: string; lessonId: string } | null = null;

export function rememberCourseHandoff(userId: string, course: CoursePackage) {
  const lessonId = course.active_lesson_id;
  if (!lessonId) return;
  handoff = { userId, lessonId, course, expires: Date.now() + 5_000 };
}

export function clearCourseHandoff() { handoff = null; studioLocation = null; locationSequence += 1; }

export function studioLocationLesson(pathname: string) {
  return studioLocation?.pathname === pathname ? studioLocation.lessonId : null;
}

export function takeCourseHandoff(userId: string, lessonId: string | null): CoursePackage | null {
  const saved = handoff;
  handoff = null;
  return saved && saved.userId === userId && saved.lessonId === lessonId && saved.expires >= Date.now() ? saved.course : null;
}

export function setStudioLessonLocation(lessonId: string) {
  if (typeof window === "undefined" || !/^\/studio(?:\/|$)/.test(window.location.pathname)) return;
  const sequence = ++locationSequence;
  const source = window.location.href;
  void import("./api").then(({ api }) => api.getWorkspaceRoute(lessonId)).then((route) => {
    if (sequence !== locationSequence || window.location.href !== source) return;
    studioLocation = { pathname: route.canonical_path, lessonId };
    const href = routeWithQuery(route.canonical_path, window.location.search, window.location.hash, ["lesson"]);
    window.history.replaceState(null, "", href);
  }).catch(() => {
    if (sequence !== locationSequence || window.location.href !== source) return;
    // Keep the exact course destination if address metadata is temporarily unavailable.
    const params = new URLSearchParams(window.location.search);
    params.set("lesson", lessonId);
    studioLocation = { pathname: "/studio", lessonId };
    window.history.replaceState(null, "", routeWithQuery("/studio", params.toString(), window.location.hash));
  });
}
