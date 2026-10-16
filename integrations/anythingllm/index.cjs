"use strict";
const path = require("node:path");
const { hash, binding, workspace, searchFilter } = require("./contracts.cjs");
const { NativeEmbedder } = require("../../third_party/anythingllm/server/utils/EmbeddingEngines/native");
const { TextSplitter } = require("../../third_party/anythingllm/server/utils/TextSplitter");
const { length, tokenBounded } = require("./token-chunks.cjs");

class SourceIndex {
  constructor(store, embedder = null) {
    this.store = store;
    const model = process.env.EMBEDDING_MODEL_PREF;
    if (!NativeEmbedder.supportedModels[model]) throw new Error("The configured embedding model is unsupported.");
    this.embedder = embedder || new NativeEmbedder();
    this.running = false;
  }
  async connect() {
    const lance = require("@lancedb/lancedb");
    return await lance.connect(path.join(this.store.root, "lancedb"));
  }
  async table(name) {
    const client = await this.connect();
    return (await client.tableNames()).includes(name) ? await client.openTable(name) : null;
  }
  async chunks(source) {
    const data = this.store.content(source);
    const catalog = this.store.readCatalog(source);
    const splitter = new TextSplitter({ chunkSize: this.embedder.embeddingMaxChunkLength, chunkOverlap: 20 });
    const pipeline = this.embedder.embedderClient ? await this.embedder.embedderClient() : null;
    const tokenizer = pipeline?.tokenizer || null;
    const tokenLimit = tokenizer?.model_max_length;
    if (tokenizer && (!Number.isSafeInteger(tokenLimit) || tokenLimit < 1)) throw new Error("The embedder tokenizer capacity is unknown.");
    const records = [];
    for (const unit of data.units) {
      const characters = Array.from(unit.text);
      const boundaries = new Set([0, characters.length]);
      for (const chapter of catalog.chapters) {
        if (!chapter.range) continue;
        const bounds = chapter.range.metadata || {};
        if (bounds.basic_unit_start === unit.id && Number.isInteger(bounds.basic_start_char)) boundaries.add(bounds.basic_start_char);
        if (bounds.basic_unit_end === unit.id && Number.isInteger(bounds.basic_end_char)) boundaries.add(bounds.basic_end_char);
      }
      const cuts = [...boundaries].sort((a,b) => a-b);
      for (let i = 0; i < cuts.length - 1; i++) {
        const segment = characters.slice(cuts[i], cuts[i+1]).join("");
        let previous = 0;
        for (const text of await splitter.splitText(segment)) {
          const start = segment.indexOf(text, previous);
          if (start < 0) throw new Error("A vector chunk lost its original location.");
          previous = Math.max(start, start + text.length - 20);
          const offset = cuts[i] + length(segment.slice(0, start));
          for (const chunk of await tokenBounded(text, tokenizer, tokenLimit, this.embedder.embeddingPrefix))
            records.push({ unit_id: unit.id, char_start: offset + chunk.start, char_end: offset + chunk.end, text: chunk.text, derived: false });
        }
      }
      if (!unit.text.trim()) {
        // Visual labels aid discovery only. Reads always return the original image.
        const labels = catalog.chapters.filter(chapter => chapter.range && chapter.range.kind === unit.locator.kind &&
          chapter.range.start <= unit.locator.start && chapter.range.end >= unit.locator.end).map(chapter => chapter.title);
        if (labels.length) for (const chunk of await tokenBounded(labels.join("\n"), tokenizer, tokenLimit, this.embedder.embeddingPrefix))
          records.push({ unit_id: unit.id, char_start: 0, char_end: 0, text: chunk.text, derived: true });
      }
    }
    return records;
  }
  async build(source) {
    const row = this.store.get(source);
    const records = await this.chunks(source);
    if (!records.length) throw new Error("No searchable source content was prepared.");
    const client = await this.connect();
    const name = workspace(source.owner_user_id);
    let table = await this.table(name);
    if (table) await table.delete(`source_id = '${source.id.replaceAll("'", "''")}' AND source_version = '${source.source_version.replaceAll("'", "''")}'`);
    for (let start = 0; start < records.length; start += 25) {
      this.store.get(source); // Revocation is checked between embedding batches.
      const batch = records.slice(start, start+25);
      const vectors = await this.embedder.embedChunks(batch.map(record => this.embedder.embeddingPrefix + record.text));
      if (!vectors || vectors.length !== batch.length) throw new Error("The embedding batch is incomplete.");
      this.store.get(source);
      const items = batch.map((record, i) => ({ ...record, vector: vectors[i], workspace: name,
        id: hash(binding(source) + ":" + start + ":" + i), source_id: source.id,
        source_version: source.source_version, content_hash: source.content_hash }));
      if (table) await table.add(items); else table = await client.createTable(name, items);
    }
    this.store.db.prepare("UPDATE sources SET index_status='ready',error='' WHERE binding=? AND active=1").run(row.binding);
  }
  kick() {
    if (this.running) return;
    this.running = true;
    const work = async () => {
      try {
        let row;
        while ((row = this.store.db.prepare("SELECT * FROM sources WHERE active=1 AND index_status='queued' LIMIT 1").get())) {
          this.store.db.prepare("UPDATE sources SET index_status='indexing' WHERE binding=?").run(row.binding);
          const document = JSON.parse(require("node:fs").readFileSync(path.join(this.store.root, "documents/custom-documents", row.artifact), "utf8"));
          const source = { owner_user_id: document.owner_user_id, id: row.source_id, source_version: row.source_version, content_hash: row.content_hash };
          try { await this.build(source); }
          catch { this.store.db.prepare("UPDATE sources SET index_status='failed',error=? WHERE binding=? AND active=1").run("Search indexing failed. Verified original reading remains available; retry indexing.", row.binding); }
        }
      } finally { this.running = false; }
    };
    setImmediate(() => void work());
  }
  async search(owner, grants, query, limit = 8) {
    if (typeof query !== "string" || !query.trim() || query.length > 10000 || !Number.isSafeInteger(limit) || limit < 1 || limit > 8)
      throw new Error("Invalid source search request.");
    const filter = searchFilter(owner, grants);
    for (const grant of grants) if (this.store.get({ ...grant, owner_user_id: owner }).index_status !== "ready") throw new Error("The requested index is not ready.");
    const table = await this.table(workspace(owner));
    if (!table) return { locations: [] };
    if (this.embedder.embedderClient) {
      const pipeline = await this.embedder.embedderClient();
      const encoded = await pipeline.tokenizer((this.embedder.modelInfo?.queryPrefix || "") + query, { truncation: false, padding: false });
      if (encoded.input_ids.data.length > pipeline.tokenizer.model_max_length) throw new Error("Narrow the source search query to the embedding model capacity.");
    }
    const vector = await this.embedder.embedTextInput(query);
    const rows = await table.vectorSearch(vector).where(filter).distanceType("cosine").limit(limit).toArray();
    for (const grant of grants) this.store.get({ ...grant, owner_user_id: owner });
    return { locations: rows.map(row => ({ source_id: row.source_id, source_version: row.source_version,
      content_hash: row.content_hash, unit_id: row.unit_id, char_start: row.char_start, char_end: row.char_end,
      score: 1 - row._distance, derived: row.derived })) };
  }
}
module.exports = { SourceIndex };
