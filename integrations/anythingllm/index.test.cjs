const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { hash } = require("./contracts.cjs");

function partialCatalogIndex(text, chapters) {
  process.env.EMBEDDING_MODEL_PREF = "MintplexLabs/multilingual-e5-small";
  const { SourceIndex } = require("./index.cjs");
  const source = { owner_user_id:"alice", id:"original", source_version:"v1", content_hash:hash("original") };
  const store = { content:() => ({units:[{id:1,text,locator:{kind:"pdf_pages",start:1,end:1}}]}),
    readCatalog:() => ({chapters}),get:() => ({binding:"fixture"}) };
  return {source,index:new SourceIndex(store,{embeddingMaxChunkLength:1000,embeddingPrefix:""})};
}

test("unlocated directory rows do not block indexing verified original text", async () => {
  const {source,index}=partialCatalogIndex("The actual original content.",[{title:"An unlocated title",range:null}]);
  const records=await index.chunks(source);
  assert.ok(records.length);
  assert.equal(records.map(row=>row.text).join(""),"The actual original content.");
  assert.ok(records.every(row=>row.derived===false));
});

test("visual navigation labels use only positioned rows and retain their derived status", async () => {
  const {source,index}=partialCatalogIndex("",[{title:"A guessed unlocated title",range:null},
    {title:"A positioned visual title",range:{kind:"pdf_pages",start:1,end:1}}]);
  const records=await index.chunks(source);
  assert.equal(records.map(row=>row.text).join(""),"A positioned visual title");
  assert.ok(records.every(row=>row.derived===true && row.char_start===0 && row.char_end===0));
});

test("a scan with no text or positioned descriptions cannot be marked indexed", async () => {
  const {source,index}=partialCatalogIndex("",[{title:"An unlocated title",range:null}]);
  index.connect=async()=>assert.fail("No empty vector table should be published");
  await assert.rejects(index.build(source),/No searchable source content/);
});

test("real LanceDB prefilters owners, versions and chapter boundaries", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "openclass-anythingllm-contract-"));
  process.env.STORAGE_DIR = root;
  process.env.EMBEDDING_MODEL_PREF = "MintplexLabs/multilingual-e5-small";
  const { SourceStore } = require("./store.cjs");
  const { SourceIndex } = require("./index.cjs");
  const store = new SourceStore();
  // A controlled embedder makes ranking deterministic; the database and scope filtering are real.
  const vector = text => text.includes("bananas") ? [0, 1, 0] : [1, 0, 0];
  const embedder = { embeddingMaxChunkLength: 1000, embeddingPrefix: "", embedChunks: async texts => texts.map(vector), embedTextInput: async text => vector(text) };
  const index = new SourceIndex(store, embedder);
  try {
    const source = { owner_user_id: "alice", id: "s1", source_version: "v1", content_hash: hash("original") };
    const content = { source_id: "s1", source_version: "v1", content_hash: source.content_hash,
      units: [{ id: 1, text: "apples bananas", locator: { kind: "text_lines", start: 1, end: 1 } }] };
    const doc = hash("fixture");
    fs.mkdirSync(path.join(root, "documents/custom-documents"), { recursive: true });
    fs.writeFileSync(path.join(root, "documents/custom-documents", doc + ".json"), JSON.stringify({ owner_user_id: "alice", source_version: "v1", content_hash: source.content_hash, positionedContent: content }));
    store.imported(source, doc);
    store.catalog(source, [{ id: "first", title: "First", mapping_status: "verified", source_content_hash: source.content_hash,
      range: { kind: "text_lines", start: 1, end: 1, metadata: { basic_unit_start: 1, basic_unit_end: 1, basic_start_char: 0, basic_end_char: 6 } } },
      { id: "second", title: "Second", mapping_status: "verified", source_content_hash: source.content_hash,
      range: { kind: "text_lines", start: 1, end: 1, metadata: { basic_unit_start: 1, basic_unit_end: 1, basic_start_char: 6, basic_end_char: 14 } } }], {});
    await index.build(source);
    const grant = { ...source, units: [{ id: 1, start: 0, end: 6 }] };
    const result = await index.search("alice", [grant], "bananas");
    assert.equal(result.locations.length, 1);
    assert.ok(result.locations.every(item => item.char_end <= 6));
    await assert.rejects(index.search("bob", [grant], "bananas"));
    await assert.rejects(index.search("alice", [{ ...grant, source_version: "v2" }], "bananas"));
    store.revoke(source);
    await assert.rejects(index.search("alice", [grant], "apples"));
  } finally { store.db.close(); fs.rmSync(root, { recursive: true, force: true }); }
});
