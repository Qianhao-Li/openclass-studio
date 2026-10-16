"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { useStudioApi, useStudioContext } from "../../contexts/studio-api-context";
import { useWorkspacePrincipal } from "../../contexts/studio-host-context";
import type { CoursePackage, Lesson } from "../../types";
import type { LessonNavigationItem } from "../../types/workspace-navigation";
import { courseNavigationItems } from "../../lib/workspace-navigation";

const TABS_CHANGED = "openclass:workspace-tabs-changed";

export function notifyWorkspaceTabsChanged() {
  window.dispatchEvent(new Event(TABS_CHANGED));
}

export function standaloneOpenLessons(packages: CoursePackage[]): Lesson[] {
  return packages.filter((item) => item.is_standalone).flatMap((item) => {
    const opened = new Set(item.workspace_tab_order);
    return item.lessons.filter((lesson) => opened.has(lesson.id));
  }).sort((left, right) => new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime());
}

// Navigation spans standalone RIDOCs; the editor still receives only the real
// active package. Never combine their documents, revisions, or history graphs.
export function useStandaloneLessonTabs(currentPackage: CoursePackage | null) {
  const api = useStudioApi();
  const context = useStudioContext();
  const user = useWorkspacePrincipal();
  const scope = `${context.kind}:${user?.id ?? ""}`;
  const enabled = context.kind === "workspace" && Boolean(currentPackage?.is_standalone);
  const [snapshot, setSnapshot] = useState<{ scope: string; lessons: LessonNavigationItem[]; error: string | null } | null>(null);
  const remember = useCallback((course: CoursePackage | null) => {
    if (context.kind !== "workspace" || !course?.is_standalone) return;
    setSnapshot((previous) => ({
      scope, error: previous?.scope === scope ? previous.error : null,
      lessons: [...(previous?.scope === scope ? previous.lessons : []).filter((item) => item.package_id !== course.id), ...courseNavigationItems(course)],
    }));
  }, [context.kind, scope]);

  useEffect(() => {
    if (!enabled) return;
    let disposed = false;
    let sequence = 0;
    async function refresh() {
      const request = ++sequence;
      try {
        const tabs = await api.getWorkspaceTabs();
        if (!disposed && request === sequence) setSnapshot({ scope, lessons: tabs.lessons.filter((item) => item.package_kind === "standalone"), error: null });
      } catch (failure) {
        if (!disposed && request === sequence) setSnapshot({ scope, lessons: [], error: failure instanceof Error ? failure.message : "Unable to load course tabs" });
      }
    }
    const onRefresh = () => { void refresh(); };
    onRefresh();
    window.addEventListener(TABS_CHANGED, onRefresh);
    window.addEventListener("focus", onRefresh);
    return () => {
      disposed = true;
      window.removeEventListener(TABS_CHANGED, onRefresh);
      window.removeEventListener("focus", onRefresh);
    };
  }, [api, enabled, scope]);

  const lessons = useMemo(() => {
    if (!enabled || !currentPackage) return null;
    return [
      ...(snapshot?.scope === scope ? snapshot.lessons : []).filter((item) => item.package_id !== currentPackage.id),
      ...courseNavigationItems(currentPackage),
    ].sort((left, right) => right.updated_at.localeCompare(left.updated_at));
  }, [currentPackage, enabled, scope, snapshot]);

  return { lessons, remember, error: enabled && snapshot?.scope === scope ? snapshot.error : null };
}
