"use strict";
const crypto = require("node:crypto");

const UPSTREAM_REVISION = "feb04ca0a57cda6d0b3a69c62578f0af44d388fc";
const CONTRACT = "openclass_anythingllm_v1";
const hash = value => crypto.createHash("sha256").update(value).digest("hex");
const workspace = owner => "owner_" + hash(owner);
const binding = source => [source.owner_user_id, source.id, source.source_version, source.content_hash].join(":");

function identity(source) {
  for (const key of ["owner_user_id", "id", "source_version", "content_hash"])
    if (typeof source?.[key] !== "string" || !source[key] || source[key].length > 256)
      throw new Error("Invalid source identity.");
  if (!/^[a-f0-9]{64}$/.test(source.content_hash)) throw new Error("Invalid original hash.");
  return source;
}

function validateImport(payload) {
  const source = identity(payload.source);
  const raw = Buffer.from(payload.original_base64 || "", "base64");
  if (!raw.length || raw.length > 209715200 || hash(raw) !== source.content_hash)
    throw new Error("The original bytes do not match this source version.");
  const data = payload.content;
  if (data?.source_id !== source.id || data?.source_version !== source.source_version || data?.content_hash !== source.content_hash)
    throw new Error("The positioned content belongs to another original.");
  if (!Array.isArray(data.units) || !data.units.length || data.units.some((unit, i) =>
    unit.id !== i + 1 || typeof unit.text !== "string" || !unit.locator?.kind))
    throw new Error("Invalid positioned source units.");
  return { source, raw, data };
}

function validateOriginal(payload) {
  const source = identity(payload.source);
  const raw = Buffer.from(payload.original_base64 || "", "base64");
  if (!raw.length || raw.length > 209715200 || hash(raw) !== source.content_hash)
    throw new Error("The original bytes do not match this source version.");
  if (typeof payload.file_name !== "string" || !payload.file_name || payload.file_name.includes("\0"))
    throw new Error("The original document format has no filename.");
  if (payload.content !== undefined) throw new Error("The native catalog reader does not accept pre-parsed application text.");
  return { source, raw };
}

function validateCatalog(chapters, data, metadata = {}) {
  const direct = metadata.catalog_display_mode === "direct";
  if (!Array.isArray(chapters) || !direct && !chapters.length) throw new Error("The catalog is empty.");
  const ids = new Set();
  for (const chapter of chapters) {
    if (!chapter.id || ids.has(chapter.id) || !direct && (!chapter.title?.trim() || !chapter.range || chapter.mapping_status !== "verified"))
      throw new Error("The catalog contains an unverified or duplicate chapter.");
    if (chapter.parent_id && !ids.has(chapter.parent_id)) throw new Error("The catalog has a dangling parent.");
    if (chapter.source_content_hash !== data.content_hash) throw new Error("The catalog original hash changed.");
    const bounds = chapter.range?.metadata || {};
    if (bounds.basic_unit_start && (!data.units[bounds.basic_unit_start - 1] || !data.units[bounds.basic_unit_end - 1]))
      throw new Error("The catalog has an invalid original-unit boundary.");
    ids.add(chapter.id);
  }
}

function sqlString(value) { return "'" + String(value).replaceAll("'", "''") + "'"; }

// An allow-list is applied BEFORE vector ranking; an empty grant is never global.
function searchFilter(owner, grants) {
  if (!Array.isArray(grants) || !grants.length) throw new Error("No source search scope was authorized.");
  const predicates = grants.map(grant => {
    identity({ ...grant, owner_user_id: owner });
    if (grant.whole_source === true)
      return `(source_id = ${sqlString(grant.id)} AND source_version = ${sqlString(grant.source_version)} AND content_hash = ${sqlString(grant.content_hash)})`;
    if (!Array.isArray(grant.units) || !grant.units.length) throw new Error("The search grant has no original units.");
    const ranges = grant.units.map(unit => {
      if (!Number.isSafeInteger(unit.id) || unit.id < 1 || !Number.isSafeInteger(unit.start) || !Number.isSafeInteger(unit.end) || unit.start < 0 || unit.end < unit.start)
        throw new Error("Invalid authorized character range.");
      return `(unit_id = ${unit.id} AND char_start >= ${unit.start} AND char_end <= ${unit.end})`;
    });
    return `(source_id = ${sqlString(grant.id)} AND source_version = ${sqlString(grant.source_version)} AND content_hash = ${sqlString(grant.content_hash)} AND (${ranges.join(" OR ")}))`;
  });
  return `workspace = ${sqlString(workspace(owner))} AND (${predicates.join(" OR ")})`;
}

module.exports = { UPSTREAM_REVISION, CONTRACT, hash, workspace, binding, identity, validateImport, validateOriginal, validateCatalog, searchFilter };
