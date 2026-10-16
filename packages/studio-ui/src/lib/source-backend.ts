import type { SourceIngestionRecord } from "../types";

export function isManagedSource(source: Pick<SourceIngestionRecord, "metadata">): boolean {
  return ["google", "local"].includes(String(source.metadata.source_backend));
}
