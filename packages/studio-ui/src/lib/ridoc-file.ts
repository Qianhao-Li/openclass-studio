import type { Lesson } from "../types";

export const RIDOC_FILE_ACCEPT = ".ridoc,application/vnd.openclass.ridoc+zip";

function safeFileName(value: string): string {
  const normalized = value.trim().replace(/[^\p{L}\p{N}._-]+/gu, "-").replace(/^-+|-+$/g, "");
  return normalized || "lesson";
}

export function ridocFileName(lesson: Pick<Lesson, "slug" | "title">): string {
  return `${safeFileName(lesson.slug || lesson.title)}.ridoc`;
}

export function downloadRidoc(blob: Blob, lesson: Pick<Lesson, "slug" | "title">) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = ridocFileName(lesson);
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
