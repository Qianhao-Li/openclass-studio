"use client";

import { useEffect, useState } from "react";
import type { SourceIngestionRecord, SourceProcessingMetrics } from "../../types";

const stages: Record<string, string> = {
  original_verification: "核验原件", parse_wait: "等待解析", native_text_extraction: "解析正文",
  native_directory_verification: "核对原书目录", directory_model: "补全或生成目录",
  directory_image_model: "阅读原始页面", catalog_publication: "发布目录",
  attachment_directory_locations: "核对原文章节位置",
  catalog_generation_total: "目录处理合计（含核对）", original_page_catalog: "原图目录处理",
};

function milliseconds(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function duration(value: unknown): string {
  const number = milliseconds(value);
  return number === null ? "未知" : `${(number / 1000).toFixed(2)} 秒`;
}

export function SourceProcessingMetricsView({ source }: { source: SourceIngestionRecord }) {
  const metrics = (source.refinement ? source.metadata.processing_metrics : source.ingestion_job?.processing_metrics ?? source.metadata.processing_metrics) as SourceProcessingMetrics | undefined;
  const status = source.ingestion_job?.status;
  const running = !source.refinement && metrics?.status === "running" && !metrics.finished_at &&
    !["failed", "cancelled", "interrupted"].includes(status ?? "") &&
    !["failed", "cancelled"].includes(String(source.metadata.catalog_status ?? ""));
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  if (!metrics) return null;
  const started = metrics.started_at ? Date.parse(metrics.started_at) : NaN;
  const elapsed = running && now !== null && Number.isFinite(started) ? Math.max(metrics.total_ms ?? 0, now - started) : metrics.total_ms;
  const strategy = source.metadata.catalog_strategy;
  const method = strategy === "attachment_text" ? "读取原文识取目录" : strategy === "native" ? "原书目录" : strategy === "toc_repaired" ? "目录补全" : strategy === "full_text" ? "正文目录生成" : null;
  return <div data-testid="source-processing-metrics" className="mt-1 text-[11px] text-gray-500">
    <span>{source.refinement ? "Initial processing" : running ? "处理中" : "处理耗时"}：{duration(elapsed)}{method ? ` · ${method}` : ""}</span>
    <details className="mt-1">
      <summary className="cursor-pointer">处理计时详情</summary>
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
        {metrics.original_store_ms !== undefined && <span>原件云端存储：{duration(metrics.original_store_ms)}</span>}
        {Object.entries(metrics.stage_timings_ms ?? {}).map(([key, value]) => <span key={key}>{stages[key] ?? key}：{duration(value)}</span>)}
        <span>模型调用：{milliseconds(metrics.model_calls) ?? "未知"}</span>
        <span>输入 token：{milliseconds(metrics.input_tokens) ?? "未知"}</span>
        <span>输出 token：{milliseconds(metrics.output_tokens) ?? "未知"}</span>
        <span>缓存输入 token：{milliseconds(metrics.cached_input_tokens) ?? "未知"}</span>
        {Object.entries(metrics.cache_hits ?? {}).map(([key, count]) => <span key={key}>复用{key === "parsed_content" ? "解析正文" : "目录观察"}：{count} 次</span>)}
      </div>
    </details>
  </div>;
}
