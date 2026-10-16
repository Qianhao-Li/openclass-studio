import { api } from "./api";

export type LocalStudioBootstrap = {
  mode: "local";
  principal: { id: string; kind: "local"; capabilities: string[] };
  capabilities: { courses: boolean; personal_models: boolean; platform_connected: boolean; cloud_enhancement: boolean };
};

export const localStudio = {
  preferences: () => api.request<{ principal_id: string; revision: number; settings: Record<string, unknown> }>("/api/workbench/v1/preferences"),
};
