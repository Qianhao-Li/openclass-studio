"use strict";
const { binding, hash } = require("./contracts.cjs");

class CatalogWorkflow {
  constructor(store) {
    this.store = store;
    store.db.exec(`CREATE TABLE IF NOT EXISTS catalog_requests (
      binding TEXT NOT NULL,batch_hash TEXT NOT NULL,input_hash TEXT NOT NULL,first_unit INTEGER NOT NULL,last_unit INTEGER NOT NULL,
      PRIMARY KEY(binding,batch_hash));`);
  }
  begin(source, batchHash, request) {
    const data = this.store.content(source);
    if (typeof request?.system_prompt !== "string" || typeof request?.user_prompt !== "string" || request?.schema?.title !== "BasicCatalogObservation")
      throw new Error("Invalid source catalog role request.");
    const input = JSON.parse(request.user_prompt);
    const ids = Array.isArray(input.units) ? input.units.map(unit => unit[0]) : [];
    let first = ids.length ? Math.min(...ids) : input.source_batch?.first_position;
    let last = ids.length ? Math.max(...ids) : input.source_batch?.last_position;
    if (!Number.isSafeInteger(first) || !Number.isSafeInteger(last) || first < 1 || last < first || last > data.units.length)
      throw new Error("The catalog task has no reliable original unit scope.");
    const inputHash = hash(JSON.stringify(request));
    const previous = this.store.db.prepare("SELECT * FROM catalog_requests WHERE binding=? AND batch_hash=?").get(binding(source), batchHash);
    if (previous && previous.input_hash !== inputHash) throw new Error("The frozen catalog task changed.");
    this.store.db.prepare("INSERT OR IGNORE INTO catalog_requests VALUES(?,?,?,?,?)").run(binding(source), batchHash, inputHash, first, last);
    return { ...this.store.beginBatch(source, batchHash), request,
      source_scope: { source_version: source.source_version, content_hash: source.content_hash, first_unit: first, last_unit: last } };
  }
  complete(source, batchHash, output) {
    const task = this.store.db.prepare("SELECT * FROM catalog_requests WHERE binding=? AND batch_hash=?").get(binding(source), batchHash);
    if (!task || output?.complete !== true || output?.unresolved?.length || !Array.isArray(output?.rows) ||
      output.rows.some(row => !Number.isSafeInteger(row.start_unit) || row.start_unit < task.first_unit || row.start_unit > task.last_unit))
      throw new Error("The catalog result is incomplete or outside its frozen original task.");
    return this.store.completeBatch(source, batchHash, output);
  }
}
module.exports = { CatalogWorkflow };
