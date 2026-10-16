"use client";

import { BookOpen, Plus } from "lucide-react";
import { LessonTabs } from "./lesson-tabs";
import { RepositoryProjectTabs } from "./repository-project-tabs";
import type { StudioUiBundle } from "../../lib/i18n/product-ui";
import type { RepositoryProjectView } from "../../types";
import type { LessonNavigationItem } from "../../types/workspace-navigation";

export function StudioNavigation({ repositoryId, repositoryProjects, readOnly, texts, lessons, activeLessonId,
  creating, pendingFork, mergeActive, onError, onSelectLesson, onCloseLesson, onCreateLesson, onNavigate, beforeNavigate, beforeLeave,
}: {
  repositoryId?: string | null;
  repositoryProjects: RepositoryProjectView[];
  readOnly: boolean;
  texts: StudioUiBundle;
  lessons: LessonNavigationItem[];
  activeLessonId: string | null;
  creating: boolean;
  pendingFork: boolean;
  mergeActive: boolean;
  onError: (message: string) => void;
  onSelectLesson: (id: string) => void | Promise<void>;
  onCloseLesson: (id: string) => void | Promise<void>;
  onCreateLesson: () => void | Promise<unknown>;
  onNavigate: (path: string) => void;
  beforeNavigate?: () => boolean | Promise<boolean>;
  beforeLeave?: () => boolean | Promise<boolean>;
}) {
  async function allowed(action: string, mergeMessage: string) {
    if (beforeNavigate && !(await beforeNavigate())) return false;
    if (pendingFork) { onError(`Return to the current version before ${action}.`); return false; }
    if (mergeActive) { onError(mergeMessage); return false; }
    return true;
  }
  const tabs = <LessonTabs
    allowCourseCollectionChanges={!repositoryId && !readOnly}
    texts={texts} lessons={lessons} activeLessonId={activeLessonId} isBusyCreating={creating}
    onSelectLesson={async (id) => {
      if (await allowed("switching lessons", "Courses cannot be switched during the merge, please submit or abandon the merge first.")) await onSelectLesson(id);
    }}
    onCloseLesson={async (id) => {
      if (await allowed("deleting this lesson", "Courses cannot be deleted during the merge.")) await onCloseLesson(id);
    }}
    onCreateLesson={async () => {
      if (await allowed("creating another lesson", "New courses cannot be created during the merge.")) await onCreateLesson();
    }}
  />;
  if (!repositoryId || !repositoryProjects.length) return tabs;
  return <div className="flex min-w-0 items-center overflow-hidden">
    <RepositoryProjectTabs activeRepositoryId={repositoryId} projects={repositoryProjects} onSelect={async (nextId) => {
      if (nextId !== repositoryId && beforeLeave && !(await beforeLeave())) return;
      if (pendingFork) { onError("Return to the current version before switching repositories."); return; }
      if (nextId !== repositoryId) onNavigate(`${repositoryProjects.find((project) => project.id === nextId)!.canonical_path}/studio`);
    }} />
    {lessons.length > 1 ? <div className="min-w-0 border-l border-gray-200">{tabs}</div> : null}
  </div>;
}

export function StudioEmptyState({ texts, creating, onCreate }: {
  texts: StudioUiBundle;
  creating: boolean;
  onCreate: () => void;
}) {
  return <section className="flex flex-1 items-center justify-center px-6">
    <div className="w-full max-w-xl rounded-[32px] border border-stone-200 bg-white/90 p-10 text-center shadow-[0_24px_70px_rgba(15,23,42,0.08)]">
      <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-[20px] bg-stone-950 text-white">
        <BookOpen className="h-7 w-7" />
      </div>
      <h2 className="mt-6 text-2xl font-semibold tracking-tight text-stone-950">{texts.emptyPackageTitle}</h2>
      <p className="mt-3 text-sm leading-7 text-stone-500">{texts.emptyPackageBody}</p>
      <div className="mt-8 flex justify-center">
        <button type="button" onClick={onCreate} disabled={creating}
          className="inline-flex items-center gap-2 rounded-full bg-stone-950 px-5 py-3 text-sm font-medium text-white transition hover:bg-stone-800 disabled:cursor-wait disabled:bg-stone-500">
          <Plus className="h-4 w-4" />{texts.createFirstPage}
        </button>
      </div>
    </div>
  </section>;
}
