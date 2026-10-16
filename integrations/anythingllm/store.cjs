"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { binding, hash, workspace, identity, validateCatalog } = require("./contracts.cjs");
const { storageRoot, atomicWrite, database } = require("./io.cjs");

class SourceStore {
  constructor() { this.root = storageRoot(); this.db = database(); }
  get(source) {
    identity(source);
    const row = this.db.prepare("SELECT * FROM sources WHERE binding=? AND active=1").get(binding(source));
    if (!row) throw new Error("The source version is absent or has been revoked.");
    return row;
  }
  content(source) {
    const row = this.get(source);
    const document = JSON.parse(fs.readFileSync(path.join(this.root, "documents", "custom-documents", row.artifact), "utf8"));
    if (document.content_hash !== source.content_hash || document.source_version !== source.source_version) throw new Error("The source artifact changed.");
    if (document.parser_provider === "anythingllm" && hash(JSON.stringify(document.positionedContent)) !== document.parsed_content_hash)
      throw new Error("The AnythingLLM parsed artifact hash changed.");
    return document.positionedContent;
  }
  imported(source, documentName) {
    const artifact = documentName + ".json";
    if (!/^[a-f0-9]{64}\.json$/.test(artifact)) throw new Error("Invalid source artifact identity.");
    const key = binding(source);
    this.db.prepare(`INSERT INTO sources(binding,workspace,source_id,source_version,content_hash,artifact)
      VALUES(?,?,?,?,?,?) ON CONFLICT(binding) DO UPDATE SET active=1`).run(key, workspace(source.owner_user_id), source.id, source.source_version, source.content_hash, artifact);
    return { document_name: documentName, binding: hash(key), contract: "openclass_anythingllm_v1" };
  }
  beginBatch(source, batchHash) {
    this.get(source);
    if (!/^[a-f0-9]{64}$/.test(batchHash)) throw new Error("Invalid catalog batch identity.");
    const row = this.db.prepare("SELECT output FROM catalog_batches WHERE binding=? AND batch_hash=?").get(binding(source), batchHash);
    return row ? { cached: true, output: JSON.parse(row.output) } : { cached: false, batch_hash: batchHash };
  }
  completeBatch(source, batchHash, output) {
    this.get(source);
    if (!/^[a-f0-9]{64}$/.test(batchHash) || output?.complete === false || output?.unread_unit_ids?.length)
      throw new Error("The catalog batch was not read completely.");
    this.db.prepare("INSERT OR REPLACE INTO catalog_batches VALUES(?,?,?)").run(binding(source), batchHash, JSON.stringify(output));
    return { complete: true };
  }
  catalog(source, chapters, metadata) {
    const data = this.content(source);
    validateCatalog(chapters, data, metadata);
    const name = hash(binding(source)) + ".json";
    atomicWrite(path.join(this.root, "catalogs", name), JSON.stringify({ chapters, metadata }));
    this.db.prepare("UPDATE sources SET catalog=? WHERE binding=? AND active=1").run(name, binding(source));
    return { chapter_count: chapters.length, verified: true, artifact_hash: hash(JSON.stringify({ chapters, metadata })) };
  }
  readCatalog(source) {
    const row = this.get(source);
    if (!row.catalog) throw new Error("The complete source catalog is not ready.");
    return JSON.parse(fs.readFileSync(path.join(this.root, "catalogs", row.catalog), "utf8"));
  }
  activate(source) {
    this.readCatalog(source);
    this.db.exec("BEGIN IMMEDIATE");
    try {
      this.db.prepare("UPDATE sources SET active=0 WHERE workspace=? AND source_id=? AND binding<>?").run(workspace(source.owner_user_id), source.id, binding(source));
      this.db.prepare("UPDATE sources SET index_status='queued',error='' WHERE binding=? AND index_status<>'ready'").run(binding(source));
      this.db.exec("COMMIT");
    } catch (error) { this.db.exec("ROLLBACK"); throw error; }
    return this.status(source);
  }
  status(source) {
    const row = this.get(source);
    return { source_id: row.source_id, source_version: row.source_version, content_hash: row.content_hash,
      catalog_ready: !!row.catalog, index_status: row.index_status, error: row.error };
  }
  revoke(source) {
    identity(source);
    this.db.prepare("UPDATE sources SET active=0,index_status='revoked' WHERE workspace=? AND source_id=?").run(workspace(source.owner_user_id), source.id);
    return { revoked: true };
  }
}

module.exports = { SourceStore };
