import type { SourceIngestionRecord } from "../../types";
import { publicAgentActivityText } from "../../lib/agent-activity";
import { SourceProcessingMetricsView } from "./source-processing-metrics";

const labels: Record<string, string> = {
  ready: "Ready", complete: "Complete", pending: "Waiting", uploading: "Uploading", processing: "Processing",
  failed: "Failed", cancelled: "Cancelled", revoked: "Revoked", unavailable: "Unavailable", unsupported: "Unsupported",
  partial: "Partially ready", not_applicable: "Not needed", parsing: "Parsing", indexing: "Preparing",
  local_pending: "Preserved, awaiting upload", cloud_unavailable: "Google unavailable", running: "Processing",
  disabled: "Disabled", not_indexed: "Not ready",
  queued: "Waiting", not_prepared: "Not prepared",
};

const ordinaryPhases: Record<string,string> = {
  uploaded: "上传完成", basic_content_waiting: "等待解析", basic_content_parsing: "解析正文",
  basic_native_directory: "读取原书目录", basic_catalog_repair: "补全目录", basic_catalog_reading: "读取原文并整理目录",
  basic_catalog_verifying: "核对章节", basic_catalog_ready: "目录就绪", basic_catalog_failed: "目录处理已停止",
};

function statusLabel(value: unknown) {
  return typeof value === "string" ? labels[value] ?? "Processing" : "Waiting";
}

function reportedCount(value: unknown) {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

export function GoogleSourceStages({ source, searchEnabled }: { source: SourceIngestionRecord; searchEnabled?: boolean | null }) {
  if (source.metadata?.source_backend !== "google") return null;
  const metadata = source.metadata;
  if (metadata.processing_mode === "ordinary") {
    return <div data-testid="google-source-stages" className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-gray-500">
      <span>Original: {metadata.storage_status === "ready" ? "Stored in Google" : statusLabel(metadata.storage_status)}</span>
      <span>Content reading: {["ready", "visual"].includes(String(metadata.basic_read_status)) ? "Ready to ask" : statusLabel(metadata.basic_read_status)}</span>
      <span>{source.refinement ? "Saved catalog" : "Catalog"}: {metadata.catalog_ready === true ? (source.refinement ? "Available" : "Ready") : statusLabel(metadata.catalog_status)}</span>
      {metadata.catalog_origin === "ai_organized" && <span>AI generated structure</span>}
      {metadata.source_processor === "anythingllm" && <span data-testid="anythingllm-index-status">Source index: {statusLabel(metadata.anythingllm_index_status)}</span>}
      {ordinaryPhases[source.ingestion_job?.phase_history.at(-1) ?? ""] && <span>{ordinaryPhases[source.ingestion_job?.phase_history.at(-1) ?? ""]}</span>}
      <span>Automatic source access: {searchEnabled === false ? "Off" : "On"}</span>
      {typeof metadata.catalog_error === "string" && metadata.catalog_error && <span className="basis-full text-amber-700">{publicAgentActivityText(metadata.catalog_error)}</span>}
      <SourceProcessingMetricsView source={source} />
    </div>;
  }
  const originalCatalog = metadata.catalog_pipeline === "source_original_catalog_v1";
  const manual = metadata.catalog_mode === "manual" || metadata.catalog_fallback === true;
  const catalogMethod = manual ? "AI reads the original" : originalCatalog ? "Search locates; AI reads original pages" : "Google parsing";
  const catalogStatus = metadata.catalog_ready === true ? "Ready" : statusLabel(metadata.catalog_status);
  const searchStatus = searchEnabled === false ? "Off"
    : metadata.search_ready === true ? "Ready"
      : metadata.search_status === "disabled" || metadata.search_status === "not_indexed" ? "Not ready"
        : statusLabel(metadata.search_status);
  const verified = source.structure_quality?.verified_chapter_count ?? reportedCount(metadata.catalog_verified_chapter_count) ?? 0;
  const total = source.structure_quality?.total_chapter_count ?? reportedCount(metadata.catalog_total_chapter_count);
  const originalPages = reportedCount(metadata.original_pages_read);
  const calls = reportedCount(metadata.model_call_count);
  const stopped = originalCatalog && ["partial", "unavailable"].includes(String(metadata.catalog_status));
  const phase = source.ingestion_job?.phase_history.at(-1);
  const reading = phase === "catalog_generation" || phase === "reading_pdf_page_images";
  const stopReason = stopped && typeof (metadata.stop_reason || metadata.catalog_warning) === "string"
    ? String(metadata.stop_reason || metadata.catalog_warning) : "";
  return <div data-testid="google-source-stages" className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-gray-500">
    <span>Original: {metadata.storage_status === "ready" ? "Stored in Google" : statusLabel(metadata.storage_status)}</span>
    <span>Cloud parsing: {manual && metadata.parsing_status === "manual" ? "Not requested" : statusLabel(metadata.parsing_status)}</span>
    <span>Search index: {metadata.search_ready === true ? "Ready" : statusLabel(metadata.search_status)}</span>
    <span>Catalog method: {catalogMethod}</span>
    <span>Catalog: {catalogStatus}</span>
    {typeof total === "number" && total > 0 && <span>{verified}/{total} chapters ready to cite</span>}
    <span>Automatic search: {searchStatus}</span>
    {originalCatalog && <span data-testid="source-original-read-counts" className="basis-full">
      Original-page reading: {metadata.catalog_ready === true ? "Complete" : stopped ? "Paused" : reading ? "In progress" : "Waiting"}
      {originalPages !== null ? ` · Original pages read: ${originalPages}` : ""}
      {calls !== null ? ` · Model calls: ${calls}` : ""}
    </span>}
    {stopReason && <span data-testid="source-original-read-stop-reason" className="basis-full text-amber-700 dark:text-amber-300">
      {publicAgentActivityText(stopReason)}
    </span>}
    {metadata.catalog_fallback === true && metadata.search_ready !== true && <span className="basis-full text-amber-700 dark:text-amber-300">
      Google is temporarily unavailable. Recovered from the original: {metadata.catalog_ready === true ? "Catalog" : "Partial catalog"}.
    </span>}
    {searchEnabled === true && metadata.search_ready !== true && metadata.catalog_ready === true && <span className="basis-full">
      The catalog is available; this source is not ready for search yet.
    </span>}
    {typeof metadata.update_status === "string" && <span className="basis-full">
      New version: {statusLabel(metadata.update_status)}.
      {metadata.catalog_ready === true || verified > 0 ? " Verified chapters remain available." : ""}
    </span>}
  </div>;
}
