"use client";

import { createContext, useContext, useMemo, type ReactNode } from "react";
import { api, createApiClient } from "../lib/api";
import { useStudioHost } from "./studio-host-context";
import type { CoursePackage, RepositoryProjectView } from "../types";

export type StudioContext =
  | { kind: "workspace" }
  | { kind: "repository"; repositoryId: string }
  | { kind: "temporary"; sessionId: string }
  | { kind: "review"; sessionId: string; prId: string; repositoryId: string }
  | { kind: "version"; repositoryId: string; versionId: string; sequence?: number; createdAt?: string };

export type CourseStudioProps = { initialCoursePackage?: CoursePackage | null; readOnly?: boolean; repositoryId?: string | null; repositoryProjects?: RepositoryProjectView[]; studioContext?: StudioContext };

const StudioApiContext = createContext(api);
const StudioModeContext = createContext<StudioContext>({ kind: "workspace" });

export function StudioApiProvider({ context, children }: { context: StudioContext; children: ReactNode }) {
  const host = useStudioHost();
  const sessionId = context.kind === "temporary" ? context.sessionId : undefined;
  const repositoryId = context.kind === "repository" ? context.repositoryId : undefined;
  const reviewSessionId = context.kind === "review" ? context.sessionId : undefined;
  const versionRepositoryId = context.kind === "version" ? context.repositoryId : undefined;
  const versionId = context.kind === "version" ? context.versionId : undefined;
  const client = useMemo(() => sessionId || repositoryId || reviewSessionId || versionId ? createApiClient(sessionId, repositoryId, reviewSessionId, versionRepositoryId && versionId ? { repositoryId: versionRepositoryId, versionId } : undefined) : host.client ?? api, [host.client, sessionId, repositoryId, reviewSessionId, versionRepositoryId, versionId]);
  return <StudioModeContext.Provider value={context}><StudioApiContext.Provider value={client}>{children}</StudioApiContext.Provider></StudioModeContext.Provider>;
}

export function useStudioApi() { return useContext(StudioApiContext); }
export function useStudioContext() { return useContext(StudioModeContext); }
