"use strict";
const path = require("node:path");
const fs = require("node:fs");
const { validateImport, validateOriginal, hash } = require("./contracts.cjs");
const { storageRoot, atomicWrite, serve } = require("./io.cjs");
const root = storageRoot();
const processSingleFile = require("../../third_party/anythingllm/collector/processSingleFile").processSingleFile;

serve(async (method, url, payload) => {
  if (method !== "POST" || !["/collect", "/collect-original"].includes(url)) throw new Error("Unknown collector operation.");
  const native = url === "/collect-original";
  const { source, raw, data } = native ? validateOriginal(payload) : validateImport(payload);
  const extension = native ? path.extname(payload.file_name).toLowerCase() : "";
  if (native && !/^\.[a-z0-9]+$/.test(extension)) throw new Error("The original document has an unknown format.");
  const original = path.join(root, "originals", source.content_hash + extension);
  if (!fs.existsSync(original)) atomicWrite(original, raw);
  if (hash(fs.readFileSync(original)) !== source.content_hash) throw new Error("The cached original is corrupt.");
  const result = await processSingleFile(source.content_hash + extension, native ? {
    absolutePath: original, sourceCatalogOriginal: true, sourceIdentity: source,
    originalFileName: payload.file_name, originalMimeType: payload.mime_type,
  } : { absolutePath: original, openclassContent: data, openclassSource: source });
  if (!result.success || result.documents.length !== 1) throw new Error("Source collection failed.");
  // Source body is an artifact, never duplicated into response metadata.
  const document = result.documents[0];
  return { document_name: document.id, content_hash: source.content_hash,
    source_version: source.source_version, unit_count: document.positionedContent.units.length,
    ...(native ? { content: document.positionedContent, parser_provider: "anythingllm",
      parser_version: document.parser_version, parsed_content_hash: document.parsed_content_hash } : {}) };
}, Number(process.env.OPENCLASS_ANYTHINGLLM_COLLECTOR_PORT || 8895));
