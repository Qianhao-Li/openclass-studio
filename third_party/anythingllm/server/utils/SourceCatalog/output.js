"use strict";
const crypto = require("node:crypto");
const { locate } = require("./locations");
const { length } = require("./context");
const hash = value => crypto.createHash("sha256").update(value).digest("hex");
const POLICY = "anythingllm_direct_catalog_v1";

function directoryResult(source, data, input, { native = false, authored = false, complete = false, modelCalls = 0, engine = "", verifiedImages = [] } = {}) {
  const rows = input.map((row,index) => ({ ...row, output_index: index,
    title: String(row.title ?? ""), level: Number.isInteger(row.level) && row.level > 0 ? row.level : 1 }));
  let points = new Map();
  try {
    // Positions enrich the result; absent/ambiguous positions never hide titles.
    const found = locate({ ...data, authored: authored || data.authored }, rows,
      { groups: [], printed: [], mode: authored ? "original_directory" : "complete_original" }, verifiedImages);
    points = new Map(found.rows.map(row => [row.output_index, row]));
  } catch { /* The model's directory remains displayable without a range. */ }
  if (native && data.format !== "pdf") {
    for (const row of rows) {
      const range = row.native_range, offset = range?.metadata?.native_char_offset || 0;
      const unit = data.units.find(unit => unit.locator.kind === range?.kind && unit.locator.start === range.start &&
        (unit.locator.container || "") === (range.container || "") && unit.text_start <= offset &&
        (offset < unit.text_end || offset === unit.text_start && offset === unit.text_end));
      if (unit) points.set(row.output_index, { ...row, unit: unit.id, char: offset - unit.text_start });
    }
  }
  const stack = [], chapters = [], version = source.catalog_version || 1;
  for (const [index,row] of rows.entries()) {
    while (stack.length && stack.at(-1).level >= row.level) stack.pop();
    const point = points.get(index), nextIndex = rows.findIndex((other,i) => i > index && other.level <= row.level);
    const next = nextIndex < 0 ? null : points.get(nextIndex);
    let range = null;
    if (point && (nextIndex < 0 || next)) {
      let endUnit = next?.unit || data.units.length, endChar = next?.char ?? null;
      if (next?.char === 0 && next.unit > point.unit) { endUnit = next.unit - 1; endChar = null; }
      const first = data.units[point.unit - 1], last = data.units[endUnit - 1];
      const wholeImage = point.original_image && !rows.some((_,i) => i !== index && points.get(i)?.unit === point.unit);
      if (first && last && (wholeImage || endUnit > point.unit || endUnit === point.unit && (endChar ?? length(last.text)) > point.char)) {
        range = { ...first.locator, end: last.locator.end, metadata: {
          source_pipeline: engine, source_version: source.source_version, source_content_hash: source.content_hash,
          basic_unit_start: point.unit, basic_unit_end: endUnit, basic_start_char: point.char, basic_end_char: endChar,
          printed_page_label: row.printed_page_label || "", parser_provider: "anythingllm", parser_version: data.parser_version,
          ...(wholeImage ? { read_original_images: true } : {}),
        } };
        if (first.locator.container !== last.locator.container) {
          if (first.locator.kind === "epub_spine") range.container = "";
          else range = null;
        }
      }
    }
    const id = "sourcechapter_" + hash(source.id + ":" + source.content_hash + ":" + version + ":" + index + ":" + row.title).slice(0,24);
    const chapter = { id, owner_user_id: source.owner_user_id, package_id: source.package_id,
      source_ingestion_id: source.id, title: row.title, level: row.level, parent_id: stack.at(-1)?.id || null,
      path: [...stack.map(parent => parent.title),row.title], order_index: index,
      range, mapping_status: range ? "verified" : "unverified", anchor_status: range ? "verified" : "unverified",
      source_content_hash: source.content_hash, catalog_version: version,
      page_start: range?.kind === "pdf_pages" ? range.start : null, page_end: range?.kind === "pdf_pages" ? range.end : null,
      metadata: { catalog_engine: engine, locator_source: range ? "original_native_text" : "", printed_page: row.printed_page_label || "",
        directory_evidence: Number.isInteger(row.directory_unit) && data.units[row.directory_unit - 1]
          ? { unit_id: row.directory_unit, locator: data.units[row.directory_unit - 1].locator,
              status: "model_observed", source_version: source.source_version, content_hash: source.content_hash } : null } };
    chapters.push(chapter); stack.push(chapter);
  }
  const verified = chapters.filter(chapter => chapter.range).length;
  const metadata = { source_processor: "anythingllm", catalog_owner: "anythingllm", catalog_engine: engine,
    catalog_pipeline: engine, catalog_policy: POLICY, catalog_display_mode: "direct", catalog_quality_gates: "disabled",
    catalog_status: "ready", directory_status: complete ? "complete" : "uncertain", catalog_result_returned: true,
    catalog_origin: authored ? "authored" : "ai_organized", catalog_label: authored ? "Table of contents" : "Content structure",
    model_call_count: modelCalls, parser_provider: "anythingllm", parser_version: data.parser_version,
    catalog_task_contract: POLICY };
  return { chapters, metadata, structure: { owner_user_id: source.owner_user_id, package_id: source.package_id,
    source_ingestion_id: source.id, status: "ready", strategy: native ? "native_directory_v1" : "ai_directory_v1",
    has_verified_toc: native && authored, chapter_count: chapters.length, source_content_hash: source.content_hash,
    catalog_version: version, catalog_schema_version: engine, catalog_model: "anythingllm_catalog_workflow",
    quality: { level: "unassessed", total_chapter_count: chapters.length, verified_chapter_count: verified,
      unverified_chapter_count: chapters.length - verified }, metadata } };
}
module.exports = { POLICY, directoryResult };
