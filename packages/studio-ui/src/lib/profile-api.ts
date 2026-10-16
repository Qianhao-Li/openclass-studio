import { request } from "./api";
import { encodeAccountTheme } from "./account-theme";
import type { ProfileSettings } from "./profile-settings-state";
import type { DrivePickerSession } from "./source-cloud-api";
import {
  ACCOUNT_SETTINGS_FIELDS,
  type AccountSettingsResponse,
} from "../types/account-settings";
import type {
  ConnectionProvider,
  ConnectionResourcePage,
  ConnectionResourceQuery,
  ConnectionResourceScope,
  ExternalConnection,
} from "../types/external-connections";

export function getAccountSettings(): Promise<AccountSettingsResponse> {
  return request("/api/account/settings");
}

export function saveAccountSettings(
  settings: ProfileSettings,
  revision: number,
): Promise<AccountSettingsResponse> {
  const supported = Object.fromEntries(
    ACCOUNT_SETTINGS_FIELDS.map((key) => [key, settings[key]]),
  );
  return request("/api/account/settings", {
    method: "PUT",
    body: JSON.stringify({
      settings: { ...supported, ...encodeAccountTheme(settings.theme) },
      expected_revision: revision,
    }),
  });
}

export function getConnectionProviders(): Promise<ConnectionProvider[]> {
  return request("/api/account/connection-providers");
}

export function getConnectedAccounts(): Promise<ExternalConnection[]> {
  return request("/api/account/connections");
}

export function authorizeConnectedAccount(provider: string, capability: string, connectionId?: string): Promise<{ authorization_url: string }> {
  return request("/api/account/connections/authorizations", {
    method: "POST",
    body: JSON.stringify({ provider, capability, connection_id: connectionId, return_path: "/settings/connections" }),
  });
}

export function updateConnectedAccount(connectionId: string, revision: number, update: { status?: "connected" | "paused"; resource_scope?: ConnectionResourceScope; enabled_capabilities?: string[]; capability_scopes?: Record<string, ConnectionResourceScope> }): Promise<ExternalConnection> {
  return request(`/api/account/connections/${encodeURIComponent(connectionId)}`, {
    method: "PATCH",
    body: JSON.stringify({ ...update, expected_revision: revision }),
  });
}

export function disconnectConnectedAccount(connectionId: string, revision: number): Promise<ExternalConnection> {
  return request(`/api/account/connections/${encodeURIComponent(connectionId)}?expected_revision=${revision}`, { method: "DELETE" });
}

export function getConnectedAccountResources(connectionId: string, query: ConnectionResourceQuery = {}): Promise<ConnectionResourcePage> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value) params.set(key, value);
  }
  const suffix = params.size ? `?${params}` : "";
  return request(`/api/account/connections/${encodeURIComponent(connectionId)}/resources${suffix}`);
}

export function getConnectedAccountPickerSession(connectionId: string): Promise<DrivePickerSession> {
  return request(`/api/account/connections/${encodeURIComponent(connectionId)}/picker-session`, { method: "POST" });
}
