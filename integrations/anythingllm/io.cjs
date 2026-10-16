"use strict";
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const crypto = require("node:crypto");
const { DatabaseSync } = require("node:sqlite");
const { CONTRACT, UPSTREAM_REVISION } = require("./contracts.cjs");

function storageRoot() {
  const root = process.env.STORAGE_DIR;
  if (!root || !path.isAbsolute(root)) throw new Error("A persistent absolute STORAGE_DIR is required.");
  const checkout = path.resolve(__dirname, "../..");
  if (root === checkout || root.startsWith(checkout + path.sep)) throw new Error("Source storage must be outside the checkout.");
  fs.mkdirSync(root, { recursive: true, mode: 0o700 });
  if (fs.lstatSync(root).isSymbolicLink()) throw new Error("Source storage must not be a symlink.");
  return root;
}

function atomicWrite(target, bytes) {
  fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 });
  if (fs.existsSync(target) && fs.lstatSync(target).isSymbolicLink()) throw new Error("Invalid artifact file.");
  const temporary = target + "." + crypto.randomUUID() + ".part";
  const fd = fs.openSync(temporary, "wx", 0o600);
  try { fs.writeFileSync(fd, bytes); fs.fsyncSync(fd); }
  finally { fs.closeSync(fd); }
  try {
    fs.renameSync(temporary, target);
    const directory = fs.openSync(path.dirname(target), "r");
    try { fs.fsyncSync(directory); } finally { fs.closeSync(directory); }
  } finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
}

function database() {
  const db = new DatabaseSync(path.join(storageRoot(), "sources.sqlite3"));
  db.exec(`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;
    CREATE TABLE IF NOT EXISTS sources (
      binding TEXT PRIMARY KEY, workspace TEXT NOT NULL, source_id TEXT NOT NULL,
      source_version TEXT NOT NULL, content_hash TEXT NOT NULL, artifact TEXT NOT NULL,
      catalog TEXT, index_status TEXT NOT NULL DEFAULT 'not_prepared', error TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS catalog_batches (binding TEXT NOT NULL, batch_hash TEXT NOT NULL, output TEXT NOT NULL,
      PRIMARY KEY(binding,batch_hash));`);
  db.prepare("UPDATE sources SET index_status='queued' WHERE index_status='indexing'").run();
  return db;
}

function serviceKey() {
  const file = process.env.OPENCLASS_ANYTHINGLLM_SERVICE_KEY_FILE;
  if (!file || fs.lstatSync(file).isSymbolicLink() || (fs.statSync(file).mode & 0o077)) throw new Error("A private service key file is required.");
  const key = fs.readFileSync(file, "utf8").trim();
  if (key.length < 32) throw new Error("Invalid service key.");
  return key;
}

function serve(handler, port) {
  const key = serviceKey();
  const server = http.createServer(async (request, response) => {
    try {
      if (request.url === "/health" && request.method === "GET") {
        response.setHeader("Content-Type", "application/json");
        response.end(JSON.stringify({ ok: true, instance_id: crypto.createHash("sha256").update(key).digest("hex"), contract: CONTRACT, upstream_revision: UPSTREAM_REVISION, local_ocr: false,
          catalog_engine: "anythingllm_source_catalog_v2" }));
        return;
      }
      const supplied = String(request.headers.authorization || "").replace(/^Bearer /, "");
      if (supplied.length !== key.length || !crypto.timingSafeEqual(Buffer.from(supplied), Buffer.from(key))) {
        response.writeHead(403); response.end('{"error":"Source runtime access denied."}'); return;
      }
      let size = 0; const chunks = [];
      for await (const chunk of request) {
        size += chunk.length;
        if (size > 384 * 1024 * 1024) throw new Error("The source payload exceeds the transport limit.");
        chunks.push(chunk);
      }
      const payload = chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {};
      const result = await handler(request.method, request.url, payload);
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify(result));
    } catch (error) {
      // Do not leak original text, filenames, paths, provider errors or credentials.
      response.writeHead(422, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ error: typeof error.publicMessage === "string" && error.publicMessage.length < 512
        ? error.publicMessage : "The source request failed identity, scope, or processing validation." }));
    }
  });
  server.requestTimeout = 0;
  server.listen(port, "127.0.0.1", () => process.stdout.write(`AnythingLLM source runtime ready on ${port}\n`));
  return server;
}

module.exports = { storageRoot, atomicWrite, database, serviceKey, serve };
