export type ConnectionProvider = {
  provider: string;
  label: string;
  capabilities: string[];
  configured: boolean;
  reason?: string;
};

export type ConnectionCapability = {
  granted?: boolean;
  enabled?: boolean;
  scopes?: string[];
  read_only?: boolean;
  resource_scope?: ConnectionResourceScope;
};

export type ConnectionResourceScope = {
  mode: string;
  resource_ids?: string[];
  folder_ids?: string[];
  [key: string]: unknown;
};

export type ExternalConnection = {
  id: string;
  owner_user_id: string;
  provider: string;
  display_name: string;
  tenant: string;
  capabilities: Record<string, ConnectionCapability>;
  resource_scope: ConnectionResourceScope;
  status: string;
  revision: number;
  generation: number;
  last_access_at: number;
  upstream_revocation?: "succeeded" | "provider_managed" | "local_only" | "failed" | "could_not_attempt";
};

export type ConnectionResource = {
  resource_id: string;
  scope_key?: string;
  title: string;
  kind: string;
  web_url?: string;
  ref?: Record<string, unknown>;
  browse?: { kind: string; resource_id: string; query?: string; capability?: string };
  temporary?: boolean;
  event?: {
    start?: { dateTime?: string; date?: string };
    end?: { dateTime?: string; date?: string };
  };
};

export type ConnectionResourcePage = {
  items: ConnectionResource[];
  next_cursor: string | null;
  complete: boolean;
  coverage: unknown;
  connection_id: string;
  generation: number;
  evidence_ready: false;
};

export type ConnectionResourceQuery = {
  capability?: string;
  query?: string;
  cursor?: string;
  resource_id?: string;
  kind?: string;
  time_min?: string;
  time_max?: string;
  time_zone?: string;
  purpose?: "use" | "scope_selection";
};
