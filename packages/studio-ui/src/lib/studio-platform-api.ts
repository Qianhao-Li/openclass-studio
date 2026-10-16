import { api } from "./api";
import type { CoursePackage } from "../types";

export type PlatformConnection = { connected: boolean; origin: string; account_id?: string; scopes?: string[] };
export type PlatformGrant = { attempt_id: string; user_code: string; verification_uri_complete: string; interval: number; expires_in: number };
export type DeviceApproval = { label: string; user_code: string; scopes: string[] };
export type PullCourseRequest = { operation_id: string; revision?: string; package_id?: string; expected_ridoc_revision?: string };
export const studioPlatform = {
  status: () => api.request<PlatformConnection>("/api/workbench/v1/platform"),
  connect: (scopes: string[]) => api.request<PlatformGrant>("/api/workbench/v1/platform/connect", { method: "POST", body: JSON.stringify({ scopes }) }),
  poll: (attempt: string) => api.request<{ status: string; interval?: number }>(`/api/workbench/v1/platform/connect/${encodeURIComponent(attempt)}`),
  disconnect: () => api.request<{ connected: false; remote_revoked: boolean; message?: string | null }>("/api/workbench/v1/platform", { method: "DELETE" }),
  devices: () => api.request<{ id: string; label: string; scopes: string[]; expires_at: number }[]>("/api/studio/v1/connections"),
  revokeDevice: (id: string) => api.request("/api/studio/v1/connections/" + encodeURIComponent(id), { method: "DELETE" }),
  courses: () => api.request<{ id: string; title: string; revision: string }[]>("/api/workbench/v1/platform/courses"),
  association: (id: string) => api.request<{ repository_id: string; local_revision: string } | null>(`/api/workbench/v1/platform/courses/${encodeURIComponent(id)}/association`),
  push: (course: CoursePackage, operation: string) => api.request<{ repository_id: string; ridoc_revision: string }>(`/api/workbench/v1/platform/courses/${encodeURIComponent(course.id)}/push`, { method: "POST", body: JSON.stringify({ operation_id: operation, expected_ridoc_revision: course.ridoc_revision }) }),
  pull: (id: string, body: PullCourseRequest) => api.request<CoursePackage>(`/api/workbench/v1/platform/courses/${encodeURIComponent(id)}/pull`, { method: "POST", body: JSON.stringify(body) }),
  submitPR: (body: Record<string, string>) => api.request<{ id: string }>("/api/workbench/v1/platform/course-prs", { method: "POST", body: JSON.stringify(body) }),
  inspect: (code: string) => api.request<DeviceApproval>(`/api/studio/v1/device/${encodeURIComponent(code)}`),
  approve: (code: string, approve: boolean) => api.request<{ status: string }>(`/api/studio/v1/device/${encodeURIComponent(code)}`, { method: "POST", body: JSON.stringify({ approve }) }),
};
