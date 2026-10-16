import { request } from "./api";
import type { ConnectionResourcePage, ConnectionResourceQuery } from "../types/external-connections";

export type CourseConnectionScope = {
  scope: { connection_ids: string[] };
  revision: number;
};

export type ConnectedSourceImport = { source_id: string; status: string; reused?: boolean; source_version?: string };
export type ConnectedSourceUpdate = {
  source_id: string;
  current_content_hash: string;
  current_source_version: string;
  provider_version: string;
  content_hash: string;
  changed: boolean;
  ref: Record<string, unknown>;
};

const packagePath = (packageId: string) => `/api/packages/${encodeURIComponent(packageId)}`;

export function getCourseConnectionScope(packageId: string): Promise<CourseConnectionScope> {
  return request(`${packagePath(packageId)}/source-access`);
}

export function saveCourseConnectionScope(packageId: string, connectionIds: string[], expectedRevision: number): Promise<CourseConnectionScope> {
  return request(`${packagePath(packageId)}/source-access`, {
    method: "PUT",
    body: JSON.stringify({ connection_ids: connectionIds, expected_revision: expectedRevision }),
  });
}

export function browseConnectedSources(connectionId: string, query: ConnectionResourceQuery = {}): Promise<ConnectionResourcePage> {
  const params = new URLSearchParams({ purpose: "use" });
  for (const [key, value] of Object.entries(query)) if (value && key !== "purpose") params.set(key, value);
  return request(`/api/account/connections/${encodeURIComponent(connectionId)}/resources?${params}`);
}

export function importConnectedSource(packageId: string, connectionId: string, ref: Record<string, unknown>): Promise<ConnectedSourceImport> {
  return request(`${packagePath(packageId)}/sources/connections/import`, {
    method: "POST",
    body: JSON.stringify({ connection_id: connectionId, ref }),
  });
}

export function checkConnectedSourceUpdate(packageId: string, sourceId: string, expectedContentHash: string, expectedSourceVersion: string): Promise<ConnectedSourceUpdate> {
  return request(`${packagePath(packageId)}/sources/${encodeURIComponent(sourceId)}/connection-update/check`, {
    method: "POST",
    body: JSON.stringify({ expected_content_hash: expectedContentHash, expected_source_version: expectedSourceVersion }),
  });
}

export function applyConnectedSourceUpdate(packageId: string, update: ConnectedSourceUpdate): Promise<ConnectedSourceImport> {
  return request(`${packagePath(packageId)}/sources/${encodeURIComponent(update.source_id)}/connection-update/apply`, {
    method: "POST",
    body: JSON.stringify({ expected_content_hash: update.current_content_hash, expected_source_version: update.current_source_version, expected_provider_version: update.provider_version, expected_snapshot_hash: update.content_hash, ref: update.ref }),
  });
}
