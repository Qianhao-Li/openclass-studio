"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { usePathname, useSearchParams } from "next/navigation";
import { useStudioHost } from "../../contexts/studio-host-context";
import { setStudioLessonLocation, studioLocationLesson, takeCourseHandoff } from "../../lib/course-handoff";
import { workspaceRouteSegments } from "../../lib/course-routes";

import { useStudioApi, useStudioContext } from "../../contexts/studio-api-context";
import { ApiRequestError } from "../../lib/api";
import { applyDocumentSaveDeltaToPackage } from "../../lib/workspace-delta";
import { CourseHistoryDeletionFence, type DeletedHistoryNode } from "../../lib/course-history-deletion-fence";
import {
  DEFAULT_LESSON_COMPOSER_STATE,
  buildLessonMessagesFromHistory,
  createLessonComposerState,
  type ChatMessage,
  type LessonComposerState,
  type LessonComposerStateMap,
  type LessonMessageMap,
} from "../../components/course-studio/history-utils";
import { mergeCoursePackageForLesson } from "./chat-turn-ui-state";
import { useStandaloneLessonTabs } from "./use-standalone-lesson-tabs";
import { courseNavigationItems } from "../../lib/workspace-navigation";
import type { CoursePackage, DocumentSaveDelta, Lesson } from "../../types";

export type CoursePackageApplyOptions = {
  blankLessonIds?: string[];
  activeLessonId?: string | null;
  rebuildMessageLessonIds?: string[];
  mergeLessonId?: string;
  deletedHistoryNodeIds?: string[];
  deletedHistoryNodes?: DeletedHistoryNode[];
};

export type AppliedCoursePackage = {
  coursePackage: CoursePackage;
  activeLesson: Lesson | null;
};

type AutoSavedPackageResult = {
  coursePackage: CoursePackage;
  savedLesson: Lesson | null;
  ignored?: boolean;
};

function createMessageMap(nextPackage: CoursePackage, current: LessonMessageMap): LessonMessageMap {
  const next: LessonMessageMap = { ...current };
  nextPackage.lessons.forEach((lesson) => {
    next[lesson.id] = lessonMessagesFromCacheOrHistory(lesson, current);
  });
  return next;
}

function lessonMessagesFromCacheOrHistory(lesson: Lesson, current: LessonMessageMap): ChatMessage[] {
  const cachedMessages = current[lesson.id];
  const historyMessages = buildLessonMessagesFromHistory(lesson);
  if (!cachedMessages || (cachedMessages.length === 0 && historyMessages.length > 0)) {
    return historyMessages;
  }
  return cachedMessages;
}

function createComposerStateMap(nextPackage: CoursePackage, current: LessonComposerStateMap): LessonComposerStateMap {
  const next: LessonComposerStateMap = { ...current };
  nextPackage.lessons.forEach((lesson) => {
    next[lesson.id] = current[lesson.id] ?? createLessonComposerState();
  });
  return next;
}

