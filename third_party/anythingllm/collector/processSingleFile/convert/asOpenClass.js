// OpenClass format adapters preserve native coordinates; this carrier never OCRs or executes input.
const fs = require("node:fs");
const crypto = require("node:crypto");
const { writeToServerDocuments } = require("../../utils/files");

module.exports = async function asOpenClass({ fullFilePath, options }) {
  const source = options.openclassSource;
  const content = options.openclassContent;
  const digest = crypto.createHash("sha256").update(fs.readFileSync(fullFilePath)).digest("hex");
  if (!source || digest !== source.content_hash || content.content_hash !== digest || content.source_id !== source.id || content.source_version !== source.source_version)
    throw new Error("The original source binding is invalid.");
  const id = crypto.createHash("sha256").update(`${source.owner_user_id}:${source.id}:${source.source_version}:${digest}`).digest("hex");
  const document = writeToServerDocuments({ filename: id, options: { atomic: true }, data: {
    id, title: source.title, owner_user_id: source.owner_user_id, docSource: "OpenClass verified positioned original",
    url: `openclass-source:${source.id}`, source_version: source.source_version, content_hash: digest,
    pageContent: content.units.map(unit => unit.text).join("\n"), positionedContent: content,
    parser_contract: "openclass_anythingllm_v1",
  }});
  return { success: true, documents: [document], reason: null };
};
