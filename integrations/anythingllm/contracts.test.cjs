const test = require("node:test");
const assert = require("node:assert/strict");
const { hash, validateImport, validateCatalog, searchFilter, workspace } = require("./contracts.cjs");
const source = { owner_user_id: "alice", id: "source_a", source_version: "v1", content_hash: hash("original") };
const content = { source_id: source.id, source_version: "v1", content_hash: source.content_hash, units: [{ id: 1, text: "Original", locator: { kind: "text_lines", start: 1, end: 1 } }] };

test("original and parsed identities are checked independently", () => {
  const payload = { source, content, original_base64: Buffer.from("original").toString("base64") };
  assert.equal(validateImport(payload).raw.toString(), "original");
  assert.throws(() => validateImport({ ...payload, original_base64: Buffer.from("changed").toString("base64") }));
  assert.throws(() => validateImport({ ...payload, content: { ...content, source_version: "v2" } }));
  assert.throws(() => validateImport({ ...payload, content: { ...content, units: [{ ...content.units[0], id: 2 }] } }));
});

test("scope filters distinguish owners, versions and exact character ranges", () => {
  const filter = searchFilter("alice", [{ ...source, units: [{ id: 1, start: 3, end: 10 }] }]);
  assert.match(filter, new RegExp(workspace("alice")));
  assert.ok(!filter.includes(workspace("bob")));
  assert.match(filter, /source_version = 'v1'/);
  assert.match(filter, /char_start >= 3 AND char_end <= 10/);
  assert.throws(() => searchFilter("alice", []));
  assert.throws(() => searchFilter("alice", [{ ...source, units: [] }]));
  assert.throws(() => searchFilter("alice", [{ ...source, units: [{ id: 1, start: -1, end: 10 }] }]));
});

test("catalog refuses missing ranges, duplicate IDs and dangling parents", () => {
  const chapter = { id: "c1", title: "Section", mapping_status: "verified", source_content_hash: source.content_hash, range: { kind: "text_lines", start: 1, end: 1 } };
  validateCatalog([chapter], content);
  assert.throws(() => validateCatalog([{ ...chapter, range: null }], content));
  assert.throws(() => validateCatalog([chapter, chapter], content));
  assert.throws(() => validateCatalog([{ ...chapter, parent_id: "missing" }], content));
});
