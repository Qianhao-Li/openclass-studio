"use client";

import { useEffect, useRef, type Dispatch, type SetStateAction } from "react";

import { useStudioApi } from "../../contexts/studio-api-context";
import { notifyWorkspaceTabsChanged } from "./use-standalone-lesson-tabs";
import { applyLessonWorkspaceDeltaToPackage } from "../../lib/workspace-delta";
import { setStudioLessonLocation } from "../../lib/course-handoff";
import type { AutoSaveReason } from "./use-board-draft";
import type { CoursePackageApplyOptions } from "./use-course-workspace";
import type { CoursePackage, Lesson } from "../../types";

type UseWorkspaceActionsOptions = {
  repositoryId?: string | null;
  coursePackage: CoursePackage | null;
  activeLesson: Lesson | null;
  lessonMap: Map<string, Lesson>;
  flushAutoSave: (reason: AutoSaveReason) => Promise<boolean>;
  updateCoursePackage: (nextPackage: CoursePackage, options?: CoursePackageApplyOptions) => void;
  selectLocalLesson: (lessonId: string) => void;
  resetDraftToLesson: (lesson: Lesson | null) => void;
  resetTransientUi: () => void;
  setError: Dispatch<SetStateAction<string | null>>;
  setBusyAction: Dispatch<SetStateAction<string | null>>;
};

