import { request } from "./api";
import type { SourceIngestionRecord } from "../types";

export type SourcePreference = { enabled: boolean; revision: number };
export type DrivePickerSession = { access_token: string; developer_key: string; app_id: string; origin: string };

export const sourceCloudApi = {
  prepareIndex: (packageId: string, sourceId: string, hash: string) => request<SourceIngestionRecord>(
    `/api/packages/${encodeURIComponent(packageId)}/sources/${encodeURIComponent(sourceId)}/prepare-index`,
    { method: "POST", body: JSON.stringify({ expected_content_hash: hash }) }),
  prepareEnhancement: (packageId: string, sourceId: string, hash: string) => request<SourceIngestionRecord>(
    `/api/packages/${encodeURIComponent(packageId)}/sources/${encodeURIComponent(sourceId)}/enhancement/prepare`,
    { method: "POST", body: JSON.stringify({ expected_content_hash: hash }) }),
  cancelEnhancement: (packageId: string, sourceId: string) => request<SourceIngestionRecord>(
    `/api/packages/${encodeURIComponent(packageId)}/sources/${encodeURIComponent(sourceId)}/enhancement/cancel`, { method: "POST" }),
  preference: () => request<SourcePreference>("/api/source-preferences"),
  setPreference: (enabled: boolean, revision: number) => request<SourcePreference>("/api/source-preferences", {
    method: "PUT", body: JSON.stringify({ enabled, expected_revision: revision }),
  }),
};
