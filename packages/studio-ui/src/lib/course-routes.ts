import type { RepositoryProjectView } from "../types";

export type WorkspaceRoute = { canonical_path: string; package_id: string; lesson_id: string | null };

export async function workspaceStudioHref(lessonId: string) {
  try {
    const { api } = await import("./api");
    return (await api.getWorkspaceRoute(lessonId)).canonical_path;
  } catch {
    // Address lookup must not turn an acknowledged save, import or fork into a failed action.
    return `/studio?lesson=${encodeURIComponent(lessonId)}`;
  }
}

export function repositoryHref(project: Pick<RepositoryProjectView, "canonical_path">, studio = false) {
  return `${project.canonical_path}${studio ? "/studio" : ""}`;
}

/** Keep deep-link state when replacing an address, including repeated parameters. */
export function routeWithQuery(path: string, search: string, hash = "", omit: string[] = []) {
  const params = new URLSearchParams(search);
  omit.forEach((key) => params.delete(key));
  const query = params.toString();
  return `${path}${query ? `?${query}` : ""}${hash}`;
}

export function workspaceRouteSegments(pathname: string): string[] | null {
  if (pathname === "/studio") return [];
  if (!pathname.startsWith("/studio/")) return null;
  try { return pathname.slice(8).split("/").filter(Boolean).map(decodeURIComponent); }
  catch { throw new Error("Invalid course address"); }
}