export function useWorkspaceActions({
  repositoryId,
  coursePackage,
  activeLesson,
  lessonMap,
  flushAutoSave,
  updateCoursePackage,
  selectLocalLesson,
  resetDraftToLesson,
  resetTransientUi,
  setError,
  setBusyAction,
}: UseWorkspaceActionsOptions) {
  const api = useStudioApi();
  const activeLessonIdRef = useRef<string | null>(activeLesson?.id ?? null);
  const coursePackageRef = useRef<CoursePackage | null>(coursePackage);
  const workspaceMutationInFlightRef = useRef(false);
  const selectionSequenceRef = useRef(0);
  const selectionQueueRef = useRef<Promise<void>>(Promise.resolve());

  useEffect(() => {
    activeLessonIdRef.current = activeLesson?.id ?? null;
  }, [activeLesson?.id]);

  useEffect(() => {
    coursePackageRef.current = coursePackage;
  }, [coursePackage]);

  async function saveGeneratedLesson(): Promise<boolean> {
    const initialActiveLessonId = activeLesson?.id ?? null;
    setBusyAction("generate");
    try {
      const delta = await api.generateLesson({
        branchFromLessonId:
          coursePackage?.storage_kind === "ridoc" || coursePackage?.is_standalone
            ? null
            : activeLesson?.id,
        startBlank: true,
        targetPackageId: coursePackage?.is_standalone ? null : coursePackage?.id,
        expectedRidocRevision:
          coursePackage?.storage_kind === "ridoc" && !coursePackage.is_standalone ? coursePackage.ridoc_revision : null,
      });
      const currentPackage = coursePackageRef.current;
      if (!currentPackage) {
        throw new Error("Course workspace is not loaded");
      }
      const nextPackage = applyLessonWorkspaceDeltaToPackage(currentPackage, delta);
      const generatedLessonId = delta.created_lesson?.id ?? delta.active_lesson_id ?? null;
      const currentActiveLessonId = activeLessonIdRef.current;
      if (delta.package_id !== currentPackage.id && generatedLessonId) {
        if (currentActiveLessonId === initialActiveLessonId) {
          updateCoursePackage(await api.openLesson(generatedLessonId), {
            activeLessonId: generatedLessonId,
            blankLessonIds: [generatedLessonId],
          });
        }
        notifyWorkspaceTabsChanged();
        return true;
      }
      const shouldPreserveCurrentLesson =
        currentActiveLessonId !== null &&
        currentActiveLessonId !== initialActiveLessonId &&
        nextPackage.workspace_tab_order.includes(currentActiveLessonId);
      updateCoursePackage(nextPackage, {
        activeLessonId: shouldPreserveCurrentLesson ? currentActiveLessonId : generatedLessonId,
        blankLessonIds: generatedLessonId ? [generatedLessonId] : [],
      });
      return true;
    } catch (generationError) {
      setError(generationError instanceof Error ? generationError.message : "Failed to generate lesson");
      return false;
    } finally {
      setBusyAction(null);
    }
  }

  async function handleCreateLesson() {
    if (workspaceMutationInFlightRef.current) {
      return false;
    }
    workspaceMutationInFlightRef.current = true;
    try {
      if (!(await flushAutoSave("create-lesson"))) {
        return false;
      }
      return await saveGeneratedLesson();
    } finally {
      workspaceMutationInFlightRef.current = false;
    }
  }

  async function handleOpenLesson(lessonId: string) {
    if (!(await flushAutoSave("open-lesson"))) {
      return;
    }
    setBusyAction("open-lesson");
    try {
      const nextPackage = await api.openLesson(lessonId);
      updateCoursePackage(nextPackage);
    } catch (openError) {
      setError(openError instanceof Error ? openError.message : "Failed to open course");
    } finally {
      setBusyAction(null);
    }
  }

  async function handleCloseLesson(lessonId: string) {
    if (workspaceMutationInFlightRef.current) {
      return;
    }
    workspaceMutationInFlightRef.current = true;
    if (activeLesson?.id === lessonId && !(await flushAutoSave("close-lesson"))) {
      workspaceMutationInFlightRef.current = false;
      return;
    }
    setBusyAction("close-lesson");
    try {
      const currentPackage = coursePackageRef.current;
      if (!currentPackage) {
        return;
      }
      const targetPackage = currentPackage.lessons.some((lesson) => lesson.id === lessonId)
        ? currentPackage : await api.getCoursePackage(null, lessonId);
      const result = await api.deleteLesson(lessonId, targetPackage.storage_kind === "ridoc" ? targetPackage.ridoc_revision : null);
      // Deleting an inactive standalone RIDOC must preserve the current editor.
      if (targetPackage.id !== currentPackage.id) {
        notifyWorkspaceTabsChanged();
        return;
      }
      let nextPackage = "packages" in result
        ? result.packages.find((course) => course.id === currentPackage.id)
        : applyLessonWorkspaceDeltaToPackage(currentPackage, result);
      if (currentPackage.is_standalone && (!nextPackage || !nextPackage.workspace_tab_order.length)) {
        const tabs = await api.getWorkspaceTabs();
        const nextLesson = tabs.lessons.find((item) => item.package_kind === "standalone");
        if (nextLesson) nextPackage = await api.openLesson(nextLesson.id);
      }
      resetTransientUi();
      if (nextPackage) {
        const nextId = activeLesson && activeLesson.id !== lessonId && nextPackage.lessons.some((lesson) => lesson.id === activeLesson.id)
          ? activeLesson.id : nextPackage.active_lesson_id ?? null;
        updateCoursePackage(nextPackage, { activeLessonId: nextId });
        resetDraftToLesson(nextPackage.lessons.find((lesson) => lesson.id === nextId) ?? null);
        if (nextId) setStudioLessonLocation(nextId);
        else window.history.replaceState(window.history.state, "", "/studio");
      } else {
        window.location.assign("/studio");
      }
      notifyWorkspaceTabsChanged();
    } catch (closeError) {
      setError(closeError instanceof Error ? closeError.message : "Failed to delete course");
    } finally {
      setBusyAction(null);
      workspaceMutationInFlightRef.current = false;
    }
  }

  async function handleSelectLesson(lessonId: string) {
    const sequence = ++selectionSequenceRef.current;
    const run = async () => {
      if (sequence !== selectionSequenceRef.current) return;
      if (activeLessonIdRef.current !== lessonId && !(await flushAutoSave("select-lesson"))) return;
      if (sequence !== selectionSequenceRef.current) return;
      resetTransientUi();
      if (repositoryId) {
        selectLocalLesson(lessonId);
        resetDraftToLesson(lessonMap.get(lessonId) ?? null);
        return;
      }
      setBusyAction("open-lesson");
      try {
        const nextPackage = await api.openLesson(lessonId);
        if (sequence === selectionSequenceRef.current) {
          updateCoursePackage(nextPackage, { activeLessonId: lessonId });
          setStudioLessonLocation(lessonId);
        }
      } catch (openError) {
        if (sequence === selectionSequenceRef.current) setError(openError instanceof Error ? openError.message : "Failed to open course");
      } finally {
        setBusyAction(null);
      }
    };
    const pending = selectionQueueRef.current.then(run, run);
    selectionQueueRef.current = pending;
    await pending;
  }

  return {
    handleCreateLesson,
    handleOpenLesson,
    handleCloseLesson,
    handleSelectLesson,
  };
}
