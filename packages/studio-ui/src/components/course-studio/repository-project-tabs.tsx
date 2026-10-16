"use client";

import clsx from "clsx";

import type { RepositoryProjectView } from "../../types";

export function RepositoryProjectTabs({
  activeRepositoryId,
  projects,
  onSelect,
}: {
  activeRepositoryId: string;
  projects: RepositoryProjectView[];
  onSelect: (repositoryId: string) => void;
}) {
  return (
    <nav className="flex min-w-0 items-center overflow-x-auto custom-scrollbar" aria-label="Repository projects">
      {projects.map((project) => (
        <button
          key={project.id}
          type="button"
          onClick={() => onSelect(project.id)}
          className={clsx(
            "flex h-12 shrink-0 items-center border-r border-gray-100 px-4 text-left text-[10px] font-bold uppercase tracking-[0.16em] transition-colors",
            project.id === activeRepositoryId
              ? "border-b-2 border-violet-700 bg-white text-violet-800"
              : "bg-white text-gray-400 hover:bg-gray-50 hover:text-black",
          )}
        >
          <span className="max-w-[190px] truncate">{project.title}</span>
        </button>
      ))}
    </nav>
  );
}
