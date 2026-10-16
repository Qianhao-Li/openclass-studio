import { getApiBase, readEffectiveAuthToken, request } from "./api";
import type { GeometryGenerationPayload, GeometryScene } from "../types/geometry";

async function geometryErrorMessage(response: Response) {
  const text = await response.text();
  try {
    const payload = JSON.parse(text) as { detail?: unknown };
    if (typeof payload.detail === "string" && payload.detail.trim()) {
      return payload.detail;
    }
  } catch {
    // Preserve the response text when the server did not return JSON.
  }
  return text || `Geometry generation request failed (${response.status})`;
}

export async function generateGeometryScene(
  lessonId: string,
  payload: GeometryGenerationPayload,
  options: { signal?: AbortSignal; request?: typeof request } = {}
): Promise<GeometryScene> {
  if (options.request) return options.request<GeometryScene>(`/api/lessons/${encodeURIComponent(lessonId)}/geometry/generate`, { method: "POST", body: JSON.stringify(payload), signal: options.signal });
  const headers = new Headers({ "Content-Type": "application/json" });
  const token = readEffectiveAuthToken();
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const response = await fetch(
    `${getApiBase()}/api/lessons/${encodeURIComponent(lessonId)}/geometry/generate`,
    {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      cache: "no-store",
      credentials: "include",
      signal: options.signal,
    }
  );
  if (!response.ok) {
    throw new Error(await geometryErrorMessage(response));
  }
  return response.json() as Promise<GeometryScene>;
}