export function useCourseWorkspace(repositoryId?: string | null, initialPackage?: CoursePackage | null) {
  const api = useStudioApi();
  const studioContext = useStudioContext();
  const host = useStudioHost();
  const principalId = host.principal?.id;
  const requestedLessonId = useSearchParams().get("lesson");
  const pathname = usePathname();
  const destinationLessonId = studioContext.kind === "workspace" && pathname === "/studio" ? requestedLessonId : null;
  const scopeKey = JSON.stringify([studioContext, principalId]);
  const loadedScope = useRef<string | null>(initialPackage ? scopeKey : null);
  const [coursePackage, setCoursePackage] = useState<CoursePackage | null>(initialPackage ?? null);
  const [isLoading, setIsLoading] = useState(!initialPackage);
  const [isEmptyWorkspace, setIsEmptyWorkspace] = useState(false);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const reload = useCallback(() => setLoadAttempt((attempt) => attempt + 1), []);
  const [error, setError] = useState<string | null>(null);
  const [lessonMessages, setLessonMessages] = useState<LessonMessageMap>(() =>
    initialPackage ? createMessageMap(initialPackage, {}) : {}
  );
  const [lessonComposerStates, setLessonComposerStates] = useState<LessonComposerStateMap>(() =>
    initialPackage ? createComposerStateMap(initialPackage, {}) : {}
  );
  const coursePackageRef = useRef<CoursePackage | null>(initialPackage ?? null);
  const deletionFence = useRef(new CourseHistoryDeletionFence());
  const applicationSequence = useRef(0);
  const getHistoryDeletionGeneration = useCallback(() => deletionFence.current.generation, []);
  useEffect(() => {
    deletionFence.current.enterScope(scopeKey);
    applicationSequence.current += 1;
  }, [scopeKey]);

  useEffect(() => {
    if (initialPackage) {
      return;
    }
    if (destinationLessonId && loadedScope.current === scopeKey && coursePackageRef.current?.active_lesson_id === destinationLessonId) return;
    if (!destinationLessonId && studioContext.kind === "workspace" && loadedScope.current === scopeKey
      && studioLocationLesson(pathname) === coursePackageRef.current?.active_lesson_id) return;
    let cancelled = false;
    const requestSequence = applicationSequence.current;
    async function load() {
      setIsLoading(true);
      setIsEmptyWorkspace(false);
      try {
        let lessonId = destinationLessonId;
        if (studioContext.kind === "workspace") {
          const segments = workspaceRouteSegments(pathname);
          if (segments?.length) {
            if (segments.length > 2) throw new Error("Course not found");
            const route = await api.resolveWorkspaceRoute(segments[0], segments[1]);
            if (cancelled || requestSequence !== applicationSequence.current) return;
            lessonId = route.lesson_id;
          }
        }
        const transferred = studioContext.kind === "workspace" && principalId && lessonId
          ? takeCourseHandoff(principalId, lessonId) : null;
        const payload = transferred ?? await api.getCoursePackage(repositoryId, lessonId);
        if (cancelled || requestSequence !== applicationSequence.current || !deletionFence.current.accepts(payload, scopeKey)) return;
        const sameScope = loadedScope.current === scopeKey;
        coursePackageRef.current = payload;
        loadedScope.current = scopeKey;
        setCoursePackage(payload);
        setLessonMessages((current) => createMessageMap(payload, sameScope ? current : {}));
        setLessonComposerStates((current) => createComposerStateMap(payload, sameScope ? current : {}));
        setError(null);
      } catch (loadError) {
        if (cancelled || requestSequence !== applicationSequence.current) return;
        if (studioContext.kind === "workspace" && pathname === "/studio" && !destinationLessonId && !repositoryId && loadError instanceof ApiRequestError && loadError.status === 404) {
          try {
            const workspace = await api.getWorkspace();
            if (cancelled || requestSequence !== applicationSequence.current) return;
            if (workspace.packages.length === 0) {
              setIsEmptyWorkspace(true);
              setError(null);
              return;
            }
          } catch (workspaceError) {
            if (cancelled || requestSequence !== applicationSequence.current) return;
            setError(workspaceError instanceof Error ? workspaceError.message : "Loading failed");
            return;
          }
        }
        coursePackageRef.current = null;
        setCoursePackage(null);
        setError(loadError instanceof Error ? loadError.message : "Loading failed");
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    }
    void load();
    return () => { cancelled = true; };
  }, [api, initialPackage, repositoryId, scopeKey, studioContext.kind, loadAttempt, destinationLessonId, principalId, pathname]);

  useEffect(() => {
    if (studioContext.kind === "workspace" && coursePackage?.active_lesson_id) {
      setStudioLessonLocation(coursePackage.active_lesson_id);
    }
  }, [studioContext.kind, coursePackage?.active_lesson_id]);

  const lessonMap = useMemo(() => {
    const next = new Map<string, Lesson>();
    coursePackage?.lessons.forEach((lesson) => next.set(lesson.id, lesson));
    return next;
  }, [coursePackage]);

  const activeLesson = useMemo(() => {
    if (!coursePackage) {
      return null;
    }
    return coursePackage.active_lesson_id
      ? lessonMap.get(coursePackage.active_lesson_id) ?? null
      : null;
  }, [coursePackage, lessonMap]);

  const openLessons = useMemo(
    () =>
      coursePackage ? courseNavigationItems(coursePackage).sort((left, right) => right.updated_at.localeCompare(left.updated_at)) : [],
    [coursePackage]
  );
  const standaloneTabs = useStandaloneLessonTabs(coursePackage);
  const rememberStandalonePackage = standaloneTabs.remember;

  const activeMessages = activeLesson
    ? lessonMessagesFromCacheOrHistory(activeLesson, lessonMessages)
    : [];

  const activeComposerState = activeLesson
    ? lessonComposerStates[activeLesson.id] ?? DEFAULT_LESSON_COMPOSER_STATE
    : DEFAULT_LESSON_COMPOSER_STATE;

  const syncLessonMessages = useCallback(
    (nextPackage: CoursePackage, options?: { blankLessonIds?: string[]; rebuildLessonIds?: string[] }) => {
      if (loadedScope.current !== scopeKey || !deletionFence.current.accepts(nextPackage, scopeKey)) return;
      const blankLessonIds = new Set(options?.blankLessonIds ?? []);
      const rebuildLessonIds = new Set(options?.rebuildLessonIds ?? []);
      setLessonMessages((current) => {
        // The loaded RIDOC is only one course; other tabs can still be streaming.
        const next: LessonMessageMap = { ...current };
        nextPackage.lessons.forEach((lesson) => {
          next[lesson.id] = blankLessonIds.has(lesson.id)
            ? []
            : rebuildLessonIds.has(lesson.id)
              ? buildLessonMessagesFromHistory(lesson)
              : lessonMessagesFromCacheOrHistory(lesson, current);
        });
        return next;
      });
    },
    [scopeKey]
  );

  const syncLessonComposerStates = useCallback((lessons: Lesson[]) => {
    if (loadedScope.current !== scopeKey) return;
    setLessonComposerStates((current) => {
      const next: LessonComposerStateMap = { ...current };
      lessons.forEach((lesson) => {
        next[lesson.id] = current[lesson.id] ?? createLessonComposerState();
      });
      return next;
    });
  }, [scopeKey]);

  const updateLessonMessages = useCallback(
    (lessonId: string, updater: (messages: ChatMessage[]) => ChatMessage[]) => {
      if (loadedScope.current !== scopeKey) return;
      setLessonMessages((current) => ({
        ...current,
        [lessonId]: updater(current[lessonId] ?? []).filter((message) =>
          !message.commitId || !coursePackageRef.current ||
          !deletionFence.current.contains(coursePackageRef.current.id, message.commitId, scopeKey, lessonId)
        ),
      }));
    },
    [scopeKey]
  );

  const updateLessonComposerState = useCallback(
    (lessonId: string, updater: (current: LessonComposerState) => LessonComposerState) => {
      if (loadedScope.current !== scopeKey) return;
      setLessonComposerStates((current) => ({
        ...current,
        [lessonId]: updater(current[lessonId] ?? createLessonComposerState()),
      }));
    },
    [scopeKey]
  );

  const updateActiveLessonComposerState = useCallback(
    (updater: (current: LessonComposerState) => LessonComposerState) => {
      if (!activeLesson) {
        return;
      }
      updateLessonComposerState(activeLesson.id, updater);
    },
    [activeLesson, updateLessonComposerState]
  );

  const applyCoursePackage = useCallback(
    (nextPackage: CoursePackage, options?: CoursePackageApplyOptions): AppliedCoursePackage => {
      const currentPackage = coursePackageRef.current;
      if (loadedScope.current !== scopeKey && currentPackage) {
        return { coursePackage: currentPackage, activeLesson: currentPackage.lessons.find((lesson) => lesson.id === currentPackage.active_lesson_id) ?? null };
      }
      if (options?.deletedHistoryNodes?.length) {
        deletionFence.current.recordNodes(nextPackage.id, options.deletedHistoryNodes, scopeKey);
      } else if (options?.deletedHistoryNodeIds?.length) {
        deletionFence.current.record(nextPackage.id, options.deletedHistoryNodeIds, scopeKey,
          options.activeLessonId ?? options.mergeLessonId ?? nextPackage.active_lesson_id ?? undefined);
      }
      if (!deletionFence.current.accepts(nextPackage, scopeKey) && coursePackageRef.current) {
        const current = coursePackageRef.current;
        return { coursePackage: current, activeLesson: current.lessons.find((lesson) => lesson.id === current.active_lesson_id) ?? null };
      }
      const packageToApply =
        options?.mergeLessonId && currentPackage
          ? mergeCoursePackageForLesson(currentPackage, nextPackage, options.mergeLessonId)
          : nextPackage;
      if (options?.mergeLessonId && currentPackage && currentPackage.id !== nextPackage.id) {
        // A background result belongs to its own RIDOC, including its revision.
        rememberStandalonePackage(nextPackage);
        return { coursePackage: currentPackage, activeLesson: currentPackage.lessons.find((lesson) => lesson.id === currentPackage.active_lesson_id) ?? null };
      }
      applicationSequence.current += 1;
      const requestedActiveLessonId = options?.activeLessonId;
      const effectiveActiveLessonId =
        requestedActiveLessonId && packageToApply.workspace_tab_order.includes(requestedActiveLessonId)
          ? requestedActiveLessonId
          : packageToApply.active_lesson_id;
      const mergedPackage =
        effectiveActiveLessonId === packageToApply.active_lesson_id
          ? packageToApply
          : { ...packageToApply, active_lesson_id: effectiveActiveLessonId };
      const nextActiveLesson =
        mergedPackage.lessons.find((lesson) => lesson.id === mergedPackage.active_lesson_id) ??
        mergedPackage.lessons[0] ??
        null;

      rememberStandalonePackage(coursePackageRef.current);
      loadedScope.current = scopeKey;
      coursePackageRef.current = mergedPackage;
      setCoursePackage(mergedPackage);
      syncLessonMessages(mergedPackage, {
        blankLessonIds: options?.blankLessonIds,
        rebuildLessonIds: options?.rebuildMessageLessonIds,
      });
      syncLessonComposerStates(mergedPackage.lessons);
      if (!options?.mergeLessonId || mergedPackage.active_lesson_id === options.mergeLessonId) {
        setError(null);
      }

      return { coursePackage: mergedPackage, activeLesson: nextActiveLesson };
    },
    [rememberStandalonePackage, scopeKey, syncLessonComposerStates, syncLessonMessages]
  );

  const applyAutoSavedCoursePackage = useCallback(
    (
      delta: DocumentSaveDelta,
      lessonId: string,
      currentActiveLessonId: string | null,
      requestDeletionGeneration?: number
    ): AutoSavedPackageResult => {
      const currentPackage = coursePackageRef.current;
      if (!currentPackage) {
        throw new Error("Course workspace is not loaded");
      }
      if (requestDeletionGeneration !== undefined && requestDeletionGeneration !== deletionFence.current.generation) {
        return { coursePackage: currentPackage, savedLesson: currentPackage.lessons.find((lesson) => lesson.id === lessonId) ?? null, ignored: true };
      }
      const packageToApply = applyDocumentSaveDeltaToPackage(currentPackage, delta);
      if (!deletionFence.current.accepts(packageToApply, scopeKey)) {
        return { coursePackage: currentPackage, savedLesson: currentPackage.lessons.find((lesson) => lesson.id === lessonId) ?? null, ignored: true };
      }
      applicationSequence.current += 1;
      const previousLesson = currentPackage.lessons.find((lesson) => lesson.id === lessonId);
      const branchChanged = previousLesson?.history_graph.current_branch !== delta.current_branch;
      const effectiveActiveLessonId =
        currentActiveLessonId && packageToApply.workspace_tab_order.includes(currentActiveLessonId)
          ? currentActiveLessonId
          : packageToApply.active_lesson_id;
      const mergedPackage =
        effectiveActiveLessonId === packageToApply.active_lesson_id
          ? packageToApply
          : { ...packageToApply, active_lesson_id: effectiveActiveLessonId };
      const savedLesson = mergedPackage.lessons.find((lesson) => lesson.id === lessonId) ?? null;

      coursePackageRef.current = mergedPackage;
      setCoursePackage(mergedPackage);
      syncLessonMessages(mergedPackage, {
        rebuildLessonIds: branchChanged ? [lessonId] : undefined,
      });
      syncLessonComposerStates(mergedPackage.lessons);
      setError(null);

      return { coursePackage: mergedPackage, savedLesson };
    },
    [scopeKey, syncLessonComposerStates, syncLessonMessages]
  );

  const selectLocalLesson = useCallback((lessonId: string) => {
    setCoursePackage((current) => {
      const nextPackage = current ? { ...current, active_lesson_id: lessonId } : current;
      coursePackageRef.current = nextPackage;
      return nextPackage;
    });
  }, []);
  const getCoursePackageSnapshot = useCallback(() => coursePackageRef.current, []);

  return {
    coursePackage,
    isLoading,
    isEmptyWorkspace,
    reload,
    error: error ?? standaloneTabs.error,
    setError,
    lessonMap,
    activeLesson,
    openLessons: standaloneTabs.lessons ?? openLessons,
    activeMessages,
    activeComposerState,
    lessonMessages,
    lessonComposerStates,
    syncLessonMessages,
    syncLessonComposerStates,
    updateLessonMessages,
    updateLessonComposerState,
    updateActiveLessonComposerState,
    applyCoursePackage,
    applyAutoSavedCoursePackage,
    getHistoryDeletionGeneration,
    getCoursePackageSnapshot,
    selectLocalLesson,
  };
}
