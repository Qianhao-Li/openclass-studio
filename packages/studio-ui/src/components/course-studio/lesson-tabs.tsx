"use client";

import clsx from "clsx";
import { LoaderCircle, Plus, X } from "lucide-react";
import { useRef, useState } from "react";
import { useInterfaceLanguage } from "../../contexts/interface-language-context";

import type { StudioUiBundle } from "../../lib/i18n/product-ui";
import type { LessonNavigationItem } from "../../types/workspace-navigation";

type LessonTabsProps = {
  allowCourseCollectionChanges?: boolean;
  texts: StudioUiBundle;
  lessons: LessonNavigationItem[];
  activeLessonId: string | null;
  isBusyCreating: boolean;
  onSelectLesson: (lessonId: string) => void | Promise<void>;
  onCloseLesson: (lessonId: string) => void | Promise<void>;
  onCreateLesson: () => void;
};

export function LessonTabs({
  allowCourseCollectionChanges = true,
  texts,
  lessons,
  activeLessonId,
  isBusyCreating,
  onSelectLesson,
  onCloseLesson,
  onCreateLesson,
}: LessonTabsProps) {
  const { language: lang } = useInterfaceLanguage();
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const deletionInFlight = useRef(false);
  const selectionSequence = useRef(0);
  async function select(id: string) {
    if (deletionInFlight.current) return;
    if (id === activeLessonId && !pendingId) return;
    const sequence = ++selectionSequence.current;
    setPendingId(id);
    try { await onSelectLesson(id); }
    finally { if (sequence === selectionSequence.current) setPendingId(null); }
  }
  async function remove(lesson: LessonNavigationItem) {
    if (deletionInFlight.current) return;
    const message = lang === "zh-CN"
      ? `删除“${lesson.title}”？删除后可在个人账户的“最近删除”中找回。`
      : `Delete “${lesson.title}”? You can restore it from Recently deleted in your account.`;
    if (!window.confirm(message)) return;
    deletionInFlight.current = true;
    setDeletingId(lesson.id);
    try { await onCloseLesson(lesson.id); }
    finally { deletionInFlight.current = false; setDeletingId(null); }
  }
  return (
    <nav className="flex min-w-0 items-center overflow-x-auto custom-scrollbar">
      {allowCourseCollectionChanges ? <button
        type="button"
        onClick={onCreateLesson}
        disabled={isBusyCreating || deletingId !== null}
        className="p-3 text-gray-300 transition-colors hover:text-black"
        title={texts.createPageTitle}
        aria-label={texts.createPageTitle}
      >
        {isBusyCreating ? (
          <LoaderCircle className="h-4 w-4 animate-spin" />
        ) : (
          <Plus className="h-4 w-4" />
        )}
      </button> : null}
      {lessons.map((lesson) => (
        <div
          key={lesson.id}
          data-lesson-tab={lesson.id}
          className={clsx(
            "group flex h-12 shrink-0 items-center gap-1 border-r border-gray-100 px-2 text-left text-[10px] font-bold uppercase tracking-[0.2em] transition-colors",
            lesson.id === activeLessonId
              ? "border-b-2 border-black bg-white text-black"
              : "bg-white text-gray-400 hover:bg-gray-50 hover:text-black"
          )}
        >
          <button type="button" onClick={() => void select(lesson.id)}
            disabled={deletingId !== null} aria-busy={pendingId === lesson.id}
            className="flex h-full items-center gap-2 px-2 text-left disabled:cursor-wait">
          <span className="max-w-[160px] truncate">{lesson.title}</span>
          <span className="max-w-[52px] truncate text-[9px] font-medium tracking-[0.16em] text-gray-300">
            {lesson.current_branch ?? ""}
          </span>
          {pendingId === lesson.id ? <LoaderCircle aria-hidden="true" className="h-3 w-3 animate-spin" /> : null}
          </button>
          {allowCourseCollectionChanges ? <button type="button"
            aria-label={lang === "zh-CN" ? `删除课程：${lesson.title}` : `Delete course: ${lesson.title}`}
            title={lang === "zh-CN" ? "删除课程，移入最近删除" : "Delete course and move to Recently deleted"}
            disabled={deletingId !== null} aria-busy={deletingId === lesson.id}
            className="rounded-md p-1 text-gray-300 opacity-0 transition hover:bg-rose-50 hover:text-rose-600 focus-visible:opacity-100 group-hover:opacity-100 disabled:cursor-wait"
            onClick={() => void remove(lesson)}
          >
            {deletingId === lesson.id ? <LoaderCircle className="h-3 w-3 animate-spin" /> : <X className="h-3 w-3" />}
          </button> : null}
        </div>
      ))}
    </nav>
  );
}
