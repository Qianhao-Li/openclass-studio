"use client";

import { createContext, useContext, type ReactNode } from "react";
import type { useStudioNavigation } from "../hooks/course-studio/use-studio-navigation";
import type { api } from "../lib/api";
import type { ProfileSettings } from "../lib/profile-settings-state";

export type WorkbenchClient = typeof api;
export type StudioModelPreferences = { principalId: string; settings: Partial<ProfileSettings> };

export type WorkspacePrincipal = {
  id: string;
  kind: "local" | "platform";
  isGuest?: boolean;
};

export type StudioHost = {
  mode: "local" | "platform";
  principal?: WorkspacePrincipal;
  homeHref: string;
  client?: WorkbenchClient;
  capabilities?: { speech?: boolean; realtime?: boolean; cloudSources?: boolean; platformCollaboration?: boolean };
  getModelPreferences?: () => Promise<StudioModelPreferences>;
  accountMenu?: ReactNode;
  renderAccountMenu?: (navigation: ReturnType<typeof useStudioNavigation>) => ReactNode;
  collaborationChrome?: (navigation: ReturnType<typeof useStudioNavigation>) => ReactNode;
};

const StudioHostContext = createContext<StudioHost>({ mode: "local", homeHref: "/studio" });

export function StudioHostProvider({ host, children }: { host: StudioHost; children: ReactNode }) {
  return <StudioHostContext.Provider value={host}>{children}</StudioHostContext.Provider>;
}

export function useStudioHost() {
  return useContext(StudioHostContext);
}

export function useWorkspacePrincipal() {
  return useStudioHost().principal;
}
