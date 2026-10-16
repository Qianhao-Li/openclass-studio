"use strict";
const crypto = require("node:crypto");
const { SourceCatalogError, directoryScope, attachmentText, batches } = require("./context");
const { directoryResult, POLICY } = require("./output");
const { PROMPT, SCHEMA } = require("./schema");
const ENGINE = "anythingllm_source_catalog_v2";
const hash = value => crypto.createHash("sha256").update(value).digest("hex");
const binding = source => [source.owner_user_id,source.id,source.source_version,source.content_hash].join(":");

class SourceCatalogWorkflow {
  constructor(store) {
    this.store = store;
    store.db.exec("CREATE TABLE IF NOT EXISTS source_catalog_tasks (id TEXT PRIMARY KEY, binding TEXT NOT NULL, state TEXT NOT NULL)");
  }
  save(task) {
    this.store.db.prepare("INSERT OR REPLACE INTO source_catalog_tasks VALUES(?,?,?)").run(task.id,binding(task.source),JSON.stringify(task));
    return this.view(task);
  }
  get(source,id) {
    this.store.get(source);
    const row = this.store.db.prepare("SELECT state FROM source_catalog_tasks WHERE id=? AND binding=?").get(id,binding(source));
    if (!row) throw new SourceCatalogError("The catalog task belongs to another original, account or version.");
    const task = JSON.parse(row.state);
    if (task.parsedHash !== hash(JSON.stringify(this.store.content(source))))
      throw new SourceCatalogError("The parsed original changed after the request was frozen.");
    return task;
  }
  view(task) {
    return { task_id: task.id, engine: ENGINE, policy: POLICY, status: task.status, phase: task.phase,
      progress: task.progress, model_calls: task.calls, read_units: new Set(task.covered).size,
      total_units: task.scopeUnitIds.length, recognized_count: task.rows.length,
      request: task.request || null, result: task.result || null, error: task.error || "" };
  }
  start(source,options) {
    const data = this.store.content(source), scope = directoryScope(data);
    const id = hash(binding(source) + ":" + POLICY + ":" + hash(JSON.stringify(data)) + ":" + JSON.stringify(options) + ":" + PROMPT);
    const previous = this.store.db.prepare("SELECT state FROM source_catalog_tasks WHERE id=? AND binding=?").get(id,binding(source));
    if (previous) {
      const task = JSON.parse(previous.state);
      if (["failed","cancelled"].includes(task.status)) {
        task.status = "processing"; task.error = ""; task.started = Date.now(); task.runCalls = 0;
        return this.next(task,data,scope);
      }
      return this.view(task);
    }
    const task = { id, source, parsedHash: hash(JSON.stringify(data)), model: options.model,
      maxBytes: options.max_input_bytes, maxImages: options.max_image_count || 6,
      supportsImages: options.supports_images === true, scopeMode: scope.mode,
      authored: scope.mode === "original_directory",
      status: "processing", phase: "reading", progress: 40, calls: 0, runCalls: 0,
      covered: [], rows: [], verifiedImages: [], scopeUnitIds: scope.groups.flatMap(group => group.units.map(unit => unit.id)),
      started: Date.now(), request: null, allBatchesRead: true };
    if (data.authored && data.native_entries.length) {
      task.rows = data.native_entries.map(entry => ({ title: entry.title, level: entry.level, native_range: entry.range }));
      task.covered = task.scopeUnitIds; task.native = true; task.authored = true;
      return this.publish(task,data,true);
    }
    return this.next(task,data,scope);
  }
  request(task,data,batch) {
    const needsImage = group => data.format === "visual" || data.format === "pdf" &&
      (!group.text.trim() || group.locator.metadata?.requires_original_image);
    const images = batch.filter(needsImage).map(group => ({ unit_id: group.units[0].id, locator: group.locator }));
    if (images.length && !task.supportsImages) throw new SourceCatalogError("The selected directory model cannot read original images.");
    const ids = batch.flatMap(group => group.units.map(unit => unit.id));
    const request = { system_prompt: PROMPT + (task.authored ? "\nAn original TOC was already found. Return only continued original TOC entries; ordinary body pages need no new rows." : ""),
      user_prompt: JSON.stringify({ source_title: task.source.title, pageContent: attachmentText(batch),
        source_batch: { unit_ids: ids, total_source_units: data.units.length }, original_image_positions: images,
        units: batch.flatMap(group => group.units.map(unit => [unit.id])) }),
      schema: SCHEMA, unit_ids: ids, original_image_requests: images };
    request.request_hash = hash(JSON.stringify(request) + JSON.stringify(task.model));
    task.request = request;
    task.progress = 40 + Math.floor(55 * new Set(task.covered).size / Math.max(1,task.scopeUnitIds.length));
    return this.save(task);
  }
  next(task,data,scope) {
    const remaining = scope.groups.filter(group => !group.units.every(unit => task.covered.includes(unit.id)));
    if (!remaining.length) return this.publish(task,data,task.allBatchesRead);
    if (task.runCalls >= 20 || Date.now() - task.started >= 15 * 60 * 1000)
      throw new SourceCatalogError("The processing budget was reached; returned directory entries remain saved.");
    const overhead = Buffer.byteLength(PROMPT) + Buffer.byteLength(JSON.stringify(SCHEMA)) + 4096;
    const batch = batches(remaining,task.maxBytes - overhead,false,task.maxImages)[0];
    return this.request(task,data,batch);
  }
  resize(source,id,requestHash,beforeUnit) {
    const task = this.get(source,id);
    if (task.status !== "processing" || task.request?.request_hash !== requestHash)
      throw new SourceCatalogError("The frozen catalog request changed or expired.");
    const data = this.store.content(source), scope = directoryScope(data);
    const batch = scope.groups.filter(group => group.units.every(unit => task.request.unit_ids.includes(unit.id)) &&
      group.units[0].id < beforeUnit);
    if (!batch.length) throw new SourceCatalogError("One original image exceeds the selected model's actual transfer capacity.");
    return this.request(task,data,batch);
  }
  advance(source,id,requestHash,output,imageReceipts = []) {
    const task = this.get(source,id);
    if (task.accepted?.includes(requestHash)) return this.view(task);
    if (task.status !== "processing" || task.request?.request_hash !== requestHash)
      throw new SourceCatalogError("The frozen catalog request changed, was cancelled or expired.");
    for (const image of task.request.original_image_requests) {
      if (!imageReceipts.some(receipt => receipt.unit_id === image.unit_id && receipt.content_hash === source.content_hash &&
        receipt.source_version === source.source_version && /^[a-f0-9]{64}$/.test(receipt.payload_sha256 || "")))
        throw new SourceCatalogError("An original image has no matching transfer receipt.");
    }
    if (!Array.isArray(output?.rows)) throw new SourceCatalogError("The model response has no directory list.");
    // No completeness, title, hierarchy, location or quality acceptance gate.
    const supplied = new Set(task.request.unit_ids);
    task.directoryUnits = [...new Set([...(task.directoryUnits || []),
      ...(output.directory_unit_ids || []).filter(id => supplied.has(id))])];
    task.rows.push(...output.rows.map(row => ({ ...row,
      directory_unit: supplied.has(row.directory_unit) ? row.directory_unit : null })));
    task.covered.push(...task.request.unit_ids);
    task.verifiedImages.push(...task.request.original_image_requests.map(image => image.unit_id));
    task.authored ||= output.authored_directory === true;
    task.allBatchesRead &&= output.complete === true && !output.unread_unit_ids?.length;
    task.calls++; task.runCalls++; task.accepted = [...(task.accepted || []),requestHash]; task.request = null;
    const data = this.store.content(source);
    if (task.authored && output.directory_complete === true) return this.publish(task,data,true);
    this.publish(task,data,false,false);
    return this.next(task,data,directoryScope(data));
  }
  publish(task,data,complete,finished = true) {
    task.result = directoryResult(task.source,data,task.rows,{ native: task.native,
      authored: task.authored, complete, modelCalls: task.calls, engine: ENGINE, verifiedImages: task.verifiedImages });
    task.result.metadata.catalog_read_unit_count = new Set(task.covered).size;
    task.result.metadata.catalog_total_unit_count = task.scopeUnitIds.length;
    const evidence = (task.directoryUnits || []).map(id => ({ unit_id: id,
      locator: data.units[id - 1].locator, status: "model_observed",
      source_version: task.source.source_version, content_hash: task.source.content_hash }));
    task.result.metadata.catalog_directory_evidence = evidence;
    task.result.metadata.catalog_directory_candidates = [...new Set(task.authored && !evidence.length
      ? task.rows.map(row => row.start_unit).filter(id => task.covered.includes(id)) : [])]
      .map(id => ({ unit_id: id, locator: data.units[id - 1].locator, status: "candidate",
        source_version: task.source.source_version, content_hash: task.source.content_hash }));
    const ranges = [];
    for (const id of [...new Set(task.native ? [] : task.covered)].sort((a,b) => a-b)) {
      const locator = data.units[id - 1].locator, last = ranges.at(-1);
      if (last && last.locator.kind === locator.kind && last.locator.container === locator.container &&
          last.unit_end + 1 === id && typeof locator.start === "number" && locator.start <= last.locator.end + 1) {
        last.unit_end = id; last.locator.end = locator.end;
      } else ranges.push({ unit_start: id, unit_end: id, locator: { ...locator },
        source_version: task.source.source_version, content_hash: task.source.content_hash });
    }
    task.result.metadata.catalog_read_ranges = ranges;
    task.result.metadata.catalog_directory_origin = task.native ? "native_navigation" : evidence.length ? "original_pages" : "location_pending";
    if (finished) { task.status = "ready"; task.phase = "ready"; task.progress = 100; task.request = null; }
    this.store.catalog(task.source,task.result.chapters,task.result.metadata);
    return this.save(task);
  }
  stop(source,id,reason) {
    const task = this.get(source,id);
    task.status = reason === "cancelled" ? "cancelled" : "failed"; task.phase = task.status;
    task.request = null; task.error = reason; return this.save(task);
  }
}
module.exports = { SourceCatalogWorkflow, ENGINE };
