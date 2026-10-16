"use strict";
const { readSourceDocument } = require("./index");
const { writeToServerDocuments } = require("../../utils/files");
const crypto = require("node:crypto");

module.exports = async function collectSourceDocument({ fullFilePath, options }) {
  const source = options.sourceIdentity;
  const content = await readSourceDocument(fullFilePath, source,
    { fileName: options.originalFileName, mimeType: options.originalMimeType });
  const id = crypto.createHash("sha256").update([source.owner_user_id, source.id,
    source.source_version, source.content_hash, content.parser_version].join(":")).digest("hex");
  const parsedHash = crypto.createHash("sha256").update(JSON.stringify(content)).digest("hex");
  const document = writeToServerDocuments({ filename: id, options: { atomic: true }, data: {
    id, title: source.title, owner_user_id: source.owner_user_id, source_version: source.source_version,
    content_hash: source.content_hash, url: "source-original:" + source.id,
    docSource: "AnythingLLM original document reader", parser_contract: "openclass_anythingllm_v1",
    parser_provider: "anythingllm", parser_version: content.parser_version,
    parsed_content_hash: parsedHash, pageContent: content.units.map(unit => unit.text).join("\n"),
    positionedContent: content,
  } });
  return { success: true, documents: [document], reason: null };
};
